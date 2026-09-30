"""Deleting a file by the object, not by its name.

The cleaner runs elevated and deletes under ``C:\\Windows\\Temp``, a directory
any user can create subdirectories in. That is the classic setup for a
time-of-check/time-of-use attack: PGM validates a path, and between the
validation and the ``unlink`` a low-privileged user swaps a directory for a
junction pointing at ``System32``. The elevated delete then follows the
junction and destroys a system file.

Re-checking the path again just moves the window; the name is the wrong
thing to trust. This module opens a handle **once**, with
``FILE_FLAG_OPEN_REPARSE_POINT`` so a reparse point is opened as itself
rather than followed, and does every check and the delete against that one
handle. After the open, the object is pinned: the attacker may rename or
replace the *name*, but the handle still refers to the object PGM inspected.

``delete_file`` is used for the actual removal. ``safe_unlink`` falls back to
an ordinary ``unlink`` off Windows (tests, and the rules never point outside
Windows in production), so the engine has one call site either way.
"""

from __future__ import annotations

import ctypes
import os
import pathlib
from ctypes import wintypes
from typing import Callable

from . import tree_delete

_IS_WINDOWS = os.name == "nt"

# CreateFileW
_DELETE = 0x00010000
_FILE_READ_ATTRIBUTES = 0x00000080
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_SHARE_DELETE = 0x00000004
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

# GetFileInformationByHandle
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_FILE_ATTRIBUTE_DIRECTORY = 0x00000010



class _ByHandleFileInformation(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", wintypes.DWORD),
        ("ftCreationTime", wintypes.FILETIME),
        ("ftLastAccessTime", wintypes.FILETIME),
        ("ftLastWriteTime", wintypes.FILETIME),
        ("dwVolumeSerialNumber", wintypes.DWORD),
        ("nFileSizeHigh", wintypes.DWORD),
        ("nFileSizeLow", wintypes.DWORD),
        ("nNumberOfLinks", wintypes.DWORD),
        ("nFileIndexHigh", wintypes.DWORD),
        ("nFileIndexLow", wintypes.DWORD),
    ]


if _IS_WINDOWS:
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    _kernel32.GetFileInformationByHandle.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(_ByHandleFileInformation)
    ]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


class DeleteError(OSError):
    """A handle-based delete could not be completed safely."""


def delete_file(path: pathlib.Path) -> int:
    """Delete a regular file by its object and return its size in bytes.

    Opens the path once without following reparse points and, on that same
    handle, refuses anything that is a directory or a reparse point, then
    marks the object for deletion. The name can be swapped after the open;
    the handle cannot.

    Raises:
        DeleteError: the object is a directory or reparse point, or a Win32
            call failed. ``FileNotFoundError`` and ``PermissionError`` (both
            ``OSError``) surface with their Windows error numbers so the
            caller can treat a locked file as "in use", as before.
    """
    if not _IS_WINDOWS:  # pragma: no cover - exercised via safe_unlink on POSIX
        size = path.stat().st_size
        path.unlink()
        return size

    # DELETE and READ_ATTRIBUTES only — exactly what the checks and the
    # delete need. Asking for GENERIC_READ as this once did makes Defender
    # scan the file on open, before it is deleted: measured at 818 us per
    # file against 87 us without it, a ninefold difference on a cleanup.
    handle = _kernel32.CreateFileW(
        str(path),
        _DELETE | _FILE_READ_ATTRIBUTES,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        None,
        _OPEN_EXISTING,
        # BACKUP_SEMANTICS lets the same call open a directory, so the check
        # below can reject one instead of the open silently refusing it.
        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if handle == _INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())

    try:
        info = _ByHandleFileInformation()
        if not _kernel32.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())

        attributes = info.dwFileAttributes
        if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            # A junction or symlink swapped in after the scan. Deleting the
            # link itself would be safe, but the cleaner never lists links,
            # so its presence here means the name was tampered with. Refuse.
            raise DeleteError(f"{path} is a reparse point; refusing to delete")
        if attributes & _FILE_ATTRIBUTE_DIRECTORY:
            raise DeleteError(f"{path} is a directory; refusing to delete")

        size = (info.nFileSizeHigh << 32) | info.nFileSizeLow

        # POSIX semantics and IGNORE_READONLY, falling back to the classic
        # disposition on older Windows — one implementation, shared with
        # the tree delete.
        error = tree_delete._mark_deleted(handle)
        if error:
            raise ctypes.WinError(error)
        return size
    finally:
        _kernel32.CloseHandle(handle)


def safe_unlink(path: pathlib.Path) -> int:
    """Delete ``path`` and return its size; handle-based on Windows."""
    return delete_file(path)


def _is_reparse_point(entry: os.DirEntry | pathlib.Path) -> bool:
    """True for a junction or symlink, without following it."""
    try:
        st = entry.stat(follow_symlinks=False) if isinstance(entry, os.DirEntry) \
            else os.lstat(entry)
        return bool(getattr(st, "st_reparse_tag", 0)) or bool(
            entry.is_symlink() if isinstance(entry, os.DirEntry) else os.path.islink(entry)
        )
    except OSError:
        return False


def delete_tree(
    root: pathlib.Path,
    *,
    keep_root: bool = False,
    spare: tuple[str, ...] = (),
    progress: Callable[[tree_delete.DeleteStats], None] | None = None,
) -> int:
    """Recursively delete a directory and return the bytes reclaimed.

    Delegates to :mod:`.tree_delete`, which opens every child relative to
    its already-open parent and never follows a link. The previous version
    here checked "is this a junction?" and then descended *by name*, which
    left a window for a user who can write inside the tree — anyone, under
    Steam's install folder — to swap the checked folder for a junction to
    System32 before the elevated delete walked into it.

    Files that are locked or denied are left behind, as before; the caller
    sees them as a folder that still exists. ``progress`` receives the
    running totals a few times a second (see :func:`.tree_delete.delete_tree`).

    Raises:
        DeleteError: the root is a reparse point or not a directory.
    """
    try:
        return tree_delete.delete_tree(
            root, keep_root=keep_root, spare=spare, progress=progress
        ).bytes
    except tree_delete.TreeDeleteError as exc:
        raise DeleteError(str(exc)) from exc
