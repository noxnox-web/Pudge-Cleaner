"""Keeping an unattended run out of the way of whoever uses the PC.

A club PC is somebody's gaming seat. The nightly run must not be felt: it
drops to the lowest CPU and disk priority, and it waits until nobody has
touched the mouse or keyboard for a while.
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

import psutil

from .logging_setup import get_logger

_log = get_logger(__name__)


class _LastInputInfo(ctypes.Structure):
    _fields_ = (("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD))


def idle_seconds() -> float | None:
    """Seconds since the last keyboard or mouse input in this session.

    ``None`` when Windows cannot say (not Windows, or no interactive
    session): the caller must not read that as "idle".
    """
    if os.name != "nt":
        return None
    info = _LastInputInfo()
    info.cbSize = ctypes.sizeof(_LastInputInfo)
    try:
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):  # type: ignore[attr-defined]
            return None
        now = ctypes.windll.kernel32.GetTickCount()  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return None
    # Both are 32-bit millisecond tick counts that wrap together after
    # ~49 days; the masked difference is right across the wrap.
    return ((now - info.dwTime) & 0xFFFFFFFF) / 1000.0


def lower_priority() -> None:
    """Drop this process to idle CPU priority and very low disk priority.

    Child processes inherit the CPU class. Failing to lower either is
    logged, not raised: running at normal priority is worse than not
    running only for the one person at the keyboard, and the idle wait
    already protects them.
    """
    process = psutil.Process()
    for label, apply in (
        ("cpu", lambda: process.nice(psutil.IDLE_PRIORITY_CLASS)),
        ("io", lambda: process.ionice(psutil.IOPRIO_VERYLOW)),
    ):
        try:
            apply()
        except (psutil.Error, AttributeError, OSError) as exc:
            _log.warning("could not lower %s priority: %s", label, exc)
