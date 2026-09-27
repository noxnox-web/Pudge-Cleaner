"""Deleting a directory tree by handle, relative to its parent, never by path.

Why not a path walk
-------------------
An elevated recursive delete that checks "is this a junction?" and then
descends *by name* has a race between the check and the use: a user with
write access to any directory in the tree swaps it for a junction to
``C:\\Windows\\System32`` in that window, and the delete follows it. Steam's
install folder is writable by every user, and the folders under
``Windows.old\\Users`` belong to their old owners, so both trees this program
deletes are exposed. It is the bug behind Rust's CVE-2022-21658, and the fix
is the one Rust adopted:

* every child is opened with ``NtOpenFile`` **relative to the already-open
  parent handle** (``OBJECT_ATTRIBUTES.RootDirectory``), so no path component
  above it is ever resolved again;
* with ``FILE_OPEN_REPARSE_POINT``, so a junction or symlink swapped in is
  opened as the link itself;
* a directory is enumerated only after its *handle* reports that it is not a
  reparse point, and it is enumerated through that handle
  (``GetFileInformationByHandleEx``), not by name.

A link is therefore always deleted as a link — its target is never touched.

Why it is also faster
---------------------
One enumeration call returns names, sizes and attributes for a whole buffer
of entries, so nothing is ``stat``-ed; a relative open parses one name
instead of a full path; and the delete is ``FileDispositionInfoEx`` with
POSIX semantics and ``IGNORE_READONLY_ATTRIBUTE``, which removes the name at
once and needs no separate "clear read-only" round trip. ``DeleteFileW``
itself is open + set-disposition + close, so this is the same number of
kernel calls per file as ``os.remove``, minus the path parsing.

Backup intent
-------------
``Windows.old`` is full of files owned by TrustedInstaller that deny even
administrators. With ``backup_intent=True`` the caller's SeBackupPrivilege and
SeRestorePrivilege are enabled for the duration and every open carries
``FILE_OPEN_FOR_BACKUP_INTENT``, which is the documented way for an
administrator to read and delete regardless of the DACL — no ownership or ACL
is rewritten anywhere, unlike ``takeown``/``icacls``.
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import pathlib
import time
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Callable, Iterator

_IS_WINDOWS = os.name == "nt"

# Access rights and share mode
_DELETE = 0x00010000
_SYNCHRONIZE = 0x00100000
_FILE_LIST_DIRECTORY = 0x0001
_FILE_READ_ATTRIBUTES = 0x0080
_SHARE_ALL = 0x7

# NtOpenFile OpenOptions
_FILE_DIRECTORY_FILE = 0x00000001
_FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
_FILE_NON_DIRECTORY_FILE = 0x00000040
_FILE_OPEN_FOR_BACKUP_INTENT = 0x00004000
_FILE_OPEN_REPARSE_POINT = 0x00200000

# CreateFileW, for the root only
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

_ATTR_DIRECTORY = 0x10
_ATTR_REPARSE_POINT = 0x400

# FILE_INFO_BY_HANDLE_CLASS
_FileDispositionInfo = 4
_FILE_ATTRIBUTE_TAG_INFO = 9
_FileFullDirectoryInfo = 14
_FileFullDirectoryRestartInfo = 15
_FileDispositionInfoEx = 21

_DISPOSITION_DELETE = 0x1
_DISPOSITION_POSIX = 0x2
_DISPOSITION_IGNORE_READONLY = 0x10

_ERROR_NO_MORE_FILES = 18
_ERROR_INVALID_PARAMETER = 87
_ERROR_NOT_SUPPORTED = 50

#: Files in one directory below which a thread pool costs more than it saves.
_PARALLEL_THRESHOLD = 64
_WORKERS = 8
_ENUM_BUFFER = 64 * 1024
#: Seconds between progress reports: often enough to look live, rare enough
#: that reporting never shows up next to ~400 us per deleted file.
_PROGRESS_INTERVAL = 0.25


class _UnicodeString(ctypes.Structure):
    _fields_ = [
        ("Length", wintypes.USHORT),
        ("MaximumLength", wintypes.USHORT),
        ("Buffer", wintypes.LPWSTR),
    ]


class _ObjectAttributes(ctypes.Structure):
    _fields_ = [
        ("Length", wintypes.ULONG),
        ("RootDirectory", wintypes.HANDLE),
        ("ObjectName", ctypes.POINTER(_UnicodeString)),
        ("Attributes", wintypes.ULONG),
        ("SecurityDescriptor", ctypes.c_void_p),
        ("SecurityQualityOfService", ctypes.c_void_p),
    ]


class _IoStatusBlock(ctypes.Structure):
    _fields_ = [("Status", ctypes.c_void_p), ("Information", ctypes.c_void_p)]


class _FileAttributeTagInfo(ctypes.Structure):
    _fields_ = [("FileAttributes", wintypes.DWORD), ("ReparseTag", wintypes.DWORD)]


if _IS_WINDOWS:
    _ntdll = ctypes.WinDLL("ntdll")
    _ntdll.NtOpenFile.restype = ctypes.c_long
    _ntdll.NtOpenFile.argtypes = [
        ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD,
        ctypes.POINTER(_ObjectAttributes), ctypes.POINTER(_IoStatusBlock),
        wintypes.ULONG, wintypes.ULONG,
    ]
    _ntdll.RtlNtStatusToDosError.restype = wintypes.ULONG
    _ntdll.RtlNtStatusToDosError.argtypes = [ctypes.c_long]

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateFileW.restype = wintypes.HANDLE
    _k32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    _k32.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ]
    _k32.SetFileInformationByHandle.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]


class TreeDeleteError(OSError):
    """The root is not a plain directory, or could not be opened."""


@dataclass(slots=True)
class DeleteStats:
    """What a pass did. ``bytes`` counts only what was actually deleted."""

    bytes: int = 0
    files: int = 0
    directories: int = 0
    links: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)

    def _fail(self, where: str, error: int) -> None:
        self.failed += 1
        if len(self.errors) < 20:
            self.errors.append(f"{where}: {ctypes.FormatError(error).strip()}")


@dataclass(frozen=True, slots=True)
class _Entry:
    name: str
    attributes: int
    size: int

    @property
    def is_link(self) -> bool:
        return bool(self.attributes & _ATTR_REPARSE_POINT)

    @property
    def is_dir(self) -> bool:
        return bool(self.attributes & _ATTR_DIRECTORY)


# -- primitives -------------------------------------------------------------


def _open_relative(
    parent: int, name: str, access: int, options: int
) -> tuple[int | None, int]:
    """``NtOpenFile(name)`` relative to ``parent``. Returns (handle, win32 error)."""
    buffer = ctypes.create_unicode_buffer(name)
    length = len(name) * 2
    text = _UnicodeString(length, length + 2, ctypes.cast(buffer, wintypes.LPWSTR))
    attributes = _ObjectAttributes(
        ctypes.sizeof(_ObjectAttributes), parent, ctypes.pointer(text), 0, None, None
    )
    handle = wintypes.HANDLE()
    status = _ntdll.NtOpenFile(
        ctypes.byref(handle), access | _SYNCHRONIZE, ctypes.byref(attributes),
        ctypes.byref(_IoStatusBlock()), _SHARE_ALL,
        options | _FILE_SYNCHRONOUS_IO_NONALERT | _FILE_OPEN_REPARSE_POINT,
    )
    if status < 0:
        return None, int(_ntdll.RtlNtStatusToDosError(status))
    return handle.value, 0


def _attributes(handle: int) -> int | None:
    info = _FileAttributeTagInfo()
    if not _k32.GetFileInformationByHandleEx(handle, _FILE_ATTRIBUTE_TAG_INFO, ctypes.byref(info), ctypes.sizeof(info)):
        return None
    return info.FileAttributes


def _mark_deleted(handle: int) -> int:
    """Delete the object behind ``handle``. Returns a win32 error, 0 on success."""
    flags = wintypes.ULONG(
        _DISPOSITION_DELETE | _DISPOSITION_POSIX | _DISPOSITION_IGNORE_READONLY
    )
    if _k32.SetFileInformationByHandle(handle, _FileDispositionInfoEx, ctypes.byref(flags), 4):
        return 0
    error = ctypes.get_last_error()
    if error not in (_ERROR_INVALID_PARAMETER, _ERROR_NOT_SUPPORTED):
        return error
    # Pre-1809 Windows or a non-NTFS volume: classic delete, which a
    # read-only file refuses — clearing the attribute needs a write right
    # the delete-only handle may lack, so that case is reported, not forced.
    classic = wintypes.BOOL(True)
    if _k32.SetFileInformationByHandle(handle, _FileDispositionInfo, ctypes.byref(classic), 4):
        return 0
    return ctypes.get_last_error()


def _entries(handle: int) -> Iterator[_Entry]:
    """Every child of the directory ``handle`` refers to, by that handle."""
    buffer = ctypes.create_string_buffer(_ENUM_BUFFER)
    info_class = _FileFullDirectoryRestartInfo
    while True:
        if not _k32.GetFileInformationByHandleEx(handle, info_class, buffer, _ENUM_BUFFER):
            error = ctypes.get_last_error()
            if error == _ERROR_NO_MORE_FILES:
                return
            raise ctypes.WinError(error)
        info_class = _FileFullDirectoryInfo
        raw = buffer.raw
        offset = 0
        while True:
            next_offset = int.from_bytes(raw[offset:offset + 4], "little")
            size = int.from_bytes(raw[offset + 40:offset + 48], "little", signed=True)
            attributes = int.from_bytes(raw[offset + 56:offset + 60], "little")
            name_length = int.from_bytes(raw[offset + 60:offset + 64], "little")
            name = raw[offset + 68:offset + 68 + name_length].decode("utf-16-le")
            if name not in (".", ".."):
                yield _Entry(name, attributes, max(size, 0))
            if next_offset == 0:
                break
            offset += next_offset


# -- the walk ----------------------------------------------------------------


class _Walker:
    def __init__(
        self,
        backup_intent: bool,
        measure_only: bool,
        progress: Callable[[DeleteStats], None] | None = None,
    ) -> None:
        self.options = _FILE_OPEN_FOR_BACKUP_INTENT if backup_intent else 0
        self.measure_only = measure_only
        self.stats = DeleteStats()
        self.pool: ThreadPoolExecutor | None = None
        self.progress = progress
        self._reported = time.monotonic()

    def _tick(self) -> None:
        """Report the running totals, at most once per interval."""
        if self.progress is None:
            return
        now = time.monotonic()
        if now - self._reported >= _PROGRESS_INTERVAL:
            self._reported = now
            self.progress(self.stats)

    def _delete_leaf(self, parent: int, entry: _Entry) -> tuple[int, int]:
        """Delete a file or a link. Returns (win32 error, bytes freed)."""
        options = self.options
        if not entry.is_link:
            options |= _FILE_NON_DIRECTORY_FILE
        handle, error = _open_relative(parent, entry.name, _DELETE, options)
        if handle is None:
            return error, 0
        try:
            error = _mark_deleted(handle)
        finally:
            _k32.CloseHandle(handle)
        return error, (0 if entry.is_link or error else entry.size)

    def directory(
        self, handle: int, where: str, spare: frozenset[str] = frozenset()
    ) -> None:
        """Process everything under the open directory ``handle``.

        ``spare`` names children (lower-case) to leave untouched.
        """
        try:
            children = [e for e in _entries(handle) if e.name.lower() not in spare]
        except OSError as exc:
            self.stats._fail(where, exc.winerror or 0)
            return

        leaves = [e for e in children if e.is_link or not e.is_dir]
        if self.measure_only:
            self.stats.bytes += sum(e.size for e in leaves if not e.is_link)
            self.stats.files += sum(1 for e in leaves if not e.is_link)
        else:
            self._delete_leaves(handle, leaves, where)

        for entry in children:
            if entry.is_dir and not entry.is_link:
                self._subdirectory(handle, entry, where)

    def _delete_leaves(self, handle: int, leaves: list[_Entry], where: str) -> None:
        if len(leaves) >= _PARALLEL_THRESHOLD:
            if self.pool is None:
                self.pool = ThreadPoolExecutor(_WORKERS, thread_name_prefix="pgm-tree")
            outcomes = self.pool.map(lambda e: self._delete_leaf(handle, e), leaves)
        else:
            outcomes = (self._delete_leaf(handle, e) for e in leaves)
        for entry, (error, freed) in zip(leaves, outcomes):
            if error:
                self.stats._fail(f"{where}\\{entry.name}", error)
            elif entry.is_link:
                self.stats.links += 1
            else:
                self.stats.files += 1
                self.stats.bytes += freed
            self._tick()

    def _subdirectory(self, parent: int, entry: _Entry, where: str) -> None:
        path = f"{where}\\{entry.name}"
        access = _FILE_LIST_DIRECTORY | _FILE_READ_ATTRIBUTES
        if not self.measure_only:
            access |= _DELETE
        handle, error = _open_relative(
            parent, entry.name, access, self.options | _FILE_DIRECTORY_FILE
        )
        if handle is None:
            self.stats._fail(path, error)
            return
        try:
            attributes = _attributes(handle)
            if attributes is None:
                self.stats._fail(path, ctypes.get_last_error())
                return
            if attributes & _ATTR_REPARSE_POINT:
                # Swapped for a link since it was listed. The handle is to the
                # link itself (FILE_OPEN_REPARSE_POINT): delete that, never
                # enumerate through it.
                if not self.measure_only:
                    error = _mark_deleted(handle)
                    if error:
                        self.stats._fail(path, error)
                    else:
                        self.stats.links += 1
                return
            self.directory(handle, path)
            if not self.measure_only:
                error = _mark_deleted(handle)
                if error:
                    self.stats._fail(path, error)
                else:
                    self.stats.directories += 1
        finally:
            _k32.CloseHandle(handle)

    def close(self) -> None:
        if self.pool is not None:
            self.pool.shutdown()


def _open_root(root: pathlib.Path, backup_intent: bool, delete: bool) -> int:
    access = _FILE_LIST_DIRECTORY | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE
    if delete:
        access |= _DELETE
    handle = _k32.CreateFileW(
        str(root), access, _SHARE_ALL, None, _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT, None,
    )
    if handle in (None, _INVALID_HANDLE_VALUE):
        raise ctypes.WinError(ctypes.get_last_error())
    attributes = _attributes(handle)
    if attributes is None or attributes & _ATTR_REPARSE_POINT or not attributes & _ATTR_DIRECTORY:
        _k32.CloseHandle(handle)
        raise TreeDeleteError(f"{root} is not a plain directory; refusing")
    return handle


def delete_tree(
    root: pathlib.Path,
    *,
    backup_intent: bool = False,
    keep_root: bool = False,
    spare: tuple[str, ...] = (),
    progress: Callable[[DeleteStats], None] | None = None,
) -> DeleteStats:
    """Delete ``root`` and everything under it, never following a link.

    Args:
        backup_intent: Open with backup semantics and the caller's backup and
            restore privileges enabled, so ACL-denied files can be removed.
        keep_root: Empty the directory but leave it in place.
        spare: Names of direct children to keep (case-insensitive). Filtered
            from the root's own enumeration, so a kept name is never opened.
        progress: Called on the deleting thread with the running totals every
            quarter of a second, and once more at the end. It receives the
            live object: read it, do not keep or change it.

    Raises:
        TreeDeleteError: ``root`` is a reparse point or not a directory.
        OSError: ``root`` could not be opened.
    """
    if not _IS_WINDOWS:  # pragma: no cover - the program only runs on Windows
        raise OSError("handle-based tree deletion is Windows-only")
    with _privileges(backup_intent):
        handle = _open_root(root, backup_intent, delete=not keep_root)
        walker = _Walker(backup_intent, measure_only=False, progress=progress)
        try:
            walker.directory(handle, str(root), frozenset(n.lower() for n in spare))
            if not keep_root:
                error = _mark_deleted(handle)
                if error:
                    walker.stats._fail(str(root), error)
                else:
                    walker.stats.directories += 1
        finally:
            walker.close()
            _k32.CloseHandle(handle)
        if progress is not None:
            progress(walker.stats)
        return walker.stats


def measure_tree(root: pathlib.Path, *, backup_intent: bool = False) -> DeleteStats:
    """Total size and file count under ``root``, links neither followed nor counted."""
    if not _IS_WINDOWS:  # pragma: no cover
        raise OSError("handle-based tree measurement is Windows-only")
    with _privileges(backup_intent):
        handle = _open_root(root, backup_intent, delete=False)
        walker = _Walker(backup_intent, measure_only=True)
        try:
            walker.directory(handle, str(root))
        finally:
            walker.close()
            _k32.CloseHandle(handle)
        return walker.stats


@contextlib.contextmanager
def _privileges(wanted: bool) -> Iterator[None]:
    if not wanted:
        yield
        return
    from .privileges import enabled_privileges

    with enabled_privileges("SeBackupPrivilege", "SeRestorePrivilege"):
        yield


__all__ = ["DeleteStats", "TreeDeleteError", "delete_tree", "measure_tree"]
