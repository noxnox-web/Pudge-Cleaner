"""Remove the ``Windows.old`` rollback folder on Windows 11.

Rationale (rule #64)
--------------------
A Windows 11 feature update leaves the previous install in ``Windows.old``,
often tens of gigabytes, purely so the upgrade can be rolled back for ten
days. On a club PC that rollback is never used and the space is wanted back.

**What is claimed:** the folder is deleted and the space is freed. **What is
not claimed:** any performance gain. This is disk reclamation, nothing more.

Irreversible (``BackupScope.NONE``): deleting ``Windows.old`` removes the
ability to roll the Windows upgrade back, so it is shown in the preview
marked as irreversible and applied only after the operator confirms. It needs
administrator rights, because the folder holds files owned by
``TrustedInstaller``.
"""

from __future__ import annotations

from ....utilities.formatting import format_size
from ....utilities.tree_delete import DeleteStats
from ....windows.cleanup import windows_old
from ..tweak import (
    ApplyResult,
    BackupRecord,
    BackupScope,
    Outcome,
    RiskLevel,
    Tweak,
    TweakContext,
    TweakState,
    Validation,
    Verification,
)


class RemoveWindowsOldTweak(Tweak):
    """Delete ``%SystemDrive%\\Windows.old`` after a Windows 11 upgrade."""

    id = "windows.remove_windows_old"
    version = 1
    name = "Удаление папки windows.old"
    description = (
        "Удаляет папку windows.old — копию прежней Windows после обновления, "
        "которую система и так удалит через 10 дней."
    )
    rationale = (
        "После обновления Windows 11 старая система остаётся в windows.old "
        "(часто десятки гигабайт) только для отката в течение 10 дней. На "
        "клубном ПК откат не нужен, а место — да."
    )
    risk = RiskLevel.LOW
    subsystem = "storage"
    scope = BackupScope.NONE
    requires_admin = True

    def __init__(self) -> None:
        super().__init__()
        # Measured at scan, so apply can show "N of M" instead of a figure
        # with nothing to compare it to.
        self._size: int | None = None

    def scan(self, ctx: TweakContext) -> TweakState:
        if not windows_old.is_windows_11():
            return TweakState(needs_change=False, summary="Не Windows 11")
        if not windows_old.is_present():
            return TweakState(needs_change=False, summary="Папки windows.old нет")
        size = self._size = windows_old.folder_size()
        freed = f" ({format_size(size)})" if size else ""
        return TweakState(
            current_value="present",
            desired_value="removed",
            needs_change=True,
            summary=f"Удалить папку windows.old{freed}",
        )

    def validate(self, ctx: TweakContext, state: TweakState) -> Validation:
        if not windows_old.is_windows_11():
            return Validation.refuse("Только для Windows 11.")
        return Validation.allow(requires_admin=True)

    def backup(self, ctx: TweakContext, state: TweakState) -> BackupRecord:
        # Never called: the engine skips backup for BackupScope.NONE.
        raise NotImplementedError("windows.old removal is not reversible.")

    def apply(self, ctx: TweakContext, state: TweakState) -> ApplyResult:
        if ctx.dry_run:
            return ApplyResult(Outcome.SUCCESS, "Пробный прогон: папка не удалена.")
        stats = windows_old.remove(
            progress=lambda running: self._report(ctx, running)
        )
        if stats is None:
            return ApplyResult(
                Outcome.FAILED,
                "Папка windows.old не удалена: вместо неё ссылка, или к ней нет "
                "доступа даже с правами администратора.",
            )
        freed = f"освобождено {format_size(stats.bytes)}, файлов: {stats.files}"
        if stats.failed:
            return ApplyResult(
                Outcome.FAILED,
                f"windows.old удалена не полностью ({freed}); не удалось: "
                f"{stats.failed} — заняты другими программами. "
                + "; ".join(stats.errors[:3]),
            )
        return ApplyResult(Outcome.SUCCESS, f"Папка windows.old удалена: {freed}.")

    def _report(self, ctx: TweakContext, running: DeleteStats) -> None:
        """Freed so far, against the size the scan measured."""
        total = self._size
        if not total:
            ctx.report_progress(None, f"удалено {format_size(running.bytes)}")
            return
        # Files can grow between scan and delete; the bar never passes 100%.
        ctx.report_progress(
            min(running.bytes / total, 1.0),
            f"удалено {format_size(running.bytes)} из {format_size(total)}",
        )

    def verify(self, ctx: TweakContext, state: TweakState) -> Verification:
        if windows_old.is_present():
            return Verification.deny("present", "Папка windows.old всё ещё на месте.")
        return Verification.confirm("removed", "Папки windows.old больше нет.")

    def rollback(self, ctx: TweakContext, backup: BackupRecord) -> ApplyResult:
        # Unreachable: with BackupScope.NONE the engine never creates a backup.
        return ApplyResult(
            Outcome.FAILED, "Удалённую папку windows.old восстановить нельзя."
        )


__all__ = ["RemoveWindowsOldTweak"]
