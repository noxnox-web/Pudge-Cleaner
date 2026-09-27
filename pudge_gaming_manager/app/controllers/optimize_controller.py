"""Optimization orchestration for the GUI.

Both phases run off the UI thread. The preview alone takes ~15 seconds on a
machine with a large Temp folder, because it counts every candidate file
before promising to remove anything — a preview that guessed would not be a
preview.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from ...core.optimization.pipeline import (
    OptimizationLevel,
    OptimizationOutcome,
    OptimizationPipeline,
    OptimizationPreview,
)
from ...core.scanner.hardware_scan import HardwareScanner
from ...core.services import create_optimization_pipeline
from ...core.types import HardwareSnapshot, Issue
from .background import BackgroundRunner


class OptimizeController(QObject):
    """Runs preview and apply on worker threads."""

    preview_started = Signal()
    preview_ready = Signal(object)
    apply_started = Signal()
    apply_progress = Signal(object)
    """Emits :class:`ApplyProgress` from the worker thread while applying.

    Connect it to a method of a UI object, so Qt delivers it queued on the
    UI thread (see ``background.py``)."""
    apply_finished = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        pipeline: OptimizationPipeline | None = None,
        parent: QObject | None = None,
        scanner: HardwareScanner | None = None,
    ) -> None:
        super().__init__(parent)
        # Shared with the dashboard's scan controller so the after-state
        # measurement reuses the inventory that scan already read, instead
        # of spending another 2.3 s in PowerShell asking the same question.
        self._scanner = scanner or HardwareScanner()
        # The GUI asks core for a working pipeline rather than opening a
        # database itself; storage lifetime is not a view concern.
        self._pipeline = pipeline or create_optimization_pipeline()
        self._runner = BackgroundRunner(self)
        self._runner.failed.connect(self._on_failed)
        self._applying = False

    @property
    def busy(self) -> bool:
        return self._runner.busy

    @property
    def applying(self) -> bool:
        """True only while changes are being written to the system.

        Preview and the idle state are both False. The window uses this to
        refuse to close mid-apply, where interrupting between a change and
        its verification could leave a setting half-applied.
        """
        return self._applying

    def start_preview(
        self,
        snapshot: HardwareSnapshot,
        issues: tuple[Issue, ...],
        *,
        level: OptimizationLevel = OptimizationLevel.NORMAL,
    ) -> None:
        """Plan a run at the level the operator chose on the main window."""
        if self._runner.start(
            lambda: self._pipeline.preview(snapshot, issues, level=level),
            self.preview_ready.emit,
        ):
            self.preview_started.emit()

    def start_apply(self, preview: OptimizationPreview) -> None:
        # The after-state is a real measurement, so the report never
        # attributes an unmeasured change to the optimization. Only the
        # inventory is reused: load, temperatures, free space and the
        # display mode are all read again, which is exactly what a tweak
        # could have moved.
        if self._runner.start(
            lambda: self._pipeline.apply(
                preview,
                rescan=lambda: self._scanner.scan(reuse_inventory=True),
                progress=self.apply_progress.emit,
            ),
            self._on_applied,
        ):
            self._applying = True
            self.apply_started.emit()

    def _on_applied(self, outcome: object) -> None:
        self._applying = False
        self.apply_finished.emit(outcome)

    def _on_failed(self, message: str) -> None:
        # Clears the apply flag whether the failure came from preview or
        # apply, so a failed run never leaves the window unclosable.
        self._applying = False
        self.failed.emit(message)

    def shutdown(self) -> None:
        self._runner.shutdown()


__all__ = [
    "OptimizeController",
    "OptimizationLevel",
    "OptimizationPreview",
    "OptimizationOutcome",
]
