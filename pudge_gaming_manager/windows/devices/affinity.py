"""Which processor handles a device's interrupts.

Windows lets a driver's INF — or an administrator — pin a device's interrupts
to chosen processors through the ``Affinity Policy`` key under the device's
``Interrupt Management``. This is the setting "GoInterruptPolicy" edits and
"AutoGpuAffinity" tries core by core: with the default policy the graphics
interrupts land wherever the scheduler puts them, usually on the very core
that also serves most of the system, and moving them to a quieter core can
even out frame times.

Three values make up a policy, and a missing key is "Windows decides":

* ``DevicePolicy`` — 4 (``IrqPolicySpecifiedProcessors``) means "use the
  mask below".
* ``AssignmentSetOverride`` — little-endian bit mask of processors, bit *n*
  is logical processor *n* of group 0.
* ``DevicePriority`` — 0 undefined, 1 low, 2 normal, 3 high.

A policy is read and written as a whole, and a snapshot records whether each
value *existed*, so putting a snapshot back restores absence as absence.
The change takes effect when the device is next started (a driver restart or
a reboot), not when the registry is written.

Only the graphics adapters PGM names are ever passed here, and the instance
id is checked against the shape of a PCI device id before it is spliced
into a registry path.
"""

from __future__ import annotations

import re
import winreg
from dataclasses import dataclass

from ...utilities.exceptions import PgmError

#: ``IrqPolicySpecifiedProcessors`` — use ``AssignmentSetOverride``.
SPECIFIED_PROCESSORS = 4
PRIORITY_HIGH = 3

#: Highest logical processor a one-group mask can name.
MAX_CPU = 63

_POLICY_KEY = (
    r"SYSTEM\CurrentControlSet\Enum\{instance}\Device Parameters"
    r"\Interrupt Management\Affinity Policy"
)
_PCI_ID = re.compile(r"^PCI\\[A-Z0-9&_]+\\[A-Z0-9&_.\-]+$", re.IGNORECASE)

_PRIORITY_NAMES = {0: "не задан", 1: "низкий", 2: "обычный", 3: "высокий"}


class AffinityError(PgmError):
    """The affinity policy could not be read or written."""


@dataclass(frozen=True, slots=True)
class AffinityPolicy:
    """One device's affinity policy, with what existed and what did not."""

    key_present: bool = False
    policy: int | None = None
    priority: int | None = None
    mask: bytes | None = None

    @classmethod
    def pinned(cls, cpu: int, priority: int = PRIORITY_HIGH) -> "AffinityPolicy":
        """Interrupts on logical processor ``cpu`` only, at ``priority``."""
        if not 0 <= cpu <= MAX_CPU:
            raise AffinityError(
                f"Нельзя закрепить прерывания за процессором {cpu}",
                "Поддерживаются процессоры 0–63 первой группы.",
            )
        return cls(
            key_present=True,
            policy=SPECIFIED_PROCESSORS,
            priority=priority,
            mask=(1 << cpu).to_bytes(8, "little").rstrip(b"\x00"),
        )

    @property
    def cpus(self) -> tuple[int, ...]:
        """Processors named by the mask, when the policy uses it."""
        if self.policy != SPECIFIED_PROCESSORS or not self.mask:
            return ()
        value = int.from_bytes(self.mask, "little")
        return tuple(n for n in range(value.bit_length()) if value >> n & 1)

    @property
    def label(self) -> str:
        if not self.key_present or self.policy is None:
            return "решает Windows (по умолчанию)"
        if self.policy == SPECIFIED_PROCESSORS:
            where = ", ".join(f"CPU {n}" for n in self.cpus) or "ни одного процессора"
            priority = _PRIORITY_NAMES.get(self.priority or 0, str(self.priority))
            return f"закреплено за {where}, приоритет: {priority}"
        return f"политика {self.policy}"

    def to_dict(self) -> dict[str, object]:
        """JSON-safe form, for a backup row."""
        return {
            "key_present": self.key_present,
            "policy": self.policy,
            "priority": self.priority,
            "mask": None if self.mask is None else self.mask.hex(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "AffinityPolicy":
        mask = data.get("mask")
        return cls(
            key_present=bool(data.get("key_present")),
            policy=_optional_int(data.get("policy")),
            priority=_optional_int(data.get("priority")),
            mask=bytes.fromhex(mask) if isinstance(mask, str) else None,
        )


def _optional_int(value: object) -> int | None:
    return None if value is None else int(value)  # type: ignore[call-overload]


def check_instance_id(instance_id: str) -> None:
    """Refuse anything that is not shaped like a PCI device instance id."""
    if not _PCI_ID.match(instance_id) or ".." in instance_id:
        raise AffinityError(
            "Не удалось обратиться к устройству",
            f"«{instance_id}» не похоже на идентификатор PCI-устройства.",
        )


def _key_path(instance_id: str) -> str:
    check_instance_id(instance_id)
    return _POLICY_KEY.format(instance=instance_id)


def read(instance_id: str) -> AffinityPolicy:
    """The device's current policy. Raises :class:`AffinityError`."""
    path = _key_path(instance_id)
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, path, 0,
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        ) as key:
            return AffinityPolicy(
                key_present=True,
                policy=_value(key, "DevicePolicy"),
                priority=_value(key, "DevicePriority"),
                mask=_value(key, "AssignmentSetOverride"),
            )
    except FileNotFoundError:
        return AffinityPolicy()
    except OSError as exc:
        raise AffinityError(
            "Не удалось прочитать политику прерываний устройства",
            str(exc.strerror or exc),
            remedy="Запустите Pudge Cleaner от имени администратора.",
        ) from exc


def _value(key, name: str):
    try:
        return winreg.QueryValueEx(key, name)[0]
    except FileNotFoundError:
        return None


def write(instance_id: str, policy: AffinityPolicy) -> None:
    """Make the device's policy exactly ``policy``. Raises :class:`AffinityError`.

    A policy without its key removes the key; otherwise each value is
    written, or deleted where the policy has none.
    """
    path = _key_path(instance_id)
    flags = winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY
    try:
        if not policy.key_present:
            _delete_key(path)
            return
        with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, path, 0, flags) as key:
            _store(key, "DevicePolicy", policy.policy, winreg.REG_DWORD)
            _store(key, "DevicePriority", policy.priority, winreg.REG_DWORD)
            _store(key, "AssignmentSetOverride", policy.mask, winreg.REG_BINARY)
    except OSError as exc:
        raise AffinityError(
            "Не удалось записать политику прерываний устройства",
            str(exc.strerror or exc),
            remedy="Запустите Pudge Cleaner от имени администратора.",
        ) from exc


def _store(key, name: str, value, kind: int) -> None:
    if value is None:
        try:
            winreg.DeleteValue(key, name)
        except FileNotFoundError:
            pass
    else:
        winreg.SetValueEx(key, name, 0, kind, value)


def _delete_key(path: str) -> None:
    try:
        winreg.DeleteKeyEx(
            winreg.HKEY_LOCAL_MACHINE, path, winreg.KEY_WOW64_64KEY, 0
        )
    except FileNotFoundError:
        pass


__all__ = [
    "AffinityError",
    "AffinityPolicy",
    "MAX_CPU",
    "PRIORITY_HIGH",
    "SPECIFIED_PROCESSORS",
    "check_instance_id",
    "read",
    "write",
]
