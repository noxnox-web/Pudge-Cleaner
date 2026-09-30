"""Disk-cleanup planning and execution for the GUI.

Both steps walk large directory trees — a Temp folder with fifty thousand
files is ordinary — so they run off the UI thread like every other slow
action in this program. The controller owns no cleanup logic of its own: it
hands work to :class:`~pudge_gaming_manager.core.cleanup.DiskCleaner` and
relays the results as signals.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from ...core.cleanup import DiskCleaner, DiskCleanupPlan
from .background import BackgroundRunner


class CleanupController(QObject):
    """Plans a disk cleanup, and runs the selection the operator approved."""

    planned = Signal(object)
    """Emits a :class:`DiskCleanupPlan`."""

    progress = Signal(object)
    """Emits :class:`Progress` from the worker thread while files are deleted.

    Connect it to a method of a UI object, so Qt delivers it queued on the
    UI thread (see ``background.py``)."""

    cleaned = Signal(object)
    """Emits a :class:`DiskCleanupResult`."""

    failed = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._cleaner = DiskCleaner()
        self._runner = BackgroundRunner(self)
        self._runner.failed.connect(self._on_failed)
        self._cleaning = False

    @property
    def busy(self) -> bool:
        return self._runner.busy

    @property
    def cleaning(self) -> bool:
        """True only while files are being deleted, for the close guard."""
        return self._cleaning

    def start_plan(self) -> None:
        """Inventory every category and the Recycle Bin. Changes nothing."""
        self._runner.start(self._cleaner.plan, self.planned.emit)

    def start_clean(self, plan: DiskCleanupPlan, selected: set[str]) -> None:
        """Delete exactly what the operator ticked in the preview."""
        if self._runner.start(
            lambda: self._cleaner.run(
                plan, selected, progress=self.progress.emit
            ),
            self._on_cleaned,
        ):
            self._cleaning = True

    def _on_cleaned(self, result: object) -> None:
        self._cleaning = False
        self.cleaned.emit(result)

    def _on_failed(self, message: str) -> None:
        self._cleaning = False
        self.failed.emit(message)

    def shutdown(self) -> None:
        self._runner.shutdown()
