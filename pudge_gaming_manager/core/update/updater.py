"""Find a newer release on GitHub, download it, and replace this program.

Releases live on the project's own page. Each carries
``PudgeCleaner-<version>.exe`` and ``PudgeCleaner-<version>.exe.sig`` -- the
publisher's signature of that version and file (see ``signature.py``).
Nothing is installed unless the signature checks out against the key this
build carries, so write access to the releases page is not enough to ship
code to installed copies.

Replacing a running program
---------------------------
Windows will not overwrite a running ``.exe`` but will *rename* one. So the
new file is downloaded beside the program, and then: running exe -> ``.old``,
new -> the running exe's name, start it, and exit. The name stays the same, so
the nightly task and shortcuts keep pointing at it. The ``.old`` file is
deleted by the next start (it is still locked until this process ends). The
new process is started from an elevated one and needs no second prompt.

Only the packaged program updates itself. A source checkout still reports
that a release exists, and the page to get it from.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import re
import sys
from dataclasses import dataclass
from typing import Any, Callable

from ... import __version__
from ...utilities import fetch
from ...utilities.command_runner import CommandRunner
from ...utilities.exceptions import PgmError
from ...utilities.logging_setup import audit_event, get_logger
from ...utilities.progress import Progress, ProgressCallback
from .key import UPDATE_PUBLIC_KEY
from .signature import VERSION, UpdateSignatureError, verify_update

_log = get_logger(__name__)

REPO = "noxnox-web/Pudge-Cleaner"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"
# The list, not ``/releases/latest``: that endpoint skips pre-releases, and
# every release so far has been one.
_RELEASES_URL = f"https://api.github.com/repos/{REPO}/releases?per_page=20"
# Only GitHub's own download address for this repository: the asset URL comes
# from the API response, and nothing from anywhere else is ever run.
_DOWNLOAD_PREFIX = f"https://github.com/{REPO}/releases/download/"
_ASSET = re.compile(rf"^PudgeCleaner-({VERSION})\.exe$")
_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([a-z]+)\.(\d+))?$")
_MAX_BYTES = 300 * 1024 * 1024


def parse_version(text: str) -> tuple | None:
    """A sortable key, or ``None``. A pre-release sorts before its release."""
    match = _VERSION.match(text.strip())
    if not match:
        return None
    major, minor, patch, label, number = match.groups()
    return (int(major), int(minor), int(patch), 0 if label else 1, label or "", int(number or 0))


@dataclass(frozen=True, slots=True)
class Release:
    version: str
    notes: str
    url: str
    signature_url: str
    size: int
    page: str


@dataclass(frozen=True, slots=True)
class UpdateInfo:
    current: str
    latest: Release | None = None
    available: bool = False
    can_install: bool = False
    page: str = RELEASES_PAGE
    """Where to get it by hand, for a copy that cannot install by itself."""

    checked: bool = True
    """False when the check could not reach the server."""


def running_exe() -> pathlib.Path | None:
    """The packaged program's own file, or ``None`` from a source checkout."""
    return pathlib.Path(sys.executable) if getattr(sys, "frozen", False) else None


def remove_leftovers(exe: pathlib.Path | None = None) -> None:
    """Delete what a previous update left beside the program. Best effort."""
    exe = exe or running_exe()
    if exe is None:
        return
    for suffix in (".old", ".new"):
        try:
            exe.with_name(exe.name + suffix).unlink(missing_ok=True)
        except OSError:
            _log.debug("could not remove %s%s yet", exe.name, suffix)


def _best(releases: Any) -> Release | None:
    """The newest release that carries both an installer and its signature."""
    if not isinstance(releases, list):
        return None
    best: tuple[tuple, Release] | None = None
    for release in releases:
        if not isinstance(release, dict) or release.get("draft"):
            continue
        assets = {a.get("name"): a for a in release.get("assets", []) if isinstance(a, dict)}
        for name, asset in assets.items():
            match = _ASSET.match(name or "")
            signature = assets.get(f"{name}.sig")
            key = parse_version(match.group(1)) if match else None
            if key is None or signature is None:
                continue
            if best is None or key > best[0]:
                best = (key, Release(
                    version=match.group(1),
                    notes=str(release.get("body") or ""),
                    url=str(asset.get("browser_download_url") or ""),
                    signature_url=str(signature.get("browser_download_url") or ""),
                    size=int(asset.get("size") or 0),
                    page=str(release.get("html_url") or RELEASES_PAGE),
                ))
    return best[1] if best else None


def _sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


class Updater:
    def __init__(
        self,
        *,
        current: str = __version__,
        public_key: str = UPDATE_PUBLIC_KEY,
        exe: pathlib.Path | None | str = "running",
        get_json: Callable[[str], Any] = fetch.get_json,
        read_text: Callable[[str], str] = fetch.read_text,
        download: Callable[..., None] = fetch.download,
        spawn: Callable[[list[str]], Any] | None = None,
    ) -> None:
        self._current = current
        self._public_key = public_key
        self._exe = running_exe() if exe == "running" else exe
        self._get_json, self._read_text, self._download = get_json, read_text, download
        self._spawn = spawn or CommandRunner().spawn

    @property
    def can_install(self) -> bool:
        return self._exe is not None and bool(self._public_key)

    # -- checking -------------------------------------------------------------

    def check(self) -> UpdateInfo:
        """Ask GitHub for the newest release. Never raises: offline is just "no news"."""
        try:
            latest = _best(self._get_json(_RELEASES_URL))
        except PgmError as exc:
            _log.info("update check failed: %s", exc.reason or exc.what)
            return UpdateInfo(self._current, can_install=self.can_install, checked=False)
        current, newest = parse_version(self._current), latest and parse_version(latest.version)
        available = bool(current and newest and newest > current)
        return UpdateInfo(
            self._current, latest, available, self.can_install,
            latest.page if latest else RELEASES_PAGE,
        )

    # -- installing -------------------------------------------------------------

    def install(
        self,
        release: Release,
        progress: ProgressCallback,
        blocker: Callable[[], str | None] = lambda: None,
    ) -> None:
        """Download, verify, replace this program and start the new one.

        Raises :class:`PgmError` with nothing replaced, except when only the
        final start failed -- then the new version is in place and the message
        says to start it by hand.
        """
        by_hand = f"Скачайте новую версию вручную: {RELEASES_PAGE}"
        exe = self._exe
        if exe is None:
            raise PgmError(
                "Обновиться изнутри нельзя",
                "Это запуск из исходников, а не собранная программа.", remedy=by_hand,
            )
        if not self._public_key:
            raise PgmError(
                "Обновиться изнутри нельзя",
                "В эту сборку не вшит ключ проверки обновлений.", remedy=by_hand,
            )
        if not (
            release.url.startswith(_DOWNLOAD_PREFIX)
            and release.signature_url.startswith(_DOWNLOAD_PREFIX)
        ):
            raise PgmError(
                "Обновление отклонено", "Адрес файла ведёт не на страницу релизов проекта."
            )

        # The small file first: an unreadable one is known before 40 MB are downloaded.
        signature = self._read_text(release.signature_url)
        staged = exe.with_name(exe.name + ".new")

        def report(fraction: float | None) -> None:
            progress(Progress(f"Скачиваю версию {release.version}…", fraction))

        try:
            self._download(release.url, staged, report, _MAX_BYTES)
            progress(Progress("Проверяю подпись…", None))
            try:
                verify_update(self._public_key, release.version, _sha256(staged), signature)
            except UpdateSignatureError as exc:
                _log.warning("update %s refused: %s", release.version, exc)
                raise PgmError(
                    "Обновление отклонено",
                    "Подпись не совпала: файл не от издателя или повреждён.",
                    remedy=f"Ничего не изменено. {by_hand}",
                ) from exc
            why = blocker()
            if why:
                raise PgmError(
                    "Обновление отложено", why, remedy="Повторите, когда операция закончится."
                )
        except BaseException:
            staged.unlink(missing_ok=True)
            raise

        progress(Progress("Заменяю программу…", None))
        old = exe.with_name(exe.name + ".old")
        try:
            old.unlink(missing_ok=True)
            os.replace(exe, old)
        except OSError as exc:
            staged.unlink(missing_ok=True)
            raise PgmError("Не удалось заменить программу", str(exc.strerror or exc)) from exc
        try:
            os.replace(staged, exe)
        except OSError as exc:
            os.replace(old, exe)  # put the running program back under its name
            staged.unlink(missing_ok=True)
            raise PgmError("Не удалось заменить программу", str(exc.strerror or exc)) from exc
        audit_event(
            "update", "install", target=str(exe), old_state=self._current,
            new_state=release.version, result="SUCCESS",
        )

        try:
            self._spawn([str(exe)])
        except PgmError as exc:
            raise PgmError(
                "Программа обновлена, но не запустилась",
                exc.reason or exc.what,
                remedy="Закройте это окно и запустите Pudge Cleaner снова.",
            ) from exc


__all__ = [
    "RELEASES_PAGE", "Release", "UpdateInfo", "Updater",
    "parse_version", "remove_leftovers", "running_exe",
]
