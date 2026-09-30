"""Planning and running a disk cleanup the operator selected.

Flow, the same one every destructive action in this program follows::

    PLAN -> SHOW -> CONFIRM -> RUN -> REPORT

:meth:`DiskCleaner.plan` scans every known category plus the Recycle Bin and
changes nothing. The dialog shows what was found, the operator ticks what
should go, and :meth:`DiskCleaner.run` executes exactly that selection —
re-validating each file at deletion time, because the plan is an inventory,
never a permission slip.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass, field

from ...utilities.disk_space import FreeSpaceDelta, drive_of
from ...utilities.formatting import format_size
from ...utilities.logging_setup import get_logger
from ...utilities.progress import Progress, ProgressCallback, guarded
from ...windows.cleanup import recycle_bin
from ...windows.cleanup.categories import CATEGORIES
from ...windows.cleanup.engine import CleanupEngine
from ...windows.cleanup.recycle_bin import RecycleBinState
from ...windows.cleanup.report import CleanResult, ScanReport
from ...windows.cleanup.rules import CleanupCategory

_log = get_logger(__name__)

#: Selection id standing for the Recycle Bin, which is not a file category
#: and therefore has no :class:`CleanupCategory` of its own.
RECYCLE_BIN_ID = "recycle_bin"


@dataclass(slots=True)
class DiskCleanupPlan:
    """Everything a cleanup scan found, before anything is selected."""

    scan: ScanReport
    bin_state: RecycleBinState

    @property
    def total_bytes(self) -> int:
        bin_bytes = self.bin_state.size_bytes if self.bin_state.available else 0
        return self.scan.total_bytes + bin_bytes

    @property
    def size_display(self) -> str:
        return format_size(self.total_bytes)

    @property
    def has_content(self) -> bool:
        return self.total_bytes > 0 or self.bin_state.has_content

    def default_selection(self) -> set[str]:
        """Ids ticked when the dialog opens.

        The categories marked ``enabled_by_default`` plus the Recycle Bin.
        Anything holding the user's own files stays unticked, so clearing
        it is always a deliberate act.
        """
        selected = {
            report.category.id
            for report in self.scan.categories
            if report.category.enabled_by_default and report.available
        }
        if self.bin_state.has_content:
            selected.add(RECYCLE_BIN_ID)
        return selected


@dataclass(slots=True)
class DiskCleanupResult:
    """What a cleanup actually removed.

    Two numbers, because they are two different facts (rule #40).
    :attr:`total_bytes` is the logical size of everything deleted.
    :attr:`reclaimed_bytes` is how much free space the drives really gained,
    read from the filesystem before and after. Shadow copies, deduplication
    and cluster rounding make them differ, and the report shows both rather
    than picking the flattering one.
    """

    files: CleanResult = field(default_factory=CleanResult)
    bin_attempted: bool = False
    bin_emptied: bool = False
    bin_detail: str = ""
    bin_bytes: int = 0
    space: FreeSpaceDelta = field(default_factory=FreeSpaceDelta)

    @property
    def total_bytes(self) -> int:
        """Logical size of every file removed."""
        return self.files.deleted_bytes + self.bin_bytes

    @property
    def size_display(self) -> str:
        return format_size(self.total_bytes)

    @property
    def reclaimed_bytes(self) -> int | None:
        """Free space the drives actually gained, or ``None`` if unmeasured."""
        return self.space.freed_bytes if self.space.measured else None

    @property
    def reclaimed_display(self) -> str:
        reclaimed = self.reclaimed_bytes
        return format_size(reclaimed) if reclaimed is not None else "не измерено"

    @property
    def space_note(self) -> str:
        """One line explaining a gap between the two numbers, if there is one.

        Silence when they agree closely: an explanation nobody needs is
        noise, and noise is what stops the real warnings being read.
        """
        reclaimed = self.reclaimed_bytes
        if reclaimed is None:
            return (
                "Свободное место до и после измерить не удалось, поэтому "
                "показан только суммарный размер удалённого."
            )
        shortfall = self.total_bytes - reclaimed
        if self.total_bytes and shortfall > max(self.total_bytes * 0.1, 64 << 20):
            return (
                f"На диске освободилось меньше, чем удалено "
                f"({format_size(reclaimed)} против {self.size_display}). "
                "Обычно так бывает, когда часть файлов удерживают теневые "
                "копии или точки восстановления, либо на том включено "
                "сжатие или дедупликация."
            )
        return ''


class DiskCleaner:
    """Plans and runs a cleanup over file categories and the Recycle Bin."""

    def __init__(self, categories: tuple[CleanupCategory, ...] = CATEGORIES) -> None:
        # Every category is scanned, not only the default ones: the dialog
        # cannot offer a choice it was never given the numbers for.
        self.categories = categories

    def plan(self) -> DiskCleanupPlan:
        """Inventory everything. Changes nothing."""
        plan = DiskCleanupPlan(
            scan=CleanupEngine(self.categories).scan(),
            bin_state=recycle_bin.query(),
        )
        _log.info(
            "cleanup plan: %s across %d categories, %d items in the recycle bin",
            plan.size_display,
            len(plan.scan.categories),
            plan.bin_state.item_count,
        )
        return plan

    def run(
        self,
        plan: DiskCleanupPlan,
        selected: set[str],
        *,
        dry_run: bool = False,
        progress: ProgressCallback | None = None,
    ) -> DiskCleanupResult:
        """Execute the operator's selection from ``plan``.

        Args:
            selected: Category ids to clean, optionally including
                :data:`RECYCLE_BIN_ID`. Ids absent from the plan are
                ignored rather than guessed at.
            dry_run: Count what would go without deleting anything.
            progress: Told what is being cleaned and how far it has got.
                Called on the thread that runs this method.
        """
        result = DiskCleanupResult()

        # Read free space before anything is deleted, on every drive the
        # selection actually touches. A dry run measures nothing: there is
        # no "after" to compare against.
        if not dry_run:
            result.space.measure_before(self._drives_touched(plan, selected))

        chosen = ScanReport(
            categories=[
                report
                for report in plan.scan.categories
                if report.category.id in selected and report.available
            ],
            duration_s=plan.scan.duration_s,
        )
        if chosen.categories:
            # The engine re-validates every path against the same gate the
            # scan used, so a stale inventory cannot authorise a delete.
            result.files = CleanupEngine(self.categories).clean(
                chosen, dry_run=dry_run, progress=progress
            )

        if RECYCLE_BIN_ID in selected and plan.bin_state.has_content:
            result.bin_attempted = True
            if dry_run:
                result.bin_emptied = True
                result.bin_bytes = plan.bin_state.size_bytes
                result.bin_detail = "Пробный прогон: корзина не очищена."
            else:
                if progress is not None:
                    # One shell call with no callbacks: nothing to measure.
                    guarded(progress)(Progress("Очистка корзины…"))
                before = plan.bin_state.size_bytes
                ok, detail = recycle_bin.empty()
                result.bin_emptied = ok
                result.bin_detail = detail
                after = recycle_bin.query()
                result.bin_bytes = max(
                    before - (after.size_bytes if after.available else before), 0
                )

        if not dry_run:
            result.space.measure_after()

        _log.info(
            "cleanup run: %s deleted in %d files, %s actually reclaimed%s",
            result.size_display,
            result.files.deleted_files,
            result.reclaimed_display,
            ", recycle bin emptied" if result.bin_emptied else "",
        )
        return result

    @staticmethod
    def _drives_touched(plan: DiskCleanupPlan, selected: set[str]) -> set[str]:
        """Volume roots the selection will delete from.

        Only these are measured. Reading every volume on the machine would
        fold an unrelated drive's activity into the reported figure.
        """
        drives: set[str] = set()
        for report in plan.scan.categories:
            if report.category.id not in selected or not report.available:
                continue
            for root in report.roots_scanned:
                anchor = drive_of(root)
                if anchor:
                    drives.add(anchor)
        if RECYCLE_BIN_ID in selected:
            # SHEmptyRecycleBinW empties every drive's bin, so every drive
            # that has one can gain space.
            drives.update(
                drive_of(root)
                for report in plan.scan.categories
                for root in report.roots_scanned
                if drive_of(root)
            )
            system_drive = drive_of(pathlib.Path.home())
            if system_drive:
                drives.add(system_drive)
        return drives


__all__ = [
    "RECYCLE_BIN_ID",
    "DiskCleaner",
    "DiskCleanupPlan",
    "DiskCleanupResult",
]
