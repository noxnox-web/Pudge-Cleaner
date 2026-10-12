"""The «Ночная очистка» window: turn the daily run on or off, see the last one.

Registering the schedule is a quick call (a fraction of a second), so it runs
on the UI thread under a wait cursor like opening any dialog would; nothing
here walks a disk.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTime
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QMessageBox,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from ...core import nightly
from ...utilities.exceptions import PgmError
from ..controllers.errors import readable_error
from ..nightly import launch_command
from . import theme, widgets

_EXPLANATION = (
    "Каждый день в выбранное время программа сама, без окна, делает то же, "
    "что «Оптимизировать ПК» на уровне «Обычная»: очищает временные файлы и "
    "корзину и приводит в порядок безопасные настройки Windows. Расписание "
    "хранит Windows: между запусками программа ничего не занимает."
)

_DETAILS = (
    "Нагрузка. Самый низкий приоритет процессора и диска. Запуск ждёт, пока "
    "вы 10 минут не трогаете мышь и клавиатуру (до полутора часов), иначе "
    "пропускает эту ночь. Не стартует, пока открыто окно программы и пока ПК "
    "работает от батареи. Если в назначенное время ПК был выключен, запуск "
    "состоится после включения, когда ПК освободится.",
    "Что делает. Только изменения с пометками «безопасно» и «низкий риск» — "
    "те, что на уровне «Обычная» отмечены в окне проверки заранее. Каждое "
    "изменение попадает в «Историю изменений».",
    "Чего не делает никогда. Не трогает «Загрузки» и журналы Windows, не "
    "удаляет программы и Windows.old, не чистит Стим, не применяет изменения "
    "со средним риском (уровни «Средняя» и «Жёсткая»). Для этого есть окно, "
    "где вы видите список заранее.",
    "Условия. Включать нужно от имени администратора. Задача работает, "
    "когда в Windows вошёл тот пользователь, который её включил.",
)


class NightlyDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Ночная очистка")
        self.setMinimumWidth(600)
        self.setStyleSheet(theme.stylesheet())

        self._command, self._arguments = launch_command()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(12)

        layout.addWidget(widgets.label("Ночная очистка", "Title"))
        explanation = widgets.label(_EXPLANATION, "Subtitle")
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        self._state = widgets.label("", "MetricValue")
        self._state.setWordWrap(True)
        layout.addWidget(self._state)

        self._last = widgets.label("", "MetricDetail")
        self._last.setWordWrap(True)
        layout.addWidget(self._last)

        clock = QHBoxLayout()
        clock.addWidget(widgets.label("Время запуска", "MetricName"))
        self._time = QTimeEdit(QTime(nightly.DEFAULT_HOUR, nightly.DEFAULT_MINUTE))
        self._time.setDisplayFormat("HH:mm")
        clock.addWidget(self._time)
        clock.addStretch(1)
        layout.addLayout(clock)

        details = QWidget()
        details_layout = QVBoxLayout(details)
        details_layout.setContentsMargins(0, 0, 0, 0)
        details_layout.setSpacing(8)
        for paragraph in _DETAILS:
            note = widgets.label(paragraph, "ScoreNote")
            note.setWordWrap(True)
            details_layout.addWidget(note)
        layout.addWidget(widgets.Disclosure("Что именно она делает", details))
        layout.addStretch(1)

        buttons = QDialogButtonBox()
        self._enable = buttons.addButton(
            "Включить", QDialogButtonBox.ButtonRole.AcceptRole
        )
        self._enable.setObjectName("Primary")
        self._enable.setAutoDefault(False)
        self._enable.clicked.connect(self._on_enable)
        self._disable = buttons.addButton(
            "Выключить", QDialogButtonBox.ButtonRole.ActionRole
        )
        self._disable.setObjectName("Secondary")
        self._disable.setAutoDefault(False)
        self._disable.clicked.connect(self._on_disable)
        close = buttons.addButton(QDialogButtonBox.StandardButton.Close)
        close.setText("Закрыть")
        close.setObjectName("Secondary")
        close.setDefault(True)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._refresh()

    # -- state -------------------------------------------------------------

    def _refresh(self) -> None:
        current = nightly.status(self._command)
        if not current.enabled:
            self._state.setText("Выключена")
            self._state.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        elif current.points_elsewhere:
            self._state.setText(
                f"Включена на {current.time_text}, но запускает другой файл "
                "программы (её перенесли или заменили). Ночью она не сработает: "
                "нажмите «Сохранить», чтобы указать этот файл."
            )
            self._state.setStyleSheet(f"color: {theme.WARNING};")
        else:
            self._state.setText(f"Включена: каждый день в {current.time_text}")
            self._state.setStyleSheet(f"color: {theme.GOOD};")

        if current.enabled:
            self._time.setTime(QTime.fromString(current.time_text, "HH:mm"))
        self._enable.setText("Сохранить" if current.enabled else "Включить")
        self._disable.setEnabled(current.enabled)

        last = nightly.last_report()
        self._last.setText(
            f"Последний запуск — {last.headline()}"
            if last
            else "Ещё ни разу не запускалась."
        )

    # -- actions -----------------------------------------------------------

    def _on_enable(self) -> None:
        picked = self._time.time()
        self._apply(
            lambda: nightly.enable(
                self._command, self._arguments, picked.hour(), picked.minute()
            )
        )

    def _on_disable(self) -> None:
        self._apply(nightly.disable)

    def _apply(self, action) -> None:
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            action()
        except PgmError as exc:
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, "Ночная очистка", readable_error(exc))
        else:
            QApplication.restoreOverrideCursor()
        self._refresh()
