"""Small building blocks shared by the dashboard's cards."""

from __future__ import annotations

from typing import Sequence

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ...core.types import Issue
from . import presenters, theme

_UNAVAILABLE = presenters.UNAVAILABLE

#: Focus reasons that mean the operator is driving with the keyboard.
_KEYBOARD_REASONS = (
    Qt.FocusReason.TabFocusReason,
    Qt.FocusReason.BacktabFocusReason,
    Qt.FocusReason.ShortcutFocusReason,
)


def card() -> QFrame:
    frame = QFrame()
    frame.setObjectName("Card")
    return frame


def label(text: str, object_name: str = "") -> QLabel:
    label = QLabel(text)
    if object_name:
        label.setObjectName(object_name)
    return label


class KeyboardFocusRing(QObject):
    """Marks the focused widget with ``kbfocus`` when focus came by keyboard.

    The stylesheet draws the focus ring from that property. Qt's stylesheets
    have no ``:focus-visible``; styling plain ``:focus`` instead would leave
    a ring on every button the mouse has clicked, which reads as a stuck
    selection. Installed once on the application, so dialogs get it too.
    """

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        kind = event.type()
        if kind == QEvent.Type.FocusIn and isinstance(watched, QWidget):
            _set_ring(watched, event.reason() in _KEYBOARD_REASONS)
        elif kind == QEvent.Type.FocusOut and isinstance(watched, QWidget):
            _set_ring(watched, False)
        return False


def _set_ring(widget: QWidget, on: bool) -> None:
    if bool(widget.property("kbfocus")) == on:
        return
    widget.setProperty("kbfocus", on)
    # A dynamic property is not re-evaluated by the stylesheet on its own.
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def finding(title: str, detail: str, tooltip: str = "") -> QFrame:
    """One finding: a bold title over its muted explanation."""
    frame = QFrame()
    frame.setObjectName("Finding")
    frame.setToolTip(tooltip)
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(14, 10, 14, 12)
    layout.setSpacing(4)
    for text, name in ((title, "FindingTitle"), (detail, "FindingDetail")):
        if text:
            line = label(text, name)
            line.setWordWrap(True)
            layout.addWidget(line)
    return frame


class FindingsCard(QFrame):
    """The findings card: a count in the heading, one block per finding.

    A scroll area of labels rather than a list widget: a list item takes a
    single font, and a finding needs its title set apart from the
    explanation under it.
    """

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("Card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)

        self._heading = label("НАХОДКИ", "SectionHeading")
        layout.addWidget(self._heading)

        body = QWidget()
        body.setObjectName("FindingsBody")
        self._rows = QVBoxLayout(body)
        self._rows.setContentsMargins(0, 0, 4, 0)
        self._rows.setSpacing(8)
        # Keeps a short list at the top of the card.
        self._rows.addStretch(1)

        scroll = QScrollArea()
        scroll.setObjectName("Findings")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        # Findings wrap; a horizontal scrollbar would only ever be an
        # empty bar across the bottom of the card.
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(body)
        layout.addWidget(scroll, stretch=1)

    def show_issues(self, issues: tuple[Issue, ...]) -> None:
        while self._rows.count() > 1:  # all but the stretch
            self._rows.takeAt(0).widget().deleteLater()
        actionable = [i for i in issues if i.status.is_actionable]
        self._heading.setText(
            f"НАХОДКИ — ТРЕБУЮТ ВНИМАНИЯ: {len(actionable)}" if actionable
            else "НАХОДКИ — НЕТ"
        )
        if not issues:
            self._rows.insertWidget(0, label("Проблем не найдено.", "FindingEmpty"))
            return
        for position, issue in enumerate(issues):
            threshold = presenters.threshold_label(issue.threshold_kind)
            self._rows.insertWidget(
                position,
                finding(
                    issue.title,
                    issue.detail,
                    issue.fix_hint + (f"\n\n{threshold}" if threshold else ""),
                ),
            )


def grouped_actions(
    columns: tuple[tuple[tuple[str, tuple[QPushButton, ...]], ...], ...],
) -> QFrame:
    """A card of buttons in columns, each column a stack of headed groups."""
    frame = card()
    grid = QGridLayout(frame)
    grid.setContentsMargins(20, 18, 20, 18)
    grid.setHorizontalSpacing(16)
    for column, stacked in enumerate(columns):
        box = QVBoxLayout()
        box.setSpacing(6)
        for index, (heading, buttons) in enumerate(stacked):
            if index:
                box.addSpacing(10)
            box.addWidget(label(heading, "SectionHeading"))
            box.addSpacing(2)
            for button in buttons:
                box.addWidget(button)
        box.addStretch(1)
        grid.addLayout(box, 0, column)
        grid.setColumnStretch(column, 1)
    return frame


class Segmented(QWidget):
    """A row of mutually exclusive choices with exactly one always selected.

    Each option is a checkable button in one exclusive group, so it is
    reachable by Tab and Space like any other button. ``changed`` fires on
    the operator's click only, never on :meth:`set_value`.
    """

    changed = Signal(object)

    def __init__(self, options: Sequence[tuple[str, object]], selected: object) -> None:
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        self._values = [value for _, value in options]
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        last = len(options) - 1
        for index, (text, _) in enumerate(options):
            button = QPushButton(text)
            button.setObjectName("Segment")
            button.setCheckable(True)
            # The stylesheet rounds only the outer corners of the row.
            button.setProperty(
                "edge", "first" if index == 0 else "last" if index == last else "middle"
            )
            self._group.addButton(button, index)
            row.addWidget(button, stretch=1)
        self._group.idClicked.connect(self._clicked)
        self.set_value(selected)

    def value(self) -> object:
        return self._values[self._group.checkedId()]

    def set_value(self, value: object) -> None:
        self._group.button(self._values.index(value)).setChecked(True)

    def _clicked(self, index: int) -> None:
        self.changed.emit(self._values[index])


class Disclosure(QWidget):
    """A header that shows or hides the widget beneath it.

    For secondary material an operator needs occasionally — what was
    skipped, what the plan never touches — so it stays reachable without
    pushing the decision itself below the fold. Starts collapsed.
    """

    def __init__(self, title: str, content: QWidget) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self._toggle = QToolButton()
        self._toggle.setObjectName("Disclosure")
        self._toggle.setText(title)
        self._toggle.setCheckable(True)
        self._toggle.setArrowType(Qt.ArrowType.RightArrow)
        self._toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._toggle.toggled.connect(self._on_toggled)
        layout.addWidget(self._toggle)

        self._content = content
        content.setVisible(False)
        layout.addWidget(content)

    def _on_toggled(self, expanded: bool) -> None:
        self._toggle.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
        )
        self._content.setVisible(expanded)
        if expanded:
            self._make_room()

    def _make_room(self) -> None:
        """Grow the window so the opened section is not overprinted.

        Qt does not enforce a wrapped label's height-for-width on a
        top-level window, so without this the new lines are drawn on top of
        each other instead of pushing the window taller.
        """
        window = self.window()
        layout = window.layout()
        if layout is None or not layout.hasHeightForWidth():
            return
        needed = layout.totalHeightForWidth(window.width())
        screen = window.screen()
        if screen is not None:
            # Leave room for the title bar; the lists inside still scroll.
            needed = min(needed, screen.availableGeometry().height() - 40)
        if needed > window.height():
            window.resize(window.width(), needed)


class ElidedLabel(QLabel):
    """A one-line label that shortens its text to the width it is given.

    Elided by measured pixels, not by character count: a count that fits
    the default window overflows a narrower one, and a right-aligned label
    then spills its start out of view ("thernet" for "Ethernet").
    """

    def __init__(self, object_name: str) -> None:
        super().__init__()
        self.setObjectName(object_name)
        self._full = ""
        # Takes whatever the row leaves over, down to its minimum width.
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def set_full_text(self, text: str) -> None:
        self._full = text
        self._elide()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self._elide()

    def _elide(self) -> None:
        self.setText(
            self.fontMetrics().elidedText(
                self._full, Qt.TextElideMode.ElideRight, self.width()
            )
        )


class MetricRow(QWidget):
    """One measured value with a status dot."""

    def __init__(self, name: str) -> None:
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(12)

        self._dot = QLabel("●")
        self._dot.setFixedWidth(14)
        self._dot.setStyleSheet(f"color: {theme.UNAVAILABLE};")

        self._name = label(name, "MetricName")
        self._name.setMinimumWidth(110)

        self._value = label("—", "MetricValue")
        self._value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._value.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        self._detail = ElidedLabel("MetricDetail")
        self._detail.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._detail.setMinimumWidth(150)

        layout.addWidget(self._dot)
        layout.addWidget(self._name)
        layout.addWidget(self._value, stretch=1)
        layout.addWidget(self._detail, stretch=2)

    def set(
        self, value: str, status: str = "GOOD", detail: str = "", tooltip: str = ""
    ) -> None:
        colour = theme.status_colour(status)
        self._dot.setStyleSheet(f"color: {colour};")
        self._value.setText(value)
        # Grey like the dot, but the text grey: the dot's shade is fine for a
        # mark (3:1) and too faint for a word (4.5:1).
        self._value.setStyleSheet(
            f"color: {theme.TEXT_FAINT};" if value == _UNAVAILABLE else ""
        )
        self._detail.set_full_text(detail)
        # The full text stays reachable even when the column elides it.
        self.setToolTip(tooltip or detail)
