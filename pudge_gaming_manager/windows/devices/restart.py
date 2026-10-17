"""Restarting a display adapter so a changed setting takes effect.

An interrupt-affinity policy is read when the device starts, so after
writing one the adapter has to be disabled and enabled again (or the PC
rebooted). That is what the "Disable device / Enable device" pair in Device
Manager does, and it blanks the screen for a few seconds and ends every
running Direct3D application — so it is never done as a side effect.

Two guards:

* The enable runs in a ``finally`` block. A failure between the two steps
  must not leave the only GPU switched off.
* Windows' own "Microsoft Basic Display" driver must be available. It is
  what draws the screen while the real driver is down; with it disabled a
  restart can end in a black screen that needs a reboot to leave.
"""

from __future__ import annotations

import winreg

from ...utilities.command_runner import CommandRunner
from ...utilities.powershell_runner import PowerShellRunner
from .affinity import check_instance_id

_SCRIPT = r"""
try { Disable-PnpDevice -InstanceId $Id -Confirm:$false -ErrorAction Stop }
finally {
  Start-Sleep -Seconds 2
  Enable-PnpDevice -InstanceId $Id -Confirm:$false -ErrorAction Stop
}
Start-Sleep -Seconds 2
"""

#: ``Start`` value meaning "disabled".
_DISABLED = 4


def basic_display_available() -> bool:
    """True unless the Basic Display driver has been switched off.

    An unreadable value counts as available: the driver ships enabled, and
    the check exists to catch someone having turned it off.
    """
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Services\BasicDisplay",
            0,
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        ) as key:
            return winreg.QueryValueEx(key, "Start")[0] != _DISABLED
    except OSError:
        return True


def restart(instance_id: str, powershell: PowerShellRunner | None = None) -> None:
    """Disable and re-enable one PCI device. Raises :class:`PgmError`."""
    check_instance_id(instance_id)
    runner = powershell or PowerShellRunner(CommandRunner())
    runner.run(
        _SCRIPT,
        parameters={"Id": instance_id},
        timeout_s=90,
        requires_admin=True,
        operation="restart display adapter",
    )


__all__ = ["basic_display_available", "restart"]
