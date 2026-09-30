"""Where the disk space went, and what starts with Windows.

Two read-first dialogs kept together because they are the same shape: a
list the operator reads, with actions on it the operator chooses.

The usage report answers "what is using 200 GB" for the folders the cleaner's
allowlist deliberately says nothing about. It used to stop there. Now the
operator can pick a folder and delete it, or move it to another drive and
leave a junction behind. Both follow the plan -> confirm -> run order every
destructive action here follows, and the dialog owns only the asking:
the window routes each request to the controller and hands the answers back,
so this class never touches the disk (rule #12).
"""

from __future__ import annotations

import pathlib

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from ...core.cleanup import (
    FolderAction,
    FolderPlan,
    FolderResult,
    FolderUsage,
    UsageReport,
    block_reason,
)
from ...core.startup import StartupEntry
from ...utilities.formatting import format_size
from . import theme

#: Item data role holding a startup row's entry object.
_ENTRY = Qt.ItemDataRole.UserRole + 1
#: Item data role holding a usage row's folder.
_FOLDER = Qt.ItemDataRole.UserRole + 2


class UsageDialog(QDialog):
    """The largest folders on this PC, with delete and move on each."""

    plan_requested = Signal(object, object, object)
    """``(FolderAction, folder path, destination parent or None)``."""

    run_requested = Signal(object)
    """A :class:`FolderPlan` the operator confirmed."""

    def __init__(self, report: UsageReport, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Что занимает место")
        self.setMinimumSize(780, 600)
        self.setStyleSheet(theme.stylesheet())
        self._report = report
        self._busy = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(12)

        self._heading = QLabel()
        self._heading.setObjectName("Title")
        layout.addWidget(self._heading)

        notes = [
            "Выберите папку, чтобы удалить её или перенести на другой диск. "
            "Перенос оставляет на старом месте ссылку, так что программы "
            "продолжат находить папку там. Удаление безвозвратное: корзина "
            "не используется. Это папки, о которых чистильщик молчит: он "
            "работает по белому списку и не трогает то, что в нём не "
            "перечислено."
        ]
        if not report.complete:
            notes.append(
                "Обход прерван по времени, поэтому список неполный: "
                "какие-то папки не успели попасть в замер."
            )
        if report.unreadable:
            notes.append(
                f"Не удалось прочитать папок: {report.unreadable} — "
                "они в итог не вошли."
            )
        subtitle = QLabel(" ".join(notes))
        subtitle.setObjectName("Subtitle")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        self._rows = QListWidget()
        self._rows.setWordWrap(True)
        self._rows.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._rows.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        for folder in report.folders:
            self._rows.addItem(self._row(folder))
        self._rows.itemSelectionChanged.connect(self._on_selection)
        layout.addWidget(self._rows, stretch=1)

        # Why the selected folder cannot be touched, or what the buttons do.
        self._reason = QLabel()
        self._reason.setObjectName("ScoreNote")
        self._reason.setWordWrap(True)
        layout.addWidget(self._reason)

        # Progress of a delete or move, in the dialog: it is modal, so the
        # bar in the main window's header is behind it.
        self._work_text = QLabel()
        self._work_text.setObjectName("ScoreNote")
        self._work_text.setWordWrap(True)
        self._work = QProgressBar()
        self._work.setTextVisible(False)
        self._work_text.hide()
        self._work.hide()
        layout.addWidget(self._work_text)
        layout.addWidget(self._work)

        footer = QLabel(
            f"Замер занял {report.duration_s:.0f} с; корней просмотрено: "
            f"{len(report.roots_scanned)}; глубина: {report.depth}."
        )
        footer.setObjectName("ScoreNote")
        layout.addWidget(footer)

        buttons = QHBoxLayout()
        self._delete = QPushButton("Удалить…")
        self._delete.setObjectName("Secondary")
        self._delete.clicked.connect(self._on_delete)
        self._move = QPushButton("Перенести на другой диск…")
        self._move.setObjectName("Secondary")
        self._move.clicked.connect(self._on_move)
        buttons.addWidget(self._delete)
        buttons.addWidget(self._move)
        buttons.addStretch(1)
        self._close = QPushButton("Закрыть")
        self._close.setObjectName("Secondary")
        self._close.clicked.connect(self.reject)
        buttons.addWidget(self._close)
        layout.addLayout(buttons)

        self._refresh_heading()
        self._on_selection()

    # -- rows --------------------------------------------------------------

    @staticmethod
    def _row(folder: FolderUsage) -> QListWidgetItem:
        item = QListWidgetItem(
            f"{folder.size_display:>10}   {folder.path}   "
            f"({folder.file_count} файлов)"
        )
        item.setData(_FOLDER, folder)
        reason = block_reason(folder.path)
        item.setToolTip(f"{folder.path}\n{reason}" if reason else str(folder.path))
        if reason:
            item.setForeground(QColor(theme.TEXT_MUTED))
        return item

    def _folders(self) -> list[FolderUsage]:
        rows = (self._rows.item(i) for i in range(self._rows.count()))
        return [r.data(_FOLDER) for r in rows if r.data(_FOLDER) is not None]

    def _refresh_heading(self) -> None:
        folders = self._folders()
        total = sum(f.size_bytes for f in folders)
        if folders:
            self._heading.setText(
                f"Крупнейшие папки: {format_size(total)} в {len(folders)} папках"
            )
        else:
            self._heading.setText("Ничего не измерено")
            self._rows.addItem(QListWidgetItem("Измеримых папок не найдено."))

    def _selected(self) -> FolderUsage | None:
        item = self._rows.currentItem()
        return item.data(_FOLDER) if item is not None and item.isSelected() else None

    def _on_selection(self) -> None:
        folder = self._selected()
        reason = block_reason(folder.path) if folder is not None else None
        allowed = folder is not None and reason is None and not self._busy
        self._delete.setEnabled(allowed)
        self._move.setEnabled(allowed)
        if folder is None:
            self._reason.setText("Выберите папку в списке.")
        elif reason:
            self._reason.setText(f"Эту папку трогать нельзя: {reason}.")
        else:
            self._reason.setText(f"{folder.path}")

    # -- asking ------------------------------------------------------------

    def _on_delete(self) -> None:
        folder = self._selected()
        if folder is None or self._busy:
            return
        self._set_busy(True, "Проверка папки…")
        self.plan_requested.emit(FolderAction.DELETE, folder.path, None)

    def _on_move(self) -> None:
        folder = self._selected()
        if folder is None or self._busy:
            return
        chosen = QFileDialog.getExistingDirectory(
            self, f"Куда перенести «{folder.path.name}» — выберите папку на другом диске"
        )
        if not chosen:
            return
        self._set_busy(True, "Проверка папки и места на диске назначения…")
        self.plan_requested.emit(FolderAction.MOVE, folder.path, pathlib.Path(chosen))

    def confirm_plan(self, plan: FolderPlan) -> None:
        """Ask the operator about a plan the controller produced.

        Cancel is the default: a stray Enter must not delete 50 GB.
        """
        name = plan.source.name
        sizes = f"{plan.size_display}, файлов: {plan.file_count}"
        if plan.action is FolderAction.DELETE:
            title = "Удалить папку?"
            body = (
                f"Удалить безвозвратно «{name}»?\n\n{plan.source}\n{sizes}\n\n"
                "Корзина не используется — вернуть удалённое нельзя."
            )
            if plan.unreadable:
                body += (
                    f"\n\nЭлементов, которые не прочитались: {plan.unreadable} — "
                    "скорее всего, они не удалятся."
                )
            verb = "Удалить"
        else:
            title = "Перенести папку?"
            body = (
                f"Перенести «{name}» на другой диск?\n\n"
                f"Откуда: {plan.source}\nКуда:   {plan.destination}\n{sizes}\n\n"
                "На старом месте останется ссылка (junction), программы продолжат "
                "находить папку там. Пока идёт перенос, закрывать программу нельзя."
            )
            verb = "Перенести"

        box = QMessageBox(QMessageBox.Icon.Warning, title, body, parent=self)
        yes = box.addButton(verb, QMessageBox.ButtonRole.AcceptRole)
        no = box.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(no)
        box.setEscapeButton(no)
        box.exec()
        if box.clickedButton() is yes:
            self._set_busy(True, "Начинаю…")
            self.run_requested.emit(plan)
        else:
            self._set_busy(False)

    # -- answers -----------------------------------------------------------

    def show_progress(self, event) -> None:
        """A report from the running delete or move."""
        self._work_text.setText(event.text)
        if event.fraction is None:
            self._work.setRange(0, 0)  # busy: the step cannot tell
        else:
            self._work.setRange(0, 1000)
            self._work.setValue(round(event.fraction * 1000))

    def show_result(self, result: FolderResult) -> None:
        """Take a finished folder out of the list, and say what happened."""
        self._set_busy(False)
        if result.action is FolderAction.MOVE or result.leftover is None:
            self._drop(result.source)
        name = result.source.name
        if result.action is FolderAction.DELETE:
            head = f"Удалено: «{name}», освобождено {result.size_display}."
        else:
            head = (
                f"Перенесено: «{name}» → {result.destination} "
                f"({result.size_display}). На старом месте ссылка."
            )
        lines = [head]
        if result.leftover is not None:
            lines.append(
                f"\nНе всё удалилось: осталась папка «{result.leftover}» — "
                "файлы заняты другими программами."
            )
        lines += [f"• {line}" for line in result.failed[:5]]
        box = QMessageBox.information if result.complete else QMessageBox.warning
        box(self, "Что занимает место", "\n".join(lines))

    def fail(self, message: str) -> None:
        """A plan was refused or a run stopped; nothing half-done is hidden."""
        self._set_busy(False)
        QMessageBox.warning(self, "Что занимает место", message)

    def _drop(self, path: pathlib.Path) -> None:
        for row in range(self._rows.count()):
            folder = self._rows.item(row).data(_FOLDER)
            if folder is not None and folder.path == path:
                self._rows.takeItem(row)
                break
        self._refresh_heading()
        self._on_selection()

    # -- busy --------------------------------------------------------------

    def _set_busy(self, busy: bool, text: str = "") -> None:
        self._busy = busy
        self._rows.setEnabled(not busy)
        self._close.setEnabled(not busy)
        self._work_text.setVisible(busy)
        self._work.setVisible(busy)
        if busy:
            self._work_text.setText(text)
            self._work.setRange(0, 0)
        self._on_selection()

    def reject(self) -> None:
        # Closing mid-run would leave the window unable to say how it ended.
        if not self._busy:
            super().reject()


class StartupDialog(QDialog):
    """What launches at sign-in, with a switch for each entry."""

    def __init__(self, entries: list[StartupEntry], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Автозагрузка")
        self.setMinimumSize(820, 560)
        self.setStyleSheet(theme.stylesheet())
        self._suppress = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(12)

        active = sum(1 for e in entries if e.enabled)
        heading = QLabel(
            f"Запускается при входе: {active} из {len(entries)}"
            if entries
            else "Записей автозагрузки нет"
        )
        heading.setObjectName("Title")
        layout.addWidget(heading)

        subtitle = QLabel(
            "Снятая галочка отключает запись так же, как это делает "
            "диспетчер задач: сама запись и ярлык остаются на месте, "
            "меняется только флаг разрешения — включить обратно можно в "
            "любой момент. Ничего не удаляется."
        )
        subtitle.setObjectName("Subtitle")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        self._rows = QListWidget()
        self._rows.setWordWrap(True)
        self._rows.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._rows.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        for entry in entries:
            self._rows.addItem(self._row(entry))
        layout.addWidget(self._rows, stretch=1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close = buttons.button(QDialogButtonBox.StandardButton.Close)
        close.setObjectName("Secondary")
        close.setText("Закрыть")
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

    # -- rows --------------------------------------------------------------

    @staticmethod
    def _row(entry: StartupEntry) -> QListWidgetItem:
        publisher = entry.publisher or "издатель неизвестен"
        text = f"{entry.name}    —    {publisher}\n{entry.location_label}: {entry.command}"
        if entry.protected_reason:
            text += f"\nНе отключается: {entry.protected_reason}"
        item = QListWidgetItem(text)
        item.setData(_ENTRY, entry)
        item.setToolTip(entry.command)
        item.setForeground(
            QColor(theme.TEXT if entry.enabled else theme.TEXT_FAINT)
        )
        flags = Qt.ItemFlag.ItemIsEnabled
        if not entry.protected_reason:
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        item.setFlags(flags)
        item.setCheckState(
            Qt.CheckState.Checked if entry.enabled else Qt.CheckState.Unchecked
        )
        return item

    def connect_toggle(self, handler) -> None:
        """Call ``handler(entry, enabled)`` when a row is ticked or unticked.

        Wired from the dashboard rather than in ``__init__`` so this dialog
        holds no controller: the registry write is the window's business,
        and a dialog that could write on its own would be a second place
        the rule "the GUI never touches the OS" had to be enforced.
        """
        self._handler = handler
        self._rows.itemChanged.connect(self._on_item_changed)

    def _on_item_changed(self, item: QListWidgetItem) -> None:
        if self._suppress:
            return
        entry = item.data(_ENTRY)
        if entry is None:
            return
        self._handler(entry, item.checkState() == Qt.CheckState.Checked)

    def apply_result(self, entry: StartupEntry) -> None:
        """Re-render the row from the state the registry actually reports.

        A toggle that Windows refused must not leave a tick claiming it
        worked, so the row follows the read-back, not the click.
        """
        for row in range(self._rows.count()):
            item = self._rows.item(row)
            existing = item.data(_ENTRY)
            if existing is None or existing.key != entry.key:
                continue
            self._suppress = True
            try:
                item.setData(_ENTRY, entry)
                item.setCheckState(
                    Qt.CheckState.Checked
                    if entry.enabled
                    else Qt.CheckState.Unchecked
                )
                item.setForeground(
                    QColor(theme.TEXT if entry.enabled else theme.TEXT_FAINT)
                )
            finally:
                self._suppress = False
            return
