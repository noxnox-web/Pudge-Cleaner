"""The result table of the «Ядра GPU» tab, and the sentences under it.

Split from the tab so the wording (which carries the honesty about noise) is
a pure function that can be tested without a window.
"""

from __future__ import annotations

from typing import Sequence

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from ...core.gpu_affinity import selection
from ...core.gpu_affinity.scan import ScanOutcome
from . import theme

#: Spread between best and worst core below which the difference is within
#: what run-to-run noise produces. A heuristic, labelled as one on screen.
NOISE_PERCENT = 3.0

_COLUMNS = ("Ядро", "Среднее", "1 % low", "0.1 % low", "Мин", "Макс", "σ")
_CPU = Qt.ItemDataRole.UserRole + 1


def _fps(value: float) -> str:
    return f"{value:,.0f}".replace(",", " ")


def summary_text(ranked: Sequence[selection.CoreResult], outcome: ScanOutcome) -> str:
    """What the measurement showed, in plain sentences."""
    lines: list[str] = []
    if ranked:
        spread = selection.spread_percent(ranked)
        lines.append(
            f"★ Лучшее по среднему, 1 % и 0.1 % low вместе — CPU {ranked[0].cpu}. "
            f"Среднее у лучшего и худшего ядра отличается на {spread:.1f} %."
        )
        if spread < NOISE_PERCENT:
            lines.append(
                f"Это меньше {NOISE_PERCENT:.0f} % (эвристика): в пределах шума замера. "
                "Скорее всего, ядро не важно — менять ничего не обязательно."
            )
    else:
        lines.append("Ни одно ядро не удалось измерить.")
    if outcome.failures:
        lines.append(
            "Не измерено: " + "; ".join(f"CPU {cpu} — {why}" for cpu, why in outcome.failures)
        )
    if outcome.cancelled:
        lines.append("Замер остановлен — в таблице только то, что успело измериться.")
    return "\n".join(lines)


class ResultsTable(QTableWidget):
    """One row per measured core, best first; the selected row is the choice."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(0, len(_COLUMNS), parent)
        self.setHorizontalHeaderLabels(_COLUMNS)
        self.verticalHeader().hide()
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        header = self.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.setColumnWidth(0, 110)

    def show_results(self, ranked: Sequence[selection.CoreResult]) -> None:
        self.setRowCount(len(ranked))
        for row, result in enumerate(ranked):
            s = result.stats
            cells = (
                f"CPU {result.cpu}" + ("  ★" if row == 0 else ""),
                _fps(s.average), _fps(s.low_1), _fps(s.low_01),
                _fps(s.minimum), _fps(s.maximum), f"{s.stdev:.0f}",
            )
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if column:
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                if row == 0 and column == 0:
                    item.setForeground(QBrush(QColor(theme.GOOD)))
                item.setData(_CPU, result.cpu)
                self.setItem(row, column, item)
        if ranked:
            self.selectRow(0)

    def selected_cpu(self) -> int | None:
        rows = self.selectionModel().selectedRows()
        if not rows:
            return None
        item = self.item(rows[0].row(), 0)
        return None if item is None else item.data(_CPU)


__all__ = ["NOISE_PERCENT", "ResultsTable", "summary_text"]
