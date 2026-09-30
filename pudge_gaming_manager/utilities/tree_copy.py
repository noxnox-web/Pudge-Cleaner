"""Copying a directory tree to a new place, with progress, refusing links.

This is the copy half of moving a folder to another drive. The delete half is
:mod:`.tree_delete`; between them the caller checks that the copy matches.

What it does
------------
Files go through ``CopyFileExW``, which carries the contents, attributes,
timestamps and alternate data streams (the "downloaded from the internet"
mark, for one), and reports bytes as it goes, so one 60 GB file still moves
a progress bar. Directories are created first and get their attributes and
times once their contents are in, because adding a file would otherwise
change the folder's modified time.

What it refuses
---------------
A link of any kind — symbolic link or junction — stops the copy. Following
one would copy whatever it points at into the destination, and copying it as
a link would recreate a pointer that means something else on the new drive.
The caller is expected to have looked first and refused a tree that holds
links; this is the second check, made on each entry as it is reached.

An EFS-encrypted file is not silently written out decrypted: the copy fails
instead, because moving a file must not change who can read it.

What it does not do
-------------------
Files and folders are named by path, not held by handle as :mod:`.tree_delete`
holds them. Someone who can write inside the source while it is being copied
could swap a folder for a link in the gap between listing it and opening it.
The consequence is a copy of the link's target in the destination the
operator chose, and each entry is checked for that as it is reached; the
source itself is deleted by handle and is not exposed. Security descriptors
are not copied: the destination inherits from its parent, as with any copy
across drives in Explorer.

Every Win32 call uses the ``\\\\?\\`` form of the path, so trees deeper than
260 characters — ordinary under ``node_modules`` and launcher caches — copy.
"""

from __future__ import annotations

import ctypes
import os
import pathlib
import shutil
from ctypes import wintypes
from dataclasses import dataclass
from typing import Callable

from .progress import Throttle
from .secure_delete import _is_reparse_point

_IS_WINDOWS = os.name == "nt"

#: ``COPY_FILE_FAIL_IF_EXISTS``: never overwrite.
_COPY_FILE_FAIL_IF_EXISTS = 0x1
_PROGRESS_CONTINUE = 0

_ATTR_HIDDEN_SYSTEM_NOINDEX = 0x2 | 0x4 | 0x2000

if _IS_WINDOWS:
    # DWORD CALLBACK CopyProgressRoutine(LARGE_INTEGER total, LARGE_INTEGER
    # transferred, LARGE_INTEGER stream_size, LARGE_INTEGER stream_done,
    # DWORD stream, DWORD reason, HANDLE src, HANDLE dst, LPVOID data).
    # LARGE_INTEGER is eight bytes passed by value, so a 64-bit integer.
    _PROGRESS_ROUTINE = ctypes.WINFUNCTYPE(
        wintypes.DWORD,
        ctypes.c_longlong, ctypes.c_longlong, ctypes.c_longlong,
        ctypes.c_longlong, wintypes.DWORD, wintypes.DWORD,
        wintypes.HANDLE, wintypes.HANDLE, ctypes.c_void_p,
    )
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CopyFileExW.restype = wintypes.BOOL
    _k32.CopyFileExW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPCWSTR, _PROGRESS_ROUTINE, ctypes.c_void_p,
        ctypes.c_void_p, wintypes.DWORD,
    ]
    _k32.SetFileAttributesW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]


class TreeCopyError(OSError):
    """The copy stopped: a link was found, or a file could not be copied."""


@dataclass(slots=True)
class CopyStats:
    """What was copied. ``bytes`` counts file contents only."""

    files: int = 0
    directories: int = 0
    bytes: int = 0


def extended(path: str | os.PathLike[str]) -> str:
    r"""``path`` in the ``\\?\`` form that has no 260-character limit.

    The path is made absolute and normalised first: the prefix turns off
    Windows' own normalisation, so ``..`` and ``/`` would otherwise be
    taken literally.
    """
    full = os.path.abspath(path)
    if not _IS_WINDOWS or full.startswith("\\\\?\\"):
        return full
    if full.startswith("\\\\"):
        return "\\\\?\\UNC\\" + full[2:]
    return "\\\\?\\" + full


def _copy_file(source: str, destination: str, on_bytes: Callable[[int], None]) -> None:
    """Copy one file; ``on_bytes`` gets the bytes transferred so far in it."""
    if not _IS_WINDOWS:  # pragma: no cover - the program only runs on Windows
        shutil.copyfile(source, destination)
        on_bytes(os.path.getsize(destination))
        return

    def routine(_total, transferred, _size, _done, _stream, _reason, _s, _d, _data):
        try:
            on_bytes(transferred)
        except Exception:  # noqa: BLE001 - a report must never abort a copy
            pass
        return _PROGRESS_CONTINUE

    callback = _PROGRESS_ROUTINE(routine)  # held until the call returns
    if not _k32.CopyFileExW(
        extended(source), extended(destination), callback, None, None,
        _COPY_FILE_FAIL_IF_EXISTS,
    ):
        raise ctypes.WinError(ctypes.get_last_error())


def _copy_directory_metadata(source: str, destination: str) -> None:
    """Times and attributes of a finished directory."""
    shutil.copystat(source, destination)
    if _IS_WINDOWS:
        attributes = getattr(os.stat(source), "st_file_attributes", 0)
        keep = attributes & _ATTR_HIDDEN_SYSTEM_NOINDEX
        if keep:
            _k32.SetFileAttributesW(destination, keep)


def copy_tree(
    source: pathlib.Path,
    destination: pathlib.Path,
    *,
    progress: Callable[[int], None] | None = None,
) -> CopyStats:
    """Copy ``source`` to ``destination``, which must not exist yet.

    Args:
        progress: Called with the bytes copied so far, a few times a second
            and once at the end, on the calling thread.

    Raises:
        TreeCopyError: a link was found inside ``source``, or ``source`` is
            itself a link or not a directory.
        OSError: ``destination`` exists, or a file could not be copied. What
            was copied so far is left for the caller to discard.
    """
    src_root, dst_root = extended(source), extended(destination)
    if _is_reparse_point(pathlib.Path(src_root)) or not os.path.isdir(src_root):
        raise TreeCopyError(f"{source} is not a plain directory; refusing")

    stats = CopyStats()
    throttle = Throttle()
    finished = 0  # bytes of files already copied whole

    def on_bytes(in_file: int) -> None:
        if progress is not None and throttle.ready():
            progress(finished + in_file)

    os.mkdir(dst_root)
    stats.directories += 1
    made: list[tuple[str, str]] = [(src_root, dst_root)]
    pending: list[tuple[str, str]] = [(src_root, dst_root)]

    while pending:
        src_dir, dst_dir = pending.pop()
        with os.scandir(src_dir) as entries:
            listing = list(entries)
        for entry in listing:
            src, dst = entry.path, os.path.join(dst_dir, entry.name)
            if _is_reparse_point(entry):
                raise TreeCopyError(f"{src} is a link; refusing to copy it")
            if entry.is_dir(follow_symlinks=False):
                os.mkdir(dst)
                stats.directories += 1
                made.append((src, dst))
                pending.append((src, dst))
                continue
            _copy_file(src, dst, on_bytes)
            size = os.path.getsize(dst)
            finished += size
            stats.files += 1
            stats.bytes += size

    # Innermost first, so finishing a folder cannot disturb its parent's time.
    for src_dir, dst_dir in reversed(made):
        _copy_directory_metadata(src_dir, dst_dir)
    if progress is not None:
        progress(finished)
    return stats


__all__ = ["CopyStats", "TreeCopyError", "copy_tree", "extended"]
