"""Named mutexes: "is the program's window open right now?".

The window holds one for as long as it lives; the nightly run looks for it
and stands aside, so the two never change the system at the same time.
"""

from __future__ import annotations

import ctypes
import os

#: Held by the dashboard while its window exists.
GUI_MUTEX = "Global\\PudgeCleaner.Gui"

_SYNCHRONIZE = 0x00100000

#: Handles kept alive for the life of the process; closing one releases it.
_held: dict[str, int] = {}


def hold(name: str) -> bool:
    """Create the mutex and keep it until the process exits.

    Returns ``False`` where it cannot be created. Holding a name some other
    process already holds still returns ``True``: this exists to be seen,
    not to exclude, so a second window is not refused here.
    """
    if os.name != "nt" or name in _held:
        return name in _held
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        return False
    _held[name] = handle
    return True


def is_held(name: str) -> bool:
    """Whether any process currently holds the mutex."""
    if os.name != "nt":
        return False
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    kernel32.OpenMutexW.restype = ctypes.c_void_p
    handle = kernel32.OpenMutexW(_SYNCHRONIZE, False, name)
    if not handle:
        return False
    kernel32.CloseHandle(ctypes.c_void_p(handle))
    return True


def release(name: str) -> None:
    """Let go of a mutex this process holds. For tests; the OS does it at exit."""
    handle = _held.pop(name, None)
    if handle:
        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(handle))  # type: ignore[attr-defined]
