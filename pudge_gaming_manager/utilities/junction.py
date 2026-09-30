"""Creating a directory junction, without a shell.

Moving a folder to another drive leaves a junction at the old path, so the
programs that still look there keep working. ``mklink /J`` would do it, but
it is a ``cmd`` built-in: the path reaches it through ``cmd``'s own parser,
which expands ``%NAME%`` even inside quotes, so a folder called ``%TEMP%``
would link somewhere else entirely. A junction is one ``FSCTL_SET_REPARSE_POINT``
on an empty directory, and this writes it directly.

Only a junction to a local directory on a lettered drive is made. A symbolic
link would need a privilege most accounts lack and can point at a network
path; neither is wanted here.
"""

from __future__ import annotations

import ctypes
import os
import pathlib
import struct
from ctypes import wintypes

_IS_WINDOWS = os.name == "nt"

_GENERIC_WRITE = 0x40000000
_SHARE_ALL = 0x7
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FSCTL_SET_REPARSE_POINT = 0x000900A4
_IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

if _IS_WINDOWS:
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateFileW.restype = wintypes.HANDLE
    _k32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    _k32.DeviceIoControl.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]


def _reparse_buffer(target: str) -> bytes:
    """The ``REPARSE_DATA_BUFFER`` of a mount point to ``target``.

    Holds the target twice, as Windows stores it: the substitute name in NT
    form (``\\??\\C:\\dir``) and the print name Explorer shows (``C:\\dir``).
    """
    substitute = ("\\??\\" + target).encode("utf-16-le")
    printed = target.encode("utf-16-le")
    path_buffer = substitute + b"\x00\x00" + printed + b"\x00\x00"
    body = struct.pack(
        "<HHHH", 0, len(substitute), len(substitute) + 2, len(printed)
    ) + path_buffer
    return struct.pack("<IHH", _IO_REPARSE_TAG_MOUNT_POINT, len(body), 0) + body


def create_junction(link: pathlib.Path, target: pathlib.Path) -> None:
    """Make ``link`` a junction to the existing directory ``target``.

    ``link`` must not exist: it is created empty and then turned into the
    junction, so an existing folder is never overwritten or merged.

    Raises:
        OSError: ``link`` exists, ``target`` is not a directory or is not on a
            lettered drive, or Windows refused. A half-made ``link`` is
            removed before the error propagates.
    """
    if not _IS_WINDOWS:  # pragma: no cover - the program only runs on Windows
        raise OSError("junctions are Windows-only")
    resolved = os.path.abspath(target)
    if not os.path.isdir(resolved):
        raise OSError(f"{target} is not a directory")
    if len(os.path.splitdrive(resolved)[0]) != 2:
        raise OSError(f"{target} is not on a lettered drive")

    os.mkdir(link)  # fails if anything is already there
    try:
        handle = _k32.CreateFileW(
            str(link), _GENERIC_WRITE, _SHARE_ALL, None, _OPEN_EXISTING,
            _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT, None,
        )
        if handle in (None, _INVALID_HANDLE_VALUE):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            buffer = _reparse_buffer(resolved)
            returned = wintypes.DWORD()
            if not _k32.DeviceIoControl(
                handle, _FSCTL_SET_REPARSE_POINT, buffer, len(buffer),
                None, 0, ctypes.byref(returned), None,
            ):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            _k32.CloseHandle(handle)
    except BaseException:
        try:
            os.rmdir(link)
        except OSError:
            pass
        raise


__all__ = ["create_junction"]
