"""Tests for the safety core.

Every test here runs against fake tweaks. Nothing in this file touches the
real machine (rule #48) — the whole point of the engine is that its
guarantees hold regardless of what a tweak does, so the tweaks are
instrumented fakes that can fail on command.
"""

from __future__ import annotations

import pathlib

import pytest

from pudge_gaming_manager.core.optimization import engine as engine_module
from pudge_gaming_manager.core.optimization.engine import TweakEngine
from pudge_gaming_manager.core.optimization.tweak import (
    ALREADY_DESIRED,
    ApplyResult,
    BackupRecord,
    BackupScope,
    Outcome,
    Phase,
    RiskLevel,
    Tweak,
    TweakContext,
    TweakState,
    Verification,
)
from pudge_gaming_manager.database.connection import Database


class FakeTweak(Tweak):
    """A controllable tweak that records the order of lifecycle calls."""

    id = "fake.tweak"
    name = "Fake tweak"
    description = "A tweak used only in tests."
    rationale = "Exercises the engine without touching the system."
    risk = RiskLevel.LOW
    subsystem = "test"
    scope = BackupScope.REGISTRY

    def __init__(
        self,
        *,
        needs_change: bool = True,
        fail_backup: bool = False,
        fail_apply: bool = False,
        fail_verify: bool = False,
        fail_rollback: bool = False,
        scan_raises: bool = False,
        current_absent: bool = False,
        risk: RiskLevel | None = None,
        requires_admin: bool = False,
        scope: BackupScope | None = None,
    ) -> None:
        self.calls: list[str] = []
        self._needs_change = needs_change
        self._fail_backup = fail_backup
        self._fail_apply = fail_apply
        self._fail_verify = fail_verify
        self._fail_rollback = fail_rollback
        self._scan_raises = scan_raises
        self._current_absent = current_absent
        self.requires_admin = requires_admin
        if risk is not None:
            self.risk = risk
        if scope is not None:
            self.scope = scope

    def scan(self, ctx: TweakContext) -> TweakState:
        self.calls.append("scan")
        if self._scan_raises:
            raise RuntimeError("probe exploded")
        return TweakState(
            current_value=None if self._current_absent else "old",
            desired_value="new",
            needs_change=self._needs_change,
            current_absent=self._current_absent,
            summary="old -> new",
        )

    def backup(self, ctx: TweakContext, state: TweakState) -> BackupRecord:
        self.calls.append("backup")
        if self._fail_backup:
            raise RuntimeError("cannot read prior state")
        return BackupRecord(
            backup_id=BackupRecord.new_id(),
            tweak_id=self.id,
            tweak_version=self.version,
            scope=self.scope,
            target="HKLM\\Fake\\Key",
            old_value=state.current_value,
            old_value_absent=state.current_absent,
            new_value=state.desired_value,
        )

    def apply(self, ctx: TweakContext, state: TweakState) -> ApplyResult:
        self.calls.append("apply")
        if self._fail_apply:
            return ApplyResult(Outcome.FAILED, "the driver refused the change")
        return ApplyResult(Outcome.SUCCESS, "changed")

    def verify(self, ctx: TweakContext, state: TweakState) -> Verification:
        self.calls.append("verify")
        if self._fail_verify:
            return Verification.deny("old", "the value reverted immediately")
        return Verification.confirm("new")

    def rollback(self, ctx: TweakContext, backup: BackupRecord) -> ApplyResult:
        self.calls.append("rollback")
        if self._fail_rollback:
            return ApplyResult(Outcome.FAILED, "restore refused")
        return ApplyResult(Outcome.SUCCESS, "restored")


@pytest.fixture()
def db(tmp_path: pathlib.Path) -> Database:
    return Database(tmp_path / "engine.db")


@pytest.fixture()
def admin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Most tests assume elevation; the gate itself is tested separately."""
    monkeypatch.setattr(engine_module, "is_admin", lambda: True)


# -- lifecycle ordering ----------------------------------------------------


def test_happy_path_runs_phases_in_order(db: Database, admin: None) -> None:
    tweak = FakeTweak()
    eng = TweakEngine(db)
    report = eng.apply(eng.plan([tweak]))

    # The second scan is the pre-apply re-check against a stale plan.
    assert tweak.calls == ["scan", "scan", "backup", "apply", "verify"]
    assert report.applied == 1
    assert report.results[0].outcome is Outcome.SUCCESS


def test_backup_is_committed_before_apply(db: Database, admin: None) -> None:
    """The invariant the whole design rests on.

    The backup row must be durable *before* the mutation, so a crash between
    the two still leaves a rollback path.
    """
    observed: list[str] = []

    class Ordered(FakeTweak):
        def apply(self, ctx: TweakContext, state: TweakState) -> ApplyResult:
            rows = ctx.database.query("SELECT state FROM backups")
            observed.append(rows[0]["state"] if rows else "NO ROW")
            return super().apply(ctx, state)

    eng = TweakEngine(db)
    eng.apply(eng.plan([Ordered()]))

    assert observed == ["APPLYING"], "backup must exist and be marked APPLYING"


def test_apply_is_skipped_when_backup_fails(db: Database, admin: None) -> None:
    """No backup, no change — regardless of how low the risk is."""
    tweak = FakeTweak(fail_backup=True)
    eng = TweakEngine(db)
    report = eng.apply(eng.plan([tweak]))

    assert "apply" not in tweak.calls
    assert report.results[0].outcome is Outcome.SKIPPED
    assert report.results[0].phase is Phase.BACKUP


def test_scopeless_tweak_skips_backup(db: Database, admin: None) -> None:
    """A change with nothing to restore needs no backup row."""
    tweak = FakeTweak(scope=BackupScope.NONE)
    eng = TweakEngine(db)
    report = eng.apply(eng.plan([tweak]))

    assert tweak.calls == ["scan", "scan", "apply", "verify"]
    assert report.applied == 1
    assert db.query("SELECT * FROM backups") == []


# -- failure handling ------------------------------------------------------


def test_failed_apply_triggers_rollback(db: Database, admin: None) -> None:
    tweak = FakeTweak(fail_apply=True)
    eng = TweakEngine(db)
    report = eng.apply(eng.plan([tweak]))

    assert "rollback" in tweak.calls
    assert report.results[0].outcome is Outcome.ROLLED_BACK
    assert report.rolled_back == 1


def test_failed_verification_triggers_rollback(db: Database, admin: None) -> None:
    """Applied-but-not-confirmed is a failure, not a success (rule #65)."""
    tweak = FakeTweak(fail_verify=True)
    eng = TweakEngine(db)
    report = eng.apply(eng.plan([tweak]))

    assert tweak.calls == ["scan", "scan", "backup", "apply", "verify", "rollback"]
    assert report.results[0].outcome is Outcome.ROLLED_BACK
    assert report.applied == 0


def test_failed_rollback_is_reported_as_failure(db: Database, admin: None) -> None:
    """The most serious outcome must never be dressed up as recovery."""
    tweak = FakeTweak(fail_apply=True, fail_rollback=True)
    eng = TweakEngine(db)
    report = eng.apply(eng.plan([tweak]))

    assert report.results[0].outcome is Outcome.FAILED
    assert report.rolled_back == 0
    row = db.query_one("SELECT state FROM backups")
    assert row is not None and row["state"] == "FAILED"


def test_backup_state_becomes_restored_after_rollback(
    db: Database, admin: None
) -> None:
    eng = TweakEngine(db)
    eng.apply(eng.plan([FakeTweak(fail_verify=True)]))
    row = db.query_one("SELECT state, restored_at FROM backups")
    assert row is not None
    assert row["state"] == "RESTORED"
    assert row["restored_at"]


def test_backup_state_becomes_applied_on_success(db: Database, admin: None) -> None:
    eng = TweakEngine(db)
    eng.apply(eng.plan([FakeTweak()]))
    row = db.query_one("SELECT state FROM backups")
    assert row is not None and row["state"] == "APPLIED"


def test_one_failing_tweak_does_not_stop_the_others(
    db: Database, admin: None
) -> None:
    good_one, bad, good_two = FakeTweak(), FakeTweak(fail_apply=True), FakeTweak()
    eng = TweakEngine(db)
    report = eng.apply(eng.plan([good_one, bad, good_two]))

    assert report.applied == 2
    assert report.rolled_back == 1


def test_scan_error_is_reported_without_killing_the_plan(db: Database) -> None:
    eng = TweakEngine(db)
    plan = eng.plan([FakeTweak(scan_raises=True), FakeTweak()])

    assert len(plan.changes) == 2
    assert not plan.changes[0].will_apply
    assert "ошибка при сканировании" in plan.changes[0].skip_reason


# -- dry run ---------------------------------------------------------------


def test_dry_run_mutates_nothing(db: Database, admin: None) -> None:
    tweak = FakeTweak()
    eng = TweakEngine(db)
    report = eng.apply(eng.plan([tweak]), dry_run=True)

    assert tweak.calls == ["scan"], "dry run must not reach backup or apply"
    assert db.query("SELECT * FROM backups") == []
    assert report.applied == 0


def test_plan_alone_mutates_nothing(db: Database, admin: None) -> None:
    tweak = FakeTweak()
    TweakEngine(db).plan([tweak])
    assert tweak.calls == ["scan"]


def test_preview_lists_planned_and_skipped(db: Database, admin: None) -> None:
    eng = TweakEngine(db)
    plan = eng.plan([FakeTweak(), FakeTweak(needs_change=False)])
    text = "\n".join(plan.preview_lines())

    assert "1 changes planned" in text
    assert "old -> new" in text
    assert ALREADY_DESIRED in text


# -- risk gating -----------------------------------------------------------


def test_medium_risk_requires_opt_in(db: Database, admin: None) -> None:
    tweak = FakeTweak(risk=RiskLevel.MEDIUM)
    plan = TweakEngine(db).plan([tweak])
    assert not plan.changes[0].will_apply
    assert "отдельного подтверждения" in plan.changes[0].skip_reason


def test_medium_risk_runs_when_opted_in(db: Database, admin: None) -> None:
    eng = TweakEngine(db, allow_risk_above_low=True)
    plan = eng.plan([FakeTweak(risk=RiskLevel.MEDIUM)])
    assert plan.changes[0].will_apply


def test_critical_is_never_applied_even_when_opted_in(
    db: Database, admin: None
) -> None:
    eng = TweakEngine(db, allow_risk_above_low=True)
    plan = eng.plan([FakeTweak(risk=RiskLevel.CRITICAL)])
    assert not plan.changes[0].will_apply
    assert "не применяются автоматически" in plan.changes[0].skip_reason


def test_safe_and_low_apply_without_opt_in(db: Database, admin: None) -> None:
    eng = TweakEngine(db)
    plan = eng.plan([FakeTweak(risk=RiskLevel.SAFE), FakeTweak(risk=RiskLevel.LOW)])
    assert all(c.will_apply for c in plan.changes)


def test_admin_requirement_blocks_when_not_elevated(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine_module, "is_admin", lambda: False)
    plan = TweakEngine(db).plan([FakeTweak(requires_admin=True)])
    assert not plan.changes[0].will_apply
    assert "права администратора" in plan.changes[0].skip_reason.lower()


def test_no_change_needed_is_not_a_failure(db: Database, admin: None) -> None:
    eng = TweakEngine(db)
    report = eng.apply(eng.plan([FakeTweak(needs_change=False)]))
    assert report.results[0].outcome is Outcome.NOT_NEEDED
    assert report.failed == 0


# -- the absent-vs-empty distinction --------------------------------------


def test_absent_prior_value_is_recorded_as_absent(db: Database, admin: None) -> None:
    """Restoring an empty string where nothing existed corrupts the system."""
    eng = TweakEngine(db)
    eng.apply(eng.plan([FakeTweak(current_absent=True)]))

    row = db.query_one("SELECT old_value, old_value_absent FROM backups")
    assert row is not None
    assert row["old_value_absent"] == 1
    assert row["old_value"] is None


def test_present_prior_value_is_not_marked_absent(db: Database, admin: None) -> None:
    eng = TweakEngine(db)
    eng.apply(eng.plan([FakeTweak(current_absent=False)]))

    row = db.query_one("SELECT old_value, old_value_absent FROM backups")
    assert row is not None
    assert row["old_value_absent"] == 0
    assert row["old_value"] == "old"


# -- bookkeeping -----------------------------------------------------------


def test_run_is_recorded_and_closed(db: Database, admin: None) -> None:
    eng = TweakEngine(db)
    plan = eng.plan([FakeTweak()])
    eng.apply(plan)

    row = db.query_one(
        "SELECT * FROM optimization_runs WHERE run_id=?", (plan.run_id,)
    )
    assert row is not None
    assert row["status"] == "COMPLETED"
    assert row["applied_count"] == 1
    assert row["finished_at"]


def test_each_phase_outcome_is_recorded(db: Database, admin: None) -> None:
    eng = TweakEngine(db)
    eng.apply(eng.plan([FakeTweak(fail_verify=True)]))
    rows = db.query("SELECT phase, result FROM tweak_applications")
    assert [(r["phase"], r["result"]) for r in rows] == [("ROLLBACK", "ROLLED_BACK")]


def test_register_records_the_catalogue(db: Database) -> None:
    TweakEngine(db).register([FakeTweak()])
    row = db.query_one("SELECT * FROM tweaks WHERE tweak_id='fake.tweak'")
    assert row is not None
    assert row["rationale"]
    assert row["reversible"] == 1


# -- the contract itself ---------------------------------------------------

def test_a_tweak_without_a_rationale_cannot_be_defined() -> None:
    """Rule #64, enforced at class-definition time."""
    with pytest.raises(TypeError, match="rationale"):

        class Unexplained(Tweak):  # noqa: D401
            id = "x"
            name = "X"

            def scan(self, ctx): ...
            def backup(self, ctx, state): ...
            def apply(self, ctx, state): ...
            def verify(self, ctx, state): ...
            def rollback(self, ctx, backup): ...


# -- stale plan (the preview sat open while the system changed) -------------


def test_a_setting_changed_since_the_preview_is_not_applied(db: Database) -> None:
    tweak = FakeTweak()
    engine = TweakEngine(db)
    plan = engine.plan([tweak])

    # Someone changed the setting while the preview dialog was open.
    tweak.scan = lambda ctx: (tweak.calls.append("scan"), TweakState(
        current_value="player-changed", desired_value="new", needs_change=True,
    ))[1]
    report = engine.apply(plan)

    assert report.results[0].outcome is Outcome.SKIPPED
    assert "изменилось после превью" in report.results[0].detail
    assert "backup" not in tweak.calls and "apply" not in tweak.calls


def test_a_setting_already_fixed_since_the_preview_is_not_needed(db: Database) -> None:
    tweak = FakeTweak()
    engine = TweakEngine(db)
    plan = engine.plan([tweak])
    tweak._needs_change = False  # fixed by someone else meanwhile

    report = engine.apply(plan)
    assert report.results[0].outcome is Outcome.NOT_NEEDED
    assert "apply" not in tweak.calls


def test_a_failed_re_read_blocks_the_change(db: Database) -> None:
    tweak = FakeTweak()
    engine = TweakEngine(db)
    plan = engine.plan([tweak])
    tweak._scan_raises = True

    report = engine.apply(plan)
    assert report.results[0].outcome is Outcome.SKIPPED
    assert "не удалось перечитать" in report.results[0].detail
    assert "apply" not in tweak.calls


# -- interrupted changes (crash between backup and verification) ------------


def test_a_crash_mid_apply_leaves_the_backup_in_applying(db: Database) -> None:
    class _Crash(BaseException):
        """Stands in for the process dying: not an Exception, so not caught."""

    tweak = FakeTweak()
    engine = TweakEngine(db)
    plan = engine.plan([tweak])

    def dying_apply(ctx, state):
        tweak.calls.append("apply")
        raise _Crash()

    tweak.apply = dying_apply
    with pytest.raises(_Crash):
        engine.apply(plan)

    row = db.query_one("SELECT state FROM backups")
    assert row is not None and row["state"] == "APPLYING"


def test_interrupted_changes_are_reported_once_and_audited(db: Database) -> None:
    from pudge_gaming_manager.core.optimization import recovery

    db.execute(
        "INSERT INTO backups (backup_id, tweak_id, tweak_version, created_at, "
        "scope, target, old_value, old_value_absent, state) "
        "VALUES ('b1', 'display.refresh', 1, '2026-09-23T10:00:00', "
        "'DISPLAY', 'DISPLAY1', '1920x1080@144', 0, 'APPLYING')"
    )
    found = recovery.find_interrupted(db)
    assert [c.backup_id for c in found] == ["b1"]
    assert "1920x1080@144" in found[0].line()

    recovery.acknowledge(db, found)
    assert recovery.find_interrupted(db) == []
    audit = db.query_one(
        "SELECT operation, result FROM audit_log WHERE module='recovery'"
    )
    assert audit is not None and audit["result"] == "FAILED"


def test_backup_is_applied_only_after_verification(db: Database) -> None:
    """A change is not recorded as APPLIED until it verifies."""
    tweak = FakeTweak()
    engine = TweakEngine(db)
    plan = engine.plan([tweak])
    seen: list[str] = []

    original_verify = tweak.verify

    def verify_and_peek(ctx, state):
        seen.append(db.query_one("SELECT state FROM backups")["state"])
        return original_verify(ctx, state)

    tweak.verify = verify_and_peek
    engine.apply(plan)
    assert seen == ["APPLYING"]
    assert db.query_one("SELECT state FROM backups")["state"] == "APPLIED"


def test_an_applied_change_is_written_to_the_audit_table(db: Database) -> None:
    """The append-only table is the tamper-resistant record; it must be used."""
    engine = TweakEngine(db)
    engine.apply(engine.plan([FakeTweak()]))
    rows = db.query("SELECT operation, result, target FROM audit_log")
    assert [(r["operation"], r["result"]) for r in rows] == [("apply", "SUCCESS")]


def test_a_rollback_is_written_to_the_audit_table(db: Database) -> None:
    engine = TweakEngine(db)
    engine.apply(engine.plan([FakeTweak(fail_verify=True)]))
    ops = [r["operation"] for r in db.query("SELECT operation FROM audit_log")]
    assert ops == ["rollback"]


def test_medium_risk_needs_elevation_even_when_opted_in(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opt-in is not enough: MEDIUM+ needs admin, whatever the tweak declares."""
    monkeypatch.setattr(engine_module, "is_admin", lambda: False)
    eng = TweakEngine(db, allow_risk_above_low=True)
    plan = eng.plan([FakeTweak(risk=RiskLevel.MEDIUM, requires_admin=False)])
    assert not plan.changes[0].will_apply
    assert "права администратора" in plan.changes[0].skip_reason


def test_low_risk_without_admin_flag_runs_unelevated(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine_module, "is_admin", lambda: False)
    plan = TweakEngine(db).plan([FakeTweak(risk=RiskLevel.LOW, requires_admin=False)])
    assert plan.changes[0].will_apply


# -- operator selection in the preview (finding 12) -------------------------


def test_unselected_changes_are_skipped_not_applied(db: Database) -> None:
    keep, drop = FakeTweak(), FakeTweak()
    engine = TweakEngine(db)
    plan = engine.plan([keep, drop]).with_selection({0})

    assert [c.will_apply for c in plan.changes] == [True, False]
    assert plan.changes[1].skip_reason == "оператор не выбрал"

    report = engine.apply(plan)
    assert "apply" in keep.calls
    assert "apply" not in drop.calls
    assert report.results[1].outcome is Outcome.SKIPPED


def test_selection_cannot_revive_a_change_the_gate_refused(db: Database) -> None:
    refused = FakeTweak(risk=RiskLevel.MEDIUM)  # no opt-in -> refused by the gate
    plan = TweakEngine(db).plan([refused]).with_selection({0})
    assert not plan.changes[0].will_apply
    assert "отдельного подтверждения" in plan.changes[0].skip_reason


def test_preview_selection_keeps_the_score_and_run(db: Database) -> None:
    from pudge_gaming_manager.core.optimization.pipeline import OptimizationPreview
    from pudge_gaming_manager.core.scoring.score import GamingScore

    plan = TweakEngine(db).plan([FakeTweak(), FakeTweak()])
    preview = OptimizationPreview(plan=plan, score_before=GamingScore(value=70.0))
    narrowed = preview.with_selection(set())

    assert narrowed.change_count == 0 and not narrowed.has_changes
    assert narrowed.plan.run_id == plan.run_id
    assert narrowed.score_before.value == 70.0


# -- progress --------------------------------------------------------------


class _ReportingTweak(FakeTweak):
    """Reports half-way through apply, as a long change does."""

    def apply(self, ctx: TweakContext, state: TweakState) -> ApplyResult:
        ctx.report_progress(0.5, "полпути")
        return super().apply(ctx, state)


def test_progress_names_each_applied_change_and_forwards_its_fraction(
    db: Database, admin: None
) -> None:
    engine = TweakEngine(db)
    events: list = []
    plan = engine.plan([FakeTweak(needs_change=False), _ReportingTweak()])

    engine.apply(plan, progress=events.append)

    # The change with nothing to do is not a step; the one that runs is 1 of 1.
    assert [(e.text, e.fraction) for e in events] == [
        ("Применение 1 из 1: Fake tweak", None),
        ("Применение 1 из 1: Fake tweak — полпути", 0.5),
    ]


def test_a_failing_progress_report_does_not_break_the_change(
    db: Database, admin: None
) -> None:
    def gone(_event: object) -> None:
        raise RuntimeError("the window is already closed")

    engine = TweakEngine(db)
    tweak = _ReportingTweak()
    run = engine.apply(engine.plan([tweak]), progress=gone)

    assert run.applied == 1
    assert tweak.calls[-1] == "verify"


def test_the_context_stops_reporting_once_the_run_ends(
    db: Database, admin: None
) -> None:
    engine = TweakEngine(db)
    engine.apply(engine.plan([_ReportingTweak()]), progress=lambda _event: None)
    assert engine.context.progress is None


def test_a_tweak_may_report_outside_a_run(db: Database, admin: None) -> None:
    """``report_progress`` without a listener is a no-op, not an error."""
    tweak = _ReportingTweak()
    state = tweak.scan(TweakContext())
    assert tweak.apply(TweakContext(), state).outcome is Outcome.SUCCESS
