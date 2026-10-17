"""Which processor a running process may use.

AutoGpuAffinity starts its test scene on the same core as the GPU driver's
interrupts, so the measurement shows that pairing rather than the scene
wandering between cores. The scene is already running when this is applied;
the scene's own start-up is long enough that the few milliseconds before the
mask takes hold do not matter.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

from ...utilities.exceptions import PgmError

_PROCESS_SET_INFORMATION = 0x0200
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def set_cpu(pid: int, cpu: int) -> None:
    """Restrict process ``pid`` to logical processor ``cpu`` of group 0."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.SetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.c_size_t]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]

    handle = kernel.OpenProcess(
        _PROCESS_SET_INFORMATION | _PROCESS_QUERY_LIMITED_INFORMATION, False, pid
    )
    if not handle:
        raise PgmError(
            "Не удалось закрепить тестовую сцену за процессором",
            f"код ошибки {ctypes.get_last_error()}",
        )
    try:
        if not kernel.SetProcessAffinityMask(handle, 1 << cpu):
            raise PgmError(
                "Не удалось закрепить тестовую сцену за процессором",
                f"код ошибки {ctypes.get_last_error()}",
            )
    finally:
        kernel.CloseHandle(handle)


__all__ = ["set_cpu"]
