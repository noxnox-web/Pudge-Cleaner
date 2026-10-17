"""Small HTTPS reads and downloads, for the self-update.

The program is otherwise offline, so this is deliberately narrow: HTTPS only,
a timeout on every call, a ceiling on how much is read, and a typed error
instead of a urllib traceback. Which hosts are acceptable is the caller's
decision; the check lives next to the code that knows its own repository.
"""

from __future__ import annotations

import json
import pathlib
import urllib.error
import urllib.request
from typing import Any, Callable

from .exceptions import PgmError

_TIMEOUT_S = 20
_CHUNK = 1 << 16
_HEADERS = {
    "User-Agent": "PudgeCleaner-updater",
    "Accept": "application/vnd.github+json, application/octet-stream, */*",
}


class FetchError(PgmError):
    """A network read failed."""


def _open(url: str):
    if not url.startswith("https://"):
        raise FetchError("Не удалось обратиться к серверу", f"«{url}» — не HTTPS-адрес.")
    request = urllib.request.Request(url, headers=_HEADERS)  # noqa: S310 - https only, checked above
    try:
        return urllib.request.urlopen(request, timeout=_TIMEOUT_S)  # noqa: S310
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", None) or exc
        raise FetchError("Не удалось связаться с сервером обновлений", str(reason)) from exc


def read_text(url: str, limit: int = 1 << 20) -> str:
    """The body as text, at most ``limit`` bytes."""
    with _open(url) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise FetchError("Ответ сервера слишком большой", f"больше {limit} байт")
    return data.decode("utf-8", errors="replace")


def get_json(url: str) -> Any:
    try:
        return json.loads(read_text(url))
    except json.JSONDecodeError as exc:
        raise FetchError("Сервер вернул не JSON", str(exc)) from exc


def download(
    url: str,
    destination: pathlib.Path,
    progress: Callable[[float | None], None],
    limit: int,
) -> None:
    """Write the body to ``destination``. ``progress`` gets 0..1, or ``None``
    when the server does not say how long the file is. Raises past ``limit``."""
    try:
        with _open(url) as response, open(destination, "wb") as out:
            length = int(response.headers.get("Content-Length") or 0)
            if length > limit:
                raise FetchError("Файл обновления слишком большой", f"{length} байт")
            done = 0
            while chunk := response.read(_CHUNK):
                done += len(chunk)
                if done > limit:
                    raise FetchError("Файл обновления слишком большой", f"больше {limit} байт")
                out.write(chunk)
                progress(done / length if length else None)
    except OSError as exc:
        if isinstance(exc, FetchError):
            raise
        raise FetchError("Не удалось скачать файл обновления", str(exc.strerror or exc)) from exc


__all__ = ["FetchError", "download", "get_json", "read_text"]
