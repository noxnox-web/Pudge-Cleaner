"""The confirmation step between planning and changing anything.

This dialog is the operator's decision point (rule #39). It shows the
complete list of planned changes with their risk levels, and what was
skipped and why. Nothing has been modified by the time it appears, and
nothing is modified unless it is accepted.

The default button is Cancel. A dialog that changes a club PC on a stray
Enter keypress is not a confirmation.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core.optimization.pipeline import (
    OptimizationLevel,
    OptimizationOutcome,
    OptimizationPreview,
)
from ...core.optimization.tweak import BackupScope
from . import presenters, theme, widgets

#: Item data role holding a row's index into ``plan.changes``.
_CHANGE_INDEX = Qt.ItemDataRole.UserRole + 1


class PreviewDialog(QDialog):
    """Shows planned changes and asks for confirmation.

    What arrives ticked follows the level chosen on the main window
    (:meth:`OptimizationPreview.ticked_by_default`): at «Средняя» MEDIUM rows
    are listed unticked, at «Жёсткая» everything is ticked. At «Обычная»
    MEDIUM is not planned and arrives as skipped rows; when any was skipped
    *only* for its risk class, a second button offers the «Средняя» plan.
    Closing with :data:`SHOW_MEDIUM` is the dashboard's cue to switch to it.
    """

    #: Result code meaning "re-plan at «Средняя»".
    SHOW_MEDIUM = QDialog.DialogCode.Accepted + 1

    def __init__(
        self, preview: OptimizationPreview, parent=None, snapshot=None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Проверка изменений")
        self.setMinimumSize(660, 460)
        self.setStyleSheet(theme.stylesheet())
        self._preview = preview

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(14)

        heading = QLabel(
            f"Запланировано изменений: {preview.change_count}"
            if preview.has_changes
            else "Изменения не нужны"
        )
        heading.setObjectName("Title")
        heading.setWordWrap(True)
        layout.addWidget(heading)

        irreversible = [
            c
            for c in preview.plan.applicable
            if c.tweak.scope is BackupScope.NONE
        ]
        subtitle = QLabel(
            "Пока ничего не изменено. Снимите галочку с того, что применять "
            "не нужно. Обратимые изменения сохраняются в резерв и "
            "откатываются сами, если не подтвердятся."
            + (
                f" Необратимых: {len(irreversible)} — они выделены цветом."
                if irreversible
                else ""
            )
            + (
                " Режим «Жёсткая»: отмечено всё, включая необратимое."
                if preview.level is OptimizationLevel.HARD
                else ""
            )
        )
        subtitle.setObjectName("Subtitle")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        self._list = _plain_list()
        # The plan is the decision; the sections below may not squeeze it.
        self._list.setMinimumHeight(150)
        layout.addWidget(self._list, stretch=1)
        self._skipped = _plain_list()
        # Opened to answer one question, so it never crowds out the plan.
        self._skipped.setMaximumHeight(200)
        self._populate()

        # Skipped entries are shown, not hidden: "why didn't it fix X?"
        # must be answerable from this screen. They sit one click away so
        # the rows that will actually change something keep the room.
        if self._skipped.count():
            layout.addWidget(
                widgets.Disclosure(
                    f"Пропущено: {self._skipped.count()} — почему", self._skipped
                )
            )

        # What the plan was computed for, and what it deliberately leaves
        # alone: the operator should not have to wonder whether HAGS or
        # Defender were touched because they are absent from the list.
        details = QWidget()
        details_layout = QVBoxLayout(details)
        details_layout.setContentsMargins(0, 0, 0, 0)
        details_layout.setSpacing(8)
        notes = [
            "Не меняется здесь: HAGS и игровой режим — только вручную в "
            "«Настройки Windows»; Защитник, Центр обновления, HPET, режим MSI "
            "и привязка к ядрам — никогда (причины — в «Аудит системы»).",
            "Применить изменение — значит обновить и проверить настройку. "
            "Это не измеренный прирост производительности.",
        ]
        if snapshot is not None:
            notes.insert(0, f"Обнаружено: {hardware_line(snapshot)}")
        for text in notes:
            note = QLabel(text)
            note.setObjectName("ScoreNote")
            note.setWordWrap(True)
            details_layout.addWidget(note)
        layout.addWidget(widgets.Disclosure("Подробности плана", details))

        buttons = QDialogButtonBox()
        self._apply = buttons.addButton(
            "Применить", QDialogButtonBox.ButtonRole.AcceptRole
        )
        self._apply.setObjectName("Primary")
        self._apply.setEnabled(preview.has_changes)

        # Offered only when something is waiting behind the risk gate, and
        # only while it is still shut: a plan already built with MEDIUM
        # allowed has nothing further to unlock.
        blocked = preview.plan.blocked_by_risk
        if blocked and not preview.allow_risk_above_low:
            show_medium = buttons.addButton(
                f"Показать изменения со средним риском ({len(blocked)})",
                QDialogButtonBox.ButtonRole.ActionRole,
            )
            show_medium.setObjectName("Secondary")
            show_medium.setAutoDefault(False)
            show_medium.clicked.connect(lambda: self.done(self.SHOW_MEDIUM))
        cancel = buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        cancel.setText("Отмена")
        cancel.setObjectName("Secondary")
        # Cancel is the default: a stray Enter must not modify a club PC.
        cancel.setDefault(True)
        cancel.setAutoDefault(True)
        self._apply.setAutoDefault(False)

        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._list.itemChanged.connect(self._sync_apply)
        # A plan consisting only of MEDIUM changes opens with nothing
        # ticked, and "Apply" must not be live until something is.
        self._apply.setEnabled(bool(self.selected_indices()))

    def selected_indices(self) -> set[int]:
        """Indices into ``plan.changes`` the operator left ticked."""
        chosen: set[int] = set()
        for row in range(self._list.count()):
            item = self._list.item(row)
            index = item.data(_CHANGE_INDEX)
            if index is not None and item.checkState() == Qt.CheckState.Checked:
                chosen.add(int(index))
        return chosen

    def selected_preview(self) -> OptimizationPreview:
        """The preview narrowed to what the operator ticked."""
        return self._preview.with_selection(self.selected_indices())

    def _sync_apply(self, _item: QListWidgetItem) -> None:
        self._apply.setEnabled(bool(self.selected_indices()))

    def _populate(self) -> None:
        for index, change in enumerate(self._preview.plan.changes):
            if not change.will_apply:
                continue
            # An irreversible change must say so on its own row, in words
            # and in colour. Relying on a general note at the top would let
            # an operator approve a deletion believing it could be undone.
            irreversible = change.tweak.scope is BackupScope.NONE
            tags = [presenters.risk_label(change.tweak.risk)]
            if irreversible:
                tags.append("НЕОБРАТИМО")
            item = QListWidgetItem(
                f"{change.summary}\n{' · '.join(tags)} — {change.tweak.rationale}"
            )
            item.setToolTip(change.tweak.description)
            item.setForeground(QColor(theme.WARNING if irreversible else theme.TEXT))
            # Each change can be left out: fixing the display must not force
            # the operator to accept an irreversible cleanup alongside it.
            item.setFlags(
                Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable
            )
            # SAFE and LOW start ticked; MEDIUM only at «Жёсткая», where the
            # operator chose the class before planning.
            item.setCheckState(
                Qt.CheckState.Checked
                if self._preview.ticked_by_default(change)
                else Qt.CheckState.Unchecked
            )
            item.setData(_CHANGE_INDEX, index)
            self._list.addItem(item)

        for change in self._preview.plan.skipped:
            item = QListWidgetItem(f"{change.summary}\n{change.skip_reason}")
            item.setForeground(QColor(theme.TEXT_MUTED))
            self._skipped.addItem(item)


def _plain_list() -> QListWidget:
    """A wrapping, non-selectable list, as every list in this dialog is."""
    rows = QListWidget()
    rows.setWordWrap(True)
    rows.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    rows.setSelectionMode(QListWidget.SelectionMode.NoSelection)
    return rows


def hardware_line(snapshot) -> str:
    """One line naming the machine the plan was built for."""
    parts = []
    if snapshot.cpu.model:
        parts.append(snapshot.cpu.model.strip())
    for gpu in snapshot.gpus:
        driver = f", драйвер {gpu.driver_version}" if gpu.driver_version else ""
        parts.append(f"{gpu.model}{driver}")
    ram = snapshot.ram.total_mb.value
    if ram:
        parts.append(f"{ram / 1024:.0f} ГБ ОЗУ")
    if snapshot.os.caption:
        parts.append(f"{snapshot.os.caption} (сборка {snapshot.os.build})")
    return " · ".join(parts) or "нет данных сканирования"


class ResultDialog(QDialog):
    """The before/after report (rule #40)."""

    def __init__(self, outcome: OptimizationOutcome, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Оптимизация завершена")
        self.setMinimumSize(640, 460)
        self.setStyleSheet(theme.stylesheet())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(12)

        heading = QLabel(
            "Оптимизация завершена"
            if outcome.run.failed == 0
            else "Оптимизация завершена с ошибками"
        )
        heading.setObjectName("Title")
        layout.addWidget(heading)

        summary = QHBoxLayout()
        summary.addWidget(
            _stat("Применено", str(outcome.run.applied), theme.GOOD)
        )
        summary.addWidget(
            _stat(
                "Откачено",
                str(outcome.run.rolled_back),
                theme.WARNING if outcome.run.rolled_back else theme.TEXT_MUTED,
            )
        )
        summary.addWidget(
            _stat(
                "Ошибок",
                str(outcome.run.failed),
                theme.CRITICAL if outcome.run.failed else theme.TEXT_MUTED,
            )
        )
        summary.addStretch(1)
        layout.addLayout(summary)

        detail = QListWidget()
        detail.setWordWrap(True)
        detail.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        detail.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        for line in outcome.report_lines():
            if line:
                detail.addItem(QListWidgetItem(line))
        for note in outcome.notes:
            detail.addItem(QListWidgetItem(note))
        layout.addWidget(detail, stretch=1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_btn = buttons.button(QDialogButtonBox.StandardButton.Close)
        close_btn.setObjectName("Secondary")
        close_btn.setText("Закрыть")
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)


def _stat(label: str, value: str, colour: str) -> QLabel:
    widget = QLabel(f"{value}\n{label}")
    widget.setAlignment(Qt.AlignmentFlag.AlignCenter)
    widget.setStyleSheet(
        f"color: {colour}; font-size: 20px; font-weight: 700; "
        f"padding: 10px 22px; border: 1px solid {theme.BORDER}; "
        "border-radius: 8px;"
    )
    return widget
