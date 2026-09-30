"""The usage dialog: pick a folder, then delete it or move it to another drive.

The dialog only asks. These tests drive it through its buttons and signals
with the real policy (pointed at a fake machine under ``tmp_path``) and with
the message boxes and the folder picker replaced, so nothing waits for a
click and nothing real is touched.
"""

from __future__ import annotations

import os
import pathlib
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from pudge_gaming_manager.app.gui import usage_dialog
from pudge_gaming_manager.app.gui.usage_dialog import UsageDialog
from pudge_gaming_manager.core.cleanup import (
    FolderAction,
    FolderPlan,
    FolderResult,
    FolderUsage,
    UsageReport,
)
from pudge_gaming_manager.utilities.progress import Progress


@pytest.fixture()
def machine(tmp_path: pathlib.Path, monkeypatch) -> SimpleNamespace:
    base = tmp_path / "machine"
    places = {
        "SystemRoot": base / "Windows", "ProgramFiles": base / "Program Files",
        "ProgramFiles(x86)": base / "Program Files (x86)", "ProgramW6432": base / "Program Files",
        "ProgramData": base / "ProgramData", "USERPROFILE": base / "Users" / "player",
        "APPDATA": base / "Users" / "player" / "AppData" / "Roaming",
        "LOCALAPPDATA": base / "Users" / "player" / "AppData" / "Local",
    }
    for name, path in places.items():
        path.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv(name, str(path))
    games = places["USERPROFILE"] / "Games" / "Big"
    games.mkdir(parents=True)
    system = places["SystemRoot"] / "System32" / "drivers"
    system.mkdir(parents=True)
    return SimpleNamespace(allowed=games, blocked=system, tmp=tmp_path)


class _FakeBox:
    """Stands in for QMessageBox: records what was asked, answers on cue."""

    Icon = usage_dialog.QMessageBox.Icon
    ButtonRole = usage_dialog.QMessageBox.ButtonRole
    answer = "reject"  # which button the "operator" presses
    shown: list = []
    told: list = []

    def __init__(self, icon, title, text, parent=None) -> None:
        self.title, self.text = title, text
        self.buttons: dict[str, object] = {}
        self.default = self.escape = None
        type(self).shown.append(self)

    def addButton(self, label, _role):
        token = SimpleNamespace(label=label)
        self.buttons[label] = token
        return token

    def setDefaultButton(self, button) -> None:
        self.default = button

    def setEscapeButton(self, button) -> None:
        self.escape = button

    def exec(self) -> int:
        return 0

    def clickedButton(self):
        return self.buttons["Отмена" if self.answer == "reject" else next(
            label for label in self.buttons if label != "Отмена"
        )]

    @classmethod
    def information(cls, _parent, title, text) -> None:
        cls.told.append(("information", title, text))

    @classmethod
    def warning(cls, _parent, title, text) -> None:
        cls.told.append(("warning", title, text))


@pytest.fixture(autouse=True)
def _boxes(monkeypatch):
    _FakeBox.shown, _FakeBox.told, _FakeBox.answer = [], [], "reject"
    monkeypatch.setattr(usage_dialog, "QMessageBox", _FakeBox)
    return _FakeBox


def _dialog(app, machine) -> UsageDialog:
    report = UsageReport(
        folders=[
            FolderUsage(machine.allowed, 5000, 7),
            FolderUsage(machine.blocked, 9000, 3),
        ],
        roots_scanned=[machine.tmp],
    )
    return UsageDialog(report)


def _select(dialog: UsageDialog, row: int) -> None:
    dialog._rows.setCurrentRow(row)
    dialog._rows.item(row).setSelected(True)


def _plan(machine, action=FolderAction.DELETE, destination=None, unreadable=0) -> FolderPlan:
    return FolderPlan(action, machine.allowed, 5000, 7, destination, unreadable)


# -- what the buttons allow ------------------------------------------------------------


def test_nothing_selected_means_no_action_is_offered(app, machine) -> None:
    dialog = _dialog(app, machine)
    assert not dialog._delete.isEnabled() and not dialog._move.isEnabled()
    assert "Выберите папку" in dialog._reason.text()


def test_an_ordinary_folder_can_be_deleted_or_moved(app, machine) -> None:
    dialog = _dialog(app, machine)
    _select(dialog, 0)
    assert dialog._delete.isEnabled() and dialog._move.isEnabled()
    assert str(machine.allowed) in dialog._reason.text()


def test_a_protected_folder_offers_no_action_and_says_why(app, machine) -> None:
    dialog = _dialog(app, machine)
    _select(dialog, 1)
    assert not dialog._delete.isEnabled() and not dialog._move.isEnabled()
    assert "системная папка Windows" in dialog._reason.text()


# -- asking ------------------------------------------------------------------------------


def test_delete_asks_for_a_plan_and_waits(app, machine) -> None:
    dialog = _dialog(app, machine)
    asked: list = []
    dialog.plan_requested.connect(lambda *args: asked.append(args))
    _select(dialog, 0)

    dialog._delete.click()

    assert asked == [(FolderAction.DELETE, machine.allowed, None)]
    assert dialog._busy and not dialog._close.isEnabled()
    assert dialog._work.isVisibleTo(dialog)


def test_move_asks_where_then_for_a_plan(app, machine, monkeypatch) -> None:
    dialog = _dialog(app, machine)
    asked: list = []
    dialog.plan_requested.connect(lambda *args: asked.append(args))
    monkeypatch.setattr(
        usage_dialog.QFileDialog, "getExistingDirectory", lambda *a, **k: str(machine.tmp / "D")
    )
    _select(dialog, 0)

    dialog._move.click()

    assert asked == [(FolderAction.MOVE, machine.allowed, machine.tmp / "D")]


def test_cancelling_the_folder_picker_does_nothing(app, machine, monkeypatch) -> None:
    dialog = _dialog(app, machine)
    asked: list = []
    dialog.plan_requested.connect(lambda *args: asked.append(args))
    monkeypatch.setattr(usage_dialog.QFileDialog, "getExistingDirectory", lambda *a, **k: "")
    _select(dialog, 0)

    dialog._move.click()

    assert asked == [] and not dialog._busy


# -- confirming --------------------------------------------------------------------------


def test_the_delete_question_names_the_folder_and_says_it_is_final(app, machine, _boxes) -> None:
    dialog = _dialog(app, machine)

    plan = _plan(machine)
    dialog.confirm_plan(plan)

    box = _boxes.shown[0]
    assert "Big" in box.text and "безвозвратно" in box.text and "Корзина не используется" in box.text
    assert plan.size_display in box.text and "файлов: 7" in box.text
    # Cancel is the default: a stray Enter must not delete 50 GB.
    assert box.default is box.buttons["Отмена"] and box.escape is box.buttons["Отмена"]


def test_the_move_question_names_both_ends_and_the_link(app, machine, _boxes) -> None:
    dialog = _dialog(app, machine)
    destination = machine.tmp / "D" / "Big"

    dialog.confirm_plan(_plan(machine, FolderAction.MOVE, destination))

    text = _boxes.shown[0].text
    assert str(machine.allowed) in text and str(destination) in text
    assert "ссылка" in text and "закрывать программу нельзя" in text


def test_unreadable_entries_are_mentioned_before_a_delete(app, machine, _boxes) -> None:
    dialog = _dialog(app, machine)
    dialog.confirm_plan(_plan(machine, unreadable=4))
    assert "4" in _boxes.shown[0].text and "не удалятся" in _boxes.shown[0].text


def test_accepting_the_question_runs_the_plan(app, machine, _boxes) -> None:
    dialog = _dialog(app, machine)
    run: list = []
    dialog.run_requested.connect(run.append)
    plan = _plan(machine)
    _boxes.answer = "accept"

    dialog.confirm_plan(plan)

    assert run == [plan] and dialog._busy


def test_declining_the_question_runs_nothing_and_frees_the_dialog(app, machine) -> None:
    dialog = _dialog(app, machine)
    run: list = []
    dialog.run_requested.connect(run.append)
    _select(dialog, 0)
    dialog._delete.click()  # busy, waiting for the plan

    dialog.confirm_plan(_plan(machine))  # answer: Отмена

    assert run == [] and not dialog._busy
    assert dialog._delete.isEnabled()


# -- running and finishing ------------------------------------------------------------------


def test_progress_fills_the_bar_or_shows_busy(app, machine) -> None:
    dialog = _dialog(app, machine)
    dialog._set_busy(True, "…")

    dialog.show_progress(Progress("Перенос — копирование: 1 ГБ из 4 ГБ", 0.25))
    assert dialog._work.maximum() == 1000 and dialog._work.value() == 250
    assert "копирование" in dialog._work_text.text()

    dialog.show_progress(Progress("Перенос — проверка копии"))
    assert (dialog._work.minimum(), dialog._work.maximum()) == (0, 0)


def test_a_deleted_folder_leaves_the_list_and_the_total_follows(app, machine, _boxes) -> None:
    dialog = _dialog(app, machine)
    assert "2 папках" in dialog._heading.text()
    dialog._set_busy(True)

    dialog.show_result(FolderResult(FolderAction.DELETE, machine.allowed, size_bytes=5000, file_count=7))

    assert [f.path for f in dialog._folders()] == [machine.blocked]
    assert "1 папках" in dialog._heading.text()
    assert not dialog._busy
    kind, _title, text = _boxes.told[0]
    assert kind == "information" and "Удалено" in text


def test_a_moved_folder_leaves_the_list_and_the_message_says_where_it_went(app, machine, _boxes) -> None:
    dialog = _dialog(app, machine)
    destination = machine.tmp / "D" / "Big"

    dialog.show_result(FolderResult(
        FolderAction.MOVE, machine.allowed, destination, 5000, 7, linked=True,
    ))

    assert [f.path for f in dialog._folders()] == [machine.blocked]
    text = _boxes.told[0][2]
    assert str(destination) in text and "ссылка" in text


def test_a_partly_deleted_folder_stays_listed_and_is_reported_as_a_warning(app, machine, _boxes) -> None:
    dialog = _dialog(app, machine)

    dialog.show_result(FolderResult(
        FolderAction.DELETE, machine.allowed, size_bytes=1000, file_count=2,
        failed=["f0.bin: занят"], leftover=machine.allowed,
    ))

    assert machine.allowed in [f.path for f in dialog._folders()]
    kind, _title, text = _boxes.told[0]
    assert kind == "warning" and "f0.bin" in text and "заняты" in text


def test_the_last_folder_going_says_so(app, machine) -> None:
    report = UsageReport(folders=[FolderUsage(machine.allowed, 5000, 7)])
    dialog = UsageDialog(report)

    dialog.show_result(FolderResult(FolderAction.DELETE, machine.allowed, size_bytes=5000, file_count=7))

    assert dialog._heading.text() == "Ничего не измерено"


def test_a_refusal_is_shown_and_frees_the_dialog(app, machine, _boxes) -> None:
    dialog = _dialog(app, machine)
    dialog._set_busy(True)

    dialog.fail("Перенос невозможен.\n\nПричина:  это тот же диск")

    assert not dialog._busy
    assert _boxes.told[0][0] == "warning" and "тот же диск" in _boxes.told[0][2]


# -- closing ------------------------------------------------------------------------------


def test_the_dialog_cannot_be_closed_while_it_works(app, machine) -> None:
    dialog = _dialog(app, machine)
    dialog.show()
    dialog._set_busy(True)

    dialog.reject()
    assert dialog.isVisible()  # ignored: a move is half done

    dialog._set_busy(False)
    dialog.reject()
    assert not dialog.isVisible()
