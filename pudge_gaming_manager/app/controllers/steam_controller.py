"""Steam reset planning and execution for the GUI.

Both steps read or delete large amounts of data, so they run off the UI
thread like the scan does. The keep-list is built into the program (see
``games.steam.default_keep``); the preview then lets the operator untick any
game, so a stray click cannot wipe a wanted title without it being seen.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from ...core.games import DEFAULT_KEEP_IDS, SteamWiper, WipePlan
from .background import BackgroundRunner


class SteamController(QObject):
    """Plans a Steam reset from a profile, and performs an approved plan."""

    planned = Signal(object)
    """Emits a :class:`WipePlan`, or ``None`` when Steam is not installed."""

    progress = Signal(object)
    """Emits :class:`Progress` from the worker thread while games are deleted.

    Connect it to a method of a UI object, so Qt delivers it queued on the
    UI thread (see ``background.py``)."""

    wiped = Signal(object)
    """Emits a :class:`WipeResult`."""

    failed = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._wiper = SteamWiper()
        self._runner = BackgroundRunner(self)
        self._runner.failed.connect(self._on_failed)
        self._wiping = False

    @property
    def busy(self) -> bool:
        return self._runner.busy

    @property
    def wiping(self) -> bool:
        """True only while games are being deleted, for the close guard."""
        return self._wiping

    def start_plan(self) -> None:
        """Plan a reset against the built-in keep-list.

        The keep-list is baked into the program (see ``default_keep``); the
        preview then lets the operator untick any game before anything is
        removed, so no profile or config file is needed.
        """
        self._runner.start(
            lambda: self._wiper.scan(set(DEFAULT_KEEP_IDS)), self.planned.emit
        )

    def start_wipe(self, plan: WipePlan) -> None:
        if self._runner.start(
            lambda: self._wiper.wipe(plan, progress=self.progress.emit),
            self._on_wiped,
        ):
            self._wiping = True

    def _on_wiped(self, result: object) -> None:
        self._wiping = False
        self.wiped.emit(result)

    def _on_failed(self, message: str) -> None:
        self._wiping = False
        self.failed.emit(message)

    def shutdown(self) -> None:
        self._runner.shutdown()
