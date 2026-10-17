"""The «Ядра GPU» tab's work, off the UI thread.

Four kinds of job share one worker, so only one runs at a time: reading the
machine, the benchmark, applying a chosen core, restarting the adapters. The
benchmark takes minutes and changes the machine while it runs, so it is the
one job that can be asked to stop; stopping makes it put the original
setting back before it returns.
"""

from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Signal

from ...core import services
from ...core.gpu_affinity.scan import ScanSettings
from ...core.gpu_affinity.service import GpuAffinityService
from .background import BackgroundRunner


class AffinityController(QObject):
    overview_ready = Signal(object)
    """Emits an :class:`Overview`."""

    progress = Signal(object)
    """Emits :class:`Progress` from the worker thread; connect it to a method
    of a UI object so Qt delivers it queued on the UI thread."""

    scan_done = Signal(object)
    """Emits a :class:`ScanOutcome`."""

    planned = Signal(object)
    """Emits the :class:`Plan` for the operator to confirm."""

    applied = Signal(object)
    """Emits ``(RunReport, notes)``."""

    restarted = Signal(object)
    """Emits the list of adapters that did not come back (empty when fine)."""

    failed = Signal(str)

    def __init__(
        self, parent: QObject | None = None, service: GpuAffinityService | None = None
    ) -> None:
        super().__init__(parent)
        self._service = service
        self._runner = BackgroundRunner(self)
        self._runner.failed.connect(self._on_failed)
        self._stop = threading.Event()
        self._changing = False
        self._scanning = False

    def _svc(self) -> GpuAffinityService:
        if self._service is None:
            self._service = services.create_gpu_affinity_service()
        return self._service

    @property
    def busy(self) -> bool:
        return self._runner.busy

    @property
    def changing(self) -> bool:
        """True while the machine is being changed: closing must wait."""
        return self._changing and self._runner.busy

    @property
    def scanning(self) -> bool:
        """True while the benchmark runs, the one job that can be stopped."""
        return self._scanning and self._runner.busy

    def _on_failed(self, message: str) -> None:
        self._changing = self._scanning = False
        self.failed.emit(message)

    def _done(self, signal: Signal):
        def done(payload: object) -> None:
            self._changing = self._scanning = False
            signal.emit(payload)

        return done

    # -- reading ---------------------------------------------------------------

    def load_overview(self) -> None:
        self._runner.start(lambda: self._svc().overview(), self.overview_ready.emit)

    # -- the benchmark -------------------------------------------------------------

    def start_scan(self, adapters: object, settings: ScanSettings) -> None:
        self._stop.clear()
        if self._runner.start(
            lambda: self._svc().scan(
                adapters, settings,
                progress=self.progress.emit, should_stop=self._stop.is_set,
            ),
            self._done(self.scan_done),
        ):
            self._changing = self._scanning = True

    def stop_scan(self) -> None:
        """Ends the benchmark at its next check; the original setting is put back."""
        self._stop.set()

    # -- choosing ---------------------------------------------------------------------

    def plan(self, adapters: object, cpu: int) -> None:
        self._runner.start(lambda: self._svc().plan(adapters, cpu), self.planned.emit)

    def apply(self, plan: object) -> None:
        if self._runner.start(lambda: self._svc().apply(plan), self._done(self.applied)):
            self._changing = True

    def restart_adapters(self, adapters: object) -> None:
        if self._runner.start(
            lambda: self._svc().restart_adapters(adapters, self.progress.emit),
            self._done(self.restarted),
        ):
            self._changing = True

    def shutdown(self) -> None:
        self._stop.set()
        self._runner.shutdown()
