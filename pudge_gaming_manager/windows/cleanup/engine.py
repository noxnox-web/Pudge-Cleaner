"""The cleanup engine: scan, then delete only what the scan approved.

Flow (rule #9)::

    SCAN -> SHOW -> CONFIRM -> CLEAN -> VERIFY

``scan`` mutates nothing and produces a full inventory with sizes and
counts. ``clean`` deletes only paths that a scan already approved, and
re-checks every safety rule at deletion time rather than trusting the
inventory — the filesystem can change between the two.

Containment
-----------
Windows Temp commonly contains junctions and symlinks. Walking one without
resolving it would take the cleaner outside its sandbox, so every candidate
is resolved and re-tested for containment before it is touched. Directory
links are never descended into.
"""

from __future__ import annotations

import os
import pathlib
import time
from concurrent.futures import ThreadPoolExecutor

from ...utilities.formatting import format_size
from ...utilities.logging_setup import audit_event, get_logger
from ...utilities.privileges import is_admin
from ...utilities.progress import (
    Progress,
    ProgressCallback,
    Throttle,
    fraction_of,
    guarded,
)
from .report import (
    CategoryReport,
    CleanResult,
    CleanupItem,
    ScanReport,
)
from . import deletion, walk
from .categories import default_categories
from .rules import (
    CleanupCategory,
    component_is_protected,
    within,
    protected_extensions,
    protected_roots,
    resolve_roots,
    waived_fragments,
)

_log = get_logger(__name__)

#: Threads used to walk the categories. Like deletion, a directory walk is
#: kernel-bound: the thread is blocked inside ``scandir`` and not holding
#: the GIL. Eight covers the shipped catalogue's twenty categories well —
#: only a few of them (Temp, the browser caches) are large enough to matter,
#: and the rest finish immediately.
SCAN_WORKERS = 8


class CleanupEngine:
    """Scans for removable data and deletes it under strict containment."""

    def __init__(self, categories: tuple[CleanupCategory, ...] | None = None) -> None:
        self.categories = categories if categories is not None else default_categories()
        self._protected_roots = protected_roots()

    # -- safety ------------------------------------------------------------

    def is_protected(
        self,
        path: pathlib.Path,
        *,
        allow_user_files: bool = False,
        allowed_fragments: frozenset[str] = frozenset(),
    ) -> bool:
        """True when a path must never be deleted.

        Fragments are matched against individual path **components**, never
        against the whole path string. Substring-matching the full path is
        both too broad and unpredictable: a directory called
        ``build-protected-assets`` would match the ``protect`` fragment
        (added for the DPAPI key store) and silently disable cleanup for
        everything beneath it.

        Args:
            allow_user_files: The owning category declares
                ``clears_user_files``, so only the credential extensions
                are refused. The path and root guards still apply in full —
                this relaxes one layer, never the sandbox itself.
            allowed_fragments: Protected path fragments the owning category
                declared it may traverse, for a launcher's own log and
                cache folders sitting under a protected vendor name.
        """
        if any(
            component_is_protected(part, allowed_fragments) for part in path.parts
        ):
            return True
        if path.suffix.lower() in protected_extensions(
            allow_user_files=allow_user_files
        ):
            return True
        for root in self._protected_roots:
            if within(path, root):
                return True
        return False

    @staticmethod
    def _contained(candidate: pathlib.Path, root: pathlib.Path) -> bool:
        """True when ``candidate`` really lives inside ``root``.

        Both sides are resolved, which is what defeats a junction pointing
        somewhere else entirely. This costs a filesystem round-trip per
        call, so the scan uses it only for entries that are actually links;
        deletion always uses it, where correctness beats speed.
        """
        try:
            resolved_candidate = candidate.resolve(strict=False)
            resolved_root = root.resolve(strict=False)
        except (OSError, ValueError, RuntimeError):
            return False
        return within(resolved_candidate, resolved_root)

    @staticmethod
    def _contained_lexically(candidate: pathlib.Path, root: pathlib.Path) -> bool:
        """Containment by path arithmetic alone — no I/O.

        Sound for entries produced by the scan's own walk, which never
        descends into a link, so no component between ``root`` and
        ``candidate`` can redirect elsewhere.
        """
        return within(candidate, root)

    def _approves(
        self,
        path: pathlib.Path,
        root: pathlib.Path,
        category: CleanupCategory,
        now: float,
        stat: os.stat_result | None = None,
        resolve_containment: bool = True,
    ) -> tuple[bool, str]:
        """The single gate every candidate passes, at scan *and* at delete.

        Args:
            stat: A stat already taken for this path, to avoid a second
                syscall per file. Omitted at deletion time, where the point
                is precisely to re-read the current state.
            resolve_containment: Use the resolving containment check. The
                scan passes ``False``: its walk never yields or enters a
                link. Deletion always leaves it on.
        """
        contained = (
            self._contained(path, root)
            if resolve_containment
            else self._contained_lexically(path, root)
        )
        if not contained:
            return False, "outside its category root"
        if self.is_protected(
            path,
            allow_user_files=category.clears_user_files,
            allowed_fragments=waived_fragments(category),
        ):
            return False, "protected"
        if stat is None:
            try:
                stat = path.stat()
            except OSError:
                return False, "unreadable"
        if category.min_age_hours > 0:
            age_hours = (now - stat.st_mtime) / 3600.0
            if age_hours < category.min_age_hours:
                return False, "too new"
        return True, ""

    # -- scan --------------------------------------------------------------

    def scan(self) -> ScanReport:
        """Inventory every enabled category. Mutates nothing.

        The categories are walked in parallel. They address disjoint
        directory trees, the walk only reads, and each one spends its time
        blocked in ``scandir`` rather than holding the GIL — so twenty
        categories that took twenty turns now take about as long as the
        slowest few. ``pool.map`` preserves order, so the report still
        lists them in catalogue order.
        """
        started = time.monotonic()
        report = ScanReport()
        now = time.time()

        if len(self.categories) < 2:
            report.categories = [
                self._scan_category(category, now) for category in self.categories
            ]
        else:
            with ThreadPoolExecutor(
                max_workers=min(len(self.categories), SCAN_WORKERS),
                thread_name_prefix="pgm-scan",
            ) as pool:
                report.categories = list(
                    pool.map(lambda c: self._scan_category(c, now), self.categories)
                )

        report.duration_s = time.monotonic() - started
        _log.info(
            "cleanup scan: %s in %d files across %d categories (%.2fs)",
            report.size_display, report.total_files, len(report.categories),
            report.duration_s,
        )
        return report

    def _scan_category(
        self, category: CleanupCategory, now: float
    ) -> CategoryReport:
        result = CategoryReport(category=category)

        if category.requires_admin and not is_admin():
            result.available = False
            result.unavailable_reason = "нужны права администратора"
            return result

        roots = resolve_roots(category)
        if not roots:
            result.available = False
            result.unavailable_reason = "на этом ПК нет"
            return result

        result.roots_scanned = roots
        for root in roots:
            for path, stat in walk.candidates(root, category):
                # The walk neither descends into nor yields links, so a
                # lexical containment check is sound here - and resolve()
                # per file is what once made a scan take a minute.
                approved, reason = self._approves(
                    path, root, category, now, stat=stat,
                    resolve_containment=False,
                )
                if approved:
                    result.items.append(
                        CleanupItem(
                            path=path,
                            size_bytes=stat.st_size,
                            modified=stat.st_mtime,
                        )
                    )
                elif reason == "protected":
                    result.skipped_protected += 1
                elif reason == "too new":
                    result.skipped_too_new += 1
                else:
                    result.unreadable += 1
        return result

    # -- clean -------------------------------------------------------------

    def clean(
        self,
        report: ScanReport,
        *,
        dry_run: bool = False,
        progress: ProgressCallback | None = None,
    ) -> CleanResult:
        """Delete what a scan approved.

        ``progress`` is told which category is being cleaned and what share
        of the approved items has been dealt with, a few times a second. It
        is called on this thread.

        Every item is re-validated against the same gate the scan used. The
        filesystem can change between scan and clean, and a stale inventory
        must never become permission to delete something.

        Validation and deletion are separated: the whole category is
        re-validated first, then the approved items are deleted, in
        parallel when there are enough of them to be worth it. Keeping the
        two apart is what lets the deletions overlap without any safety
        check running off the main thread.
        """
        result = CleanResult()
        now = time.time()
        send = guarded(progress) if progress is not None and not dry_run else None
        total = sum(len(c.items) for c in report.categories if c.available)
        throttle = Throttle()

        def handled() -> int:
            return result.deleted_files + result.failed_files + result.refused_files

        for category_report in report.categories:
            if not category_report.available:
                continue
            category = category_report.category
            roots = category_report.roots_scanned
            if send is not None:
                send(Progress(
                    f"Очистка диска — {category.name}", fraction_of(handled(), total)
                ))

            # Resolve each root once for the whole category instead of once
            # per file per root. resolve() is a filesystem round-trip (~77 us
            # here) and it dominated the delete phase: with three roots the
            # old code paid for it up to eight times per file, which is most
            # of the second the validation took per two thousand files.
            #
            # The roots are directories the rules name and do not move during
            # a run, and pinning them is the safe direction anyway: if one
            # were swapped for a junction mid-run, its files would resolve
            # somewhere else and fail containment against the root captured
            # here, so they would be refused rather than followed.
            resolved_roots = [_resolve(root) for root in roots]

            # Resolving a path costs a filesystem round-trip, and every file
            # in a directory shares that directory's answer. One resolve per
            # *parent* instead of per file is the difference between 20 000
            # round-trips and 200 on a typical Temp folder.
            #
            # It is also sufficient. The attack this defends against is an
            # intermediate directory becoming a junction between the scan and
            # the delete, which this still catches: the parent resolves
            # outside the root and every file under it is refused. The other
            # half — the file itself being swapped for a link — is caught at
            # the moment of deletion, where the handle is opened without
            # following reparse points and a link is rejected outright.
            # parent -> (resolved parent, is any ancestor component
            # protected). Both answers are the same for every file in a
            # directory, and both were being recomputed per file: the
            # protection check walks every component of the path, which for
            # a browser cache is eleven of them against a 180-entry table.
            parents: dict[pathlib.Path, tuple[pathlib.Path, bool]] = {}

            approved: list[CleanupItem] = []
            for item in category_report.items:
                ok, reason = self._approves_for_delete(
                    item.path, resolved_roots, parents, category, now
                )
                if ok:
                    approved.append(item)
                    continue
                # "too new" here means the file was rewritten since the
                # scan, i.e. something is using it. Correct to refuse.
                result.refused_files += 1
                _log.debug("refused %s at delete time: %s", item.path, reason)

            if dry_run:
                result.deleted_files += len(approved)
                result.deleted_bytes += sum(i.size_bytes for i in approved)
                continue

            def tick(name: str = category.name) -> None:
                if send is not None and throttle.ready():
                    send(Progress(
                        f"Очистка диска — {name}: удалено "
                        f"{format_size(result.deleted_bytes)}",
                        fraction_of(handled(), total),
                    ))

            deletion.run(approved, result, on_item=tick)

            if category.remove_empty_dirs:
                if send is not None:
                    send(Progress(
                        f"Очистка диска — {category.name}: удаление пустых папок"
                    ))
                self._remove_empty_dirs(roots)

        if not dry_run:
            audit_event(
                "cleanup",
                "clean",
                target=",".join(c.category.id for c in report.categories),
                new_state={
                    "deleted_files": result.deleted_files,
                    "deleted_bytes": result.deleted_bytes,
                    "failed": result.failed_files,
                    "refused": result.refused_files,
                },
                result="SUCCESS" if not result.errors else "PARTIAL",
            )
        return result

    def _approves_for_delete(
        self,
        path: pathlib.Path,
        resolved_roots: list[pathlib.Path],
        parents: dict[pathlib.Path, tuple[pathlib.Path, bool]],
        category: CleanupCategory,
        now: float,
    ) -> tuple[bool, str]:
        """The delete-time gate, re-run immediately before removal.

        Containment is checked on the *resolved parent directory*, cached
        across the files that share it, because that is where the dangerous
        redirection can happen. Protection is evaluated on the original
        path, because that is the name :func:`delete_file` will open.

        Args:
            resolved_roots: The category's roots, already resolved once.
            parents: Cache of parent -> (resolved parent, ancestor
                protected), reused across every file in the directory.
        """
        allowed = waived_fragments(category)
        parent = path.parent
        cached = parents.get(parent)
        if cached is None:
            cached = (
                _resolve(parent),
                self.is_protected(parent, allowed_fragments=allowed),
            )
            parents[parent] = cached
        resolved_parent, parent_protected = cached

        if not any(
            within(resolved_parent, root) for root in resolved_roots
        ):
            return False, "outside its category root"
        if parent_protected:
            return False, "protected"
        # The ancestors are settled by the cache above; only this file's own
        # name and extension are left to check.
        if component_is_protected(path.name, allowed) or path.suffix.lower() in (
            protected_extensions(allow_user_files=category.clears_user_files)
        ):
            return False, "protected"
        if category.min_age_hours > 0:
            # Only categories with a threshold pay for the stat. For a
            # shader cache, where anything present may go, re-reading the
            # mtime of every file answers a question nobody asked.
            try:
                stat = path.stat()
            except OSError:
                return False, "unreadable"
            if (now - stat.st_mtime) / 3600.0 < category.min_age_hours:
                return False, "too new"
        return True, ""

    def _remove_empty_dirs(self, roots: list[pathlib.Path]) -> None:
        """Remove directories left empty. The roots themselves are kept.

        A hand-written bottom-up walk that never enters a link. It used
        ``os.walk(followlinks=False)``, which on Windows still descends
        into junctions: nothing outside the root was removed - the resolve
        check below refused it - but a junction in Temp pointing at a large
        tree made every cleanup walk that whole tree for nothing.
        """
        for root in roots:
            self._prune(root, _resolve(root))

    def _prune(self, directory: pathlib.Path, resolved_root: pathlib.Path) -> bool:
        """Remove empty directories under ``directory``; True if it is now empty."""
        try:
            entries = list(os.scandir(directory))
        except OSError:
            return False
        empty = True
        for entry in entries:
            try:
                is_link = entry.is_symlink() or walk.entry_is_junction(entry)
                is_dir = not is_link and entry.is_dir(follow_symlinks=False)
            except OSError:
                is_dir = False
            if not is_dir:
                empty = False
                continue
            child = pathlib.Path(entry.path)
            # Same pre-resolved comparison as the delete gate: one resolve
            # for the directory, none for the root.
            if (
                not self._prune(child, resolved_root)
                or self.is_protected(child)
                or not within(_resolve(child), resolved_root)
            ):
                empty = False
                continue
            try:
                child.rmdir()
            except OSError:
                empty = False  # not empty any more, or locked
        return empty


def _resolve(path: pathlib.Path) -> pathlib.Path:
    """Resolve once, tolerating a path that cannot be resolved."""
    try:
        return path.resolve(strict=False)
    except (OSError, ValueError, RuntimeError):
        return path





