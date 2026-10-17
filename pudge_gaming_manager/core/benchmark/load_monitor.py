"""A load recording taken while someone plays: what the hardware did.

The guides are right that average FPS alone hides what matters, and right
that a tweak's effect has to be measured rather than assumed. What this
program can measure without injecting into the game or installing a kernel
tracer is the machine side:

* CPU load, overall and on the busiest core — one pegged core with the rest
  idle is what a CPU-limited game looks like;
* RAM in use;
* on NVIDIA, through NVML: GPU load, temperature, board power, core clock,
  and the throttle reasons the driver reports.

What it does not measure, and says so
-------------------------------------
Frame rate, frame time and 1% lows need per-frame present events from ETW
(what PresentMon, CapFrameX and the NVIDIA/AMD overlays read). This recording
reads none, so it reports no FPS number at all rather than an estimate.
(PresentMon ships with PGM only for the «Ядра GPU» tab's own test scene,
which measures the GPU interrupt core, not a game.) Compare two recordings of
the same scene before and after a change; do not read either one as "the
change gave N frames".
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field
from typing import Callable

import psutil

from ...hardware.gpu import nvml

#: A core at or above this is "pegged".
_PEGGED = 95.0
#: GPU at or above this is the limiting part.
_GPU_BOUND = 95.0


@dataclass(frozen=True, slots=True)
class Sample:
    at: float
    cpu_total: float
    cpu_max_core: float
    ram_percent: float
    gpu_util: float | None = None
    gpu_temp: float | None = None
    gpu_power: float | None = None
    gpu_clock: float | None = None
    throttle: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Stats:
    avg: float
    low: float
    high: float
    p95: float

    @classmethod
    def of(cls, values: list[float]) -> "Stats | None":
        if not values:
            return None
        ordered = sorted(values)
        index = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
        return cls(statistics.fmean(values), ordered[0], ordered[-1], ordered[index])

    def text(self, unit: str) -> str:
        return (
            f"в среднем {self.avg:.0f}{unit}, мин {self.low:.0f}{unit}, "
            f"макс {self.high:.0f}{unit}, 95% замеров ≤ {self.p95:.0f}{unit}"
        )


@dataclass(slots=True)
class LoadReport:
    samples: list[Sample] = field(default_factory=list)
    gpu_note: str = ""
    stopped_early: bool = False

    @property
    def duration_s(self) -> float:
        if len(self.samples) < 2:
            return 0.0
        return self.samples[-1].at - self.samples[0].at

    def _share(self, predicate) -> float:
        if not self.samples:
            return 0.0
        return 100.0 * sum(1 for s in self.samples if predicate(s)) / len(self.samples)

    def lines(self) -> list[str]:
        if not self.samples:
            return ["Замеров нет."]
        lines = [f"Длительность: {self.duration_s:.0f} с, замеров: {len(self.samples)}"]
        pairs = [
            ("Процессор, всего", [s.cpu_total for s in self.samples], "%"),
            ("Процессор, самое загруженное ядро", [s.cpu_max_core for s in self.samples], "%"),
            ("Память", [s.ram_percent for s in self.samples], "%"),
            ("Видеокарта, загрузка", [s.gpu_util for s in self.samples if s.gpu_util is not None], "%"),
            ("Видеокарта, температура", [s.gpu_temp for s in self.samples if s.gpu_temp is not None], " °C"),
            ("Видеокарта, мощность", [s.gpu_power for s in self.samples if s.gpu_power is not None], " Вт"),
            ("Видеокарта, частота ядра", [s.gpu_clock for s in self.samples if s.gpu_clock is not None], " МГц"),
        ]
        for name, values, unit in pairs:
            stats = Stats.of(values)
            if stats is not None:
                lines.append(f"{name}: {stats.text(unit)}")
        if self.gpu_note:
            lines.append(f"Видеокарта: {self.gpu_note}")
        return lines

    def findings(self) -> list[str]:
        """What the numbers show, stated no further than they go."""
        if not self.samples:
            return []
        found = []
        has_gpu = any(s.gpu_util is not None for s in self.samples)
        cpu_limited = self._share(
            lambda s: s.cpu_max_core >= _PEGGED
            and (s.gpu_util is None or s.gpu_util < 85)
        )
        if cpu_limited >= 20:
            found.append(
                f"В {cpu_limited:.0f}% замеров одно ядро было загружено почти "
                "полностью, а видеокарта — нет: так выглядит упор в процессор. "
                "Настройки видеокарты здесь мало что дадут."
            )
        gpu_bound = self._share(lambda s: (s.gpu_util or 0) >= _GPU_BOUND)
        if has_gpu and gpu_bound >= 50:
            found.append(
                f"В {gpu_bound:.0f}% замеров видеокарта была загружена полностью: "
                "предел задаёт она. Это нормально для требовательной игры."
            )
        reasons: dict[str, int] = {}
        for s in self.samples:
            for reason in s.throttle:
                reasons[reason] = reasons.get(reason, 0) + 1
        for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
            found.append(
                f"Драйвер сообщал ограничение частоты «{reason}» в "
                f"{100 * count / len(self.samples):.0f}% замеров."
            )
        ram_high = max(s.ram_percent for s in self.samples)
        if ram_high >= 90:
            found.append(
                f"Память доходила до {ram_high:.0f}%: при такой загрузке Windows "
                "начинает выгружать данные на диск, и это даёт подёргивания."
            )
        found.append(
            "FPS и время кадра не измерялись: для них нужна трассировка кадров "
            "(PresentMon, CapFrameX или оверлей драйвера). Сравнивайте две "
            "записи одной сцены до и после изменения."
        )
        return found


def _gpu_sample(session: nvml.NvmlSession) -> dict:
    if not session.available:
        return {}
    telemetry = session.read_all()
    if not telemetry:
        return {}
    busiest = max(telemetry, key=lambda t: t.utilization_percent.value or 0)
    return {
        "gpu_util": busiest.utilization_percent.value,
        "gpu_temp": busiest.temperature_c.value,
        "gpu_power": busiest.power_watts.value,
        "gpu_clock": busiest.clock_sm_mhz.value,
        # "idle" is the driver saying the GPU had nothing to do, not a limit.
        "throttle": tuple(r for r in busiest.throttle_reasons if r != "idle"),
    }


def record(
    duration_s: float,
    *,
    interval_s: float = 1.0,
    should_stop: Callable[[], bool] = lambda: False,
    progress: Callable[[float], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> LoadReport:
    """Sample every ``interval_s`` for ``duration_s``. Read-only."""
    report = LoadReport()
    psutil.cpu_percent(percpu=True)  # prime: the first call has no baseline
    with nvml.NvmlSession() as session:
        if not session.available:
            report.gpu_note = f"не измерялась — {session.unavailable_reason}"
        start = clock()
        while True:
            if should_stop():
                report.stopped_early = True
                break
            sleep(interval_s)
            cores = psutil.cpu_percent(percpu=True) or [0.0]
            report.samples.append(
                Sample(
                    at=clock(),
                    cpu_total=statistics.fmean(cores),
                    cpu_max_core=max(cores),
                    ram_percent=psutil.virtual_memory().percent,
                    **_gpu_sample(session),
                )
            )
            elapsed = clock() - start
            if progress is not None:
                progress(min(elapsed / duration_s, 1.0))
            if elapsed >= duration_s:
                break
    return report


__all__ = ["LoadReport", "Sample", "Stats", "record"]
