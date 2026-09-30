"""What the operator may delete or move, and what is refused outright.

The cleaner's protected-name table is written for *unattended* cleanup and
closes Program Files, ``Documents`` and every launcher. Here a person is
choosing, and the folders they choose most are exactly those: a Steam
library to move, a game to delete. So that table is used only for what is
never the operator's to touch — club software, anti-cheat, credentials — and
the rest is refused by a short list of what would break the machine:

* the Windows folder and everything in it;
* the profile, Program Files, ProgramData and AppData themselves, whole;
* Windows and Microsoft components inside them (``WindowsApps``,
  ``Common Files``, ``ProgramData\\Microsoft``, ``AppData\\...\\Microsoft``,
  ``Packages``), which hold credentials, the shell and Defender;
* ``Temp``, which the running system expects to exist.

The list is a denylist and so is never complete. It covers what cannot be
put back; anything else the operator chooses is theirs to choose, behind a
confirmation that names the folder and says it cannot be undone.

Everything here reads names and attributes; nothing is changed.
"""

from __future__ import annotations

import os
import pathlib

from ..utilities.secure_delete import _is_reparse_point
from .cleanup.rules import component_is_protected, within

#: Fragments of the cleaner's protected-name table a person may knowingly
#: act on: launchers, and their own files. Everything else in that table —
#: club software, anti-cheat, credentials — still applies.
_OPERATOR_MAY_TOUCH = frozenset({
    "steamapps", "steamlibrary", "epic games", "riot games", "battle.net",
    "gog galaxy",
    "documents", "desktop", "pictures", "videos", "music", "onedrive",
    "saved games", "my games",
})

#: Direct children of Program Files that belong to Windows.
_PROGRAM_FILES_SYSTEM = frozenset({
    "common files", "dotnet", "internet explorer", "modifiablewindowsapps",
    "msbuild", "reference assemblies", "uninstall information",
})
_PROGRAM_DATA_SYSTEM = frozenset({
    "package cache", "packages", "usoprivate", "usoshared",
})
_APP_DATA_SYSTEM = frozenset({"packages", "temp"})

#: Names that, as a prefix, mean a Windows or Microsoft component.
_SYSTEM_PREFIXES = ("windows", "microsoft")




def _env(name: str) -> pathlib.Path | None:
    value = os.environ.get(name)
    return pathlib.Path(value) if value else None


def resolved(path: pathlib.Path) -> pathlib.Path:
    try:
        return path.resolve(strict=False)
    except (OSError, ValueError, RuntimeError):
        return path


def _first_below(path: pathlib.Path, base: pathlib.Path) -> str | None:
    """Lower-cased name of ``path``'s first component below ``base``."""
    if not within(path, base) or os.path.normcase(str(path)) == os.path.normcase(str(base)):
        return None
    return path.parts[len(base.parts)].lower()


def _is_system_name(name: str, names: frozenset[str]) -> bool:
    return name in names or name.startswith(_SYSTEM_PREFIXES)


def _policy_reason(path: pathlib.Path) -> str | None:
    """Why this path may not be touched, judged on its name alone."""
    if len(path.parts) < 3:
        return "это корень диска или папка верхнего уровня"

    windows = _env("SystemRoot")
    if windows and (within(path, windows) or within(windows, path)):
        return "системная папка Windows"

    # The path is, or contains, one of these: deleting it would take the
    # whole of it, and everything Windows keeps there, along.
    for name in (
        "USERPROFILE", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432",
        "ProgramData", "APPDATA", "LOCALAPPDATA",
    ):
        base = _env(name)
        if base and within(base, path):
            return "эту папку нельзя трогать целиком: в ней живёт система"

    for name, system in (
        ("ProgramFiles", _PROGRAM_FILES_SYSTEM),
        ("ProgramFiles(x86)", _PROGRAM_FILES_SYSTEM),
        ("ProgramW6432", _PROGRAM_FILES_SYSTEM),
        ("ProgramData", _PROGRAM_DATA_SYSTEM),
        ("APPDATA", _APP_DATA_SYSTEM),
        ("LOCALAPPDATA", _APP_DATA_SYSTEM),
    ):
        base = _env(name)
        first = _first_below(path, base) if base else None
        if first is not None and _is_system_name(first, system):
            return "компонент Windows или Microsoft: его удаление ломает систему"

    for part in path.parts[1:]:
        if component_is_protected(part, _OPERATOR_MAY_TOUCH):
            return (
                "защищено: клубное ПО, античит или учётные данные — "
                "этого чистильщик не трогает ни при каких условиях"
            )
    return None


def block_reason(path: pathlib.Path | str) -> str | None:
    """Why ``path`` may not be deleted or moved, or ``None`` when it may.

    The path is judged both as given and as it resolves, so a folder reached
    through a junction is not judged by where it pretends to be.
    """
    folder = pathlib.Path(path)
    try:
        if _is_reparse_point(folder):
            return "это ссылка (junction или symlink), а не папка"
        if not folder.is_dir():
            return "это не папка или её уже нет"
    except OSError:
        return "папка недоступна"
    for candidate in (folder, resolved(folder)):
        reason = _policy_reason(candidate)
        if reason:
            return reason
    return None


__all__ = ["block_reason", "resolved"]
