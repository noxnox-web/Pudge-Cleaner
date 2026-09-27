"""TweakEngine — the only component permitted to mutate system state.

The engine owns the safety invariants so that no individual tweak can forget
them:

* ``apply`` never runs without a durable backup already committed to SQLite.
* A failed ``verify`` triggers ``rollback`` automatically.
* A tweak whose ``backup`` fails is skipped, whatever its risk level.
* MEDIUM and above require elevation *and* explicit opt-in.
* Dry run stops after ``validate`` and mutates nothing.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from typing import Iterable, Sequence

from ...database.connection import Database
from ...utilities.exceptions import PgmError
from ...utilities.logging_setup import get_logger
from ...utilities.privileges import is_admin
from .progress import ProgressCallback, guarded_progress, step_reporter
from .store import RunStore
from .tweak import (
    ALREADY_DESIRED,
    BackupRecord,
    BackupScope,
    Outcome,
    Phase,
    RiskLevel,
    Tweak,
    TweakContext,
    TweakState,
    Validation,
)

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PlannedChange:
    """One entry in the dry-run preview (rule #39)."""

    tweak: Tweak
    state: TweakState
    validation: Validation
    will_apply: bool
    skip_reason: str = ""

    blocked_by_risk: bool = False
    """Skipped only because its risk class needs explicit approval.

    A structured flag rather than something to be recovered by reading
    ``skip_reason``: that string is shown to the operator and is therefore
    free to be reworded or translated, and a decision made by matching
    display text is a bug waiting for the next translator (this codebase
    has already had one). The preview uses this to offer the opt-in, and
    offers it for nothing else — a change refused because it needs
    administrator rights, or because the setting is absent, is not
    something a checkbox can fix."""

    @property
    def summary(self) -> str:
        return self.state.summary or self.tweak.name


@dataclass(frozen=True, slots=True)
class Plan:
    """What an optimization run intends to do, before it does anything."""

    run_id: str
    changes: tuple[PlannedChange, ...]

    @property
    def applicable(self) -> tuple[PlannedChange, ...]:
        return tuple(c for c in self.changes if c.will_apply)

    @property
    def skipped(self) -> tuple[PlannedChange, ...]:
        return tuple(c for c in self.changes if not c.will_apply)

    def with_selection(self, selected: set[int]) -> Plan:
        """This plan with only the chosen changes left to apply.

        ``selected`` holds indices into :attr:`changes`. Every applicable
        change the operator did not select becomes skipped, with that reason,
        so the report still shows it. Skipped changes stay skipped; selection
        can narrow a plan, never widen it past the engine's own gate.
        """
        return Plan(
            run_id=self.run_id,
            changes=tuple(
                change
                if not change.will_apply or index in selected
                else replace(
                    change, will_apply=False,
                    skip_reason="оператор не выбрал",
                )
                for index, change in enumerate(self.changes)
            ),
        )

    @property
    def blocked_by_risk(self) -> tuple[PlannedChange, ...]:
        """Changes the operator could allow by approving their risk class."""
        return tuple(c for c in self.changes if c.blocked_by_risk)

    def by_risk(self) -> dict[RiskLevel, int]:
        counts: dict[RiskLevel, int] = {}
        for change in self.applicable:
            counts[change.tweak.risk] = counts.get(change.tweak.risk, 0) + 1
        return counts

    def preview_lines(self) -> list[str]:
        """Human-readable dry-run output."""
        lines = [f"{len(self.applicable)} changes planned"]
        for change in self.applicable:
            lines.append(f"  + [{change.tweak.risk.value}] {change.summary}")
        for change in self.skipped:
            lines.append(f"  - [пропущено] {change.summary}: {change.skip_reason}")
        return lines


@dataclass(frozen=True, slots=True)
class TweakReport:
    tweak_id: str
    name: str
    outcome: Outcome
    phase: Phase
    detail: str = ""
    before: object = None
    after: object = None
    needs_restart: bool = False


@dataclass(slots=True)
class RunReport:
    run_id: str
    dry_run: bool
    results: list[TweakReport] = field(default_factory=list)

    @property
    def applied(self) -> int:
        return sum(1 for r in self.results if r.outcome is Outcome.SUCCESS)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.outcome is Outcome.FAILED)

    @property
    def rolled_back(self) -> int:
        return sum(1 for r in self.results if r.outcome is Outcome.ROLLED_BACK)

    @property
    def needs_restart(self) -> bool:
        return any(r.needs_restart for r in self.results)


class TweakEngine:
    """Drives the scan/validate/backup/apply/verify lifecycle."""

    def __init__(
        self,
        database: Database,
        context: TweakContext | None = None,
        *,
        allow_risk_above_low: bool = False,
    ) -> None:
        """
        Args:
            allow_risk_above_low: Opt-in required for MEDIUM+ tweaks
                (rule #38). Off by default so a caller must decide
                deliberately rather than inherit the permission.
        """
        self.database = database
        self._store = RunStore(database)
        self.context = context or TweakContext()
        self.context.database = database
        self.allow_risk_above_low = allow_risk_above_low

    # -- planning (no mutation) -------------------------------------------

    def plan(self, tweaks: Iterable[Tweak]) -> Plan:
        """Scan and validate, changing nothing.

        This is the dry run. It always runs before ``apply``, and its output
        is what the operator confirms.
        """
        run_id = uuid.uuid4().hex
        changes: list[PlannedChange] = []

        for tweak in tweaks:
            try:
                state = tweak.scan(self.context)
            except PgmError as exc:
                changes.append(
                    PlannedChange(
                        tweak, TweakState(summary=tweak.name),
                        Validation.refuse(exc.reason or exc.what),
                        will_apply=False,
                        skip_reason=f"не удалось прочитать: {exc.reason or exc.what}",
                    )
                )
                continue
            except Exception as exc:  # noqa: BLE001 - one bad tweak must not kill the plan
                _log.exception("scan failed for %s", tweak.id)
                changes.append(
                    PlannedChange(
                        tweak, TweakState(summary=tweak.name),
                        Validation.refuse(str(exc)),
                        will_apply=False,
                        skip_reason=f"ошибка при сканировании: {exc}",
                    )
                )
                continue

            validation = tweak.validate(self.context, state)
            will_apply, reason, by_risk = self._gate(tweak, state, validation)
            changes.append(
                PlannedChange(
                    tweak, state, validation, will_apply, reason,
                    blocked_by_risk=by_risk,
                )
            )

        return Plan(run_id=run_id, changes=tuple(changes))

    def _gate(
        self, tweak: Tweak, state: TweakState, validation: Validation
    ) -> tuple[bool, str, bool]:
        """Decide whether a validated tweak may actually run.

        Returns ``(will_apply, reason, blocked_by_risk)``. The last is true
        only when the sole obstacle is the risk class, which is the one
        obstacle the operator can lift.
        """
        if not state.needs_change:
            return False, ALREADY_DESIRED, False
        if not validation.ok:
            return False, validation.reason or "проверка отклонила изменение", False
        # MEDIUM and above need elevation whatever the tweak itself declares:
        # the risk class, not the tweak author's flag, decides who may run it.
        needs_admin = (
            validation.requires_admin
            or tweak.requires_admin
            or not tweak.risk.auto_applicable
        )
        if needs_admin and not is_admin():
            return False, "нужны права администратора", False
        if not tweak.risk.auto_applicable and not self.allow_risk_above_low:
            return False, (
                f"уровень риска {tweak.risk.value} требует отдельного "
                "подтверждения"
            ), True
        if tweak.risk is RiskLevel.CRITICAL:
            # Never offered, and never openable by a checkbox: CRITICAL is
            # defined as "could leave the machine unusable".
            return (
                False,
                "изменения уровня CRITICAL никогда не применяются автоматически",
                False,
            )
        return True, "", False

    # -- execution ---------------------------------------------------------

    def apply(
        self,
        plan: Plan,
        *,
        dry_run: bool = False,
        progress: ProgressCallback | None = None,
    ) -> RunReport:
        """Execute a plan.

        Args:
            dry_run: Report what would happen and mutate nothing.
            progress: Told which change is running, and how far a long one
                has got. Called on this (the worker) thread.
        """
        report = RunReport(run_id=plan.run_id, dry_run=dry_run)
        self._store.open_run(
            plan.run_id, dry_run=dry_run, planned=len(plan.applicable)
        )
        send = guarded_progress(progress) if progress is not None else None
        total, step = len(plan.applicable), 0
        try:
            for change in plan.changes:
                if not change.will_apply:
                    report.results.append(
                        TweakReport(
                            tweak_id=change.tweak.id,
                            name=change.tweak.name,
                            outcome=(
                                Outcome.NOT_NEEDED
                                if not change.state.needs_change
                                else Outcome.SKIPPED
                            ),
                            phase=Phase.VALIDATE,
                            detail=change.skip_reason,
                        )
                    )
                    continue

                if dry_run:
                    report.results.append(
                        TweakReport(
                            tweak_id=change.tweak.id,
                            name=change.tweak.name,
                            outcome=Outcome.SKIPPED,
                            phase=Phase.VALIDATE,
                            detail="dry run: no changes made",
                            before=change.state.current_value,
                            after=change.state.desired_value,
                        )
                    )
                    continue

                step += 1
                if send is not None:
                    self.context.progress = step_reporter(
                        send, f"Применение {step} из {total}: {change.tweak.name}"
                    )
                    self.context.report_progress(None)
                report.results.append(self._execute(change, plan.run_id))
        finally:
            # A tweak's context must not keep reporting into a finished run.
            self.context.progress = None

        self._store.close_run(
            plan.run_id, applied=report.applied, failed=report.failed,
            rolled_back=report.rolled_back,
        )
        return report

    def _execute(self, change: PlannedChange, run_id: str) -> TweakReport:
        tweak, state = change.tweak, change.state
        self.context.run_id = run_id

        # -- re-check: the plan may be stale -------------------------------
        # The operator approved a preview computed earlier. If the system
        # moved since, applying would overwrite someone's change and back up
        # a value that no longer exists. Nothing is touched in that case.
        try:
            drift = tweak.drift_since_plan(self.context, state)
        except Exception as exc:  # noqa: BLE001 - a failed re-read blocks the change
            _log.exception("pre-apply re-check failed for %s", tweak.id)
            drift = f"не удалось перечитать перед применением ({exc})"
        if drift is not None:
            outcome = (
                Outcome.NOT_NEEDED
                if drift == ALREADY_DESIRED
                else Outcome.SKIPPED
            )
            self._store.record_application(
                run_id, tweak, Phase.VALIDATE, outcome, state, drift
            )
            return TweakReport(
                tweak_id=tweak.id, name=tweak.name, outcome=outcome,
                phase=Phase.VALIDATE, detail=drift,
            )

        # -- backup: mandatory before any mutation ------------------------
        backup: BackupRecord | None = None
        if tweak.scope is not BackupScope.NONE:
            try:
                backup = tweak.backup(self.context, state)
                self._store.persist_backup(backup, run_id)
            except Exception as exc:  # noqa: BLE001
                _log.exception("backup failed for %s", tweak.id)
                self._store.record_application(
                    run_id, tweak, Phase.BACKUP, Outcome.SKIPPED, state, str(exc)
                )
                return TweakReport(
                    tweak_id=tweak.id,
                    name=tweak.name,
                    outcome=Outcome.SKIPPED,
                    phase=Phase.BACKUP,
                    detail=(
                        "Prior state could not be saved, so the change was not "
                        f"attempted. ({exc})"
                    ),
                )

        # -- apply ---------------------------------------------------------
        try:
            result = tweak.apply(self.context, state)
        except PgmError as exc:
            return self._fail_and_rollback(
                run_id, tweak, state, backup, exc.user_message()
            )
        except Exception as exc:  # noqa: BLE001
            _log.exception("apply failed for %s", tweak.id)
            return self._fail_and_rollback(run_id, tweak, state, backup, str(exc))

        if result.outcome is not Outcome.SUCCESS:
            return self._fail_and_rollback(
                run_id, tweak, state, backup, result.detail
            )

        # -- verify ----------------------------------------------------------
        # The backup stays in APPLYING until verification confirms the change.
        # A process that dies anywhere between the backup and this point
        # therefore leaves APPLYING behind, which startup reports as an
        # interrupted change (see core.optimization.recovery).
        try:
            verification = tweak.verify(self.context, state)
        except Exception as exc:  # noqa: BLE001
            _log.exception("verify failed for %s", tweak.id)
            return self._fail_and_rollback(
                run_id, tweak, state, backup, f"verification error: {exc}"
            )

        if not verification.confirmed:
            return self._fail_and_rollback(
                run_id,
                tweak,
                state,
                backup,
                verification.detail
                or "The setting did not hold after it was applied.",
            )

        if backup is not None:
            self._store.mark_backup(backup.backup_id, "APPLIED")
        self._store.record_application(
            run_id, tweak, Phase.VERIFY, Outcome.SUCCESS, state, None
        )
        self._store.audit(
            tweak.subsystem or "tweak", "apply", target=tweak.id,
            old_state=state.current_value,
            new_state=verification.observed_value or state.desired_value,
            result="SUCCESS",
        )
        return TweakReport(
            tweak_id=tweak.id,
            name=tweak.name,
            outcome=Outcome.SUCCESS,
            phase=Phase.VERIFY,
            detail=result.detail,
            before=state.current_value,
            after=verification.observed_value or state.desired_value,
            needs_restart=result.needs_restart,
        )

    def _fail_and_rollback(
        self,
        run_id: str,
        tweak: Tweak,
        state: TweakState,
        backup: BackupRecord | None,
        detail: str,
    ) -> TweakReport:
        """Undo a change that failed to apply or failed to verify."""
        if backup is None:
            self._store.record_application(
                run_id, tweak, Phase.APPLY, Outcome.FAILED, state, detail
            )
            self._store.audit(
                tweak.subsystem or "tweak", "apply", target=tweak.id,
                result="FAILED", error=detail,
            )
            return TweakReport(
                tweak_id=tweak.id, name=tweak.name, outcome=Outcome.FAILED,
                phase=Phase.APPLY, detail=detail,
            )

        try:
            rollback_result = tweak.rollback(self.context, backup)
            restored = rollback_result.outcome is Outcome.SUCCESS
        except Exception as exc:  # noqa: BLE001
            _log.exception("rollback failed for %s", tweak.id)
            restored = False
            detail = f"{detail} Rollback also failed: {exc}"

        self._store.mark_backup(
            backup.backup_id, "RESTORED" if restored else "FAILED", finished=True
        )
        outcome = Outcome.ROLLED_BACK if restored else Outcome.FAILED
        self._store.record_application(run_id, tweak, Phase.ROLLBACK, outcome, state, detail)
        self._store.audit(
            tweak.subsystem or "tweak", "rollback", target=tweak.id,
            old_state=state.desired_value, new_state=backup.old_value,
            result="SUCCESS" if restored else "FAILED", error=detail,
        )
        return TweakReport(
            tweak_id=tweak.id, name=tweak.name, outcome=outcome,
            phase=Phase.ROLLBACK, detail=detail,
        )

    # -- catalogue ---------------------------------------------------------

    def register(self, tweaks: Sequence[Tweak]) -> None:
        """Record tweak metadata so history stays readable across versions."""
        self._store.register(tweaks)
