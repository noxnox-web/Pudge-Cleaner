"""The tools: the usage survey, folder delete/move from it, and the startup manager.

They are small enough that a controller each would be files of boilerplate
around one worker call. They share one because they share a shape — survey
or enumerate on a worker thread, then hand the result to a dialog — not
because they are related.

The usage survey walks whole directory trees and the startup enumeration
opens registry keys that can block; neither belongs on the UI thread.
Toggling a startup entry writes to the registry, so it is a worker call
too and its result is the re-read state, never an assumption.

Deleting or moving a folder takes a plan first, like every destructive
action here: measure and refuse on a worker, let the operator confirm, then
run the plan on a worker. Moving 100 GB takes many minutes, so the run
reports progress and blocks closing the window (:attr:`modifying`).
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from ...core.cleanup import FolderAction, FolderPlan, plan_delete, plan_move, run_folder, survey
from ...core.startup import StartupEntry, StartupManager
from .background import BackgroundRunner


class ToolsController(QObject):
    """Runs the disk-usage survey and the startup-entry list off the UI thread."""

    usage_ready = Signal(object)
    """Emits a :class:`UsageReport`."""

    startup_ready = Signal(object)
    """Emits a ``list[StartupEntry]``."""

    startup_toggled = Signal(object)
    """Emits the re-read :class:`StartupEntry` after a toggle."""

    folder_planned = Signal(object)
    """Emits a :class:`FolderPlan` for the operator to confirm."""

    progress = Signal(object)
    """Emits :class:`Progress` from the worker thread during the usage survey
    and while a folder is deleted or moved. Connect it to a method of a UI
    object, so Qt delivers it queued on the UI thread (see ``background.py``)."""

    folder_done = Signal(object)
    """Emits a :class:`FolderResult`."""

    failed = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._startup = StartupManager()
        self._runner = BackgroundRunner(self)
        self._runner.failed.connect(self._on_failed)
        self._modifying = False

    @property
    def busy(self) -> bool:
        return self._runner.busy

    @property
    def modifying(self) -> bool:
        """True only while a folder is being deleted or moved, for the close guard."""
        return self._modifying

    def _on_failed(self, message: str) -> None:
        self._modifying = False
        self.failed.emit(message)

    # -- disk usage --------------------------------------------------------

    def start_usage_survey(self) -> None:
        """Measure the largest folders. Reads only; deletes nothing."""
        self._runner.start(
            lambda: survey(progress=self.progress.emit), self.usage_ready.emit
        )

    # -- folders from the survey ---------------------------------------------

    def start_folder_plan(
        self, action: FolderAction, source: object, destination_parent: object = None
    ) -> None:
        """Measure a folder and refuse what must not be touched. Changes nothing."""
        if action is FolderAction.DELETE:
            work = lambda: plan_delete(source)  # noqa: E731
        else:
            work = lambda: plan_move(source, destination_parent)  # noqa: E731
        self._start_folder_job(work, self.folder_planned.emit)

    def start_folder_run(self, plan: FolderPlan) -> None:
        """Do exactly what the operator confirmed."""
        if self._start_folder_job(
            lambda: run_folder(plan, progress=self.progress.emit),
            self._on_folder_done,
        ):
            self._modifying = True

    def _start_folder_job(self, work, on_done) -> bool:
        """Start a folder job, or say so: the dialog that asked is waiting.

        The dialog cannot be closed while it waits, so a request that was
        silently dropped because the worker was busy would strand it.
        """
        if self._runner.start(work, on_done):
            return True
        self.failed.emit("Подождите: предыдущая операция ещё не закончилась.")
        return False

    def _on_folder_done(self, result: object) -> None:
        self._modifying = False
        self.folder_done.emit(result)

    # -- startup -----------------------------------------------------------

    def start_startup_scan(self) -> None:
        self._runner.start(self._startup.entries, self.startup_ready.emit)

    def start_startup_toggle(self, entry: StartupEntry, enabled: bool) -> None:
        self._runner.start(
            lambda: self._startup.set_enabled(entry, enabled),
            self.startup_toggled.emit,
        )

    def shutdown(self) -> None:
        self._runner.shutdown()
