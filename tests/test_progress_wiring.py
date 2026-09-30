"""Progress from the worker thread to the window, for cleanup, Steam and folders.

Three layers are covered, each with the layer below faked: the controllers
carry a report from the worker thread to a method on the UI thread; the window
puts it on the bar under the status line (or, for a folder delete or move, in
the dialog that asked); and the window refuses to close while a folder is
being moved.
"""

from __future__ import annotations

import os
import pathlib
import threading
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEventLoop, QObject, QTimer, Slot
from PySide6.QtWidgets import QLabel, QProgressBar

from pudge_gaming_manager.app.controllers import tools_controller
from pudge_gaming_manager.app.controllers.cleanup_controller import CleanupController
from pudge_gaming_manager.app.controllers.steam_controller import SteamController
from pudge_gaming_manager.app.controllers.tools_controller import ToolsController
from pudge_gaming_manager.app.gui.dashboard import Dashboard
from pudge_gaming_manager.core.cleanup import FolderAction, FolderPlan, FolderRefused
from pudge_gaming_manager.utilities.progress import Progress


def _wait(until) -> None:
    loop = QEventLoop()
    timer = QTimer()
    timer.timeout.connect(lambda: until() and loop.quit())
    timer.start(10)
    QTimer.singleShot(5000, loop.quit)
    loop.exec()
    timer.stop()


class _Receiver(QObject):
    """A UI-thread object with a slot: what the dashboard is to a controller."""

    def __init__(self) -> None:
        super().__init__()
        self.got: list = []
        self.threads: list = []

    @Slot(object)
    def take(self, event: object) -> None:
        self.got.append(event)
        self.threads.append(threading.current_thread())


# -- controllers: worker thread -> UI thread --------------------------------------------


def test_cleanup_progress_arrives_on_the_ui_thread(app) -> None:
    class Cleaner:
        def run(self, _plan, _selected, *, progress=None):
            progress(Progress("Очистка диска — Temp", 0.5))
            return "result"

    controller = CleanupController()
    controller._cleaner = Cleaner()
    receiver, done = _Receiver(), _Receiver()
    controller.progress.connect(receiver.take)
    controller.cleaned.connect(done.take)

    controller.start_clean("plan", {"x"})  # type: ignore[arg-type]
    _wait(lambda: done.got)

    assert [e.text for e in receiver.got] == ["Очистка диска — Temp"]
    assert receiver.threads == [threading.main_thread()]


def test_steam_progress_arrives_on_the_ui_thread(app) -> None:
    class Wiper:
        def wipe(self, _plan, *, dry_run=False, progress=None):
            progress(Progress("Очистка Steam — игра 1 из 2: Dota 2", 0.3))
            return "result"

    controller = SteamController()
    controller._wiper = Wiper()
    receiver, done = _Receiver(), _Receiver()
    controller.progress.connect(receiver.take)
    controller.wiped.connect(done.take)

    controller.start_wipe("plan")  # type: ignore[arg-type]
    _wait(lambda: done.got)

    assert receiver.got[0].fraction == 0.3
    assert receiver.threads == [threading.main_thread()]


def test_the_survey_reports_through_the_tools_controller(app, monkeypatch) -> None:
    def survey(*, progress=None):
        progress(Progress("Замер занятого места — C:\\ (1 из 5)", 0.1))
        return "report"

    monkeypatch.setattr(tools_controller, "survey", survey)
    controller = ToolsController()
    receiver, done = _Receiver(), _Receiver()
    controller.progress.connect(receiver.take)
    controller.usage_ready.connect(done.take)

    controller.start_usage_survey()
    _wait(lambda: done.got)

    assert receiver.got[0].text.startswith("Замер занятого места")


# -- the tools controller: plan, then run ------------------------------------------------------


def _plan(action=FolderAction.DELETE) -> FolderPlan:
    return FolderPlan(action, pathlib.Path("C:/x/Big"), 100, 2)


def test_a_delete_plan_is_asked_for_by_path(app, monkeypatch) -> None:
    asked: list = []
    monkeypatch.setattr(tools_controller, "plan_delete", lambda p: asked.append(p) or _plan())
    controller = ToolsController()
    planned = _Receiver()
    controller.folder_planned.connect(planned.take)

    controller.start_folder_plan(FolderAction.DELETE, pathlib.Path("C:/x/Big"))
    _wait(lambda: planned.got)

    assert asked == [pathlib.Path("C:/x/Big")] and planned.got[0].action is FolderAction.DELETE


def test_a_move_plan_is_asked_for_with_its_destination(app, monkeypatch) -> None:
    asked: list = []
    monkeypatch.setattr(
        tools_controller, "plan_move",
        lambda src, dst: asked.append((src, dst)) or _plan(FolderAction.MOVE),
    )
    controller = ToolsController()
    planned = _Receiver()
    controller.folder_planned.connect(planned.take)

    controller.start_folder_plan(
        FolderAction.MOVE, pathlib.Path("C:/x/Big"), pathlib.Path("D:/")
    )
    _wait(lambda: planned.got)

    assert asked == [(pathlib.Path("C:/x/Big"), pathlib.Path("D:/"))]


def test_a_refused_plan_reaches_the_window_as_readable_text(app, monkeypatch) -> None:
    def refuses(_path):
        raise FolderRefused("Удаление невозможно", "«Windows»: системная папка Windows.")

    monkeypatch.setattr(tools_controller, "plan_delete", refuses)
    controller = ToolsController()
    failed = _Receiver()
    controller.failed.connect(failed.take)

    controller.start_folder_plan(FolderAction.DELETE, pathlib.Path("C:/Windows"))
    _wait(lambda: failed.got)

    assert "системная папка Windows" in failed.got[0]


def test_modifying_is_true_only_while_a_folder_is_being_changed(app, monkeypatch) -> None:
    release = threading.Event()

    def run(plan, progress=None):
        progress(Progress("Перенос — копирование", 0.5))
        release.wait(5)
        return "result"

    monkeypatch.setattr(tools_controller, "run_folder", run)
    controller = ToolsController()
    progress, done = _Receiver(), _Receiver()
    controller.progress.connect(progress.take)
    controller.folder_done.connect(done.take)
    assert controller.modifying is False

    controller.start_folder_run(_plan(FolderAction.MOVE))
    assert controller.modifying is True  # closing the window now would strand a move

    release.set()
    _wait(lambda: done.got)
    assert controller.modifying is False
    assert progress.got[0].fraction == 0.5 and done.got == ["result"]


def test_modifying_clears_when_the_run_fails(app, monkeypatch) -> None:
    def dies(plan, progress=None):
        raise FolderRefused("Перенос не выполнен", "диск отключился")

    monkeypatch.setattr(tools_controller, "run_folder", dies)
    controller = ToolsController()
    failed = _Receiver()
    controller.failed.connect(failed.take)

    controller.start_folder_run(_plan(FolderAction.MOVE))
    _wait(lambda: failed.got)

    assert controller.modifying is False
    assert "диск отключился" in failed.got[0]


def test_a_request_while_the_worker_is_busy_is_answered_not_dropped(app, monkeypatch) -> None:
    """The dialog that asked cannot be closed while it waits: silence would strand it."""
    release = threading.Event()
    monkeypatch.setattr(tools_controller, "survey", lambda progress=None: release.wait(5) or "report")
    monkeypatch.setattr(tools_controller, "plan_delete", lambda p: _plan())
    controller = ToolsController()
    failed, planned, surveyed = _Receiver(), _Receiver(), _Receiver()
    controller.failed.connect(failed.take)
    controller.folder_planned.connect(planned.take)
    controller.usage_ready.connect(surveyed.take)

    controller.start_usage_survey()  # the worker is now busy
    controller.start_folder_plan(FolderAction.DELETE, pathlib.Path("C:/x/Big"))
    controller.start_folder_run(_plan())

    _wait(lambda: len(failed.got) == 2)
    assert all("Подождите" in message for message in failed.got)
    assert planned.got == [] and controller.modifying is False  # nothing ran, nothing is held

    release.set()
    _wait(lambda: surveyed.got)


# -- the window ----------------------------------------------------------------------------------


def _window() -> SimpleNamespace:
    """The real Dashboard methods on a stand-in with the widgets they touch."""
    stub = SimpleNamespace(
        _status=QLabel(), _work=QProgressBar(), _usage_dialog=None, calls=[],
    )
    stub._work.hide()
    stub._show_work = lambda fraction: Dashboard._show_work(stub, fraction)
    stub._on_progress = lambda event: Dashboard._on_progress(stub, event)
    stub._end_work = lambda: Dashboard._end_work(stub)
    return stub


def test_beginning_an_operation_shows_a_busy_bar_with_its_first_line(app) -> None:
    window = _window()

    Dashboard._begin_work(window, "Очистка диска…")

    assert window._status.text() == "Очистка диска…"
    assert not window._work.isHidden()
    assert (window._work.minimum(), window._work.maximum()) == (0, 0)


def test_a_report_moves_the_bar_and_a_missing_fraction_makes_it_busy_again(app) -> None:
    window = _window()

    Dashboard._on_progress(window, Progress("Очистка диска — Temp: удалено 1 ГБ", 0.4))
    assert window._work.maximum() == 1000 and window._work.value() == 400
    assert "Temp" in window._status.text()

    Dashboard._on_progress(window, Progress("Очистка корзины…"))
    assert (window._work.minimum(), window._work.maximum()) == (0, 0)


def test_ending_an_operation_clears_the_line_and_hides_the_bar(app) -> None:
    window = _window()
    Dashboard._begin_work(window, "Удаление игр…")

    Dashboard._end_work(window)

    assert window._status.text() == "" and window._work.isHidden()


def test_a_folder_run_reports_into_its_dialog_not_the_header(app) -> None:
    window = _window()
    shown: list = []
    window._usage_dialog = SimpleNamespace(show_progress=shown.append)

    Dashboard._on_tools_progress(window, Progress("Перенос — копирование", 0.2))

    assert len(shown) == 1 and window._status.text() == ""  # the header stays quiet


def test_the_survey_reports_into_the_header_when_no_dialog_is_open(app) -> None:
    window = _window()

    Dashboard._on_tools_progress(window, Progress("Замер занятого места — C:\\ (1 из 5)", 0.1))

    assert "Замер занятого места" in window._status.text() and not window._work.isHidden()


def test_a_refusal_is_answered_in_the_dialog_that_asked(app, monkeypatch) -> None:
    window = _window()
    window._set_profile_actions = lambda _enabled: None
    told: list = []
    window._usage_dialog = SimpleNamespace(fail=told.append)
    monkeypatch.setattr(
        "PySide6.QtWidgets.QMessageBox.warning",
        lambda *a, **k: pytest.fail("the window must not open its own box"),
    )
    from pudge_gaming_manager.app.gui.dashboard_actions import ProfileAndGamesActions

    ProfileAndGamesActions._on_tools_failed(window, "Перенос невозможен")

    assert told == ["Перенос невозможен"]


def test_the_window_will_not_close_during_a_move(app, monkeypatch) -> None:
    class _Event:
        ignored = False

        def ignore(self) -> None:
            self.ignored = True

    shown: list = []
    monkeypatch.setattr("PySide6.QtWidgets.QMessageBox.warning", lambda *a, **k: shown.append(a[2]))
    idle = SimpleNamespace(applying=False, wiping=False, cleaning=False, modifying=False)
    stub = SimpleNamespace(
        _optimizer=idle, _steam=idle, _cleanup=idle, _system=idle,
        _tools=SimpleNamespace(modifying=True),
    )
    event = _Event()

    Dashboard.closeEvent(stub, event)  # type: ignore[arg-type]

    assert event.ignored and "перенос" in shown[0]
