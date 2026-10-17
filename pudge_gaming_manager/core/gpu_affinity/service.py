"""The «Ядра GPU» tab: look, measure, choose, apply.

Four steps, each of which the operator sees before the next one happens:

1. :meth:`overview` — read-only. Which adapters, what their interrupt policy
   is now, whether MSI is on, which cores a benchmark would try, and anything
   that stops the benchmark from running here.
2. :meth:`scan` — the benchmark (see :mod:`.scan`).
3. :meth:`plan` / :meth:`apply` — pin the adapters to the chosen core through
   the tweak engine: previewed, backed up, verified, undoable from the
   history. Nothing is chosen for the operator; the best core is a
   suggestion.
4. :meth:`restart_adapters` — the change is read when the adapter starts, so
   this makes it live without a reboot. It blanks the screen and ends
   running games, which is why it is its own confirmed step.

MSI mode is shown but never written. Forcing it on a driver that does not
handle it leaves the device without interrupts (see ``windows/devices/msi``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

from ...database.connection import Database
from ...hardware.cpu import topology
from ...utilities.exceptions import PgmError
from ...utilities.privileges import is_admin
from ...utilities.progress import Progress, ProgressCallback, guarded
from ...windows.devices import affinity, msi
from ...windows.devices import restart as device_restart
from ...windows.devices.affinity import AffinityPolicy
from ..optimization.engine import Plan, RunReport, TweakEngine
from ..optimization.tweak import TweakContext
from ..optimization.tweaks.gpu_affinity import GpuInterruptAffinityTweak
from . import selection, tools
from .scan import AffinityScan, ScanOutcome, ScanSettings


@dataclass(frozen=True, slots=True)
class AdapterInfo:
    name: str
    instance_id: str
    policy: AffinityPolicy
    msi_label: str
    msi_on: bool | None

    @property
    def pair(self) -> tuple[str, str]:
        return self.name, self.instance_id


@dataclass(frozen=True, slots=True)
class Overview:
    adapters: tuple[AdapterInfo, ...]
    candidates: tuple[int, ...]
    """Logical processors a benchmark would try."""

    main_core_note: str
    blockers: tuple[str, ...]
    """Why the benchmark cannot run here; empty when it can."""


class GpuAffinityService:
    def __init__(
        self,
        database: Database,
        *,
        restore_point: Callable[[], tuple[bool, str]] | None = None,
        cores: Callable[[], list[topology.CpuCore]] = topology.read_cores,
        adapters: Callable[[], list[tuple[str, str]]] = msi.display_adapters,
        read: Callable[[str], AffinityPolicy] = affinity.read,
        msi_state: Callable[[str], msi.MsiState] = msi.read,
        admin: Callable[[], bool] = is_admin,
        display_ok: Callable[[], bool] = device_restart.basic_display_available,
        locate_tools: Callable[[], tools.BenchTools] = tools.locate,
        scanner: AffinityScan | None = None,
        restart: Callable[[str], None] = device_restart.restart,
    ) -> None:
        self.database = database
        self.restore_point = restore_point
        self._cores = cores
        self._adapters = adapters
        self._read = read
        self._msi = msi_state
        self._admin = admin
        self._display_ok = display_ok
        self._locate_tools = locate_tools
        self._scanner = scanner
        self._restart = restart

    # -- 1. look -----------------------------------------------------------

    def overview(self) -> Overview:
        cores = self._cores()
        candidates = selection.candidate_cpus(cores)
        main = selection.main_core(cores)
        infos: list[AdapterInfo] = []
        for name, pnp in self._adapters():
            state = self._msi(pnp)
            try:
                policy = self._read(pnp)
            except PgmError:
                policy = AffinityPolicy()
            infos.append(
                AdapterInfo(name, pnp, policy, state.label, state.supported)
            )

        blockers: list[str] = []
        if not self._admin():
            blockers.append("Нужны права администратора: запустите Pudge Cleaner от его имени.")
        if not infos:
            blockers.append("Не найдено ни одной PCI-видеокарты.")
        if not cores:
            blockers.append("Windows не сообщила устройство процессора.")
        elif len(candidates) < 2:
            blockers.append(
                "Для сравнения нужно хотя бы два ядра, кроме основного: "
                f"на этом процессоре их {len(candidates)}."
            )
        if not self._display_ok():
            blockers.append(
                "Выключен драйвер «Базовый видеоадаптер Майкрософт»: без него "
                "перезапуск видеодрайвера может оставить чёрный экран."
            )
        try:
            self._locate_tools()
        except PgmError as exc:
            blockers.append(f"{exc.what}. {exc.reason}".strip())

        span = (
            f"{main.logical[0]}–{main.logical[-1]}"
            if main and len(main.logical) > 1
            else "0"
        )
        note = (
            f"CPU {span} — основное ядро: на нём работает почти вся система, "
            "в проверку оно не входит."
        )
        return Overview(tuple(infos), tuple(candidates), note, tuple(blockers))

    # -- 2. measure --------------------------------------------------------

    def scan(
        self,
        adapters: Sequence[tuple[str, str]],
        settings: ScanSettings,
        *,
        progress: ProgressCallback,
        should_stop: Callable[[], bool] = lambda: False,
    ) -> ScanOutcome:
        cpus = selection.candidate_cpus(self._cores())
        scanner = self._scanner or AffinityScan(self.database)
        return scanner.run(
            adapters, cpus, settings, progress=progress, should_stop=should_stop
        )

    # -- 3. choose ---------------------------------------------------------

    def _engine(self) -> TweakEngine:
        # The operator picked the core and confirms the plan, which is the
        # per-change consent a MEDIUM-risk change needs (rule #38).
        return TweakEngine(
            self.database, TweakContext(database=self.database),
            allow_risk_above_low=True,
        )

    def plan(self, adapters: Sequence[tuple[str, str]], cpu: int) -> Plan:
        tweak = GpuInterruptAffinityTweak(adapters, cpu)
        engine = self._engine()
        engine.register([tweak])
        return engine.plan([tweak])

    def apply(self, plan: Plan) -> tuple[RunReport, list[str]]:
        notes: list[str] = []
        if plan.applicable and self.restore_point is not None:
            try:
                _created, message = self.restore_point()
                notes.append(message)
            except Exception as exc:  # noqa: BLE001 - never blocks the run
                notes.append(f"Точка восстановления не создана: {exc}")
        return self._engine().apply(plan), notes

    # -- 4. make it live ---------------------------------------------------

    def restart_adapters(
        self, adapters: Sequence[tuple[str, str]], progress: ProgressCallback
    ) -> list[str]:
        """Restart each adapter. Returns what failed, empty when all came back."""
        say = guarded(progress)
        problems: list[str] = []
        for name, pnp in adapters:
            say(Progress(f"Перезапуск видеодрайвера: {name} — экран ненадолго погаснет", None))
            try:
                self._restart(pnp)
            except PgmError as exc:
                problems.append(f"{name}: {exc.reason or exc.what}")
        return problems


__all__ = ["AdapterInfo", "GpuAffinityService", "Overview"]
