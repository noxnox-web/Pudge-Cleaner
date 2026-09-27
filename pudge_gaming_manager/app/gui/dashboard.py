"""The main dashboard window.

Layout follows the brief: machine identity, scan state and rescan at the top;
the measured metrics and the grouped tools on the left; the score with the
single primary action, and the findings, on the right.

Presentation rules this window enforces:

* An unavailable value reads ``UNAVAILABLE`` in grey with its reason in the
  tooltip. It never shows ``0`` and never borrows a warning colour.
* The score shows its own arithmetic on demand (rule #30).
* ``OPTIMIZE PC`` is the only primary button, and is disabled until a scan
  has finished: a button that does nothing teaches an operator to distrust it.
* Status colours mark status. A scan or an apply in progress is not a
  warning, so it is written in the muted tone, not in amber.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..controllers.optimize_controller import OptimizeController
from ..controllers.cleanup_controller import CleanupController
from ..controllers.profile_controller import ProfileController
from ..controllers.scan_controller import ScanController, ScanResult
from ..controllers.steam_controller import SteamController
from ..controllers.system_controller import SystemController
from ..controllers.tools_controller import ToolsController
from . import presenters, theme, widgets
from .dashboard_actions import ProfileAndGamesActions
from .optimize_actions import OptimizeActions
from .system_actions import SystemActions
from .resources import logo_path
from .widgets import MetricRow


class Dashboard(ProfileAndGamesActions, SystemActions, OptimizeActions, QMainWindow):
    """Administrator dashboard."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Pudge Cleaner")
        self.setWindowIcon(QIcon(logo_path()))
        self.resize(1180, 760)
        self.setStyleSheet(theme.stylesheet())

        self._result: ScanResult | None = None
        self._controller = ScanController(self)
        # The optimizer measures the after-state with the scan
        # controller's scanner, so both share one cached inventory.
        self._optimizer = OptimizeController(
            parent=self, scanner=self._controller.scanner
        )
        self._profiles = ProfileController(self)
        self._steam = SteamController(self)
        self._cleanup = CleanupController(self)
        self._tools = ToolsController(self)
        self._system = SystemController(self)
        # Held only while the startup dialog is open, so a toggle's
        # result can be routed back to the row that asked for it.
        self._startup_dialog = None

        # Widgets must exist before any signal is bound to them.
        self._build()

        self._controller.started.connect(self._on_scan_started)
        self._controller.progress.connect(self._status.setText)
        self._controller.completed.connect(self._on_scan_completed)
        self._controller.failed.connect(self._on_scan_failed)

        self._optimizer.preview_ready.connect(self._on_preview_ready)
        self._optimizer.apply_progress.connect(self._on_apply_progress)
        self._optimizer.apply_finished.connect(self._on_optimize_finished)
        self._optimizer.failed.connect(self._on_optimize_failed)

        self._profiles.saved.connect(self._on_profile_saved)
        self._profiles.compared.connect(self._on_profile_compared)
        self._profiles.failed.connect(self._on_profile_failed)

        self._steam.planned.connect(self._on_steam_planned)
        self._steam.wiped.connect(self._on_steam_wiped)
        self._steam.failed.connect(self._on_steam_failed)

        self._cleanup.planned.connect(self._on_cleanup_planned)
        self._cleanup.cleaned.connect(self._on_cleanup_finished)
        self._cleanup.failed.connect(self._on_cleanup_failed)

        self._tools.usage_ready.connect(self._on_usage_ready)
        self._tools.startup_ready.connect(self._on_startup_ready)
        self._tools.startup_toggled.connect(self._on_startup_toggled)
        self._tools.failed.connect(self._on_tools_failed)
        self._connect_system()

        self._controller.start()
        # After the window is up, report any change a previous run left
        # unfinished (a crash or power cut mid-apply).
        QTimer.singleShot(0, self._report_interrupted_changes)

    # -- construction ------------------------------------------------------

    def _build(self) -> None:
        root = QWidget()
        outer = QVBoxLayout(root)
        outer.setContentsMargins(28, 24, 28, 24)
        outer.setSpacing(18)

        outer.addLayout(self._build_header())
        outer.addLayout(self._build_body(), stretch=1)
        self.setCentralWidget(root)

    def _build_header(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(14)

        # The club's own mark, at the height of the two text lines beside it.
        logo = QLabel()
        logo.setPixmap(QIcon(logo_path()).pixmap(46, 46))
        logo.setFixedSize(46, 46)
        row.addWidget(logo, alignment=Qt.AlignmentFlag.AlignVCenter)

        left = QVBoxLayout()
        left.setSpacing(2)

        # The name and its byline share a row so the credit sits on the
        # title's baseline rather than under it, where it would read as a
        # subtitle and crowd out the machine name.
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        title_row.addWidget(widgets.label("PUDGE CLEANER", "Title"))
        byline = widgets.label("by fortnoxycake", "Byline")
        byline.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom
        )
        title_row.addWidget(byline)
        title_row.addStretch(1)
        left.addLayout(title_row)

        self._machine = widgets.label("Сканирование…", "Subtitle")
        left.addWidget(self._machine)

        right = QVBoxLayout()
        right.setSpacing(2)
        self._state = widgets.label("", "ScoreCaption")
        self._state.setAlignment(Qt.AlignmentFlag.AlignRight)
        self._status = widgets.label("", "ScoreNote")
        self._status.setAlignment(Qt.AlignmentFlag.AlignRight)
        right.addWidget(self._state)
        right.addWidget(self._status)
        # Shown only while OPTIMIZE writes: filled when the running change
        # can say how far it is (windows.old takes minutes), busy otherwise.
        self._work = QProgressBar()
        self._work.setTextVisible(False)
        self._work.setFixedWidth(280)
        self._work.hide()
        right.addWidget(self._work, alignment=Qt.AlignmentFlag.AlignRight)
        self._set_state("СКАНИРОВАНИЕ", None)

        # Rescan sits with the scan state it refreshes, not among the tools.
        self._rescan = self._secondary("Пересканировать", self._controller.start)

        row.addLayout(left)
        row.addStretch(1)
        row.addLayout(right)
        row.addWidget(self._rescan, alignment=Qt.AlignmentFlag.AlignVCenter)
        return row

    def _set_state(self, text: str, colour: str | None) -> None:
        """Header state. ``None`` is work in progress: muted, not a status."""
        self._state.setText(text)
        self._state.setStyleSheet(f"color: {colour or theme.TEXT_MUTED};")

    def _build_body(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(18)

        left = QVBoxLayout()
        left.setSpacing(18)
        left.addWidget(self._build_metrics())
        left.addWidget(self._build_tools(), stretch=1)
        row.addLayout(left, stretch=3)

        right = QVBoxLayout()
        right.setSpacing(18)
        right.addWidget(self._build_score())
        self._findings = widgets.FindingsCard()
        right.addWidget(self._findings, stretch=1)
        row.addLayout(right, stretch=4)
        return row

    def _build_metrics(self) -> QFrame:
        card = widgets.card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(2)

        layout.addWidget(widgets.label("СИСТЕМА", "SectionHeading"))
        layout.addSpacing(6)

        self._metrics: dict[str, MetricRow] = {}
        for key, name in (
            ("cpu", "CPU"),
            ("gpu", "GPU"),
            ("ram", "Память"),
            ("disk", "Системный диск"),
            ("display", "Дисплей"),
            ("network", "Сеть"),
        ):
            widget = MetricRow(name)
            self._metrics[key] = widget
            layout.addWidget(widget)

        layout.addSpacing(8)
        divider = QFrame()
        divider.setObjectName("Divider")
        divider.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(divider)
        layout.addSpacing(8)

        self._scan_meta = widgets.label("", "MetricDetail")
        self._scan_meta.setWordWrap(True)
        layout.addWidget(self._scan_meta)
        return card

    def _build_tools(self) -> QFrame:
        """Every secondary action, grouped by what the operator is doing.

        The buttons are created by the mixins that handle them, so a button
        and its handler stay in one file; the grouping is layout, and
        layout lives here. Two columns keep the window under 1024 px wide.
        """
        self._build_actions()
        self._build_system_actions()
        groups = (
            (
                ("ДИАГНОСТИКА", (self._audit_button, self._disk_usage, self._monitor_button)),
                ("ПРОФИЛЬ КЛУБА", (self._save_profile, self._compare_profile)),
            ),
            (
                ("НАСТРОЙКА", (self._settings_button, self._startup, self._history_button)),
                ("ОЧИСТКА", (self._clean_disk, self._reset_steam)),
            ),
        )
        return widgets.grouped_actions(groups)

    def _build_score(self) -> QFrame:
        card = widgets.card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(6)

        layout.addWidget(widgets.label("ГОТОВНОСТЬ К ИГРАМ", "SectionHeading"))

        self._score_value = widgets.label("—", "ScoreValue")
        self._score_value.setStyleSheet(f"color: {theme.UNAVAILABLE};")
        layout.addWidget(self._score_value)

        self._score_note = widgets.label(
            "Диагностический показатель, не прогноз FPS.", "ScoreNote"
        )
        self._score_note.setWordWrap(True)
        layout.addWidget(self._score_note)

        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setTextVisible(False)
        layout.addWidget(self._progress)
        layout.addSpacing(10)
        # The level is chosen here, before planning, so the first plan
        # already holds what the operator wants to review.
        layout.addLayout(self._build_level_toggle())
        layout.addSpacing(8)

        buttons = QHBoxLayout()
        self._optimize = QPushButton("Оптимизировать ПК")
        self._optimize.setObjectName("Primary")
        self._optimize.setEnabled(False)
        self._optimize.clicked.connect(self._on_optimize)
        buttons.addWidget(self._optimize, stretch=2)

        self._explain = QPushButton("Как это посчитано?")
        self._explain.setObjectName("Secondary")
        self._explain.setEnabled(False)
        self._explain.clicked.connect(self._on_explain)
        buttons.addWidget(self._explain, stretch=1)
        layout.addLayout(buttons)
        return card

    # -- scan lifecycle ----------------------------------------------------

    def _on_scan_started(self) -> None:
        self._set_state("СКАНИРОВАНИЕ", None)
        self._rescan.setEnabled(False)
        self._optimize.setEnabled(False)
        self._explain.setEnabled(False)
        self._set_profile_actions(False)

    def _on_scan_failed(self, message: str) -> None:
        self._set_state("СБОЙ СКАНИРОВАНИЯ", theme.CRITICAL)
        self._status.setText("")
        self._rescan.setEnabled(True)
        QMessageBox.warning(self, "Сбой сканирования", message)

    def _on_scan_completed(self, result: ScanResult) -> None:
        self._result = result
        self._rescan.setEnabled(True)
        self._status.setText("")

        self._populate_metrics(result)
        self._populate_score(result)
        self._findings.show_issues(result.issues)

        actionable = len(result.actionable_issues)
        if actionable:
            self._set_state("ТРЕБУЕТСЯ ДЕЙСТВИЕ", theme.WARNING)
        else:
            self._set_state("ГОТОВО", theme.GOOD)

        # Always available once a scan has finished. It used to wait for a
        # fixable *finding* (low disk space, a slow monitor), but the plan
        # checks far more than the findings — temp files, the power scheme,
        # USB suspend, pointer acceleration, Game DVR, the settings
        # catalogue, apps — so a PC with no findings still had work for it
        # and no way to reach it. The preview says when nothing is needed.
        self._optimize.setEnabled(True)
        self._explain.setEnabled(True)
        self._set_profile_actions(True)

        snapshot = result.snapshot
        self._machine.setText(
            f"{snapshot.os.machine_name} · {snapshot.os.caption} "
            f"(build {snapshot.os.build})"
        )
        self._scan_meta.setText(
            f"Сканирование за {snapshot.scan_duration_s:.1f} с · "
            f"предупреждений: {len(snapshot.warnings)}"
        )

    # -- population --------------------------------------------------------

    def _populate_metrics(self, result: ScanResult) -> None:
        """Render each row from its presenter.

        All formatting lives in ``presenters`` so it can be tested without
        constructing a window.
        """
        for key, widget in self._metrics.items():
            view = (
                presenters.network_view(result.network)
                if key == "network"
                else presenters.METRIC_VIEWS[key](result.snapshot)
            )
            widget.set(view.value, view.status, view.detail, tooltip=view.tooltip)

    def _populate_score(self, result: ScanResult) -> None:
        score = result.score
        colour = theme.score_colour(score.value)
        self._score_value.setText(score.display())
        self._score_value.setStyleSheet(f"color: {colour};")
        self._progress.setValue(int(score.value or 0))
        self._progress.setStyleSheet(
            f"QProgressBar::chunk {{ background-color: {colour}; border-radius: 3px; }}"
        )
        if score.excluded:
            self._score_note.setText(
                "Диагностический показатель, не прогноз FPS. "
                f"Исключено: {', '.join(score.excluded)}."
            )

    # -- actions -----------------------------------------------------------

    def _on_explain(self) -> None:
        if self._result is None:
            return
        score = self._result.score
        body = "\n".join(f"• {line}" for line in score.formula_lines())
        QMessageBox.information(
            self,
            "Как считается оценка готовности",
            f"Оценка: {score.display()}\n\n{body}\n\n"
            "Это диагностический показатель, а не прогноз FPS. Рост "
            "оценки сам по себе не доказывает прирост производительности.",
        )

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        # A write in progress must not be interrupted: applying a tweak or
        # removing games between the change and its verification could strand
        # the system half-done, so either one blocks the close outright.
        # Scans and profile reads only read, so closing waits for them to
        # finish (shutdown joins the worker).
        blocker = (
            "Применяются изменения."
            if self._optimizer.applying
            else "Идёт очистка Стима."
            if self._steam.wiping
            else "Идёт очистка диска."
            if self._cleanup.cleaning
            else "Применяются или откатываются настройки."
            if self._system.applying
            else None
        )
        if blocker is not None:
            QMessageBox.warning(
                self, "Операция выполняется",
                f"{blocker} Дождитесь завершения перед закрытием.",
            )
            event.ignore()
            return
        self._controller.shutdown()
        self._optimizer.shutdown()
        self._profiles.shutdown()
        self._steam.shutdown()
        self._cleanup.shutdown()
        self._tools.shutdown()
        self._system.shutdown()
        super().closeEvent(event)
