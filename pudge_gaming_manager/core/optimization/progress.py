"""How one change in an optimization run reports progress.

Most changes finish in milliseconds, but removing ``Windows.old`` deletes
hundreds of thousands of files and takes minutes. The engine names the
change that is running; a long change can add how far it has got through
:meth:`.tweak.TweakContext.report_progress`. The report type itself is shared
with every other long operation: :mod:`...utilities.progress`.
"""

from __future__ import annotations

from typing import Callable

from ...utilities.progress import Progress, ProgressCallback


def step_reporter(
    send: ProgressCallback, prefix: str
) -> Callable[[float | None, str], None]:
    """What ``TweakContext.progress`` holds while one change runs."""

    def report(fraction: float | None, detail: str = "") -> None:
        send(Progress(f"{prefix} — {detail}" if detail else prefix, fraction))

    return report


__all__ = ["step_reporter"]
