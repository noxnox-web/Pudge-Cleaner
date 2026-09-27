"""The OPTIMIZE PC pipeline.

Turns a hardware scan into a set of tweaks, plans them, and executes the
plan the operator approved::

    Scan -> Diagnose -> Plan -> [preview] -> Backup -> Apply -> Verify
                                                                  |
                                                        failed ->  Rollback

The pipeline chooses *which* tweaks are relevant; `TweakEngine` owns the
safety invariants for each one. Keeping those separate means adding a tweak
cannot weaken the guarantees, and changing the selection logic cannot break
rollback.

Only findings the scan actually produced generate tweaks. There is no
"apply everything" list, because a change that addresses no observed problem
is the kind of speculative tweak rule #64 forbids.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from ...database.connection import Database
from ...hardware.models import HardwareSnapshot
from ...utilities.logging_setup import get_logger
from ...windows.cleanup.categories import default_categories
from ...windows.cleanup.rules import CleanupCategory
from ..diagnostics.issues import Issue
from ..scoring.score import GamingScore, compute_score
from ..settings.catalog import Tier, by_tier
from .engine import Plan, PlannedChange, RunReport, TweakEngine
from .progress import ApplyProgress, ProgressCallback, guarded_progress
from .tweak import Tweak, TweakContext
from .tweaks.apps import app_tweaks
from .tweaks.choice import tweak_for
from .tweaks.cleanup import CleanTemporaryFilesTweak
from .tweaks.display import tweaks_for_underperforming_displays
from .tweaks.dota_hosts import BlockDotaWebTweak
from .tweaks.game_dvr import DisableGameDvrPolicyTweak, DisableGameDvrTweak
from .tweaks.mouse import DisableMouseAccelerationTweak
from .tweaks.power import SetPowerPlanTweak
from .tweaks.power_settings import (
    DisablePcieAspmTweak,
    DisableUsbSelectiveSuspendTweak,
)
from .tweaks.recycle_bin import EmptyRecycleBinTweak
from .tweaks.windows_old import RemoveWindowsOldTweak

_log = get_logger(__name__)


class OptimizationLevel(str, Enum):
    """How far one OPTIMIZE run goes, chosen on the main window before planning.

    The preview is shown at every level and any row can be unticked there;
    the level decides what the plan contains and what arrives ticked.
    """

    NORMAL = "NORMAL"
    """SAFE and LOW only. MEDIUM stays behind the risk gate, listed as skipped."""

    MEDIUM = "MEDIUM"
    """MEDIUM is planned too, but arrives unticked: allowing the class and
    choosing a change in it stay two separate acts (rule #38)."""

    HARD = "HARD"
    """MEDIUM is planned and arrives ticked, irreversible removals included.
    Choosing this level on the main window is the operator's choice of the
    class; the preview still names every row and marks the irreversible ones."""

    @property
    def allows_risk_above_low(self) -> bool:
        return self is not OptimizationLevel.NORMAL


@dataclass(frozen=True, slots=True)
class OptimizationPreview:
    """What the operator confirms before anything is changed."""

    plan: Plan
    score_before: GamingScore
    level: OptimizationLevel = OptimizationLevel.NORMAL
    """What this plan was built under, so the dialog knows what to tick and
    whether the opt-in is still available, and ``with_selection`` cannot
    lose it."""

    @property
    def allow_risk_above_low(self) -> bool:
        return self.level.allows_risk_above_low

    def ticked_by_default(self, change: PlannedChange) -> bool:
        """Whether an applicable change starts ticked in the preview.

        SAFE and LOW always do. Anything above does only at HARD, where the
        operator chose the whole class before planning.
        """
        return change.tweak.risk.auto_applicable or self.level is OptimizationLevel.HARD

    @property
    def change_count(self) -> int:
        return len(self.plan.applicable)

    @property
    def has_changes(self) -> bool:
        return self.change_count > 0

    def lines(self) -> list[str]:
        return self.plan.preview_lines()

    def with_selection(self, selected: set[int]) -> OptimizationPreview:
        """The same preview with only the operator's chosen changes applied."""
        return OptimizationPreview(
            plan=self.plan.with_selection(selected),
            score_before=self.score_before,
            level=self.level,
        )


@dataclass(slots=True)
class OptimizationOutcome:
    """The before/after report (rule #40).

    Only measurements that were actually taken appear here. Nothing is
    attributed to the optimization that was not observed both before and
    after.
    """

    run: RunReport
    score_before: GamingScore
    score_after: GamingScore | None = None
    snapshot_after: HardwareSnapshot | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def score_delta(self) -> float | None:
        if self.score_after is None or self.score_after.value is None:
            return None
        if self.score_before.value is None:
            return None
        return self.score_after.value - self.score_before.value

    def report_lines(self) -> list[str]:
        lines = [
            f"Применено: {self.run.applied}",
            f"Ошибок: {self.run.failed}",
            f"Откачено: {self.run.rolled_back}",
            "",
            f"Оценка до: {self.score_before.display()}",
        ]
        if self.score_after is not None:
            lines.append(f"Оценка после: {self.score_after.display()}")
            delta = self.score_delta
            if delta is not None:
                lines.append(f"Изменение: {delta:+.0f}")
        # The dashboard score includes the network; these do not, so they
        # can differ from it. Optimizing never re-measures the network, and
        # counting the old measurement as the "after" state would report a
        # value nobody measured.
        lines.append(
            "Обе оценки — только по железу; сеть после оптимизации не "
            "измеряется заново и в сравнение не входит."
        )
        lines.append("")
        lines.append(
            "Изменённая настройка — это проверенное изменение конфигурации, "
            "а не измеренный прирост производительности."
        )
        for result in self.run.results:
            if result.outcome.value in {"SUCCESS", "FAILED", "ROLLED_BACK"}:
                lines.append(f"  [{result.outcome.value}] {result.name}: {result.detail}")
        if self.run.needs_restart:
            lines.append("")
            lines.append("Для полного применения части изменений нужна перезагрузка.")
        return lines


class OptimizationPipeline:
    """Builds and runs an optimization pass."""

    def __init__(
        self,
        database: Database,
        *,
        allow_risk_above_low: bool = False,
        cleanup_categories: tuple[CleanupCategory, ...] | None = None,
        restore_point: Callable[[], tuple[bool, str]] | None = None,
    ) -> None:
        """
        Args:
            restore_point: Creates a Windows restore point before a real
                run. Injected so tests never create one; the application
                passes :func:`.restore_point.create`.
        """
        self.database = database
        self.restore_point = restore_point
        self.allow_risk_above_low = allow_risk_above_low
        self.cleanup_categories = (
            cleanup_categories if cleanup_categories is not None
            else default_categories()
        )

    # -- selection ---------------------------------------------------------

    def build_tweaks(
        self, snapshot: HardwareSnapshot, issues: tuple[Issue, ...] = ()
    ) -> list[Tweak]:
        """Choose the tweaks this pass will offer.

        Two kinds of tweak appear here. Ones that depend on what the scan
        *found* — a display running below its capability — are built from
        the snapshot. Ones that can decide for themselves are always
        offered and self-skip in their own ``scan``: the plan then shows
        "already correct" instead of quietly omitting them, which is the
        difference between a report the operator can trust and one that
        hides its reasoning.
        """
        # ``issues`` stays in the signature because selection is allowed to
        # depend on the diagnosis; today only the display tweaks are
        # snapshot-driven, and they read the monitors directly.
        tweaks: list[Tweak] = []

        # Displays running below their capability at the current resolution.
        tweaks.extend(tweaks_for_underperforming_displays(snapshot.monitors))

        # Reclaimable disk space is worth reclaiming whether or not the
        # drive is under pressure. This used to be gated on the system disk
        # being under 25% free, which meant a PC with room to spare never
        # had its shader caches or crash dumps cleared at all — the cleanup
        # was invisible rather than absent, which is worse.
        tweaks.append(CleanTemporaryFilesTweak(self.cleanup_categories))

        # The Recycle Bin. Self-skips when empty; the preview states the
        # size and item count before the operator confirms.
        tweaks.append(EmptyRecycleBinTweak())

        # The active power scheme, then the two settings inside it that
        # the scheme does not necessarily cover. Each self-skips when it is
        # already right or absent on this machine.
        tweaks.append(SetPowerPlanTweak(cpu_model=snapshot.cpu.model))
        tweaks.append(DisableUsbSelectiveSuspendTweak())
        tweaks.append(DisablePcieAspmTweak())

        # Settings that trade a user-visible behaviour for consistency or
        # for work the machine stops doing. MEDIUM, so they appear in the
        # preview as held back until the operator asks for that class, and
        # then arrive unticked.
        tweaks.append(DisableMouseAccelerationTweak())
        tweaks.append(DisableGameDvrTweak())
        tweaks.append(DisableGameDvrPolicyTweak())

        # Experimental, on a single unreplicated report. MEDIUM, so it is
        # held behind the opt-in and arrives unticked; its rationale states
        # the evidence and what stops loading in the Dota 2 client.
        tweaks.append(BlockDotaWebTweak())

        # The catalogue's recommended tier: settings every source agrees on,
        # at the recommended option. HAGS, Game Mode and the MMCSS/scheduler
        # values are deliberately *not* here — the sources disagree on them,
        # so they live in the «Настройки Windows» dialog, where the operator
        # sees the current state and the reasoning and chooses.
        for spec in by_tier(Tier.RECOMMENDED):
            if spec.recommended is not None:
                tweaks.append(tweak_for(spec, spec.recommended))

        # Removals. These are the only tweaks that take something away and
        # cannot put it back, so each is marked irreversible in the preview
        # and every one of them is MEDIUM — never applied without the
        # operator asking for that class first.
        tweaks.extend(app_tweaks())

        # The Windows 11 upgrade leftover, if present. The tweak scans for its
        # own applicability, so it is always offered and self-skips elsewhere.
        tweaks.append(RemoveWindowsOldTweak())

        _log.info("optimization plan: %d candidate tweaks", len(tweaks))
        return tweaks

    # -- planning ----------------------------------------------------------

    def preview(
        self,
        snapshot: HardwareSnapshot,
        issues: tuple[Issue, ...] = (),
        score_before: GamingScore | None = None,
        *,
        level: OptimizationLevel | None = None,
    ) -> OptimizationPreview:
        """Produce the dry-run preview. Changes nothing.

        The preview is mandatory and not skippable: the operator approves
        this exact list before the pipeline touches anything.

        Args:
            level: What the operator chose on the main window, for this plan
                only. ``None`` keeps the pipeline's own default, which never
                goes past NORMAL unless the pipeline was built to allow MEDIUM.
        """
        if level is None:
            level = (
                OptimizationLevel.MEDIUM
                if self.allow_risk_above_low
                else OptimizationLevel.NORMAL
            )
        engine = self._engine(allow_risk_above_low=level.allows_risk_above_low)
        tweaks = self.build_tweaks(snapshot, issues)
        engine.register(tweaks)
        plan = engine.plan(tweaks)
        return OptimizationPreview(
            plan=plan,
            score_before=score_before or compute_score(snapshot),
            level=level,
        )

    def apply(
        self,
        preview: OptimizationPreview,
        *,
        dry_run: bool = False,
        rescan: object | None = None,
        progress: ProgressCallback | None = None,
    ) -> OptimizationOutcome:
        """Execute an approved preview.

        Args:
            rescan: A callable returning a fresh :class:`HardwareSnapshot`,
                used to measure the after-state. Injected rather than
                imported so this module does not depend on the scanner.
            progress: Told which change is running and how far a long one
                has got (removing ``Windows.old`` takes minutes). Called on
                the thread that runs this method.
        """
        restore_note = ""
        if not dry_run and self.restore_point is not None and preview.has_changes:
            # A second net under PGM's own per-change backups. Its failure
            # is reported and never blocks the run: those backups are what
            # rollback actually relies on.
            try:
                _created, restore_note = self.restore_point()
            except Exception as exc:  # noqa: BLE001 - see above
                _log.exception("restore point failed")
                restore_note = f"Точка восстановления не создана: {exc}"

        engine = self._engine()
        run = engine.apply(preview.plan, dry_run=dry_run, progress=progress)

        outcome = OptimizationOutcome(run=run, score_before=preview.score_before)
        if restore_note:
            outcome.notes.append(restore_note)
        if dry_run:
            outcome.notes.append("Пробный прогон: ничего не изменено.")
            return outcome

        if callable(rescan):
            if progress is not None:
                guarded_progress(progress)(
                    ApplyProgress("Замер состояния после изменений…")
                )
            try:
                snapshot_after = rescan()
                outcome.snapshot_after = snapshot_after
                outcome.score_after = compute_score(snapshot_after)
            except Exception as exc:  # noqa: BLE001
                # A failed rescan must not invalidate a successful run; it
                # only means the after-state is unknown, and saying so is
                # better than reporting a stale score as the new one.
                _log.exception("post-optimization rescan failed")
                outcome.notes.append(
                    f"Состояние «после» не удалось измерить: {exc}"
                )
        else:
            outcome.notes.append(
                "Повторное сканирование не выполнялось, состояние «после» не показано."
            )
        return outcome

    def _engine(self, *, allow_risk_above_low: bool | None = None) -> TweakEngine:
        return TweakEngine(
            self.database,
            TweakContext(database=self.database),
            allow_risk_above_low=(
                self.allow_risk_above_low
                if allow_risk_above_low is None
                else allow_risk_above_low
            ),
        )
