"""Dashboard mixin: system audit, «Настройки Windows», change history.

Kept beside the dialogs it opens rather than in ``dashboard.py``, the same
split ``dashboard_actions`` uses: the dashboard owns layout and the scan
lifecycle, each mixin owns its buttons and handlers.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QInputDialog,
    QMessageBox,
    QPushButton,
)

from ...core.audit.report import Level
from . import theme
from .history_dialog import HistoryDialog
from .nightly_dialog import NightlyDialog
from .report_dialog import ReportDialog
from .settings_dialog import PlanConfirmDialog, SettingsDialog

_LEVEL_COLOURS = {
    Level.OK: theme.GOOD,
    Level.ATTENTION: theme.WARNING,
    Level.INFO: theme.TEXT,
    Level.MANUAL: theme.ACCENT_HOVER,
    Level.EXCLUDED: theme.TEXT_FAINT,
}


class SystemActions:
    """Mixin for :class:`Dashboard`. Expects ``self._system`` and ``self._result``."""

    _settings_dialog: SettingsDialog | None = None
    _history_dialog: HistoryDialog | None = None

    def _build_system_actions(self) -> None:
        """Create this mixin's buttons; the dashboard places them."""
        self._audit_button = self._secondary("Аудит системы…", self._on_audit)
        self._settings_button = self._secondary("Настройки Windows…", self._on_settings)
        self._history_button = self._secondary("История изменений…", self._on_history)
        self._monitor_button = self._secondary("Замер нагрузки…", self._on_monitor)
        # Changes the schedule, not the PC, so it needs no scan first.
        self._nightly_button = self._secondary("Ночная очистка…", self._on_nightly)
        self._nightly_button.setEnabled(True)

    def _secondary(self, text: str, handler) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName("Secondary")
        button.setEnabled(False)
        button.clicked.connect(handler)
        return button

    def _connect_system(self) -> None:
        system = self._system  # type: ignore[attr-defined]
        system.audit_ready.connect(self._on_audit_ready)
        system.settings_loaded.connect(self._on_settings_loaded)
        system.settings_planned.connect(self._on_settings_planned)
        system.settings_applied.connect(self._on_settings_applied)
        system.history_ready.connect(self._on_history_ready)
        system.restored.connect(self._on_restored)
        system.monitor_progress.connect(self._on_monitor_progress)
        system.monitor_done.connect(self._on_monitor_done)
        system.failed.connect(self._on_system_failed)

    def _set_system_actions(self, enabled: bool) -> None:
        for button in (self._audit_button, self._settings_button, self._history_button):
            button.setEnabled(enabled)
        # A recording in progress must stay stoppable whatever else runs.
        self._monitor_button.setEnabled(enabled or self._system.monitoring)  # type: ignore[attr-defined]

    def _status_text(self, text: str) -> None:
        self._status.setText(text)  # type: ignore[attr-defined]

    # -- audit -------------------------------------------------------------

    def _on_audit(self) -> None:
        if self._system.busy:  # type: ignore[attr-defined]
            return
        self._status_text("Аудит системы…")
        result = self._result  # type: ignore[attr-defined]
        self._system.start_audit(result.snapshot if result else None)  # type: ignore[attr-defined]

    def _on_audit_ready(self, report) -> None:
        self._status_text("")
        rows = []
        for section in report.sections:
            rows.append((f"— {section.title.upper()} —", theme.TEXT_MUTED))
            for item in section.items:
                rows.append((item.line(), _LEVEL_COLOURS.get(item.level, theme.TEXT)))
        attention = len(report.attention)
        ReportDialog(
            "Аудит системы",
            f"Требует внимания: {attention}" if attention else "Замечаний нет",
            rows,
            subtitle=(
                "Только чтение — отсюда ничего не меняется. ⚠ — стоит "
                "посмотреть, ✎ — делается вручную в панели драйвера, ✕ — "
                "программа намеренно этого не делает (причина под строкой)."
            ),
            copy_text=report.text(),
            parent=self,
        ).exec()

    # -- settings ----------------------------------------------------------

    def _on_settings(self) -> None:
        if self._system.busy:  # type: ignore[attr-defined]
            return
        self._status_text("Чтение настроек Windows…")
        self._system.load_settings()  # type: ignore[attr-defined]

    def _on_settings_loaded(self, view) -> None:
        self._status_text("")
        dialog = SettingsDialog(view, self)
        dialog.plan_requested.connect(self._on_settings_plan_requested)
        self._settings_dialog = dialog
        try:
            dialog.exec()
        finally:
            self._settings_dialog = None

    def _on_settings_plan_requested(self, choices, device_ids) -> None:
        if self._settings_dialog is not None:
            self._settings_dialog.set_busy(True)
        self._system.plan_settings(choices, device_ids)  # type: ignore[attr-defined]

    def _on_settings_planned(self, plan) -> None:
        self._status_text("")
        dialog = self._settings_dialog
        if dialog is not None:
            dialog.set_busy(False)
        confirm = PlanConfirmDialog(plan, dialog or self)
        if confirm.exec() != QDialog.DialogCode.Accepted:
            return
        if dialog is not None:
            dialog.set_busy(True)
        self._status_text("Применение настроек…")
        self._system.apply_settings(plan)  # type: ignore[attr-defined]

    def _on_settings_applied(self, payload) -> None:
        self._status_text("")
        report, notes = payload
        if self._settings_dialog is not None:
            self._settings_dialog.accept()
        rows = []
        for result in report.results:
            if result.outcome.value in ("NOT_NEEDED",):
                continue
            colour = {
                "SUCCESS": theme.GOOD,
                "FAILED": theme.CRITICAL,
                "ROLLED_BACK": theme.WARNING,
            }.get(result.outcome.value, theme.TEXT_MUTED)
            rows.append((f"[{result.outcome.value}] {result.name}: {result.detail}", colour))
        rows.extend((note, theme.TEXT_MUTED) for note in notes)
        if report.needs_restart:
            rows.append(("Часть изменений вступит в силу после перезагрузки.", theme.WARNING))
        ReportDialog(
            "Настройки применены",
            f"Применено: {report.applied}, ошибок: {report.failed}, откачено: {report.rolled_back}",
            rows,
            subtitle="Любое изменение можно вернуть в «История изменений…».",
            parent=self,
        ).exec()

    def _on_fix_profile_settings(self, fixes) -> None:
        """Plan the profile's correctable drift; the usual confirmation follows."""
        if self._system.busy:  # type: ignore[attr-defined]
            return
        self._status_text("Планирование изменений по профилю…")
        self._system.plan_profile_fixes(fixes)  # type: ignore[attr-defined]

    # -- load recording ------------------------------------------------------

    _DURATIONS = {"1 минута": 60, "3 минуты": 180, "5 минут": 300, "10 минут": 600}

    def _on_monitor(self) -> None:
        system = self._system  # type: ignore[attr-defined]
        if system.monitoring:
            system.stop_monitor()
            self._monitor_button.setText("Останавливается…")
            return
        choice, ok = QInputDialog.getItem(
            self, "Замер нагрузки",
            "Запустите игру и играйте, пока идёт замер. Программа раз в "
            "секунду записывает загрузку процессора, памяти и видеокарты.\n"
            "Длительность:",
            list(self._DURATIONS), 1, False,
        )
        if not ok:
            return
        self._monitor_button.setText("Остановить замер")
        system.start_monitor(self._DURATIONS[choice])

    def _on_monitor_progress(self, fraction: float) -> None:
        self._status_text(f"Идёт замер нагрузки: {fraction * 100:.0f}%")

    def _on_monitor_done(self, report) -> None:
        self._status_text("")
        self._monitor_button.setText("Замер нагрузки…")
        rows = [(line, theme.TEXT) for line in report.lines()]
        rows += [(line, theme.WARNING) for line in report.findings()]
        ReportDialog(
            "Замер нагрузки",
            "Замер остановлен досрочно" if report.stopped_early else "Замер завершён",
            rows,
            subtitle=(
                "Только измеренное. Чтобы оценить изменение настройки, "
                "сделайте замер одной и той же сцены до и после."
            ),
            copy_text="\n".join(report.lines() + report.findings()),
            parent=self,
        ).exec()

    # -- history -----------------------------------------------------------

    def _on_nightly(self) -> None:
        NightlyDialog(self).exec()  # type: ignore[arg-type]

    def _on_history(self) -> None:
        if self._system.busy:  # type: ignore[attr-defined]
            return
        self._system.load_history()  # type: ignore[attr-defined]

    def _on_history_ready(self, records) -> None:
        if self._history_dialog is not None:
            self._history_dialog.set_records(records)
            self._history_dialog.set_busy(False)
            return
        dialog = HistoryDialog(records, self)
        dialog.restore_requested.connect(self._on_restore_requested)
        dialog.restore_all_requested.connect(self._on_restore_all_requested)
        self._history_dialog = dialog
        try:
            dialog.exec()
        finally:
            self._history_dialog = None

    def _on_restore_requested(self, backup_id: str) -> None:
        if self._history_dialog is not None:
            self._history_dialog.set_busy(True)
        self._system.restore(backup_id)  # type: ignore[attr-defined]

    def _on_restore_all_requested(self) -> None:
        if self._history_dialog is not None:
            self._history_dialog.set_busy(True)
        self._system.restore_all()  # type: ignore[attr-defined]

    def _on_restored(self, results) -> None:
        failed = [r for r in results if not r.ok]
        lines = "\n".join(
            f"{'✓' if r.ok else '✕'} {r.name}: {r.message}" for r in results
        ) or "Откатывать было нечего."
        box = QMessageBox.warning if failed else QMessageBox.information
        box(
            self._history_dialog or self,
            "Откат",
            lines[:4000],
        )
        self._system.load_history()  # type: ignore[attr-defined]

    def _on_system_failed(self, message: str) -> None:
        self._status_text("")
        for dialog in (self._settings_dialog, self._history_dialog):
            if dialog is not None:
                dialog.set_busy(False)
        QMessageBox.warning(self, "Ошибка", message)
