"""Deleting, or moving to another drive, one folder the operator picked.

The usage survey lists the largest folders and deliberately says nothing
about what to do with them. This is what happens when the operator does
decide: delete one, or move it to another drive and leave a junction behind
so the programs that look in the old place keep working.

Flow, the same one every destructive action here follows::

    PLAN -> SHOW -> CONFIRM -> RUN -> REPORT

:func:`plan_delete` and :func:`plan_move` change nothing and refuse what must
not be touched; :func:`run_folder` does exactly that plan, and re-checks it,
because a plan is an inventory and never a permission slip.

What may be touched
-------------------
Decided by :mod:`.folder_policy`, which refuses the Windows folder, the
profile and Program Files as a whole, Windows and Microsoft components,
and club software, anti-cheat and credentials. Everything else the
operator chooses is theirs to choose, behind a confirmation that names the
folder and says it cannot be undone.

Why a move is a copy, a swap and a delete
-----------------------------------------
Across drives there is no atomic move. The folder is first *renamed and
renamed back*: a folder with anything open inside refuses to be renamed, so
that is a reliable "in use" test, and it fails in a moment rather than after
copying 100 GB. Then it is copied, and the copy is checked against a second
measurement of the source (a program writing into it while it was copied
shows as a difference). Only then is the source renamed aside, the junction
made in its place, and the renamed original deleted. If any step before the
delete fails, the original is where it was and the partial copy is removed.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import uuid
from dataclasses import dataclass, field
from enum import Enum

from ..utilities import tree_copy, tree_delete
from ..utilities.exceptions import PgmError
from ..utilities.formatting import format_size
from ..utilities.junction import create_junction
from ..utilities.logging_setup import audit_event, get_logger
from ..utilities.progress import Progress, ProgressCallback, fraction_of, guarded
from ..utilities.secure_delete import _is_reparse_point
from .cleanup.rules import within
from .folder_policy import block_reason, resolved

_log = get_logger(__name__)

#: Free space kept on the destination beyond the folder itself.
_SPACE_MARGIN = 256 << 20

class FolderAction(str, Enum):
    DELETE = "DELETE"
    MOVE = "MOVE"


class FolderRefused(PgmError):
    """The folder may not be, or could not safely be, deleted or moved."""


@dataclass(frozen=True, slots=True)
class FolderPlan:
    """What the operator confirms: one folder, and what will happen to it."""

    action: FolderAction
    source: pathlib.Path
    size_bytes: int
    file_count: int
    destination: pathlib.Path | None = None
    """For a move: the folder it will have on the other drive."""

    unreadable: int = 0
    """Entries that could not be read while measuring. A delete goes ahead
    and reports them; a move refuses, because it would leave them behind."""

    @property
    def size_display(self) -> str:
        return format_size(self.size_bytes)


@dataclass(slots=True)
class FolderResult:
    """What was actually done."""

    action: FolderAction
    source: pathlib.Path
    destination: pathlib.Path | None = None
    size_bytes: int = 0
    file_count: int = 0
    failed: list[str] = field(default_factory=list)
    leftover: pathlib.Path | None = None
    """A folder still on disk with files that could not be deleted."""

    linked: bool = False
    """A move left a junction at the old path."""

    @property
    def size_display(self) -> str:
        return format_size(self.size_bytes)

    @property
    def complete(self) -> bool:
        return self.leftover is None and not self.failed


def _require_actionable(path: pathlib.Path, what: str) -> None:
    reason = block_reason(path)
    if reason:
        raise FolderRefused(what, f"«{path.name}»: {reason}.")


# -- planning (no mutation) -------------------------------------------------


def _measure(path: pathlib.Path, what: str) -> tree_delete.DeleteStats:
    try:
        return tree_delete.measure_tree(path)
    except OSError as exc:
        raise FolderRefused(
            what, f"не удалось прочитать «{path.name}»: {exc.strerror or exc}."
        ) from exc


def plan_delete(source: pathlib.Path) -> FolderPlan:
    """Measure ``source`` for deletion. Changes nothing.

    Raises:
        FolderRefused: the folder is not one that may be deleted.
    """
    what = "Удаление невозможно"
    _require_actionable(source, what)
    stats = _measure(source, what)
    return FolderPlan(
        FolderAction.DELETE, source, stats.bytes, stats.files,
        unreadable=stats.failed,
    )


def _other_volume(a: pathlib.Path, b: pathlib.Path) -> bool:
    """True when the two paths are on different volumes.

    Compared by volume serial rather than drive letter, so a ``subst`` alias
    or a folder mounted from the same disk is not taken for another drive.
    """
    return os.stat(a).st_dev != os.stat(b).st_dev


def plan_move(source: pathlib.Path, destination_parent: pathlib.Path) -> FolderPlan:
    """Check that ``source`` can move into ``destination_parent``. Changes nothing.

    Raises:
        FolderRefused: the folder may not be moved, the destination is not
            another drive, is taken or is too small, or the folder holds
            links or entries that cannot be read.
    """
    what = "Перенос невозможен"
    source = pathlib.Path(source)
    _require_actionable(source, what)

    parent = pathlib.Path(destination_parent)
    if not parent.is_dir():
        raise FolderRefused(what, f"папки назначения «{parent}» нет.")
    if _is_reparse_point(parent):
        raise FolderRefused(what, f"«{parent}» — ссылка, а не обычная папка.")
    if not _other_volume(source, parent):
        raise FolderRefused(
            what, "это тот же диск, места не прибавится.",
            "Выберите папку на другом диске.",
        )
    destination = parent / source.name
    if os.path.lexists(destination):
        raise FolderRefused(
            what, f"в «{parent}» уже есть «{source.name}».",
            "Выберите другую папку назначения.",
        )
    if within(resolved(destination), resolved(source)):
        raise FolderRefused(what, "нельзя переносить папку внутрь самой себя.")

    stats = _measure(source, what)
    if stats.links:
        raise FolderRefused(
            what,
            f"внутри {stats.links} ссылок (junction или symlink): при переносе "
            "они потеряли бы смысл.",
            "Перенесите папку вручную или удалите ссылки.",
        )
    if stats.failed:
        raise FolderRefused(
            what, f"{stats.failed} элементов внутри не читаются, их бы оставили.",
            "Запустите программу от имени администратора.",
        )
    free = shutil.disk_usage(parent).free
    if free < stats.bytes + _SPACE_MARGIN:
        raise FolderRefused(
            what,
            f"на диске назначения свободно {format_size(free)}, а нужно "
            f"{format_size(stats.bytes + _SPACE_MARGIN)} вместе с запасом.",
        )
    return FolderPlan(
        FolderAction.MOVE, source, stats.bytes, stats.files, destination
    )


# -- running ----------------------------------------------------------------


def run_folder(
    plan: FolderPlan, progress: ProgressCallback | None = None
) -> FolderResult:
    """Do what ``plan`` says, after checking again that it is still allowed.

    ``progress`` is told what is happening and how far it has got, from the
    thread that calls this.

    Raises:
        FolderRefused: a check failed, or a step did. Nothing is left half
            done except what the message says.
    """
    send = guarded(progress) if progress is not None else None
    try:
        if plan.action is FolderAction.DELETE:
            result = _delete(plan, send)
        else:
            result = _move(plan, send)
    except FolderRefused as exc:
        audit_event(
            "folders", plan.action.value.lower(), target=str(plan.source),
            result="FAILED", error=exc.reason or exc.what,
        )
        raise
    audit_event(
        "folders", plan.action.value.lower(), target=str(plan.source),
        new_state={
            "destination": str(result.destination) if result.destination else None,
            "bytes": result.size_bytes, "files": result.file_count,
            "linked": result.linked, "failed": len(result.failed),
            "leftover": str(result.leftover) if result.leftover else None,
        },
        result="SUCCESS" if result.complete else "PARTIAL",
    )
    return result


def _delete(plan: FolderPlan, send) -> FolderResult:
    what = "Удаление невозможно"
    source = plan.source
    _require_actionable(source, what)
    name = source.name

    def report(stats: tree_delete.DeleteStats) -> None:
        send(Progress(
            f"Удаление «{name}» — удалено {format_size(stats.bytes)} "
            f"из {format_size(plan.size_bytes)}",
            fraction_of(stats.bytes, plan.size_bytes),
        ))

    try:
        stats = tree_delete.delete_tree(source, progress=report if send else None)
    except OSError as exc:
        raise FolderRefused(
            what, f"«{name}»: {exc.strerror or exc}."
        ) from exc
    return FolderResult(
        FolderAction.DELETE, source,
        size_bytes=stats.bytes, file_count=stats.files, failed=stats.errors,
        leftover=source if os.path.lexists(source) else None,
    )


def _move(plan: FolderPlan, send) -> FolderResult:
    what = "Перенос не выполнен"
    source, destination = plan.source, plan.destination
    if destination is None:
        raise FolderRefused(what, "в плане нет папки назначения.")
    _require_actionable(source, what)
    if os.path.lexists(destination):
        raise FolderRefused(what, f"«{destination}» уже существует.")
    name = source.name
    token = uuid.uuid4().hex[:8]

    def say(text: str, fraction: float | None = None) -> None:
        if send is not None:
            send(Progress(f"Перенос «{name}» — {text}", fraction))

    say("проверка, что папку никто не использует")
    _probe_unused(source, token, what)

    def copying(copied: int) -> None:
        say(
            f"копирование: {format_size(copied)} из {format_size(plan.size_bytes)}",
            fraction_of(copied, plan.size_bytes),
        )

    aside = source.with_name(f"{name}.pgm-old-{token}")
    originals_safe = True
    try:
        copied = tree_copy.copy_tree(source, destination, progress=copying)
        say("проверка копии")
        _verify_copy(source, destination, copied, what)
        _rename(source, aside, what, "папку начали использовать во время копирования")
        try:
            create_junction(source, destination)
        except OSError as exc:
            try:
                os.rename(aside, source)
            except OSError:
                originals_safe = False
                raise FolderRefused(
                    what,
                    f"ссылку создать не удалось, а вернуть папку на место тоже: "
                    f"оригинал лежит в «{aside}», копия — в «{destination}».",
                ) from exc
            raise FolderRefused(
                what, f"не удалось создать ссылку: {exc.strerror or exc}."
            ) from exc
    except FolderRefused:
        if originals_safe:
            _discard(destination)
        raise
    except OSError as exc:
        _discard(destination)
        raise FolderRefused(
            what, f"копирование остановилось: {exc.strerror or exc}."
        ) from exc

    def deleting(stats: tree_delete.DeleteStats) -> None:
        say(
            f"удаление старой копии: {format_size(stats.bytes)} "
            f"из {format_size(plan.size_bytes)}",
            fraction_of(stats.bytes, plan.size_bytes),
        )

    failed: list[str] = []
    try:
        stats = tree_delete.delete_tree(aside, progress=deleting if send else None)
        failed = stats.errors
    except OSError as exc:
        failed = [f"{aside.name}: {exc.strerror or exc}"]
    return FolderResult(
        FolderAction.MOVE, source, destination,
        size_bytes=copied.bytes, file_count=copied.files, failed=failed,
        leftover=aside if os.path.lexists(aside) else None, linked=True,
    )


def _probe_unused(source: pathlib.Path, token: str, what: str) -> None:
    """Rename ``source`` away and back: it refuses while anything is open in it."""
    probe = source.with_name(f"{source.name}.pgm-probe-{token}")
    _rename(source, probe, what, "в ней что-то открыто — закройте программы, которые её используют")
    try:
        os.rename(probe, source)
    except OSError as exc:
        raise FolderRefused(
            what,
            f"папку переименовали для проверки и не смогли вернуть: "
            f"она лежит в «{probe}».",
        ) from exc


def _rename(source: pathlib.Path, target: pathlib.Path, what: str, why: str) -> None:
    try:
        os.rename(source, target)
    except OSError as exc:
        raise FolderRefused(
            what, f"«{source.name}» не переименовать: {why}.",
            "Закройте программы, которые используют эту папку, и повторите.",
        ) from exc


def _verify_copy(
    source: pathlib.Path,
    destination: pathlib.Path,
    copied: tree_copy.CopyStats,
    what: str,
) -> None:
    """The copy equals what was copied, and the source did not change meanwhile."""
    expected = (copied.files, copied.bytes)
    now = _measure(source, what)
    if (now.files, now.bytes) != expected or now.links:
        raise FolderRefused(
            what, "папка изменилась, пока копировалась (её использует программа).",
            "Закройте программы, которые пишут в эту папку, и повторите.",
        )
    there = _measure(destination, what)
    if (there.files, there.bytes) != expected or there.links or there.failed:
        raise FolderRefused(
            what, "копия на новом диске не совпала с оригиналом, оригинал не тронут."
        )


def _discard(destination: pathlib.Path) -> None:
    """Remove a partial copy. The original was never changed, so this is safe."""
    try:
        if os.path.lexists(destination):
            tree_delete.delete_tree(destination)
    except OSError:
        _log.exception("could not remove the partial copy at %s", destination)


__all__ = [
    "FolderAction",
    "FolderPlan",
    "FolderRefused",
    "FolderResult",
    "plan_delete",
    "plan_move",
    "run_folder",
]
