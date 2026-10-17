"""Measuring the GPU on each core: the benchmark half of the «Ядра GPU» tab.

For every candidate core the scan does what AutoGpuAffinity does:

1. pin the graphics adapter's interrupts to that core and restart the
   adapter, so the policy is live;
2. start a Vulkan test scene, on the same core, and let it warm up;
3. have PresentMon record every presented frame for a fixed time;
4. work out average, 1 % low and 0.1 % low from the capture.

The scan changes the machine — a different pinned core per step, and a
driver restart each time — so it holds itself to the rules of every other
change:

* The adapters' policies are written to a ``backups`` row before the first
  step, in state ``APPLYING``. If the process dies mid-scan the next start
  lists it as an interrupted change, with the original policy.
* The original policy is put back afterwards, always (``finally``), exactly
  as it was — including when it was "no policy". AutoGpuAffinity deletes the
  policy instead, which would silently discard a core the operator had chosen.
* It can be stopped between and during steps; the partial results are kept.

Everything that touches the system is injected, so the sequencing is tested
against fakes and the test never restarts a real driver.
"""

from __future__ import annotations

import math
import pathlib
import tempfile
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Sequence

from ...database.connection import Database
from ...utilities.command_runner import CommandRunner
from ...utilities.exceptions import PgmError
from ...utilities.logging_setup import get_logger
from ...utilities.progress import Progress, ProgressCallback, guarded
from ...windows.devices import affinity
from ...windows.devices import restart as device_restart
from ...windows.devices.affinity import AffinityPolicy
from ...windows.processes import affinity as process_affinity
from ..benchmark import frametime
from ..optimization.store import RunStore
from ..optimization.tweak import BackupRecord, BackupScope
from ..optimization.tweaks.gpu_affinity import GpuInterruptAffinityTweak, encode_policies
from .selection import CoreResult
from .tools import BenchTools, locate

_log = get_logger(__name__)

_TICK_S = 0.25
#: Seconds the scene is given to open its window before the warm-up counts.
_SCENE_START_S = 5
#: Allowance for PresentMon to start, beyond the capture time it is asked for.
_CAPTURE_SLACK_S = 30
#: Cores in a row that may fail before the scan gives up: a scene that will
#: not start fails on every core, and each attempt restarts the driver.
_MAX_CONSECUTIVE_FAILURES = 2


@dataclass(frozen=True, slots=True)
class ScanSettings:
    benchmark_s: int = 30
    """Capture time per core."""

    warmup_s: int = 5
    """Scene run time before capturing, so shaders and caches are built."""

    settle_s: int = 5
    """Pause after a driver restart before the scene starts."""

    pin_scene: bool = True
    """Run the scene on the same core as the interrupts."""

    def seconds_per_core(self) -> int:
        return self.settle_s + _SCENE_START_S + self.warmup_s + self.benchmark_s + 6


def estimate_seconds(cores: int, settings: ScanSettings) -> int:
    return cores * settings.seconds_per_core()


@dataclass(frozen=True, slots=True)
class ScanOutcome:
    results: tuple[CoreResult, ...] = ()
    failures: tuple[tuple[int, str], ...] = ()
    """``(cpu, reason)`` for each core that could not be measured."""

    cancelled: bool = False
    restore_error: str = ""
    """Set when the original policy could not be put back or the adapter
    could not be restarted: the one outcome the operator must act on."""


class _Stopped(Exception):
    """The operator asked the scan to stop."""


class AffinityScan:
    def __init__(
        self,
        database: Database,
        *,
        tools: BenchTools | None = None,
        runner: CommandRunner | None = None,
        read: Callable[[str], AffinityPolicy] = affinity.read,
        write: Callable[[str, AffinityPolicy], None] = affinity.write,
        restart: Callable[[str], None] = device_restart.restart,
        set_scene_cpu: Callable[[int, int], None] = process_affinity.set_cpu,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._store = RunStore(database)
        self._tools = tools
        self._runner = runner or CommandRunner()
        self._read, self._write, self._restart = read, write, restart
        self._set_scene_cpu = set_scene_cpu
        self._sleep = sleep

    # -- the scan ------------------------------------------------------------

    def run(
        self,
        adapters: Sequence[tuple[str, str]],
        cpus: Sequence[int],
        settings: ScanSettings,
        *,
        progress: ProgressCallback,
        should_stop: Callable[[], bool] = lambda: False,
    ) -> ScanOutcome:
        """Measure each of ``cpus``. Raises :class:`PgmError` before any change."""
        tools = self._tools or locate()
        snapshot = {pnp: self._read(pnp) for _name, pnp in adapters}
        say = guarded(progress)

        run_id, backup_id = self._open_journal(snapshot)
        results: list[CoreResult] = []
        failures: list[tuple[int, str]] = []
        cancelled = False
        touched = False
        try:
            with tempfile.TemporaryDirectory(prefix="pgm-gpu-affinity-") as work:
                for index, cpu in enumerate(cpus):
                    if should_stop():
                        cancelled = True
                        break
                    touched = True
                    try:
                        stats = self._measure(
                            adapters, cpu, settings, tools, pathlib.Path(work),
                            lambda text, step, i=index: say(Progress(
                                f"CPU {cpu} ({i + 1} из {len(cpus)}): {text}",
                                (i + step) / len(cpus),
                            )),
                            should_stop,
                        )
                    except _Stopped:
                        cancelled = True
                        break
                    except PgmError as exc:
                        _log.warning("core %s not measured: %s", cpu, exc.what)
                        failures.append((cpu, exc.reason or exc.what))
                        if _trailing_failures(results, failures) >= _MAX_CONSECUTIVE_FAILURES:
                            break
                        continue
                    results.append(CoreResult(cpu, stats))
        finally:
            say(Progress("Возвращаю прежние настройки видеокарты…", None))
            error = self._restore(adapters, snapshot, restart=touched)
            self._close_journal(run_id, backup_id, snapshot, results, error)
        return ScanOutcome(tuple(results), tuple(failures), cancelled, error)

    def _measure(
        self,
        adapters: Sequence[tuple[str, str]],
        cpu: int,
        settings: ScanSettings,
        tools: BenchTools,
        work: pathlib.Path,
        say: Callable[[str, float], None],
        should_stop: Callable[[], bool],
    ) -> frametime.FrameStats:
        say("закрепляю прерывания и перезапускаю видеодрайвер — экран ненадолго погаснет", 0.0)
        for _name, pnp in adapters:
            self._write(pnp, AffinityPolicy.pinned(cpu))
            self._restart(pnp)
        self._wait(settings.settle_s, should_stop)

        capture = work / f"CPU-{cpu}.csv"
        scene = self._runner.spawn(
            [
                tools.scene, "--fullscreen=1", "--width=640", "--height=480",
                "--fps_cap=0", "--triple_buffering=0",
            ],
            cwd=str(work),
        )
        try:
            if settings.pin_scene:
                self._set_scene_cpu(scene.pid, cpu)
            say("прогрев тестовой сцены", 0.33)
            self._wait(_SCENE_START_S + settings.warmup_s, should_stop)
            if not scene.running:
                raise PgmError(
                    "Тестовая сцена закрылась",
                    "Видеодрайвер не поддерживает Vulkan или сцена не смогла открыть окно.",
                )

            say(f"замер {settings.benchmark_s} с", 0.66)
            monitor = self._runner.spawn(
                [
                    tools.presentmon, "-stop_existing_session", "-no_top",
                    "-timed", str(settings.benchmark_s),
                    "-process_name", tools.scene_process,
                    "-output_file", str(capture), "-terminate_after_timed",
                ],
                cwd=str(work),
            )
            try:
                self._wait_until_done(
                    monitor, settings.benchmark_s + _CAPTURE_SLACK_S, should_stop
                )
            finally:
                monitor.stop()
        finally:
            scene.stop()

        frames = frametime.read_presentmon_csv(capture) if capture.exists() else []
        stats = frametime.stats(frames)
        if stats is None:
            raise PgmError(
                "Замер не дал достаточно кадров",
                f"PresentMon записал {len(frames)} кадров, нужно не меньше "
                f"{frametime.MIN_FRAMES}.",
            )
        return stats

    # -- waiting ---------------------------------------------------------------

    def _wait(self, seconds: float, should_stop: Callable[[], bool]) -> None:
        for _ in range(math.ceil(seconds / _TICK_S)):
            if should_stop():
                raise _Stopped
            self._sleep(_TICK_S)

    def _wait_until_done(self, process, limit_s: float, should_stop) -> None:
        for _ in range(math.ceil(limit_s / _TICK_S)):
            if not process.running:
                return
            if should_stop():
                raise _Stopped
            self._sleep(_TICK_S)

    # -- putting things back -----------------------------------------------------

    def _restore(
        self,
        adapters: Sequence[tuple[str, str]],
        snapshot: dict[str, AffinityPolicy],
        *,
        restart: bool,
    ) -> str:
        """Original policies back, adapters restarted. Returns what went wrong."""
        problems: list[str] = []
        for pnp, original in snapshot.items():
            try:
                self._write(pnp, original)
            except PgmError as exc:
                problems.append(exc.reason or exc.what)
        if restart:
            for name, pnp in adapters:
                try:
                    self._restart(pnp)
                except PgmError as exc:
                    problems.append(f"{name}: {exc.reason or exc.what}")
        return "; ".join(problems)

    # -- the journal ---------------------------------------------------------------

    def _open_journal(self, snapshot: dict[str, AffinityPolicy]) -> tuple[str, str]:
        run_id, backup_id = uuid.uuid4().hex, BackupRecord.new_id()
        self._store.register([GpuInterruptAffinityTweak()])
        self._store.open_run(run_id, dry_run=False, planned=1)
        self._store.persist_backup(
            BackupRecord(
                backup_id=backup_id,
                tweak_id=GpuInterruptAffinityTweak.id,
                tweak_version=GpuInterruptAffinityTweak.version,
                scope=BackupScope.DEVICE,
                target=GpuInterruptAffinityTweak.id,
                old_value=encode_policies(snapshot),
                old_value_kind="JSON_AFFINITY_POLICY",
                new_value="тестовые ядра (замер)",
            ),
            run_id,
        )
        return run_id, backup_id

    def _close_journal(
        self,
        run_id: str,
        backup_id: str,
        snapshot: dict[str, AffinityPolicy],
        results: Sequence[CoreResult],
        error: str,
    ) -> None:
        # RESTORED: the scan's temporary pins were undone. FAILED if they
        # could not be, so the history does not claim a clean machine.
        self._store.mark_backup(backup_id, "FAILED" if error else "RESTORED", finished=True)
        self._store.close_run(
            run_id, applied=0, failed=1 if error else 0, rolled_back=0 if error else 1
        )
        self._store.audit(
            "gpu_affinity", "benchmark", target=GpuInterruptAffinityTweak.id,
            old_state=encode_policies(snapshot),
            new_state=f"измерено ядер: {len(results)}",
            result="FAILED" if error else "SUCCESS", error=error or None,
        )


def _trailing_failures(
    results: Sequence[CoreResult], failures: Sequence[tuple[int, str]]
) -> int:
    """Failures since the last core that was measured."""
    last_ok = results[-1].cpu if results else -1
    return sum(1 for cpu, _reason in failures if cpu > last_ok)


__all__ = [
    "AffinityScan",
    "ScanOutcome",
    "ScanSettings",
    "estimate_seconds",
]
