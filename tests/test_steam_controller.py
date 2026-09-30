"""Tests for the Steam reset controller and the window's wipe guard.

The wiper is faked, so no test plans, deletes or stops anything real. These
cover: a reset reads its keep-list from the chosen profile; ``wiping`` is true
only while games are being deleted and always clears; and the window refuses
to close mid-wipe.
"""

from __future__ import annotations

import os
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEventLoop, QTimer

from pudge_gaming_manager.app.controllers.steam_controller import SteamController



def _wait(until) -> None:
    loop = QEventLoop()
    timer = QTimer()
    timer.timeout.connect(lambda: until() and loop.quit())
    timer.start(10)
    QTimer.singleShot(5000, loop.quit)
    loop.exec()
    timer.stop()


class _FakeWiper:
    def __init__(self) -> None:
        self.release = threading.Event()
        self.scanned_keep: set[int] | None = None

    def scan(self, keep_app_ids: set[int]):
        self.scanned_keep = keep_app_ids
        return f"plan(keep={sorted(keep_app_ids)})"

    def wipe(self, _plan, *, dry_run: bool = False, progress=None):
        self.release.wait(5)
        return "result"


def _controller(wiper: _FakeWiper) -> SteamController:
    controller = SteamController()
    controller._wiper = wiper  # inject the fake
    return controller


def test_plan_uses_the_built_in_keep_list(app) -> None:
    from pudge_gaming_manager.games.steam.default_keep import DEFAULT_KEEP_IDS

    wiper = _FakeWiper()
    controller = _controller(wiper)
    plans: list = []
    controller.planned.connect(plans.append)

    controller.start_plan()  # no profile, no file dialog
    _wait(lambda: plans)

    assert wiper.scanned_keep == set(DEFAULT_KEEP_IDS)
    assert 730 in wiper.scanned_keep  # CS2 is kept by default
    assert 228980 in wiper.scanned_keep  # Steamworks redistributables


def test_wiping_is_true_during_wipe_and_clears_after(app) -> None:
    wiper = _FakeWiper()
    controller = _controller(wiper)
    done: list = []
    controller.wiped.connect(done.append)

    controller.start_wipe("plan")
    assert controller.wiping is True

    wiper.release.set()
    _wait(lambda: done)
    assert controller.wiping is False
    assert done == ["result"]


def test_wiping_clears_when_the_wipe_fails(app) -> None:
    class _Boom(_FakeWiper):
        def wipe(self, _plan, *, dry_run: bool = False, progress=None):
            raise RuntimeError("disk detached")

    controller = _controller(_Boom())
    errors: list = []
    controller.failed.connect(errors.append)
    controller.start_wipe("plan")
    _wait(lambda: errors)
    assert controller.wiping is False
    assert errors == ["disk detached"]


def test_close_event_is_refused_while_wiping(app, monkeypatch) -> None:
    from pudge_gaming_manager.app.gui.dashboard import Dashboard

    class _Event:
        def __init__(self) -> None:
            self.ignored = False

        def ignore(self) -> None:
            self.ignored = True

    wiper = _FakeWiper()
    controller = _controller(wiper)
    controller.start_wipe("plan")
    assert controller.wiping

    class _StubOptimizer:
        applying = False

    stub = type("Stub", (), {"_optimizer": _StubOptimizer(), "_steam": controller})()
    monkeypatch.setattr(
        "PySide6.QtWidgets.QMessageBox.warning", lambda *a, **k: None
    )
    event = _Event()
    Dashboard.closeEvent(stub, event)  # type: ignore[arg-type]
    assert event.ignored is True

    wiper.release.set()
    _wait(lambda: not controller.wiping)
    controller.shutdown()
