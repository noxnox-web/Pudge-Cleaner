"""Where the disk space went: a read-only survey of the largest folders.

This module deletes nothing and is allowed to look where the cleaner is
not. That is the point of it. The cleaner works from an allowlist precisely
so it can never delete something nobody listed; the cost of that discipline
is that it says nothing about the 200 GB sitting in a folder no category
names. This answers that question and leaves the decision to a person.

What it reports
---------------
Directories at a fixed depth below each root, with the recursive size of
each one's subtree. A fixed depth keeps the results disjoint — no directory
in the list can contain another — which is what makes the numbers add up
instead of double-counting a parent and its child.

Budgets, not promises
---------------------
Walking a full drive can take minutes. The survey stops at
``time_budget_s`` and says so, because a partial answer labelled partial is
useful and a GUI frozen for four minutes is not.
"""

from __future__ import annotations

import os
import pathlib
import time
from dataclasses import dataclass, field

from ..utilities.formatting import format_size
from ..utilities.logging_setup import get_logger
from ..utilities.progress import Progress, ProgressCallback, Throttle, guarded

_log = get_logger(__name__)

#: Roots surveyed by default: where a club PC's space actually goes.
#: ``%USERPROFILE%`` covers Downloads, AppData and the shell folders;
#: the rest cover installed software and Windows itself.
DEFAULT_ROOTS: tuple[str, ...] = (
    "%USERPROFILE%",
    "%ProgramData%",
    "%ProgramFiles%",
    "%ProgramFiles(x86)%",
    "%SystemRoot%",
)

#: Depth below each root at which directories are reported. Depth costs
#: nothing to compute — the subtree has to be walked either way — and two
#: was too coarse: it put the whole of ``AppData\\Local`` in one 45 GB row,
#: which names a folder everybody already knows is large and says nothing
#: about what is in it. Three reaches ``AppData\\Local\\<vendor>``.
DEFAULT_DEPTH = 3

DEFAULT_LIMIT = 20

#: Total wall-clock budget, split between the roots. A full drive walk can
#: take minutes; the survey returns what it has and marks itself partial.
DEFAULT_TIME_BUDGET_S = 60.0


@dataclass(frozen=True, slots=True)
class FolderUsage:
    """One directory and the size of everything under it."""

    path: pathlib.Path
    size_bytes: int
    file_count: int

    @property
    def size_display(self) -> str:
        return format_size(self.size_bytes)


@dataclass(slots=True)
class UsageReport:
    """What the survey found, and how complete it is."""

    folders: list[FolderUsage] = field(default_factory=list)
    roots_scanned: list[pathlib.Path] = field(default_factory=list)
    unreadable: int = 0
    """Directories Windows refused to enumerate. Reported rather than
    hidden: a survey that silently skipped half of ProgramData would
    understate the total and nobody could tell."""

    duration_s: float = 0.0
    complete: bool = True
    """False when the time budget ran out before every root was walked."""

    depth: int = DEFAULT_DEPTH

    @property
    def total_bytes(self) -> int:
        return sum(f.size_bytes for f in self.folders)

    @property
    def size_display(self) -> str:
        return format_size(self.total_bytes)


def _directory_size(
    path: pathlib.Path, deadline: float
) -> tuple[int, int, int, bool]:
    """Recursive ``(bytes, files, unreadable, finished)`` for one directory.

    Symbolic links and junctions are never followed: a junction pointing at
    another drive would otherwise be counted as if it lived here, and a
    cycle would never terminate.
    """
    total = 0
    files = 0
    unreadable = 0
    stack: list[pathlib.Path] = [path]

    while stack:
        if time.monotonic() > deadline:
            return total, files, unreadable, False
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            continue
                        stat = entry.stat(follow_symlinks=False)
                        if getattr(stat, "st_reparse_tag", 0):
                            continue  # a junction: counted where it lives
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(pathlib.Path(entry.path))
                        else:
                            total += stat.st_size
                            files += 1
                    except OSError:
                        unreadable += 1
        except OSError:
            unreadable += 1

    return total, files, unreadable, True


def _targets(root: pathlib.Path, depth: int) -> tuple[list[pathlib.Path], int]:
    """Directories exactly ``depth`` below ``root``, plus unreadable count.

    A branch that ends before that depth contributes its deepest directory
    instead, so a shallow tree is still represented rather than dropped.
    """
    level = [root]
    unreadable = 0

    for _ in range(depth):
        nxt: list[pathlib.Path] = []
        for directory in level:
            children: list[pathlib.Path] = []
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        try:
                            if entry.is_symlink() or not entry.is_dir(
                                follow_symlinks=False
                            ):
                                continue
                            if getattr(
                                entry.stat(follow_symlinks=False),
                                "st_reparse_tag",
                                0,
                            ):
                                continue
                            children.append(pathlib.Path(entry.path))
                        except OSError:
                            unreadable += 1
            except OSError:
                unreadable += 1
            # A directory with no subdirectories is itself the leaf worth
            # reporting; dropping it would lose, say, a 40 GB flat folder.
            nxt.extend(children or [directory])
        level = nxt

    return level, unreadable


def survey(
    roots: tuple[str, ...] = DEFAULT_ROOTS,
    *,
    depth: int = DEFAULT_DEPTH,
    limit: int = DEFAULT_LIMIT,
    time_budget_s: float = DEFAULT_TIME_BUDGET_S,
    progress: ProgressCallback | None = None,
) -> UsageReport:
    """Measure the largest directories under ``roots``. Changes nothing.

    ``progress`` is told which root is being walked and how many of its
    folders are measured, a few times a second, from the calling thread.
    """
    started = time.monotonic()
    report = UsageReport(depth=depth)
    send = guarded(progress) if progress is not None else None
    throttle = Throttle()

    # Each root gets its own slice of the budget rather than competing for
    # one deadline. With a shared deadline a large user profile consumed
    # the whole allowance and Program Files and Windows were never looked
    # at at all — the report then said "these are the largest folders"
    # while having surveyed one root out of five.
    present: list[pathlib.Path] = []
    for template in roots:
        expanded = os.path.expandvars(template)
        if "%" in expanded:
            continue
        root = pathlib.Path(expanded)
        if root.is_dir() and root not in present:
            present.append(root)

    slice_s = time_budget_s / len(present) if present else time_budget_s
    found: list[FolderUsage] = []

    for index, root in enumerate(present):
        report.roots_scanned.append(root)
        deadline = time.monotonic() + slice_s

        targets, unreadable = _targets(root, depth)
        report.unreadable += unreadable
        for done, target in enumerate(targets):
            if send is not None and throttle.ready():
                # Every root gets an equal share of the bar, whatever its size.
                send(Progress(
                    f"Замер занятого места — {root} ({index + 1} из {len(present)})",
                    (index + done / len(targets)) / len(present),
                ))
            size, files, bad, finished = _directory_size(target, deadline)
            report.unreadable += bad
            if size:
                found.append(
                    FolderUsage(path=target, size_bytes=size, file_count=files)
                )
            if not finished:
                # Out of time for this root; move to the next one rather
                # than abandoning the remaining roots entirely.
                report.complete = False
                break

    found.sort(key=lambda f: f.size_bytes, reverse=True)
    report.folders = found[:limit]
    report.duration_s = time.monotonic() - started

    _log.info(
        "usage survey: %d folders, %s across %d roots in %.1fs (complete=%s)",
        len(report.folders),
        report.size_display,
        len(report.roots_scanned),
        report.duration_s,
        report.complete,
    )
    return report


__all__ = [
    "DEFAULT_DEPTH",
    "DEFAULT_LIMIT",
    "DEFAULT_ROOTS",
    "DEFAULT_TIME_BUDGET_S",
    "FolderUsage",
    "UsageReport",
    "survey",
]
