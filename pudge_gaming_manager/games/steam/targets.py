"""One thing a Steam reset clears, and how it is cleared safely.

Three shapes cover everything the reset touches:

* ``TREE`` — a whole folder Steam recreates (``workshop``, ``appcache``...).
* ``CONTENTS`` — a folder emptied except for named files it must keep
  (``config`` keeps ``config.vdf``).
* ``FILE`` — a single file (a user's ``local.vdf`` sign-in tokens).

Every removal goes through ``utilities.secure_delete``: the target is opened
without following reparse points, so a junction planted in place of a cache
folder cannot redirect an elevated delete outside Steam's tree.
"""

from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from ...utilities.formatting import format_size
from ...utilities.secure_delete import delete_file, delete_tree
from ...utilities.tree_delete import DeleteStats


class ClearMode(str, Enum):
    TREE = "TREE"
    CONTENTS = "CONTENTS"
    FILE = "FILE"


@dataclass(frozen=True, slots=True)
class CacheTarget:
    """One folder or file the reset would clear."""

    path: pathlib.Path
    label: str
    size_bytes: int | None
    mode: ClearMode = ClearMode.TREE
    keep_names: frozenset[str] = field(default_factory=frozenset)
    """For ``CONTENTS``: lower-case names of direct children to keep."""

    @property
    def size_display(self) -> str:
        return format_size(self.size_bytes) if self.size_bytes is not None else "?"

    def clear(self, progress: Callable[[DeleteStats], None] | None = None) -> int:
        """Remove this target and return the bytes reclaimed.

        ``progress`` receives the running totals of a folder delete; a single
        file has nothing to report.

        Raises:
            DeleteError: the target (or a child) turned into a reparse point
                or changed type since the scan — refused, never followed.
            OSError: a file is locked or the delete was denied.
        """
        if self.mode is ClearMode.FILE:
            return delete_file(self.path) if self.path.exists() else 0
        if not self.path.exists():
            return 0
        if self.mode is ClearMode.TREE:
            return delete_tree(self.path, progress=progress)

        # CONTENTS: empty the folder through its own handle, sparing the kept
        # names. Listing it by path and deleting each child by path would let
        # the folder be swapped for a junction between the two.
        return delete_tree(
            self.path, keep_root=True, spare=tuple(self.keep_names), progress=progress
        )


def dir_size(path: pathlib.Path) -> int | None:
    """Sum a folder's files without following links. ``None`` if unreadable."""
    if not path.is_dir():
        return None
    total = 0
    try:
        for current, _dirs, files in os.walk(path, followlinks=False):
            here = pathlib.Path(current)
            for name in files:
                try:
                    total += (here / name).stat().st_size
                except OSError:
                    continue
    except OSError:
        return None
    return total


__all__ = ["CacheTarget", "ClearMode", "dir_size"]
