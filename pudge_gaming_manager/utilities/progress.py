"""What a long operation tells the window while it works.

One shape for every slow action that shows a bar: optimization, disk
cleanup, the Steam reset, moving or deleting a folder. It lives in
``utilities`` because the code that knows how far it has got sits in
``windows`` and ``games``, below the layer that draws the bar.

A report is a line of text and, when the operation can tell, how far along
it is. ``None`` means "working, cannot say": the window shows a busy bar
rather than a number nobody measured.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from .logging_setup import get_logger

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Progress:
    """Where an operation has got to."""

    text: str
    fraction: float | None = None
    """0..1 when the operation can tell; ``None`` is "working"."""


ProgressCallback = Callable[[Progress], None]


def guarded(progress: ProgressCallback) -> ProgressCallback:
    """``progress``, logging its own failures instead of raising them.

    A status line that failed to update must never abort a change between
    its apply and its verification, or stop a delete half way through a tree.
    """

    def send(event: Progress) -> None:
        try:
            progress(event)
        except Exception:  # noqa: BLE001 - see above
            _log.exception("progress report failed")

    return send


def fraction_of(done: float, total: float) -> float | None:
    """``done / total`` clamped to 0..1, or ``None`` when there is no total.

    Totals come from an earlier scan and the files may have grown since, so
    the bar is clamped rather than trusted to stop at 100%.
    """
    if total <= 0:
        return None
    return max(0.0, min(done / total, 1.0))


class Throttle:
    """Lets through at most one report per interval.

    Deleting a file takes ~400 us; reporting each one would cost more than
    the delete. ``ready`` is true at most once per ``interval`` seconds.
    """

    def __init__(self, interval: float = 0.25) -> None:
        self._interval = interval
        self._last = time.monotonic()

    def ready(self) -> bool:
        now = time.monotonic()
        if now - self._last >= self._interval:
            self._last = now
            return True
        return False


__all__ = ["Progress", "ProgressCallback", "Throttle", "fraction_of", "guarded"]
