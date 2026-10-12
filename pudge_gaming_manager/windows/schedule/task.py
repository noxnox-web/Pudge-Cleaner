"""The nightly run as a Windows Task Scheduler task.

Why the scheduler and not a program that stays open: between runs nothing of
this program is in memory at all. Windows starts the executable at the chosen
time and it exits when done.

Everything the task needs is in one XML definition, because only XML can say
"wait for idle", "run when a missed start becomes possible" and "skip on
battery". It is handed to the scheduler through its COM interface, in-process,
the way ``utilities.wmi`` reads WMI. The first version drove ``schtasks.exe``
instead and was dropped: a console program writes in the OEM codepage and
replaces what it cannot represent, and for ``«`` and ``»`` it wrote ``<`` and
``>`` — into the middle of the XML. COM passes Unicode strings and has no
such step.
"""

from __future__ import annotations

import datetime
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any, Callable
from xml.sax.saxutils import escape

from ...utilities.exceptions import PgmError
from ...utilities.logging_setup import get_logger
from ...utilities.privileges import require_admin

_log = get_logger(__name__)

TASK_NAME = "PudgeCleaner\\Nightly"

_NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}

_DESCRIPTION = (
    "Pudge Cleaner: ежедневная безопасная оптимизация и очистка без окна. "
    "Выключается в программе: «Ночная очистка…»."
)


@dataclass(frozen=True, slots=True)
class TaskInfo:
    """What Task Scheduler currently has registered."""

    installed: bool
    command: str = ""
    arguments: str = ""
    hour: int = 0
    minute: int = 0
    enabled: bool = True

    @property
    def time_text(self) -> str:
        return f"{self.hour:02d}:{self.minute:02d}"


def current_user() -> str:
    """``DOMAIN\\name`` of whoever is installing the task.

    The task must run as that person, not as SYSTEM: the cleanup works on the
    *current user's* Temp and cache folders, and SYSTEM's are somebody else's.
    """
    name = os.environ.get("USERNAME", "")
    domain = os.environ.get("USERDOMAIN", "")
    return f"{domain}\\{name}" if domain and name else name


def next_start(now: datetime.datetime, hour: int, minute: int) -> datetime.datetime:
    """The next ``hour:minute`` after ``now``.

    A start time already past makes Windows treat today's run as missed and
    run it at once, so enabling the task at 10:00 would clean immediately.
    """
    start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return start if start > now else start + datetime.timedelta(days=1)


def build_xml(
    command: str,
    arguments: str,
    hour: int,
    minute: int,
    *,
    user: str,
    now: datetime.datetime | None = None,
) -> str:
    """The task definition."""
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"not a time of day: {hour}:{minute}")
    start = next_start(now or datetime.datetime.now(), hour, minute)
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="{_NS['t']}">
  <RegistrationInfo>
    <Description>{escape(_DESCRIPTION)}</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>{start:%Y-%m-%dT%H:%M:%S}</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(user)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>true</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT3H</ExecutionTimeLimit>
    <Priority>10</Priority>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(command)}</Command>
      <Arguments>{escape(arguments)}</Arguments>
    </Exec>
  </Actions>
</Task>
"""


class SchedulerError(PgmError):
    """Task Scheduler could not be read or changed."""


#: ``TaskFolder.RegisterTask`` constants (taskschd.h).
_CREATE_OR_UPDATE = 6
_LOGON_INTERACTIVE_TOKEN = 3

#: HRESULTs that mean "there is no such task" (file / path not found).
_NOT_FOUND = {0x80070002, 0x80070003}
_ACCESS_DENIED = 0x80070005


_DISP_E_EXCEPTION = 0x80020009


def _com_code(exc: Exception) -> int:
    """The HRESULT of a ``pywintypes.com_error``, as an unsigned number.

    Calls made through IDispatch, as the scheduler's are, report
    ``DISP_E_EXCEPTION`` outside and the real error inside, in the
    exception info's ``scode``. Reading only the outer code would take
    "no such task" for an unknown failure.
    """
    args = getattr(exc, "args", ())
    if not args or not isinstance(args[0], int):
        return 0
    code = args[0] & 0xFFFFFFFF
    if code == _DISP_E_EXCEPTION and len(args) > 2 and isinstance(args[2], tuple):
        inner = args[2][5] if len(args[2]) > 5 else 0
        if isinstance(inner, int) and inner:
            return inner & 0xFFFFFFFF
    return code


def _connect_root() -> Any:
    """Connect to Task Scheduler and return its root folder.

    Raises:
        SchedulerError: COM or the scheduler service is unavailable.
    """
    try:
        import win32com.client
    except ImportError as exc:
        raise SchedulerError(
            what="Планировщик Windows недоступен", reason=f"нет pywin32: {exc}"
        ) from exc
    try:
        service = win32com.client.Dispatch("Schedule.Service")
        service.Connect()
        return service.GetFolder("\\")
    except Exception as exc:  # noqa: BLE001 - pywintypes.com_error
        raise SchedulerError(
            what="Не удалось подключиться к планировщику Windows", reason=str(exc)
        ) from exc


def _on_scheduler(action: Callable[[Any], Any]) -> Any:
    """Run ``action(root_folder)`` on COM initialised for this thread.

    The GUI calls this on its own thread and a worker may too, so the
    apartment is entered and left here rather than assumed.
    """
    import pythoncom

    pythoncom.CoInitialize()
    try:
        # A separate frame, so every COM reference dies before
        # CoUninitialize rather than after it.
        return _call(action)
    finally:
        pythoncom.CoUninitialize()


def _call(action: Callable[[Any], Any]) -> Any:
    return action(_connect_root())


def install(
    command: str,
    arguments: str,
    hour: int,
    minute: int,
    *,
    name: str = TASK_NAME,
    user: str | None = None,
) -> None:
    """Create the task, or replace it if it exists.

    Raises:
        PrivilegeError: not elevated; the task asks for the highest
            privileges, which only an administrator may grant.
        SchedulerError: the scheduler refused the definition.
    """
    require_admin("создать задачу в планировщике Windows")
    user = user or current_user()
    xml = build_xml(command, arguments, hour, minute, user=user)

    def register(root: Any) -> None:
        # A folder in the path is created on demand. The user stays out of
        # the call: the XML's principal already names them, and passing it
        # here as well makes Windows validate the account, which a local
        # account with a blank password fails (0x8007052F) even though an
        # interactive-token task never needs the password.
        root.RegisterTask(name, xml, _CREATE_OR_UPDATE, "", "", _LOGON_INTERACTIVE_TOKEN)

    try:
        _on_scheduler(register)
    except SchedulerError:
        raise
    except Exception as exc:  # noqa: BLE001 - pywintypes.com_error
        raise SchedulerError(
            what="Не удалось создать задачу в планировщике Windows",
            reason=str(exc),
            remedy=(
                "Запустите Pudge Cleaner от имени администратора."
                if _com_code(exc) == _ACCESS_DENIED
                else None
            ),
        ) from exc
    _log.info("scheduled task %s installed for %02d:%02d", name, hour, minute)


def remove(*, name: str = TASK_NAME) -> None:
    """Delete the task, and its folder if that leaves it empty.

    A task that is not there is already the wanted state.
    """
    require_admin("удалить задачу из планировщика Windows")

    def delete(root: Any) -> None:
        try:
            root.DeleteTask(name, 0)
        except Exception as exc:  # noqa: BLE001
            if _com_code(exc) in _NOT_FOUND:
                return
            raise
        folder = name.rpartition("\\")[0]
        if folder:
            try:
                root.DeleteFolder(folder, 0)  # refused while it holds anything else
            except Exception:  # noqa: BLE001
                pass

    try:
        _on_scheduler(delete)
    except SchedulerError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise SchedulerError(
            what="Не удалось удалить задачу из планировщика Windows", reason=str(exc)
        ) from exc
    _log.info("scheduled task %s removed", name)


def query(*, name: str = TASK_NAME) -> TaskInfo:
    """What is registered under ``name``. Reading needs no elevation."""

    def read(root: Any) -> str | None:
        try:
            return root.GetTask(name).Xml
        except Exception as exc:  # noqa: BLE001
            if _com_code(exc) in _NOT_FOUND:
                return None
            raise

    try:
        xml = _on_scheduler(read)
    except SchedulerError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise SchedulerError(
            what="Не удалось прочитать задачу из планировщика Windows", reason=str(exc)
        ) from exc
    return TaskInfo(installed=False) if xml is None else parse_xml(xml)


def parse_xml(text: str) -> TaskInfo:
    """Read command, arguments and time out of a task definition."""
    # The declaration says UTF-16 but the text arrives already decoded;
    # ElementTree refuses a str that still carries one.
    body = re.sub(r"^\s*<\?xml[^>]*\?>", "", text)
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        _log.warning("scheduled task definition is not valid XML")
        return TaskInfo(installed=False)

    def text_of(path: str) -> str:
        node = root.find(path, _NS)
        return (node.text or "").strip() if node is not None else ""

    boundary = text_of("t:Triggers/t:CalendarTrigger/t:StartBoundary")
    hour = minute = 0
    if match := re.search(r"T(\d{2}):(\d{2})", boundary):
        hour, minute = int(match[1]), int(match[2])
    return TaskInfo(
        installed=True,
        command=text_of("t:Actions/t:Exec/t:Command"),
        arguments=text_of("t:Actions/t:Exec/t:Arguments"),
        hour=hour,
        minute=minute,
        enabled=text_of("t:Settings/t:Enabled").lower() != "false",
    )
