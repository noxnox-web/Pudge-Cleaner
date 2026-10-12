"""Entry point of the windowless nightly run (``PudgeCleaner.exe --nightly``).

Task Scheduler starts it. No Qt application is created: nothing here draws
anything, and a window flashing on a club PC at five in the morning is
exactly what this exists to avoid.
"""

from __future__ import annotations

import pathlib
import sys

from ..core import nightly
from ..utilities import low_impact
from ..utilities.logging_setup import setup_logging


def main() -> int:
    # Windowed builds have no stderr to write to.
    setup_logging(console=False)
    low_impact.lower_priority()
    report = nightly.run()
    return 0 if report.ok else 1


def launch_command() -> tuple[str, str]:
    """The program file and arguments Task Scheduler should start.

    The packaged exe is its own command. From source it is the interpreter
    running ``run.py``, which is how a developer's checkout schedules itself.
    """
    if getattr(sys, "frozen", False):
        return sys.executable, nightly.NIGHTLY_ARGUMENT
    run_py = pathlib.Path(__file__).resolve().parents[2] / "run.py"
    return sys.executable, f'"{run_py}" {nightly.NIGHTLY_ARGUMENT}'
