"""Dashboard mixin: «Проверить обновления» and the update banner.

A check runs once shortly after the window opens, silently — no news and no
network are both just a hidden button. The operator can also ask, and then
gets an answer either way. Installing downloads the signed release, verifies
it, replaces the program and restarts it; this mixin only asks and shows.
"""

from __future__ import annotations

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QMessageBox, QPushButton

from ...utilities.formatting import format_size


class UpdateActions:
    """Mixin for :class:`Dashboard`. Expects ``self._updates`` and ``self._busy_reason``."""

    _update_info = None
    _manual_check = False

    def _build_update_actions(self) -> None:
        self._update_button = QPushButton("")
        self._update_button.setObjectName("Secondary")
        self._update_button.hide()
        self._update_button.clicked.connect(self._on_update_clicked)
        self._check_updates = QPushButton("Проверить обновления")
        self._check_updates.setObjectName("Secondary")
        self._check_updates.clicked.connect(self._on_check_updates)

    def _connect_updates(self) -> None:
        updates = self._updates  # type: ignore[attr-defined]
        updates.checked.connect(self._on_updates_checked)
        updates.progress.connect(self._on_progress)  # type: ignore[attr-defined]
        updates.installed.connect(self._on_update_installed)
        updates.failed.connect(self._on_update_failed)
        # After the window is up and the scan has had its start.
        QTimer.singleShot(4000, updates.check)

    def _on_check_updates(self) -> None:
        if self._updates.busy:  # type: ignore[attr-defined]
            return
        self._manual_check = True
        self._check_updates.setEnabled(False)
        self._status.setText("Проверяю обновления…")  # type: ignore[attr-defined]
        self._updates.check()  # type: ignore[attr-defined]

    def _on_updates_checked(self, info) -> None:
        self._update_info = info
        shown = bool(info.available and info.latest)
        self._update_button.setVisible(shown)
        if shown:
            self._update_button.setText(f"Доступна версия {info.latest.version} — обновить")
        if self._manual_check:
            self._manual_check = False
            self._check_updates.setEnabled(True)
            self._status.setText("")  # type: ignore[attr-defined]
            if not info.checked:
                QMessageBox.information(
                    self, "Обновления", "Не удалось связаться с GitHub. Проверьте подключение к сети."
                )
            elif not shown:
                QMessageBox.information(
                    self, "Обновления", f"У вас последняя версия: {info.current}."
                )

    def _on_update_clicked(self) -> None:
        info = self._update_info
        if info is None or not info.latest:
            return
        release = info.latest
        notes = release.notes.strip()
        if len(notes) > 900:
            notes = notes[:900].rstrip() + "…"
        if not info.can_install:
            answer = QMessageBox.question(
                self, "Новая версия",
                f"Вышла версия {release.version} (у вас {info.current}). Эта копия не "
                "умеет обновляться сама — откройте страницу релиза и скачайте файл.\n\n"
                f"{notes}\n\nОткрыть страницу?",
            )
            if answer == QMessageBox.StandardButton.Yes:
                QDesktopServices.openUrl(QUrl(release.page))
            return
        blocked = self._busy_reason()  # type: ignore[attr-defined]
        if blocked:
            QMessageBox.warning(self, "Сейчас нельзя обновиться", f"{blocked} Дождитесь завершения.")
            return
        size = f" ({format_size(release.size)})" if release.size else ""
        answer = QMessageBox.question(
            self, "Обновить Pudge Cleaner?",
            f"Версия {release.version}{size}, у вас {info.current}.\n\n{notes}\n\n"
            "Программа скачает файл, проверит подпись издателя, заменит себя и "
            "перезапустится. Настройки и история изменений сохраняются.\n\nОбновить?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._update_button.setEnabled(False)
        self._begin_work(f"Обновление до {release.version}…")  # type: ignore[attr-defined]
        # Asked again once the download is done: it took a while, and an
        # apply or a cleanup may have started in it.
        self._updates.install(release, self._busy_reason)  # type: ignore[attr-defined]

    def _on_update_installed(self) -> None:
        self._end_work()  # type: ignore[attr-defined]
        self.close()  # type: ignore[attr-defined]

    def _on_update_failed(self, message: str) -> None:
        self._end_work()  # type: ignore[attr-defined]
        self._update_button.setEnabled(True)
        self._check_updates.setEnabled(True)
        self._manual_check = False
        QMessageBox.warning(self, "Обновление не выполнено", message)
