"""The nightly run: «Оптимизировать ПК» with nobody at the window.

What it is allowed to do is deliberately less than the window is. It plans at
:attr:`OptimizationLevel.NORMAL` — SAFE and LOW changes only, the ones the
window ticks by default — and nothing above that. The disk categories that
hold the user's own files (Downloads, Windows logs), removals (apps,
``Windows.old``), the Steam reset and every MEDIUM setting are out of reach
of an unattended run, because nobody previews what it does. Whoever wants
those opens the window.

It also stays out of the way. It stands aside when the window is open, waits
until the PC has seen no keyboard or mouse for a while, and gives up after a
fixed time rather than wait forever: a skipped night costs nothing, a clean
in the middle of somebody's game costs the game.

The result is written down in two places an operator can find: the audit and
application logs (the engine records every change itself) and a short
summary file the window shows as «Последний запуск».
"""

from __future__ import annotations

import json
import os
import pathlib
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Callable

from ..hardware.models import HardwareSnapshot
from ..utilities import low_impact, single_instance
from ..utilities.logging_setup import audit_event, default_log_dir, get_logger
from ..windows.schedule import task
from .diagnostics.issues import detect_issues
from .optimization.pipeline import OptimizationLevel, OptimizationPipeline
from .optimization.tweak import BackupScope
from .scanner.hardware_scan import HardwareScanner

_log = get_logger(__name__)

#: The switch the scheduled task starts the program with.
NIGHTLY_ARGUMENT = "--nightly"

DEFAULT_HOUR, DEFAULT_MINUTE = 5, 0

#: No keyboard or mouse for this long counts as "nobody is using the PC".
IDLE_REQUIRED_S = 10 * 60
#: How long to wait for that before skipping the night.
IDLE_WAIT_LIMIT_S = 90 * 60
_POLL_S = 30.0

#: ``outcome`` values of a :class:`NightlyReport`.
DONE, NOTHING_TO_DO, SKIPPED, FAILED = "done", "nothing", "skipped", "failed"


@dataclass(slots=True)
class NightlyReport:
    """What one run did, as shown in the window."""

    started: str
    outcome: str
    finished: str = ""
    applied: int = 0
    failed: int = 0
    rolled_back: int = 0
    message: str = ""
    lines: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.outcome != FAILED

    def headline(self) -> str:
        """One line for the window."""
        when = self.started.replace("T", " ")[:16]
        text = {
            DONE: f"применено изменений: {self.applied}"
            + (f", ошибок: {self.failed}" if self.failed else "")
            + (f", откачено: {self.rolled_back}" if self.rolled_back else ""),
            NOTHING_TO_DO: "делать было нечего",
            SKIPPED: f"пропущен — {self.message}",
            FAILED: f"сбой — {self.message}",
        }.get(self.outcome, self.outcome)
        return f"{when}: {text}"


# -- the run ---------------------------------------------------------------


def run(
    *,
    scanner: HardwareScanner | None = None,
    pipeline: OptimizationPipeline | None = None,
    window_open: Callable[[], bool] | None = None,
    idle_seconds: Callable[[], float | None] = low_impact.idle_seconds,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    report_path: pathlib.Path | None = None,
) -> NightlyReport:
    """Run one nightly pass and record it. Never raises."""
    started = datetime.now().isoformat(timespec="seconds")
    try:
        report = _run(
            started, scanner, pipeline,
            window_open or (lambda: single_instance.is_held(single_instance.GUI_MUTEX)),
            idle_seconds, sleep, clock,
        )
    except Exception as exc:  # noqa: BLE001 - an unattended run must end cleanly
        _log.exception("nightly run failed")
        report = NightlyReport(started, FAILED, message=str(exc) or type(exc).__name__)
    report.finished = datetime.now().isoformat(timespec="seconds")
    _save(report, report_path)
    audit_event(
        "nightly", "run", result="SUCCESS" if report.ok else "FAILED",
        error=None if report.ok else report.message,
        outcome=report.outcome, applied=report.applied, failed=report.failed,
    )
    _log.info("nightly run: %s", report.headline())
    return report


def _run(started, scanner, pipeline, window_open, idle_seconds, sleep, clock):
    if window_open():
        return NightlyReport(started, SKIPPED, message="окно программы открыто")
    if not _wait_for_idle(idle_seconds, sleep, clock):
        return NightlyReport(
            started, SKIPPED, message="за полтора часа ПК так и не освободился"
        )

    snapshot: HardwareSnapshot = (scanner or HardwareScanner()).scan()
    if pipeline is None:
        from .services import create_optimization_pipeline

        pipeline = create_optimization_pipeline()
    preview = pipeline.preview(
        snapshot, tuple(detect_issues(snapshot, None)),
        level=OptimizationLevel.NORMAL,
    )
    if not preview.has_changes:
        return NightlyReport(started, NOTHING_TO_DO)

    # A restore point costs a minute of disk work and Windows keeps only
    # one a day. It protects *settings*; when everything planned is a
    # deletion there is nothing for it to restore.
    if all(c.tweak.scope is BackupScope.NONE for c in preview.plan.applicable):
        pipeline.restore_point = None

    outcome = pipeline.apply(preview)
    results = outcome.run
    return NightlyReport(
        started,
        DONE,
        applied=results.applied,
        failed=results.failed,
        rolled_back=results.rolled_back,
        lines=[
            f"{r.name}: {r.detail}"
            for r in results.results
            if r.outcome.value in {"SUCCESS", "FAILED", "ROLLED_BACK"}
        ],
    )


def _wait_for_idle(idle_seconds, sleep, clock) -> bool:
    """Wait until nobody has used the PC for :data:`IDLE_REQUIRED_S`.

    When Windows cannot report input idleness the answer is "not idle":
    guessing the other way would clean under a player.
    """
    deadline = clock() + IDLE_WAIT_LIMIT_S
    while True:
        idle = idle_seconds()
        if idle is not None and idle >= IDLE_REQUIRED_S:
            return True
        if clock() >= deadline:
            return False
        sleep(_POLL_S)


# -- the summary file ------------------------------------------------------


def report_path() -> pathlib.Path:
    return default_log_dir().parent / "nightly_last.json"


def _save(report: NightlyReport, path: pathlib.Path | None) -> None:
    path = path or report_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(asdict(report), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except OSError:
        _log.warning("could not write %s", path, exc_info=True)


def last_report(path: pathlib.Path | None = None) -> NightlyReport | None:
    """The previous run's summary, or ``None`` if there is none to trust."""
    try:
        data = json.loads((path or report_path()).read_text(encoding="utf-8"))
        return NightlyReport(
            started=str(data["started"]),
            outcome=str(data["outcome"]),
            finished=str(data.get("finished", "")),
            applied=int(data.get("applied", 0)),
            failed=int(data.get("failed", 0)),
            rolled_back=int(data.get("rolled_back", 0)),
            message=str(data.get("message", "")),
            lines=[str(x) for x in data.get("lines", [])][:50],
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


# -- the schedule ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NightlyStatus:
    enabled: bool
    time_text: str = ""
    points_elsewhere: bool = False
    """The task starts a program file that is not the one running now, e.g.
    after the exe was moved or replaced by a build in another folder. It
    would fail at night, so the window says so instead of showing a tick."""


def status(command: str) -> NightlyStatus:
    """Whether the schedule is on, for the program file named ``command``."""
    info = task.query()
    if not info.installed or not info.enabled:
        return NightlyStatus(False)
    same = os.path.normcase(info.command.strip('"')) == os.path.normcase(command)
    return NightlyStatus(True, info.time_text, points_elsewhere=not same)


def enable(command: str, arguments: str, hour: int, minute: int) -> None:
    """Schedule the run daily. ``arguments`` must lead with the nightly switch.

    Raises:
        PgmError: not elevated, or Task Scheduler refused the definition.
    """
    if NIGHTLY_ARGUMENT not in arguments.split():
        raise ValueError("the task must start the program with --nightly")
    task.install(command, arguments, hour, minute)


def disable() -> None:
    task.remove()
