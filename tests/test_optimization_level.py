"""The OPTIMIZE level toggle: «Обычная», «Средняя», «Жёсткая».

The level decides what the plan contains and what arrives ticked in the
preview; the preview itself is shown at every level. Nothing here plans
against, or changes, the real machine: plans are built by hand from stubs.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt

from pudge_gaming_manager.core.optimization.engine import Plan, PlannedChange
from pudge_gaming_manager.core.optimization.pipeline import (
    OptimizationLevel,
    OptimizationPreview,
)
from pudge_gaming_manager.core.optimization.tweak import (
    BackupScope,
    RiskLevel,
    TweakState,
    Validation,
)

LOW, MEDIUM = RiskLevel.LOW, RiskLevel.MEDIUM


def _change(risk: RiskLevel, scope: BackupScope = BackupScope.REGISTRY, **kwargs) -> PlannedChange:
    tweak = SimpleNamespace(
        id=f"stub.{risk.value}.{scope.value}", name="stub", risk=risk, scope=scope,
        rationale="why", description="what",
    )
    return PlannedChange(
        tweak,  # type: ignore[arg-type]
        TweakState(needs_change=True, summary=f"{risk.value} {scope.value}"),
        Validation.allow(),
        **{"will_apply": True, **kwargs},
    )


def _preview(level: OptimizationLevel, *changes: PlannedChange) -> OptimizationPreview:
    return OptimizationPreview(
        plan=Plan(run_id="r", changes=changes),
        score_before=None,  # type: ignore[arg-type]
        level=level,
    )


# -- what the level means --------------------------------------------------


@pytest.mark.parametrize(
    ("level", "allows"),
    [
        (OptimizationLevel.NORMAL, False),
        (OptimizationLevel.MEDIUM, True),
        (OptimizationLevel.HARD, True),
    ],
)
def test_only_normal_keeps_medium_behind_the_gate(level, allows) -> None:
    assert level.allows_risk_above_low is allows
    assert _preview(level).allow_risk_above_low is allows


def test_low_is_ticked_at_every_level() -> None:
    for level in OptimizationLevel:
        assert _preview(level).ticked_by_default(_change(LOW))


def test_medium_arrives_unticked_at_medium() -> None:
    """Seeing the class is not choosing a change in it (rule #38)."""
    assert not _preview(OptimizationLevel.MEDIUM).ticked_by_default(_change(MEDIUM))


def test_hard_ticks_everything_including_irreversible() -> None:
    preview = _preview(OptimizationLevel.HARD)
    assert preview.ticked_by_default(_change(MEDIUM))
    assert preview.ticked_by_default(_change(MEDIUM, BackupScope.NONE))


def test_narrowing_to_the_selection_keeps_the_level() -> None:
    preview = _preview(OptimizationLevel.HARD, _change(LOW), _change(MEDIUM))
    assert preview.with_selection({0}).level is OptimizationLevel.HARD


# -- the preview dialog ------------------------------------------------------


def _ticked(app, preview: OptimizationPreview) -> set[int]:
    from pudge_gaming_manager.app.gui.preview_dialog import PreviewDialog

    return PreviewDialog(preview).selected_indices()


def test_the_dialog_ticks_medium_only_at_hard(app) -> None:
    changes = (_change(LOW), _change(MEDIUM), _change(MEDIUM, BackupScope.NONE))
    assert _ticked(app, _preview(OptimizationLevel.MEDIUM, *changes)) == {0}
    assert _ticked(app, _preview(OptimizationLevel.HARD, *changes)) == {0, 1, 2}


def test_the_dialog_says_when_hard_ticked_everything(app) -> None:
    from PySide6.QtWidgets import QLabel

    from pudge_gaming_manager.app.gui.preview_dialog import PreviewDialog

    def texts(level):
        dialog = PreviewDialog(_preview(level, _change(MEDIUM, BackupScope.NONE)))
        return " ".join(label.text() for label in dialog.findChildren(QLabel))

    assert "«Жёсткая»" in texts(OptimizationLevel.HARD)
    assert "«Жёсткая»" not in texts(OptimizationLevel.MEDIUM)


# -- the toggle ----------------------------------------------------------------


def test_the_toggle_reports_clicks_but_not_programmatic_changes(app) -> None:
    from PySide6.QtWidgets import QPushButton

    from pudge_gaming_manager.app.gui.widgets import Segmented

    toggle = Segmented([("a", 1), ("b", 2), ("c", 3)], 1)
    seen: list = []
    toggle.changed.connect(seen.append)
    assert toggle.value() == 1

    toggle.set_value(2)
    assert toggle.value() == 2 and seen == []

    toggle.findChildren(QPushButton)[2].click()
    assert toggle.value() == 3 and seen == [3]


# -- the main window -----------------------------------------------------------


class _Optimizer:
    busy = False

    def __init__(self) -> None:
        self.levels: list = []

    def start_preview(self, _snapshot, _issues, *, level) -> None:
        self.levels.append(level)


def _window(app):
    """The real mixin on a minimal host carrying what it touches."""
    from PySide6.QtWidgets import QLabel, QPushButton

    from pudge_gaming_manager.app.gui.optimize_actions import OptimizeActions

    class _Window(OptimizeActions):
        # The bar helpers belong to Dashboard; the status line is all the
        # mixin needs from them here.
        def _begin_work(self, text):
            self._status.setText(text)

        def _end_work(self):
            self._status.setText("")

    from PySide6.QtWidgets import QWidget

    window = _Window()
    window._host = QWidget()  # owns the toggle, as the score card does
    window._host.setLayout(window._build_level_toggle())
    window._optimize, window._rescan = QPushButton(), QPushButton()
    window._status = QLabel()
    window._result = SimpleNamespace(snapshot=object(), issues=())
    window._optimizer = _Optimizer()
    return window


def test_optimize_plans_at_the_level_on_the_toggle(app) -> None:
    from pudge_gaming_manager.app.gui.optimize_actions import OptimizationLevel as Level

    window = _window(app)
    window._level.set_value(Level.HARD)
    window._on_optimize()
    assert window._optimizer.levels == [Level.HARD]


def test_the_hint_names_what_the_level_does(app) -> None:
    window = _window(app)
    assert "безопасные" in window._level_hint.text()
    window._show_level(OptimizationLevel.HARD)
    assert "необратимое" in window._level_hint.text()


def test_asking_for_medium_from_the_preview_moves_the_toggle(app, monkeypatch) -> None:
    """The preview's «show MEDIUM» button is «Средняя»: the toggle must agree."""
    from pudge_gaming_manager.app.gui import optimize_actions
    from pudge_gaming_manager.app.gui.preview_dialog import PreviewDialog

    class _Dialog:
        SHOW_MEDIUM = PreviewDialog.SHOW_MEDIUM
        DialogCode = PreviewDialog.DialogCode

        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def exec(self):
            return PreviewDialog.SHOW_MEDIUM

    monkeypatch.setattr(optimize_actions, "PreviewDialog", _Dialog)
    window = _window(app)

    window._on_preview_ready(_preview(OptimizationLevel.NORMAL))

    assert window._level.value() is OptimizationLevel.MEDIUM
    assert "средним риском" in window._level_hint.text()
    assert window._optimizer.levels == [OptimizationLevel.MEDIUM]
