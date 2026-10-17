"""Checking for a newer release, and installing it, off the UI thread."""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QObject, Signal

from ...core.update.updater import Release, Updater
from .background import BackgroundRunner


class UpdateController(QObject):
    checked = Signal(object)
    """Emits an :class:`UpdateInfo`."""

    progress = Signal(object)
    """Emits :class:`Progress` from the worker thread; connect it to a method
    of a UI object so Qt delivers it queued on the UI thread."""

    installed = Signal()
    """The new program is in place and started; this one should close."""

    failed = Signal(str)

    def __init__(self, parent: QObject | None = None, updater: Updater | None = None) -> None:
        super().__init__(parent)
        self._updater = updater
        self._runner = BackgroundRunner(self)
        self._runner.failed.connect(self._on_failed)
        self._installing = False

    def _service(self) -> Updater:
        if self._updater is None:
            self._updater = Updater()
        return self._updater

    @property
    def busy(self) -> bool:
        return self._runner.busy

    @property
    def installing(self) -> bool:
        """True while the program is being replaced: closing must wait."""
        return self._installing and self._runner.busy

    def check(self) -> None:
        self._runner.start(lambda: self._service().check(), self.checked.emit)

    def install(self, release: Release, blocker: Callable[[], str | None]) -> None:
        if self._runner.start(
            lambda: self._service().install(release, self.progress.emit, blocker),
            self._on_installed,
        ):
            self._installing = True

    def _on_installed(self, _payload: object) -> None:
        self._installing = False
        self.installed.emit()

    def _on_failed(self, message: str) -> None:
        self._installing = False
        self.failed.emit(message)

    def shutdown(self) -> None:
        self._runner.shutdown()
