"""Tests for the OPTIMIZE controller and the window's close guard.

The pipeline is faked, so no test plans, applies or scans anything real.
These cover the two invariants that keep a club PC safe at close time:

* ``applying`` is true only while changes are being written, and always
  clears afterwards — a failed run must not leave the window unclosable.
* The window refuses to close mid-apply, and closes normally otherwise.
"""

from __future__ import annotations

import os
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEventLoop, QTimer

from pudge_gaming_manager.app.controllers.optimize_controller import OptimizeController



def _wait(until) -> None:
    loop = QEventLoop()
    timer = QTimer()
    timer.timeout.connect(lambda: until() and loop.quit())
    timer.start(10)
    QTimer.singleShot(5000, loop.quit)
    loop.exec()
    timer.stop()


class _FakePipeline:
    """Stands in for OptimizationPipeline. Blocks apply until released."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.applied = False

    def preview(self, _snapshot, _issues, level=None):
        self.level = level
        return "preview"

    def apply(self, _preview, *, rescan=None, dry_run=False, progress=None):
        self.release.wait(5)
        self.applied = True
        return "outcome"


def _controller(pipeline: _FakePipeline) -> OptimizeController:
    return OptimizeController(pipeline=pipeline)  # type: ignore[arg-type]


def test_idle_controller_is_not_applying(app) -> None:
    assert _controller(_FakePipeline()).applying is False


def test_preview_is_not_counted_as_applying(app) -> None:
    controller = _controller(_FakePipeline())
    seen: list = []
    controller.preview_ready.connect(seen.append)
    controller.start_preview(snapshot=object(), issues=())  # type: ignore[arg-type]
    # A preview reads only; closing during one is safe, so applying stays off.
    assert controller.applying is False
    _wait(lambda: seen)
    assert controller.applying is False


def test_applying_is_true_during_apply_and_clears_after(app) -> None:
    pipeline = _FakePipeline()
    controller = _controller(pipeline)
    done: list = []
    controller.apply_finished.connect(done.append)

    controller.start_apply("preview")  # type: ignore[arg-type]
    assert controller.applying is True  # writing to the system now

    pipeline.release.set()
    _wait(lambda: done)
    assert controller.applying is False
    assert done == ["outcome"]


def test_applying_clears_when_apply_fails(app) -> None:
    class _Boom(_FakePipeline):
        def apply(self, _preview, *, rescan=None, dry_run=False, progress=None):
            raise RuntimeError("powercfg vanished")

    controller = _controller(_Boom())
    errors: list = []
    controller.failed.connect(errors.append)
    controller.start_apply("preview")  # type: ignore[arg-type]
    _wait(lambda: errors)
    assert controller.applying is False
    assert errors == ["powercfg vanished"]


def test_close_event_ignores_the_event_while_applying(app, monkeypatch) -> None:
    """The window's close guard reads ``applying`` and vetoes the close.

    Exercised without building a real Dashboard (which would launch a live
    scan): the method is called against a minimal stand-in carrying the same
    controller, so the guard's decision is what is under test.
    """
    from pudge_gaming_manager.app.gui.dashboard import Dashboard

    class _Event:
        def __init__(self) -> None:
            self.ignored = False

        def ignore(self) -> None:
            self.ignored = True

    pipeline = _FakePipeline()
    controller = _controller(pipeline)
    controller.start_apply("preview")  # type: ignore[arg-type]
    assert controller.applying

    # A stand-in with just what closeEvent touches on the applying path.
    stub = type("Stub", (), {"_optimizer": controller})()
    monkeypatch.setattr(
        "PySide6.QtWidgets.QMessageBox.warning", lambda *a, **k: None
    )
    event = _Event()
    Dashboard.closeEvent(stub, event)  # type: ignore[arg-type]
    assert event.ignored is True

    pipeline.release.set()
    _wait(lambda: not controller.applying)
    controller.shutdown()


def test_apply_progress_reaches_the_window_from_the_worker(app) -> None:
    class _Reporting(_FakePipeline):
        def apply(self, _preview, *, rescan=None, dry_run=False, progress=None):
            progress("half-way")
            return "outcome"

    controller = _controller(_Reporting())
    seen: list = []
    done: list = []
    controller.apply_progress.connect(seen.append)
    controller.apply_finished.connect(done.append)

    controller.start_apply("preview")  # type: ignore[arg-type]
    _wait(lambda: done)
    assert seen == ["half-way"]


def test_the_work_bar_is_busy_until_a_step_can_tell_how_far_it_is(app) -> None:
    """Driven through the real method on a stand-in, as the close guard is."""
    from PySide6.QtWidgets import QProgressBar

    from pudge_gaming_manager.app.gui.dashboard import Dashboard

    stub = type("Stub", (), {"_work": QProgressBar()})()
    stub._work.hide()

    Dashboard._show_work(stub, None)  # type: ignore[arg-type]
    assert (stub._work.minimum(), stub._work.maximum()) == (0, 0)  # busy
    assert not stub._work.isHidden()

    Dashboard._show_work(stub, 0.25)  # type: ignore[arg-type]
    assert stub._work.maximum() == 1000 and stub._work.value() == 250
