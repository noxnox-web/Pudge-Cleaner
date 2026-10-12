"""The «Ночная очистка» window: what it shows and what its buttons do.

Runs on Qt's offscreen platform with the schedule faked, so nothing is
registered with Windows.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QTime
from PySide6.QtWidgets import QMessageBox

from pudge_gaming_manager.app.gui import nightly_dialog
from pudge_gaming_manager.app.gui.nightly_dialog import NightlyDialog
from pudge_gaming_manager.core.nightly import NightlyReport, NightlyStatus
from pudge_gaming_manager.utilities.exceptions import PrivilegeError


class FakeSchedule:
    """Stands in for ``core.nightly``: remembers calls, answers from state."""

    DEFAULT_HOUR, DEFAULT_MINUTE = 5, 0

    def __init__(self, status: NightlyStatus, last: NightlyReport | None = None) -> None:
        self._status = status
        self._last = last
        self.enabled_with: list[tuple] = []
        self.disabled = 0
        self.fail_with: Exception | None = None

    def status(self, command: str) -> NightlyStatus:
        return self._status

    def last_report(self):
        return self._last

    def enable(self, command, arguments, hour, minute) -> None:
        if self.fail_with:
            raise self.fail_with
        self.enabled_with.append((command, arguments, hour, minute))
        self._status = NightlyStatus(True, f"{hour:02d}:{minute:02d}")

    def disable(self) -> None:
        self.disabled += 1
        self._status = NightlyStatus(False)


@pytest.fixture
def make(app, monkeypatch: pytest.MonkeyPatch):
    def build(status: NightlyStatus, last: NightlyReport | None = None):
        fake = FakeSchedule(status, last)
        monkeypatch.setattr(nightly_dialog, "nightly", fake)
        monkeypatch.setattr(nightly_dialog, "launch_command", lambda: (r"C:\a.exe", "--nightly"))
        return NightlyDialog(), fake

    return build


def test_off_state_offers_to_enable_at_five(make) -> None:
    dialog, _ = make(NightlyStatus(False))
    assert dialog._state.text() == "Выключена"
    assert dialog._enable.text() == "Включить"
    assert not dialog._disable.isEnabled()
    assert dialog._time.time() == QTime(5, 0)
    assert "Ещё ни разу" in dialog._last.text()


def test_on_state_shows_the_time_and_the_last_run(make) -> None:
    last = NightlyReport("2026-10-04T05:02:11", "done", applied=3)
    dialog, _ = make(NightlyStatus(True, "03:30"), last)
    assert "каждый день в 03:30" in dialog._state.text()
    assert dialog._time.time() == QTime(3, 30)
    assert dialog._enable.text() == "Сохранить"
    assert dialog._disable.isEnabled()
    assert "применено изменений: 3" in dialog._last.text()


def test_a_task_pointing_at_another_file_is_called_out(make) -> None:
    dialog, _ = make(NightlyStatus(True, "05:00", points_elsewhere=True))
    assert "другой файл" in dialog._state.text()
    assert "не сработает" in dialog._state.text()


def test_enable_passes_the_picked_time_and_the_nightly_switch(make) -> None:
    dialog, fake = make(NightlyStatus(False))
    dialog._time.setTime(QTime(4, 15))
    dialog._enable.click()
    assert fake.enabled_with == [(r"C:\a.exe", "--nightly", 4, 15)]
    assert "04:15" in dialog._state.text()  # the window re-reads and shows it


def test_disable_turns_it_off(make) -> None:
    dialog, fake = make(NightlyStatus(True, "05:00"))
    dialog._disable.click()
    assert fake.disabled == 1
    assert dialog._state.text() == "Выключена"
    assert not dialog._disable.isEnabled()


def test_a_refusal_is_shown_and_the_state_stays_off(
    make, monkeypatch: pytest.MonkeyPatch
) -> None:
    shown: list[str] = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text: shown.append(text))
    dialog, fake = make(NightlyStatus(False))
    fake.fail_with = PrivilegeError("создать задачу")
    dialog._enable.click()
    assert shown and "администратора" in shown[0]
    assert dialog._state.text() == "Выключена"
