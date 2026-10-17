"""Application entry point."""

from __future__ import annotations

import sys

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from .. import __version__
from ..core.nightly import NIGHTLY_ARGUMENT
from ..core.update.updater import remove_leftovers
from ..utilities import single_instance
from ..utilities.logging_setup import setup_logging
from .gui.dashboard import Dashboard
from .gui.resources import logo_path
from .gui.widgets import KeyboardFocusRing


def main(argv: list[str] | None = None) -> int:
    """Start the administrator GUI, or the windowless nightly run."""
    if NIGHTLY_ARGUMENT in (argv if argv is not None else sys.argv)[1:]:
        from .nightly import main as nightly_main

        return nightly_main()
    setup_logging()
    # An update leaves the previous program beside the new one; it is
    # deletable only once that process has ended, which it has by now.
    remove_leftovers()

    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("Pudge Cleaner")
    app.setApplicationVersion(__version__)
    app.setWindowIcon(QIcon(logo_path()))
    # Parented to the app so it lives as long as the event loop.
    app.installEventFilter(KeyboardFocusRing(app))

    # Seen by the nightly run, which stands aside while this window is open.
    single_instance.hold(single_instance.GUI_MUTEX)

    window = Dashboard()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
