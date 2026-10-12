"""The nightly run: when it stands aside, what it plans, how it reports.

Everything the run touches is injected, so none of this scans hardware,
waits in real time or changes the machine.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pudge_gaming_manager.core import nightly
from pudge_gaming_manager.core.optimization.pipeline import OptimizationLevel
from pudge_gaming_manager.core.optimization.tweak import BackupScope
from pudge_gaming_manager.windows.schedule.task import TaskInfo

# -- fakes -----------------------------------------------------------------


class FakeScanner:
    def __init__(self) -> None:
        self.scans = 0

    def scan(self):
        self.scans += 1
        return SimpleNamespace()


class FakePipeline:
    def __init__(self, scopes=(), fail: Exception | None = None) -> None:
        self.scopes = scopes
        self.fail = fail
        self.restore_point = object()
        self.levels: list = []
        self.applied = 0

    def preview(self, snapshot, issues, *, level):
        self.levels.append(level)
        changes = [SimpleNamespace(tweak=SimpleNamespace(scope=s)) for s in self.scopes]
        return SimpleNamespace(
            has_changes=bool(changes), plan=SimpleNamespace(applicable=changes)
        )

    def apply(self, preview):
        if self.fail:
            raise self.fail
        self.applied += 1
        result = SimpleNamespace(
            name="Очистка временных файлов", detail="20 МБ",
            outcome=SimpleNamespace(value="SUCCESS"),
        )
        return SimpleNamespace(
            run=SimpleNamespace(applied=2, failed=0, rolled_back=0, results=[result])
        )


class Clock:
    """A clock that only moves when the run sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.slept += seconds


@pytest.fixture(autouse=True)
def _no_diagnosis(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(nightly, "detect_issues", lambda snapshot, network: [])


@pytest.fixture(autouse=True)
def _no_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(nightly, "audit_event", lambda *a, **k: None)


def _run(tmp_path, *, pipeline=None, scanner=None, window_open=False,
         idle=lambda: 9999.0, clock=None):
    clock = clock or Clock()
    scanner = scanner or FakeScanner()
    report = nightly.run(
        scanner=scanner,
        pipeline=pipeline or FakePipeline(),
        window_open=lambda: window_open,
        idle_seconds=idle,
        sleep=clock.sleep,
        clock=clock,
        report_path=tmp_path / "last.json",
    )
    return report, scanner, clock


# -- standing aside --------------------------------------------------------


def test_stands_aside_while_the_window_is_open(tmp_path) -> None:
    report, scanner, _ = _run(tmp_path, window_open=True)
    assert report.outcome == nightly.SKIPPED
    assert "окно" in report.message
    assert scanner.scans == 0  # it never even looked at the machine


def test_skips_the_night_when_the_pc_never_goes_idle(tmp_path) -> None:
    report, scanner, clock = _run(tmp_path, idle=lambda: 5.0)
    assert report.outcome == nightly.SKIPPED
    assert scanner.scans == 0
    # It waited the full allowance, and not a poll longer.
    assert clock.slept >= nightly.IDLE_WAIT_LIMIT_S
    assert clock.slept < nightly.IDLE_WAIT_LIMIT_S + 60


def test_unknown_idleness_is_not_idleness(tmp_path) -> None:
    """If Windows cannot say, the answer must be "someone may be playing"."""
    report, scanner, _ = _run(tmp_path, idle=lambda: None)
    assert report.outcome == nightly.SKIPPED
    assert scanner.scans == 0


def test_waits_for_the_player_to_leave_then_runs(tmp_path) -> None:
    readings = iter([0.0, 30.0, 400.0, nightly.IDLE_REQUIRED_S + 1])
    report, scanner, clock = _run(tmp_path, idle=lambda: next(readings))
    assert report.outcome == nightly.NOTHING_TO_DO  # FakePipeline plans nothing
    assert scanner.scans == 1
    assert clock.slept > 0


# -- what it does ----------------------------------------------------------


def test_plans_only_the_default_level_and_applies(tmp_path) -> None:
    pipeline = FakePipeline(scopes=[BackupScope.NONE])
    report, _, _ = _run(tmp_path, pipeline=pipeline)
    # Never MEDIUM or HARD: nobody previews an unattended run.
    assert pipeline.levels == [OptimizationLevel.NORMAL]
    assert pipeline.applied == 1
    assert report.outcome == nightly.DONE
    assert (report.applied, report.failed) == (2, 0)
    assert report.lines == ["Очистка временных файлов: 20 МБ"]


def test_nothing_to_do_applies_nothing(tmp_path) -> None:
    pipeline = FakePipeline(scopes=[])
    report, _, _ = _run(tmp_path, pipeline=pipeline)
    assert report.outcome == nightly.NOTHING_TO_DO
    assert pipeline.applied == 0


def test_no_restore_point_when_everything_planned_is_a_deletion(tmp_path) -> None:
    pipeline = FakePipeline(scopes=[BackupScope.NONE, BackupScope.NONE])
    _run(tmp_path, pipeline=pipeline)
    assert pipeline.restore_point is None


def test_restore_point_is_kept_when_a_setting_changes(tmp_path) -> None:
    marker = object()
    pipeline = FakePipeline(scopes=[BackupScope.NONE, BackupScope.POWER])
    pipeline.restore_point = marker
    _run(tmp_path, pipeline=pipeline)
    assert pipeline.restore_point is marker


def test_a_failure_is_recorded_not_raised(tmp_path) -> None:
    pipeline = FakePipeline(scopes=[BackupScope.NONE], fail=RuntimeError("диск недоступен"))
    report, _, _ = _run(tmp_path, pipeline=pipeline)
    assert report.outcome == nightly.FAILED
    assert not report.ok
    assert "диск недоступен" in report.message
    assert nightly.last_report(tmp_path / "last.json").outcome == nightly.FAILED


# -- the summary file ------------------------------------------------------


def test_summary_survives_a_round_trip(tmp_path) -> None:
    _run(tmp_path, pipeline=FakePipeline(scopes=[BackupScope.NONE]))
    last = nightly.last_report(tmp_path / "last.json")
    assert last is not None
    assert last.outcome == nightly.DONE and last.applied == 2
    assert last.finished >= last.started


@pytest.mark.parametrize("content", ["", "not json", "[]", '{"started": "x"}'])
def test_unreadable_summary_is_no_summary(tmp_path, content: str) -> None:
    path = tmp_path / "last.json"
    path.write_text(content, encoding="utf-8")
    assert nightly.last_report(path) is None
    assert nightly.last_report(tmp_path / "missing.json") is None


@pytest.mark.parametrize(
    ("report", "expected"),
    [
        (nightly.NightlyReport("2026-10-04T05:02:11", nightly.DONE, applied=3),
         "2026-10-04 05:02: применено изменений: 3"),
        (nightly.NightlyReport("2026-10-04T05:02:11", nightly.DONE, applied=1, failed=1),
         "2026-10-04 05:02: применено изменений: 1, ошибок: 1"),
        (nightly.NightlyReport("2026-10-04T05:02:11", nightly.NOTHING_TO_DO),
         "2026-10-04 05:02: делать было нечего"),
        (nightly.NightlyReport("2026-10-04T05:02:11", nightly.SKIPPED, message="окно открыто"),
         "2026-10-04 05:02: пропущен — окно открыто"),
    ],
)
def test_headline_reads_for_the_window(report, expected: str) -> None:
    assert report.headline() == expected


# -- the schedule ----------------------------------------------------------


def test_status_is_off_without_a_task(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(nightly.task, "query", lambda: TaskInfo(installed=False))
    assert not nightly.status(r"C:\a.exe").enabled


def test_status_is_off_for_a_disabled_task(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        nightly.task, "query",
        lambda: TaskInfo(True, r"C:\a.exe", "--nightly", 5, 0, enabled=False),
    )
    assert not nightly.status(r"C:\a.exe").enabled


def test_status_flags_a_task_that_points_at_another_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        nightly.task, "query", lambda: TaskInfo(True, r"C:\old\a.exe", "--nightly", 5, 0)
    )
    moved = nightly.status(r"C:\new\a.exe")
    assert moved.enabled and moved.points_elsewhere and moved.time_text == "05:00"


def test_status_compares_paths_the_way_windows_does(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        nightly.task, "query", lambda: TaskInfo(True, r'"C:\Pudge\A.EXE"', "--nightly", 5, 0)
    )
    assert not nightly.status(r"c:\pudge\a.exe").points_elsewhere


def test_enable_refuses_a_task_that_would_not_run_headless(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed: list = []
    monkeypatch.setattr(nightly.task, "install", lambda *a: installed.append(a))
    with pytest.raises(ValueError):
        nightly.enable(r"C:\a.exe", "", 5, 0)
    nightly.enable(r"C:\a.exe", "--nightly", 5, 0)
    assert installed == [(r"C:\a.exe", "--nightly", 5, 0)]


# -- the entry points ------------------------------------------------------


def test_the_nightly_switch_never_starts_the_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pudge_gaming_manager.app import main as app_main
    from pudge_gaming_manager.app import nightly as app_nightly

    monkeypatch.setattr(app_nightly, "main", lambda: 7)
    monkeypatch.setattr(
        app_main, "QApplication",
        lambda *a: pytest.fail("the nightly run must not create a Qt application"),
    )
    assert app_main.main(["PudgeCleaner.exe", "--nightly"]) == 7


def test_the_run_lowers_its_priority_before_doing_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pudge_gaming_manager.app import nightly as app_nightly

    order: list[str] = []
    monkeypatch.setattr(app_nightly, "setup_logging", lambda **k: order.append("log"))
    monkeypatch.setattr(app_nightly.low_impact, "lower_priority", lambda: order.append("nice"))
    monkeypatch.setattr(
        app_nightly.nightly, "run",
        lambda: order.append("run") or SimpleNamespace(ok=True),
    )
    assert app_nightly.main() == 0
    assert order == ["log", "nice", "run"]


def test_a_failed_run_exits_non_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    from pudge_gaming_manager.app import nightly as app_nightly

    monkeypatch.setattr(app_nightly, "setup_logging", lambda **k: None)
    monkeypatch.setattr(app_nightly.low_impact, "lower_priority", lambda: None)
    monkeypatch.setattr(app_nightly.nightly, "run", lambda: SimpleNamespace(ok=False))
    assert app_nightly.main() == 1


def test_launch_command_from_a_frozen_exe_is_the_exe_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    from pudge_gaming_manager.app import nightly as app_nightly

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\Club\PudgeCleaner.exe")
    assert app_nightly.launch_command() == (r"C:\Club\PudgeCleaner.exe", "--nightly")


def test_launch_command_from_source_runs_run_py(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    from pudge_gaming_manager.app import nightly as app_nightly

    monkeypatch.delattr(sys, "frozen", raising=False)
    command, arguments = app_nightly.launch_command()
    assert command == sys.executable
    assert arguments.endswith('run.py" --nightly')
