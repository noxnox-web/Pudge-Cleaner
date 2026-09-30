"""Profile, disk-cleanup and Steam-reset button handlers for the dashboard.

Kept out of ``dashboard.py`` as a mixin so that file stays focused on layout
and the scan/optimize lifecycle, and under the module-size limit. Every
method here runs on the UI thread and delegates the slow, blocking or
destructive work to a controller's worker thread.

The type: ignore comments are because the mixin reaches attributes the
``Dashboard`` defines (``_result``, ``_profiles``, ``_steam``, ``_status``);
they exist at runtime on every real instance.
"""

from __future__ import annotations

from PySide6.QtWidgets import QFileDialog, QMessageBox

from ...core import services
from ...core.profiles.storage import PROFILE_FILENAME
from ...utilities.exceptions import PgmError
from ...utilities.logging_setup import get_logger
from . import presenters
from .cleanup_dialog import CleanupDialog, CleanupResultDialog
from .profile_dialog import ComparisonDialog
from .steam_dialog import SteamResetDialog, SteamResultDialog
from .usage_dialog import StartupDialog, UsageDialog

_log = get_logger(__name__)


class ProfileAndGamesActions:
    """Mixin: profile, disk cleanup, Steam reset, startup recovery."""

    # -- construction ------------------------------------------------------

    def _build_actions(self) -> None:
        """Create the buttons this mixin handles.

        Each button is created next to the handler it calls; the dashboard
        decides where it goes. All start disabled: every one of them needs
        a finished scan.
        """
        secondary = self._secondary  # type: ignore[attr-defined]
        # Golden Profile actions need a scan to capture or compare against.
        self._save_profile = secondary("Сохранить профиль…", self._on_save_profile)
        self._compare_profile = secondary("Сравнить с профилем…", self._on_compare_profile)
        # Read-only, so it sits with the diagnostics rather than beside the
        # cleaner: an operator looks here first to decide whether the
        # cleaner is even the right tool for what is filling the disk.
        self._disk_usage = secondary("Что занимает место…", self._on_disk_usage)
        self._startup = secondary("Автозагрузка…", self._on_startup)
        # Disk cleanup stands on its own rather than hiding inside
        # OPTIMIZE: it is the action an operator reaches for by name, and
        # it is the only path that offers every category, including the
        # ones that are off by default. It is not the primary action,
        # though; the window has one, and it is OPTIMIZE.
        self._clean_disk = secondary("Очистка диска…", self._on_clean_disk)
        # A club reset: remove every Steam game except those a profile keeps.
        self._reset_steam = secondary("Очистка Стима…", self._on_reset_steam)

    # -- startup -----------------------------------------------------------

    def _report_interrupted_changes(self) -> None:
        """Tell the operator about changes a previous run never finished.

        Shown once: acknowledging marks them reported. Nothing is rolled back
        automatically — an unattended change nobody previewed is exactly what
        the safety model forbids, and the operator may already have fixed it.
        """
        try:
            changes = services.interrupted_changes()
        except PgmError as exc:
            _log.warning("could not check for interrupted changes: %s", exc.what)
            return
        if not changes:
            return
        body = "\n".join(f"• {c.line()}" for c in changes)
        QMessageBox.warning(
            self,
            "Прерванные изменения",
            "Программа была закрыта или аварийно завершилась во время "
            "изменения этих настроек, поэтому они не были подтверждены. "
            "Возможно, применены частично. Проверьте и при необходимости "
            "верните исходное значение вручную:\n\n"
            f"{body}",
        )
        try:
            services.acknowledge_interrupted(changes)
        except PgmError as exc:
            _log.warning("could not record interrupted changes: %s", exc.what)

    # -- golden profile ----------------------------------------------------

    def _set_profile_actions(self, enabled: bool) -> None:
        self._save_profile.setEnabled(enabled)  # type: ignore[attr-defined]
        self._compare_profile.setEnabled(enabled)  # type: ignore[attr-defined]
        self._reset_steam.setEnabled(enabled)  # type: ignore[attr-defined]
        self._clean_disk.setEnabled(enabled)  # type: ignore[attr-defined]
        self._disk_usage.setEnabled(enabled)  # type: ignore[attr-defined]
        self._startup.setEnabled(enabled)  # type: ignore[attr-defined]
        self._set_system_actions(enabled)  # type: ignore[attr-defined]

    def _on_save_profile(self) -> None:
        if self._result is None or self._profiles.busy:  # type: ignore[attr-defined]
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Сохранить этот ПК как профиль", PROFILE_FILENAME,
            "Профиль (*.json)",
        )
        if not path:
            return
        self._set_profile_actions(False)
        self._status.setText("Сохранение профиля…")  # type: ignore[attr-defined]
        self._profiles.start_save(  # type: ignore[attr-defined]
            self._result.snapshot, path,  # type: ignore[attr-defined]
            presenters.profile_name_from_path(path),
        )

    def _on_compare_profile(self) -> None:
        if self._result is None or self._profiles.busy:  # type: ignore[attr-defined]
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Сравнить с профилем", "", "Профиль (*.json)"
        )
        if not path:
            return
        self._set_profile_actions(False)
        self._status.setText("Сравнение с профилем…")  # type: ignore[attr-defined]
        self._profiles.start_compare(self._result.snapshot, path)  # type: ignore[attr-defined]

    def _on_profile_saved(self, path: object) -> None:
        self._status.setText("")  # type: ignore[attr-defined]
        self._set_profile_actions(True)
        QMessageBox.information(self, "Профиль сохранён", f"Сохранено в {path}")

    def _on_profile_compared(self, result: object) -> None:
        self._status.setText("")  # type: ignore[attr-defined]
        self._set_profile_actions(True)
        dialog = ComparisonDialog(result, self)  # type: ignore[arg-type]
        dialog.fix_settings_requested.connect(self._on_fix_profile_settings)  # type: ignore[attr-defined]
        dialog.exec()

    def _on_profile_failed(self, message: str) -> None:
        self._status.setText("")  # type: ignore[attr-defined]
        self._set_profile_actions(True)
        QMessageBox.warning(self, "Профиль", message)

    # -- disk cleanup ------------------------------------------------------

    def _on_clean_disk(self) -> None:
        """Inventory every cleanup category, then show what was found.

        The scan changes nothing; it is the preview that asks permission.
        Unlike the cleanup inside OPTIMIZE, this offers every category the
        program knows about, including the ones that are off by default.
        """
        if self._cleanup.busy:  # type: ignore[attr-defined]
            return
        self._set_profile_actions(False)
        self._begin_work("Поиск мусора на диске…")  # type: ignore[attr-defined]
        self._cleanup.start_plan()  # type: ignore[attr-defined]

    def _on_cleanup_planned(self, plan: object) -> None:
        self._end_work()  # type: ignore[attr-defined]
        self._set_profile_actions(True)
        if not plan.has_content:  # type: ignore[union-attr]
            QMessageBox.information(
                self, "Очистка диска", "Удалять нечего — на диске чисто."
            )
            return

        dialog = CleanupDialog(plan, self)  # type: ignore[arg-type]
        if dialog.exec() != CleanupDialog.DialogCode.Accepted:
            return
        selected = dialog.selection()
        if not selected:
            return

        self._set_profile_actions(False)
        self._begin_work("Очистка диска…")  # type: ignore[attr-defined]
        self._cleanup.start_clean(plan, selected)  # type: ignore[attr-defined]

    def _on_cleanup_finished(self, result: object) -> None:
        self._end_work()  # type: ignore[attr-defined]
        self._set_profile_actions(True)
        CleanupResultDialog(result, self).exec()  # type: ignore[arg-type]
        # Free space changed, so the disk figure and the score are stale.
        # Re-measure rather than leaving a number nobody took — but only
        # measure: a cleanup cannot change which CPU is installed or the
        # latency to the router, and re-reading those cost four seconds.
        self._controller.refresh()  # type: ignore[attr-defined]

    def _on_cleanup_failed(self, message: str) -> None:
        self._end_work()  # type: ignore[attr-defined]
        self._set_profile_actions(True)
        QMessageBox.warning(self, "Очистка диска", message)

    # -- disk usage survey -------------------------------------------------

    def _on_disk_usage(self) -> None:
        """Measure the largest folders. Reads only; deletes nothing."""
        if self._tools.busy:  # type: ignore[attr-defined]
            return
        self._set_profile_actions(False)
        self._begin_work("Замер занятого места… это занимает до минуты")  # type: ignore[attr-defined]
        self._tools.start_usage_survey()  # type: ignore[attr-defined]

    def _on_usage_ready(self, report: object) -> None:
        self._end_work()  # type: ignore[attr-defined]
        self._set_profile_actions(True)
        # Held while open: the dialog asks for a delete or a move, and the
        # answers (plan, progress, result) are routed back to it.
        dialog = UsageDialog(report, self)  # type: ignore[arg-type]
        self._usage_dialog = dialog  # type: ignore[attr-defined]
        dialog.plan_requested.connect(self._on_folder_plan_requested)
        dialog.run_requested.connect(self._tools.start_folder_run)  # type: ignore[attr-defined]
        try:
            dialog.exec()
        finally:
            self._usage_dialog = None  # type: ignore[attr-defined]

    def _on_folder_plan_requested(self, action: object, path: object, destination: object) -> None:
        self._tools.start_folder_plan(action, path, destination)  # type: ignore[attr-defined]

    def _on_folder_planned(self, plan: object) -> None:
        if self._usage_dialog is not None:  # type: ignore[attr-defined]
            self._usage_dialog.confirm_plan(plan)  # type: ignore[attr-defined]

    def _on_tools_progress(self, event: object) -> None:
        """The survey reports into the header bar; a folder run into its dialog."""
        if self._usage_dialog is not None:  # type: ignore[attr-defined]
            self._usage_dialog.show_progress(event)  # type: ignore[attr-defined]
        else:
            self._on_progress(event)  # type: ignore[attr-defined]

    def _on_folder_done(self, result: object) -> None:
        if self._usage_dialog is not None:  # type: ignore[attr-defined]
            self._usage_dialog.show_result(result)  # type: ignore[attr-defined]

    # -- startup manager ---------------------------------------------------

    def _on_startup(self) -> None:
        if self._tools.busy:  # type: ignore[attr-defined]
            return
        self._set_profile_actions(False)
        self._status.setText("Чтение автозагрузки…")  # type: ignore[attr-defined]
        self._tools.start_startup_scan()  # type: ignore[attr-defined]

    def _on_startup_ready(self, entries: object) -> None:
        self._status.setText("")  # type: ignore[attr-defined]
        self._set_profile_actions(True)

        dialog = StartupDialog(entries, self)  # type: ignore[arg-type]
        # The dialog stays open while each toggle round-trips through the
        # worker, so it is held on the window and the result is routed back
        # to it rather than being applied optimistically.
        self._startup_dialog = dialog
        dialog.connect_toggle(self._on_startup_toggle_requested)
        dialog.exec()
        self._startup_dialog = None

    def _on_startup_toggle_requested(self, entry: object, enabled: bool) -> None:
        self._tools.start_startup_toggle(entry, enabled)  # type: ignore[attr-defined]

    def _on_startup_toggled(self, entry: object) -> None:
        if self._startup_dialog is not None:
            self._startup_dialog.apply_result(entry)  # type: ignore[arg-type]

    def _on_tools_failed(self, message: str) -> None:
        self._end_work()  # type: ignore[attr-defined]
        self._set_profile_actions(True)
        if self._usage_dialog is not None:  # type: ignore[attr-defined]
            # A refused plan or a stopped run is answered where it was asked.
            self._usage_dialog.fail(message)  # type: ignore[attr-defined]
            return
        QMessageBox.warning(self, "Инструменты", message)

    # -- steam reset -------------------------------------------------------

    def _on_reset_steam(self) -> None:
        """Plan a reset against the built-in keep-list.

        No profile or config file: the keep-list is baked into the program
        (``games.steam.default_keep``), and the preview then lets the
        operator untick any game before anything is removed.
        """
        if self._steam.busy:  # type: ignore[attr-defined]
            return
        self._set_profile_actions(False)
        self._begin_work("Подготовка очистки Стима…")  # type: ignore[attr-defined]
        self._steam.start_plan()  # type: ignore[attr-defined]

    def _on_steam_planned(self, plan: object) -> None:
        self._end_work()  # type: ignore[attr-defined]
        self._set_profile_actions(True)
        if plan is None:
            QMessageBox.information(
                self, "Очистка Стима",
                "Steam на этом ПК не установлен — очищать нечего.",
            )
            return
        dialog = SteamResetDialog(plan, self)  # type: ignore[arg-type]
        if dialog.exec() != SteamResetDialog.DialogCode.Accepted:
            return
        # Games the operator unticked are moved back to keep.
        confirmed = dialog.confirmed_plan()
        self._set_profile_actions(False)
        self._begin_work("Удаление игр…")  # type: ignore[attr-defined]
        self._steam.start_wipe(confirmed)  # type: ignore[attr-defined]

    def _on_steam_wiped(self, result: object) -> None:
        self._end_work()  # type: ignore[attr-defined]
        self._set_profile_actions(True)
        SteamResultDialog(result, self).exec()  # type: ignore[arg-type]

    def _on_steam_failed(self, message: str) -> None:
        self._end_work()  # type: ignore[attr-defined]
        self._set_profile_actions(True)
        QMessageBox.warning(self, "Очистка Стима", message)
