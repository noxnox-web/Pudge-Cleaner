"""What an optimization run tells the window while it works.

Most changes finish in milliseconds, but removing ``Windows.old`` deletes
hundreds of thousands of files and takes minutes. The engine names the
change that is running; a long change can add how far it has got through
:meth:`.tweak.TweakContext.report_progress`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ...utilities.logging_setup import get_logger

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ApplyProgress:
    """Where a run has got to, for the window's status line."""

    text: str
    fraction: float | None = None
    """0..1 of the current step when the step can tell; ``None`` is "working"."""


ProgressCallback = Callable[[ApplyProgress], None]


def guarded_progress(progress: ProgressCallback) -> ProgressCallback:
    """``progress``, logging its own failures instead of raising them.

    A status line that failed to update must never abort a change between
    its apply and its verification.
    """

    def send(event: ApplyProgress) -> None:
        try:
            progress(event)
        except Exception:  # noqa: BLE001 - see above
            _log.exception("progress report failed")

    return send


def step_reporter(
    send: ProgressCallback, prefix: str
) -> Callable[[float | None, str], None]:
    """What ``TweakContext.progress`` holds while one change runs."""

    def report(fraction: float | None, detail: str = "") -> None:
        send(ApplyProgress(f"{prefix} — {detail}" if detail else prefix, fraction))

    return report


__all__ = ["ApplyProgress", "ProgressCallback", "guarded_progress", "step_reporter"]
