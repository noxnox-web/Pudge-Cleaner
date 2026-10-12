"""The scheduled-task definition and its registration.

The XML is the whole contract with Task Scheduler, so it is checked in
detail: when it starts, as whom, and under which conditions it may not.
"""

from __future__ import annotations

import datetime
from types import SimpleNamespace

import pytest
import pywintypes

from pudge_gaming_manager.utilities.exceptions import PrivilegeError
from pudge_gaming_manager.windows.schedule import task

COMMAND = r"C:\Program Files\Pudge & Co\PudgeCleaner.exe"


def _xml(now=datetime.datetime(2026, 10, 3, 10, 0), hour=5, minute=0, **kw):
    return task.build_xml(
        kw.get("command", COMMAND), kw.get("arguments", "--nightly"),
        hour, minute, user=r"CLUB\admin", now=now,
    )


def test_definition_round_trips_through_the_parser() -> None:
    info = task.parse_xml(_xml())
    assert info.installed and info.enabled
    assert info.command == COMMAND  # the ampersand survived escaping
    assert info.arguments == "--nightly"
    assert info.time_text == "05:00"


def test_description_with_guillemets_and_an_ellipsis_survives_parsing() -> None:
    """Regression: through schtasks.exe these came back as < and >, which
    broke the XML, and the task then read as "not installed"."""
    xml = _xml()
    assert "«" in xml and "»" in xml
    assert task.parse_xml(xml).installed


def test_first_start_is_the_next_occurrence_not_today_when_it_has_passed() -> None:
    """A start in the past makes Windows run the "missed" slot at once."""
    after = task.next_start(datetime.datetime(2026, 10, 3, 10, 0), 5, 0)
    assert after == datetime.datetime(2026, 10, 4, 5, 0)
    before = task.next_start(datetime.datetime(2026, 10, 3, 3, 0), 5, 0)
    assert before == datetime.datetime(2026, 10, 3, 5, 0)
    assert "2026-10-04T05:00:00" in _xml()


@pytest.mark.parametrize(
    "fragment",
    [
        "<LogonType>InteractiveToken</LogonType>",   # as the user, not SYSTEM
        "<RunLevel>HighestAvailable</RunLevel>",     # elevated, no UAC prompt
        "<StartWhenAvailable>true</StartWhenAvailable>",
        "<DisallowStartIfOnBatteries>true</DisallowStartIfOnBatteries>",
        "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>",
        "<Priority>10</Priority>",                   # idle CPU class
        r"<UserId>CLUB\admin</UserId>",
    ],
)
def test_definition_carries_the_load_and_safety_settings(fragment: str) -> None:
    assert fragment in _xml()


@pytest.mark.parametrize(("hour", "minute"), [(24, 0), (5, 60), (-1, 0)])
def test_rejects_a_time_that_is_not_a_time_of_day(hour: int, minute: int) -> None:
    with pytest.raises(ValueError):
        _xml(hour=hour, minute=minute)


def test_unparseable_definition_reads_as_not_installed() -> None:
    assert not task.parse_xml("this is not xml").installed


# -- registration through the scheduler's COM interface ---------------------

NOT_FOUND_AS_IDISPATCH_REPORTS_IT = (
    -2147352567, "Ошибка.", (0, None, None, None, 0, -2147024894), None
)


class FakeRoot:
    """The scheduler's root folder, holding tasks in a dict."""

    def __init__(self, tasks: dict[str, str] | None = None, *, fail_with=None) -> None:
        self.tasks = dict(tasks or {})
        self.fail_with = fail_with
        self.registered: list[tuple] = []
        self.deleted_folders: list[str] = []

    def RegisterTask(self, name, xml, flags, user, password, logon):  # noqa: N802
        if self.fail_with:
            raise self.fail_with
        self.registered.append((name, flags, user, password, logon))
        self.tasks[name] = xml

    def GetTask(self, name):  # noqa: N802
        if name not in self.tasks:
            raise pywintypes.com_error(*NOT_FOUND_AS_IDISPATCH_REPORTS_IT)
        return SimpleNamespace(Xml=self.tasks[name])

    def DeleteTask(self, name, flags):  # noqa: N802
        if name not in self.tasks:
            raise pywintypes.com_error(*NOT_FOUND_AS_IDISPATCH_REPORTS_IT)
        del self.tasks[name]

    def DeleteFolder(self, name, flags):  # noqa: N802
        self.deleted_folders.append(name)


@pytest.fixture
def scheduler(monkeypatch: pytest.MonkeyPatch) -> FakeRoot:
    root = FakeRoot()
    monkeypatch.setattr(task, "_connect_root", lambda: root)
    monkeypatch.setattr(task, "require_admin", lambda operation: None)
    return root


def test_install_registers_the_task_for_the_interactive_user(scheduler: FakeRoot) -> None:
    task.install(COMMAND, "--nightly", 5, 0, user=r"CLUB\admin")
    [(name, flags, user, password, logon)] = scheduler.registered
    assert name == task.TASK_NAME
    assert flags == 6  # create or update: re-saving replaces in place
    assert logon == 3  # interactive token
    # Regression: passing the user here made Windows validate the account
    # and refuse a local one with a blank password (0x8007052F). The XML
    # already names the user.
    assert (user, password) == ("", "")
    assert COMMAND.replace("&", "&amp;") in scheduler.tasks[task.TASK_NAME]


def test_install_needs_an_administrator(
    scheduler: FakeRoot, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(operation: str) -> None:
        raise PrivilegeError(operation)

    monkeypatch.setattr(task, "require_admin", refuse)
    with pytest.raises(PrivilegeError):
        task.install(COMMAND, "--nightly", 5, 0)
    assert scheduler.registered == []  # it never touched the scheduler


def test_access_denied_says_to_run_as_administrator(
    scheduler: FakeRoot,
) -> None:
    scheduler.fail_with = pywintypes.com_error(
        -2147352567, "Ошибка.", (0, None, None, None, 0, -2147024891), None
    )
    with pytest.raises(task.SchedulerError) as caught:
        task.install(COMMAND, "--nightly", 5, 0)
    assert "администратора" in caught.value.user_message()


def test_any_other_refusal_is_a_readable_error(scheduler: FakeRoot) -> None:
    scheduler.fail_with = pywintypes.com_error(-2147216609, "boom", None, None)
    with pytest.raises(task.SchedulerError):
        task.install(COMMAND, "--nightly", 5, 0)


def test_remove_deletes_the_task_and_its_empty_folder(scheduler: FakeRoot) -> None:
    scheduler.tasks[task.TASK_NAME] = _xml()
    task.remove()
    assert task.TASK_NAME not in scheduler.tasks
    assert scheduler.deleted_folders == ["PudgeCleaner"]


def test_removing_a_task_that_is_not_there_is_fine(scheduler: FakeRoot) -> None:
    """Regression: COM reports "not found" inside the exception info, not
    in the outer HRESULT, and reading only the outer one raised here."""
    task.remove()
    task.remove()


def test_query_reads_the_registered_definition(scheduler: FakeRoot) -> None:
    scheduler.tasks[task.TASK_NAME] = _xml(hour=3, minute=30)
    info = task.query()
    assert info.installed and info.time_text == "03:30" and info.command == COMMAND


def test_query_of_a_missing_task_is_not_installed(scheduler: FakeRoot) -> None:
    assert not task.query().installed


def test_query_failure_is_an_error_not_a_silent_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the scheduler cannot be read, "off" would be a guess."""
    def broken() -> None:
        raise task.SchedulerError(what="нет связи")

    monkeypatch.setattr(task, "_connect_root", broken)
    with pytest.raises(task.SchedulerError):
        task.query()


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (pywintypes.com_error(*NOT_FOUND_AS_IDISPATCH_REPORTS_IT), 0x80070002),
        (pywintypes.com_error(-2147024894, "x", None, None), 0x80070002),
        (pywintypes.com_error(-2147352567, "x", (0, None, None, None, 0, 0), None), 0x80020009),
        (ValueError("not a com error"), 0),
    ],
)
def test_the_real_error_code_is_found_inside_the_exception_info(error, code: int) -> None:
    assert task._com_code(error) == code
