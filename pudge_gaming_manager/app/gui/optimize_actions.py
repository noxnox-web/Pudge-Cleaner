"""OPTIMIZE PC for the dashboard: the level toggle, the plan, the apply.

Kept out of ``dashboard.py`` as a mixin, like the other action groups, so
that file stays focused on layout and the scan lifecycle. Every method here
runs on the UI thread; planning and applying run on the optimizer's worker.

The level is chosen *before* planning, on the main window, so the first
plan already contains what the operator wants to see — re-planning from the
preview used to cost another full planning pass. It is not remembered
between launches: every session starts at «Обычная».

The type: ignore comments are because the mixin reaches attributes the
``Dashboard`` defines (``_result``, ``_optimizer``, ``_status`` and so on);
they exist at runtime on every real instance.
"""

from __future__ import annotations

from PySide6.QtWidgets import QMessageBox, QVBoxLayout

from ..controllers.optimize_controller import OptimizationLevel
from . import theme, widgets
from .preview_dialog import PreviewDialog, ResultDialog

#: The toggle's positions, left to right, with one line on what each does.
_LEVELS = (
    (
        OptimizationLevel.NORMAL, "Обычная",
        "Только безопасные изменения. Средние остаются в списке пропущенных.",
    ),
    (
        OptimizationLevel.MEDIUM, "Средняя",
        "Добавлены изменения со средним риском — без галочек, отметите сами.",
    ),
    (
        OptimizationLevel.HARD, "Жёсткая",
        "Отмечено всё, включая необратимое удаление приложений. Проверка "
        "перед применением остаётся.",
    ),
)


class OptimizeActions:
    """Mixin: OPTIMIZE PC, from the level toggle to the report."""

    # -- construction ------------------------------------------------------

    def _build_level_toggle(self) -> QVBoxLayout:
        # A layout, not a widget: a plain container widget would paint the
        # window background over the card it sits in.
        layout = QVBoxLayout()
        layout.setSpacing(6)
        layout.addWidget(widgets.label("РЕЖИМ ОПТИМИЗАЦИИ", "SectionHeading"))
        self._level = widgets.Segmented(
            [(text, level) for level, text, _ in _LEVELS], OptimizationLevel.NORMAL
        )
        self._level.changed.connect(self._show_level)
        layout.addWidget(self._level)
        self._level_hint = widgets.label("", "ScoreNote")
        self._level_hint.setWordWrap(True)
        layout.addWidget(self._level_hint)
        self._show_level(OptimizationLevel.NORMAL)
        return layout

    def _show_level(self, level: object) -> None:
        """Say what the chosen level does; «Жёсткая» in the warning tone."""
        self._level_hint.setText(next(hint for lv, _, hint in _LEVELS if lv is level))
        self._level_hint.setStyleSheet(
            f"color: {theme.WARNING};" if level is OptimizationLevel.HARD else ""
        )

    # -- planning ----------------------------------------------------------

    def _on_optimize(self) -> None:
        """Build the preview at the chosen level. Nothing changes until accepted."""
        if self._result is None or self._optimizer.busy:  # type: ignore[attr-defined]
            return
        self._plan(self._level.value())

    def _plan(self, level: object) -> None:
        self._optimize.setEnabled(False)  # type: ignore[attr-defined]
        self._rescan.setEnabled(False)  # type: ignore[attr-defined]
        self._status.setText("Планирование изменений…")  # type: ignore[attr-defined]
        self._optimizer.start_preview(  # type: ignore[attr-defined]
            self._result.snapshot, self._result.issues, level=level  # type: ignore[attr-defined]
        )

    def _on_preview_ready(self, preview: object) -> None:
        """Show the plan and ask for confirmation (rule #39)."""
        self._status.setText("")  # type: ignore[attr-defined]
        self._rescan.setEnabled(True)  # type: ignore[attr-defined]
        self._optimize.setEnabled(True)  # type: ignore[attr-defined]

        result = self._result  # type: ignore[attr-defined]
        dialog = PreviewDialog(  # type: ignore[arg-type]
            preview, self, snapshot=result.snapshot if result else None
        )
        outcome = dialog.exec()
        if outcome == PreviewDialog.SHOW_MEDIUM:
            # Asked from a «Обычная» plan to see what the risk gate held back:
            # that is «Средняя», so the toggle says so, and the plan is built
            # again — the gate is part of planning, and a plan built without
            # MEDIUM must not quietly start containing it.
            self._level.set_value(OptimizationLevel.MEDIUM)
            self._show_level(OptimizationLevel.MEDIUM)
            self._plan(OptimizationLevel.MEDIUM)
            return
        if outcome != PreviewDialog.DialogCode.Accepted:
            return
        chosen = dialog.selected_preview()  # only what the operator ticked

        self._optimize.setEnabled(False)  # type: ignore[attr-defined]
        self._rescan.setEnabled(False)  # type: ignore[attr-defined]
        self._set_state("ОПТИМИЗАЦИЯ", None)  # type: ignore[attr-defined]
        self._status.setText("Применение изменений…")  # type: ignore[attr-defined]
        self._show_work(None)
        self._optimizer.start_apply(chosen)  # type: ignore[attr-defined]

    # -- applying ----------------------------------------------------------

    def _on_apply_progress(self, event: object) -> None:
        self._status.setText(event.text)  # type: ignore[attr-defined]
        self._show_work(event.fraction)  # type: ignore[attr-defined]

    def _show_work(self, fraction: float | None) -> None:
        if fraction is None:
            self._work.setRange(0, 0)  # type: ignore[attr-defined]  # busy: the step cannot tell
        else:
            self._work.setRange(0, 1000)  # type: ignore[attr-defined]
            self._work.setValue(round(fraction * 1000))  # type: ignore[attr-defined]
        self._work.show()  # type: ignore[attr-defined]

    def _on_optimize_finished(self, outcome: object) -> None:
        self._status.setText("")  # type: ignore[attr-defined]
        self._work.hide()  # type: ignore[attr-defined]
        self._rescan.setEnabled(True)  # type: ignore[attr-defined]
        ResultDialog(outcome, self).exec()  # type: ignore[arg-type]
        # The machine changed, so the dashboard must re-measure rather than
        # keep showing the state that justified the changes. A refresh, not
        # a scan: the pipeline has just taken the after-state reading for
        # its own report, and repeating the two PowerShell calls to learn
        # which CPU is installed and what the ping is would add four
        # seconds to an operation that has already finished.
        self._controller.refresh()  # type: ignore[attr-defined]

    def _on_optimize_failed(self, message: str) -> None:
        self._status.setText("")  # type: ignore[attr-defined]
        self._work.hide()  # type: ignore[attr-defined]
        self._rescan.setEnabled(True)  # type: ignore[attr-defined]
        self._optimize.setEnabled(True)  # type: ignore[attr-defined]
        self._set_state("ТРЕБУЕТСЯ ДЕЙСТВИЕ", theme.WARNING)  # type: ignore[attr-defined]
        QMessageBox.warning(self, "Сбой оптимизации", message)  # type: ignore[arg-type]
