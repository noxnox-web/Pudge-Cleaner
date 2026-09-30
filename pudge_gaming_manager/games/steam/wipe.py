"""The Steam reset engine: scan, then remove only what the plan approved.

The same shape as the temp-file cleaner (rule #9)::

    SCAN -> SHOW -> CONFIRM -> WIPE -> VERIFY

``scan`` mutates nothing and returns a full plan: every game, split into
"remove" and "keep" by the exceptions list, plus the cache folders that would
be cleared, all with sizes. ``wipe`` removes only what a plan listed, and only
after Steam is stopped so its file locks are gone.

Deleting a game is **irreversible** — like the temp-file cleaner, it takes no
backup (a 100 GB game cannot be snapshotted, and Steam re-downloads it). The
preview is therefore not optional: the operator sees the exact list, the sizes
and what is kept before anything is removed.

A reset also **signs every Steam account out** (see ``signout``), so the next
player never lands in the previous player's account.

Safety: every path is confined to the library it belongs to, and every
removal goes through the handle-based, reparse-point-refusing deletes in
``utilities.secure_delete`` — so a junction swapped in for a game
folder cannot redirect an elevated delete out of Steam's tree.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ...utilities.command_runner import CommandRunner
from ...utilities.formatting import format_size
from ...utilities.logging_setup import audit_event, get_logger
from ...utilities.progress import Progress, ProgressCallback, fraction_of, guarded
from ...utilities.secure_delete import DeleteError, delete_file, delete_tree
from ...utilities.tree_delete import DeleteStats
from .library import discover_libraries, find_steam, installed_games
from .models import InstalledGame, SteamInstall, SteamLibrary
from .signout import signout_targets
from .targets import CacheTarget, dir_size
from .vdf import VdfError, loads

_log = get_logger(__name__)

#: Per-library folders cleared on a club reset, as SteamWiper does. Steam
#: regenerates all of them. ``workshop`` is cleared whole — including the
#: workshop content of games that are kept (maps, mods, wallpapers): on a
#: club PC that content piles up from every player, and Steam re-downloads
#: whatever a subscribed item still needs.
_LIBRARY_CACHE_DIRS = ("downloading", "temp", "shadercache", "workshop", "sourcemods")

#: Steam-root folders safe to clear on a club reset. ``userdata`` holds the
#: previous player's cloud saves and screenshots — cleared so a shared PC does
#: not carry one player's data into the next session.
_ROOT_CACHE_DIRS = ("appcache", "logs", "dumps", "userdata")

_STEAM_PROCESSES = ("steam", "steamwebhelper", "steamservice")
_STEAM_SERVICE = "Steam Client Service"


@dataclass(slots=True)
class WipePlan:
    """What a reset intends to do, before it does anything."""

    install: SteamInstall
    remove: list[InstalledGame] = field(default_factory=list)
    keep: list[InstalledGame] = field(default_factory=list)
    caches: list[CacheTarget] = field(default_factory=list)
    signout: list[CacheTarget] = field(default_factory=list)
    """What is cleared to sign every account out (config, tokens, cookies)."""

    accounts: int = 0
    """Accounts Steam remembers on this PC, from ``loginusers.vdf``."""

    @property
    def games_bytes(self) -> int:
        return sum(g.size_bytes or 0 for g in self.remove)

    @property
    def cache_bytes(self) -> int:
        return sum(c.size_bytes or 0 for c in self.caches)

    @property
    def signout_bytes(self) -> int:
        return sum(c.size_bytes or 0 for c in self.signout)

    @property
    def total_bytes(self) -> int:
        return self.games_bytes + self.cache_bytes + self.signout_bytes

    @property
    def has_changes(self) -> bool:
        return bool(self.remove or self.caches or self.signout)

    @property
    def size_display(self) -> str:
        return format_size(self.total_bytes)

    def with_kept_back(self, app_ids: set[int]) -> WipePlan:
        """This plan with the given games moved from *remove* to *keep*.

        The operator unticks a game in the preview to rescue it — even one
        not in the built-in keep-list. Caches and sign-out are unchanged.
        """
        return WipePlan(
            install=self.install,
            remove=[g for g in self.remove if g.app_id not in app_ids],
            keep=self.keep + [g for g in self.remove if g.app_id in app_ids],
            caches=self.caches,
            signout=self.signout,
            accounts=self.accounts,
        )

    def preview_lines(self) -> list[str]:
        lines = [
            f"{format_size(self.total_bytes)} освободится — "
            f"удалить игр: {len(self.remove)}, оставить: {len(self.keep)}"
        ]
        for game in sorted(self.remove, key=lambda g: g.size_bytes or 0, reverse=True):
            lines.append(f"  - удалить  {game.size_display:>10}  {game.name}")
        for game in self.keep:
            lines.append(f"  = оставить {game.size_display:>10}  {game.name}")
        for cache in self.caches:
            lines.append(f"  - кэш     {cache.size_display:>10}  {cache.label}")
        for target in self.signout:
            lines.append(f"  - выход   {target.size_display:>10}  {target.label}")
        return lines


@dataclass(slots=True)
class WipeResult:
    """What a reset actually removed."""

    removed_games: int = 0
    reclaimed_bytes: int = 0
    failed: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)
    steam_stopped: bool = False
    signed_out: bool = False
    """Every sign-in target was cleared without a failure or refusal."""

    @property
    def size_display(self) -> str:
        return format_size(self.reclaimed_bytes)


class _Reporter:
    """Turns one tree delete's byte counts into the reset's overall progress.

    The bar measures bytes against the plan's total, so a 60 GB game moves
    it more than a cache folder does. The totals are from manifests and an
    earlier scan, so :func:`fraction_of` clamps rather than trusting them.
    """

    def __init__(self, progress: ProgressCallback, result: WipeResult, total: int) -> None:
        self._send = guarded(progress)
        self._result = result
        self._total = total
        self._text = ""

    def phase(self, text: str) -> None:
        """Start a step; ``result`` already holds everything finished before it."""
        self._text = text
        self._send(Progress(text, fraction_of(self._result.reclaimed_bytes, self._total)))

    def tree(self, stats: DeleteStats) -> None:
        """The running totals of the folder being deleted now."""
        self._send(Progress(
            f"{self._text} — удалено {format_size(stats.bytes)}",
            fraction_of(self._result.reclaimed_bytes + stats.bytes, self._total),
        ))


def _remembered_accounts(install: SteamInstall) -> int:
    """How many accounts ``loginusers.vdf`` lists. 0 if it cannot be read."""
    try:
        users = loads(
            (install.path / "config" / "loginusers.vdf").read_text(encoding="utf-8")
        ).get("users")
    except (OSError, VdfError):
        return 0
    return len(users) if isinstance(users, dict) else 0


class SteamWiper:
    """Plans and performs a Steam games reset for a club PC."""

    def __init__(self, runner: CommandRunner | None = None) -> None:
        self._runner = runner or CommandRunner()

    # -- scan (no mutation) ------------------------------------------------

    def scan(self, keep_app_ids: set[int]) -> WipePlan | None:
        """Build the reset plan. Returns ``None`` when Steam is not found."""
        install = find_steam()
        if install is None:
            return None
        libraries = discover_libraries(install)
        plan = WipePlan(install=install)

        for game in installed_games(libraries):
            (plan.keep if game.app_id in keep_app_ids else plan.remove).append(game)

        plan.caches.extend(self._cache_targets(install, libraries))
        plan.signout.extend(signout_targets(install))
        plan.accounts = _remembered_accounts(install)

        _log.info(
            "steam reset plan: remove %d, keep %d, %s reclaimable",
            len(plan.remove), len(plan.keep), plan.size_display,
        )
        return plan

    def _cache_targets(
        self, install: SteamInstall, libraries: list[SteamLibrary]
    ) -> list[CacheTarget]:
        targets: list[CacheTarget] = []
        for library in libraries:
            for name in _LIBRARY_CACHE_DIRS:
                path = library.steamapps / name
                if path.is_dir():
                    targets.append(
                        CacheTarget(path, f"{name} ({library.path.drive or library.path})",
                                    dir_size(path))
                    )
        for name in _ROOT_CACHE_DIRS:
            path = install.path / name
            if path.is_dir():
                targets.append(CacheTarget(path, f"{name} (Steam)", dir_size(path)))
        return targets

    # -- wipe --------------------------------------------------------------

    def wipe(
        self,
        plan: WipePlan,
        *,
        dry_run: bool = False,
        progress: ProgressCallback | None = None,
    ) -> WipeResult:
        """Remove what the plan approved. Stops Steam first to free locks.

        ``progress`` is told which step is running and how many of the
        plan's bytes are gone, from the thread that calls this method.
        """
        result = WipeResult()
        report = (
            _Reporter(progress, result, plan.total_bytes)
            if progress is not None and not dry_run
            else None
        )
        if not dry_run:
            if report is not None:
                report.phase("Очистка Steam — остановка Steam")
            result.steam_stopped = self._stop_steam()

        keep_ids = {g.app_id for g in plan.keep}
        # Re-check at delete time: the exceptions list is authoritative,
        # and a stale plan must never delete a game now marked to keep.
        games = [g for g in plan.remove if g.app_id not in keep_ids]
        for index, game in enumerate(games, 1):
            if report is not None:
                report.phase(f"Очистка Steam — игра {index} из {len(games)}: {game.name}")
            self._remove_game(game, result, dry_run, report)

        for cache in plan.caches:
            if report is not None:
                report.phase(f"Очистка Steam — кэш: {cache.label}")
            self._remove_cache(cache, result, dry_run, report)
        if plan.signout and report is not None:
            report.phase("Очистка Steam — выход из аккаунтов")
        cleared = [self._remove_cache(t, result, dry_run, report) for t in plan.signout]
        result.signed_out = bool(cleared) and all(cleared)

        if not dry_run:
            audit_event(
                "steam", "wipe", target=str(plan.install.path),
                new_state={
                    "removed_games": result.removed_games,
                    "reclaimed_bytes": result.reclaimed_bytes,
                    "failed": len(result.failed),
                    "refused": len(result.refused),
                },
                result="SUCCESS" if not result.failed else "PARTIAL",
            )
        return result

    def _remove_game(
        self,
        game: InstalledGame,
        result: WipeResult,
        dry_run: bool,
        report: _Reporter | None = None,
    ) -> None:
        install_path = game.install_path
        # Confine to the library's common folder: the install dir name comes
        # from the manifest, which is Steam's own file, but a defence in depth.
        try:
            contained = install_path.resolve(strict=False).is_relative_to(
                game.library.common.resolve(strict=False)
            )
        except (OSError, ValueError):
            contained = False
        if not contained:
            result.refused.append(f"{game.name}: outside its library")
            return

        if dry_run:
            result.removed_games += 1
            result.reclaimed_bytes += game.size_bytes or 0
            return

        try:
            if install_path.is_dir():
                result.reclaimed_bytes += delete_tree(
                    install_path, progress=report.tree if report else None
                )
            result.removed_games += 1
        except DeleteError as exc:
            result.refused.append(f"{game.name}: {exc}")
            return
        except OSError as exc:
            result.failed.append(f"{game.name}: {exc.strerror or exc}")
            return
        # The manifest is what makes Steam think the game is installed; remove
        # it last, so a failed folder delete leaves the game still recognised.
        try:
            if game.manifest_path.exists():
                delete_file(game.manifest_path)
        except OSError as exc:
            result.failed.append(f"{game.name} manifest: {exc.strerror or exc}")

    def _remove_cache(
        self,
        cache: CacheTarget,
        result: WipeResult,
        dry_run: bool,
        report: _Reporter | None = None,
    ) -> bool:
        """Clear one target; True when it was cleared completely."""
        if dry_run:
            result.reclaimed_bytes += cache.size_bytes or 0
            return True
        try:
            result.reclaimed_bytes += cache.clear(
                progress=report.tree if report else None
            )
        except DeleteError as exc:
            result.refused.append(f"{cache.label}: {exc}")
            return False
        except OSError as exc:
            result.failed.append(f"{cache.label}: {exc.strerror or exc}")
            return False
        return True

    def _stop_steam(self) -> bool:
        """Stop Steam and its service so games are not locked. Best-effort."""
        stopped = False
        service = self._runner.try_run(
            ["sc", "stop", _STEAM_SERVICE], timeout_s=20
        )
        stopped |= bool(service and service.ok)
        for name in _STEAM_PROCESSES:
            killed = self._runner.try_run(
                ["taskkill", "/F", "/IM", f"{name}.exe"], timeout_s=20
            )
            stopped |= bool(killed and killed.ok)
        if stopped:
            # Give Windows a moment to release the file handles.
            time.sleep(1.5)
        return stopped


__all__ = ["SteamWiper", "WipePlan", "WipeResult", "CacheTarget"]
