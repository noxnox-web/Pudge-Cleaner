"""Removing ``%SystemDrive%\\Windows.old`` after a Windows 11 upgrade.

A feature update leaves the previous install in ``Windows.old`` so the user
can roll back for ten days, after which Windows deletes it on its own. On a
club PC it is dead weight — often tens of gigabytes — and there is no reason
to keep the rollback.

It cannot be removed like an ordinary folder: many files are owned by
``TrustedInstaller`` and deny even administrators.

How it is removed, and why not ``takeown``/``icacls``
-------------------------------------------------------
This used to run ``takeown /r``, ``icacls /grant /t`` and ``rmdir /s``. Two
things were wrong with that.

**It reached outside the folder.** ``takeown /r`` and ``icacls /t`` walk
*into* directory junctions — verified on the development machine — and
``Windows.old`` contains them (``Documents and Settings``, ``Users\\All
Users`` and others), which can point at the *live* ``C:\\Users`` and
``C:\\ProgramData``. The elevated run could re-own and re-permission the
running system's user profiles.

**It was three passes over every file,** two of them rewriting a security
descriptor per file: 25 s for 20 000 files, against 1.7 s now.

Now the tree is deleted by handle with backup intent
(:func:`...utilities.tree_delete.delete_tree`): SeBackupPrivilege and
SeRestorePrivilege — which every administrator token holds, disabled — are
enabled for the duration, the documented way to delete regardless of a
file's ACL. No ownership or permission is changed anywhere, every child is
opened relative to its parent, and a link is deleted as a link.

Deleting it removes the ability to roll the Windows upgrade back. That is the
operator's decision, made at the preview; this module only carries it out.
"""

from __future__ import annotations

import os
import pathlib
import sys
from typing import Callable

from ...utilities import tree_delete
from ...utilities.logging_setup import get_logger
from ...utilities.secure_delete import _is_reparse_point

_log = get_logger(__name__)

#: Windows 11 is build 22000 and up; ``Windows.old`` on 10 is out of scope.
_WINDOWS_11_BUILD = 22000


def is_windows_11() -> bool:
    """True when this is Windows 11 (build 22000+)."""
    if os.name != "nt":
        return False
    try:
        return sys.getwindowsversion().build >= _WINDOWS_11_BUILD  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return False


def windows_old_path() -> pathlib.Path:
    """``Windows.old`` on the system drive."""
    system_drive = os.environ.get("SystemDrive", "C:")
    return pathlib.Path(system_drive + "\\") / "Windows.old"


def is_present(path: pathlib.Path | None = None) -> bool:
    """True when a real ``Windows.old`` directory exists (not a link)."""
    target = path or windows_old_path()
    try:
        return target.is_dir() and not _is_reparse_point(target)
    except OSError:
        return False


def folder_size(path: pathlib.Path | None = None) -> int | None:
    """Size of ``Windows.old``; ``None`` if it cannot be read.

    Read through the same handle walk as the delete, with backup intent, so
    TrustedInstaller-owned files are counted rather than skipped, and links
    are neither followed nor counted. The old ``os.walk`` descended into
    junctions and could add the live ``ProgramData`` to the figure; it also
    ``stat``-ed every file, where one enumeration call returns sizes for a
    whole buffer of them — ten times faster.
    """
    target = path or windows_old_path()
    if not is_present(target):
        return None
    try:
        return tree_delete.measure_tree(target, backup_intent=True).bytes
    except OSError as exc:
        _log.warning("cannot measure %s: %s", target, exc)
        return None


def remove(
    path: pathlib.Path | None = None,
    *,
    progress: Callable[[tree_delete.DeleteStats], None] | None = None,
) -> tree_delete.DeleteStats | None:
    """Delete ``Windows.old``. Returns what was done; ``None`` if refused.

    Refuses a reparse point: a junction planted in place of the folder must
    not redirect an elevated delete. Needs administrator rights, for the
    backup and restore privileges. ``progress`` receives the running totals
    (see :func:`...utilities.tree_delete.delete_tree`): a real folder takes
    minutes, and the operator should see it moving.
    """
    target = path or windows_old_path()
    if not os.path.lexists(target):
        return tree_delete.DeleteStats()
    try:
        # Refuses a link or a non-directory in place of the folder itself.
        stats = tree_delete.delete_tree(
            target, backup_intent=True, progress=progress
        )
    except OSError as exc:
        _log.warning("windows.old removal refused at %s: %s", target, exc)
        return None
    _log.info(
        "windows.old removal at %s: %d files, %d links, %d failed",
        target, stats.files, stats.links, stats.failed,
    )
    return stats


__all__ = [
    "folder_size",
    "is_present",
    "is_windows_11",
    "remove",
    "windows_old_path",
]
