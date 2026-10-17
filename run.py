"""Frozen-executable entry point for Pudge Cleaner.

PyInstaller bundles this module rather than the package's ``__main__``, so
its relative imports do not have to be resolved from a frozen ``__main__``.

Setting ``PGM_SELFTEST=1`` constructs the dashboard off-screen, pumps the
event loop briefly and exits 0. It is how the build is smoke-tested without a
UAC prompt or a visible window: if a hidden import is missing, Qt fails to
load, or a module was left out of the bundle, this exits non-zero.
"""

from __future__ import annotations

import os
import sys


def _selftest() -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    from pudge_gaming_manager.app.gui.dashboard import Dashboard
    from pudge_gaming_manager.app.gui.resources import logo_path

    # The scans read WMI through pywin32's COM bindings; a bundle missing
    # them would fall back to PowerShell silently and lose three seconds.
    import time

    from pudge_gaming_manager.utilities import wmi

    started = time.perf_counter()
    rows = wmi.query({"cpu": ("root/cimv2", "SELECT Name FROM Win32_Processor")})
    assert rows["cpu"], "in-process WMI returned no processor"
    print(f"WMI in-process OK ({time.perf_counter() - started:.2f}s)")

    # The nightly schedule goes through the same kind of COM binding. Reading
    # needs no elevation and changes nothing; "no task" is a valid answer, a
    # bundle missing the scheduler's COM support is not.
    from pudge_gaming_manager.windows.schedule import task

    info = task.query()
    print(f"Task Scheduler COM OK (nightly installed: {info.installed})")

    # The «Ядра GPU» benchmark programs must be in the bundle, unaltered, and
    # the update check needs Cryptodome's compiled Ed25519 -- a bundle
    # missing either would only fail when someone pressed the button.
    from Cryptodome.PublicKey import ECC
    from Cryptodome.Signature import eddsa

    from pudge_gaming_manager.core.gpu_affinity import tools
    from pudge_gaming_manager.core.update.signature import signed_message, verify_update

    tools.locate()
    key = ECC.generate(curve="Ed25519")
    digest = "0" * 64
    signature = eddsa.new(key, "rfc8032").sign(signed_message("1.0.0", digest)).hex()
    verify_update(key.public_key().export_key(format="raw").hex(), "1.0.0", digest, signature)
    print("Benchmark tools and update signature OK")

    app = QApplication([])
    window = Dashboard()
    window.show()
    assert not QIcon(logo_path()).isNull(), "bundled logo failed to load"
    QTimer.singleShot(2500, app.quit)
    app.exec()
    window.close()
    print("PGM selftest OK")
    return 0


def main() -> int:
    if os.environ.get("PGM_SELFTEST") == "1":
        return _selftest()
    from pudge_gaming_manager.app.main import main as app_main

    return app_main()


if __name__ == "__main__":
    sys.exit(main())
