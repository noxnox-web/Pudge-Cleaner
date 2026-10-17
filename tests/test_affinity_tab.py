"""Tests for the «Ядра GPU» tab and its controller.

The service is faked, so no test restarts a driver, writes the registry or
starts a benchmark. These cover what the operator can see and be hurt by:
buttons that stay off while something blocks them, a choice that follows the
selected row, confirmations that can be declined, and a window that refuses
to close while the machine is being changed.
"""

from __future__ import annotations

import os
import threading
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QMessageBox

from pudge_gaming_manager.app.controllers.affinity_controller import AffinityController
from pudge_gaming_manager.app.gui.affinity_results import NOISE_PERCENT, summary_text
from pudge_gaming_manager.app.gui.affinity_tab import AffinityTab
from pudge_gaming_manager.core.benchmark.frametime import FrameStats
from pudge_gaming_manager.core.gpu_affinity import selection
from pudge_gaming_manager.core.gpu_affinity.scan import ScanOutcome
from pudge_gaming_manager.core.gpu_affinity.selection import CoreResult
from pudge_gaming_manager.core.gpu_affinity.service import AdapterInfo, Overview
from pudge_gaming_manager.core.optimization.tweak import Outcome
from pudge_gaming_manager.utilities.progress import Progress
from pudge_gaming_manager.windows.devices.affinity import AffinityPolicy

ADAPTERS = (("RTX 3060", r"PCI\VEN_10DE&DEV_2504\4&1&0&0008"),)


def _wait(until, timeout_ms: int = 5000) -> None:
    loop = QEventLoop()
    timer = QTimer()
    timer.timeout.connect(lambda: until() and loop.quit())
    timer.start(10)
    QTimer.singleShot(timeout_ms, loop.quit)
    loop.exec()
    timer.stop()


def _result(cpu: int, average: float, low_1: float = 100, low_01: float = 50) -> CoreResult:
    return CoreResult(cpu, FrameStats(1000, average, 10, average * 1.2, 5, low_1, low_01))


def _overview(blockers: tuple[str, ...] = (), msi_on: bool | None = True) -> Overview:
    info = AdapterInfo(
        ADAPTERS[0][0], ADAPTERS[0][1], AffinityPolicy(),
        "включён" if msi_on else "не задано", msi_on,
    )
    return Overview((info,), (2, 4, 6), "CPU 0–1 — основное ядро.", blockers)


class FakeService:
    def __init__(self, overview: Overview | None = None) -> None:
        self.overview_value = overview or _overview()
        self.release = threading.Event()
        self.block_scan = False
        self.scans: list = []
        self.planned: list = []
        self.applied: list = []
        self.restarted: list = []

    def overview(self):
        return self.overview_value

    def scan(self, adapters, settings, *, progress, should_stop):
        self.scans.append((list(adapters), settings))
        progress(Progress("CPU 2: замер", 0.4))
        if self.block_scan:
            self.release.wait(5)
        return ScanOutcome(results=(_result(2, 900), _result(4, 1100), _result(6, 1000)))

    def plan(self, adapters, cpu):
        self.planned.append((list(adapters), cpu))
        change = types.SimpleNamespace(summary=f"закрепить за CPU {cpu}", skip_reason="")
        return types.SimpleNamespace(applicable=[change], changes=[change])

    def apply(self, plan):
        self.applied.append(plan)
        result = types.SimpleNamespace(outcome=Outcome.SUCCESS, detail="Закреплено.")
        return types.SimpleNamespace(results=[result]), []

    def restart_adapters(self, adapters, progress):
        self.restarted.append(list(adapters))
        return []


@pytest.fixture()
def boxes(monkeypatch: pytest.MonkeyPatch):
    """Every QMessageBox call recorded; questions answered ``answer["yes"]``."""
    shown: list[tuple[str, str]] = []
    answer = {"yes": True}

    def record(kind):
        def show(_parent, title, text, *rest, **kw):
            shown.append((kind, title))
            return None
        return staticmethod(show)

    def question(_parent, title, text, *rest, **kw):
        shown.append(("question", title))
        return QMessageBox.StandardButton.Yes if answer["yes"] else QMessageBox.StandardButton.No

    for kind in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, kind, record(kind))
    monkeypatch.setattr(QMessageBox, "question", staticmethod(question))
    return types.SimpleNamespace(shown=shown, answer=answer)


def _tab(app, service: FakeService) -> AffinityTab:
    tab = AffinityTab(controller=AffinityController(service=service))
    tab.show()
    _wait(lambda: tab._overview is not None)
    return tab


def _scanned(tab: AffinityTab) -> None:
    tab._on_start()
    _wait(lambda: tab._outcome is not None)


def test_the_tab_lists_the_adapter_and_offers_a_measurement(app, boxes) -> None:
    tab = _tab(app, FakeService())

    assert len(tab._checks) == 1
    assert tab._start.isEnabled()
    assert not tab._apply.isEnabled() and not tab._restart.isEnabled()
    assert "CPU 2, 4, 6" in tab._estimate.text()


def test_a_blocker_keeps_the_measurement_off_and_is_shown(app, boxes) -> None:
    tab = _tab(app, FakeService(_overview(("Нужны права администратора.",))))

    assert not tab._start.isEnabled()
    assert "администратора" in tab._blockers.text()


def test_no_ticked_adapter_means_nothing_to_measure(app, boxes) -> None:
    tab = _tab(app, FakeService())

    tab._checks[0][0].setChecked(False)

    assert not tab._start.isEnabled()


def test_a_disabled_msi_is_called_out(app, boxes) -> None:
    tab = _tab(app, FakeService(_overview(msi_on=False)))

    texts = [label.text() for label in tab.findChildren(type(tab._estimate))]
    assert any("MSI не включён" in t for t in texts)


def test_declining_the_warning_starts_nothing(app, boxes) -> None:
    service = FakeService()
    tab = _tab(app, service)
    boxes.answer["yes"] = False

    tab._on_start()

    assert service.scans == []


def test_a_measurement_fills_the_table_best_first_and_selects_it(app, boxes) -> None:
    service = FakeService()
    tab = _tab(app, service)

    _scanned(tab)

    assert [tab._table.item(r, 0).text().split()[1] for r in range(3)] == ["4", "6", "2"]
    assert "★" in tab._table.item(0, 0).text()
    assert tab._selected_cpu() == 4
    assert tab._apply.isEnabled() and "CPU 4" in tab._apply.text()
    assert service.scans[0][1].benchmark_s == tab._seconds.value()


def test_the_choice_follows_the_selected_row_not_the_best(app, boxes) -> None:
    service = FakeService()
    tab = _tab(app, service)
    _scanned(tab)

    tab._table.selectRow(2)
    tab._on_apply()
    _wait(lambda: service.applied)

    assert service.planned == [(list(ADAPTERS), 2)]


def test_applying_asks_first_and_enables_the_restart(app, boxes) -> None:
    service = FakeService()
    tab = _tab(app, service)
    _scanned(tab)

    tab._on_apply()
    _wait(lambda: service.applied)
    _wait(lambda: tab._restart.isEnabled())

    assert ("question", "Закрепить прерывания видеокарты?") in boxes.shown
    assert tab._restart.isEnabled()


def test_declining_the_preview_applies_nothing(app, boxes) -> None:
    service = FakeService()
    tab = _tab(app, service)
    _scanned(tab)
    boxes.answer["yes"] = False

    tab._on_apply()
    _wait(lambda: service.planned)
    _wait(lambda: not tab._controller.busy)

    assert service.applied == [] and not tab._restart.isEnabled()


def test_restarting_the_driver_asks_first(app, boxes) -> None:
    service = FakeService()
    tab = _tab(app, service)
    _scanned(tab)
    tab._on_apply()
    _wait(lambda: tab._restart.isEnabled())
    boxes.answer["yes"] = False

    tab._on_restart()

    assert service.restarted == []

    boxes.answer["yes"] = True
    tab._on_restart()
    _wait(lambda: service.restarted)

    assert service.restarted == [list(ADAPTERS)]


def test_the_window_may_not_close_while_a_measurement_runs(app, boxes) -> None:
    service = FakeService()
    service.block_scan = True
    tab = _tab(app, service)

    assert tab.blocks_close() is None
    tab._on_start()
    _wait(lambda: service.scans)

    assert tab.blocks_close() is not None
    assert tab._stop.isEnabled() and not tab._start.isEnabled()

    service.release.set()
    _wait(lambda: tab._outcome is not None)

    assert tab.blocks_close() is None


def test_stop_reaches_the_running_scan(app, boxes) -> None:
    seen = {}

    class Stoppable(FakeService):
        def scan(self, adapters, settings, *, progress, should_stop):
            self.scans.append(1)
            for _ in range(500):
                if should_stop():
                    seen["stopped"] = True
                    break
                threading.Event().wait(0.01)
            return ScanOutcome(cancelled=True)

    service = Stoppable()
    tab = _tab(app, service)
    tab._on_start()
    _wait(lambda: service.scans)

    tab._on_stop()
    _wait(lambda: tab._outcome is not None)

    assert seen.get("stopped") and tab._outcome.cancelled


# -- the words ----------------------------------------------------------------------------


def test_a_tiny_spread_is_called_noise() -> None:
    ranked = selection.ranked([_result(2, 1000), _result(4, 1010)])

    text = summary_text(ranked, ScanOutcome(results=tuple(ranked)))

    assert "в пределах шума" in text and f"{NOISE_PERCENT:.0f} %" in text


def test_a_real_spread_is_not_called_noise() -> None:
    ranked = selection.ranked([_result(2, 800), _result(4, 1000)])

    assert "шума" not in summary_text(ranked, ScanOutcome(results=tuple(ranked)))


def test_failures_and_a_stop_are_stated() -> None:
    outcome = ScanOutcome(failures=((2, "сцена закрылась"),), cancelled=True)

    text = summary_text([], outcome)

    assert "сцена закрылась" in text and "остановлен" in text
