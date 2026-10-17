"""The «Ядра GPU» tab: find the quietest core for the graphics interrupts.

Two programs' worth of work in one place, in the order the guide does it:

1. **Measure** (AutoGpuAffinity): for each physical core except the main one,
   pin the video card's interrupts to it and record the frame rate of a test
   scene. The screen goes dark for a few seconds per core while the driver
   restarts, and the PC should not be used meanwhile.
2. **Pin** (GoInterruptPolicy): write the chosen core into the video card's
   interrupt policy, with high priority. The operator picks the core — the
   best-measured one is only preselected — and confirms a preview first.
3. **Restart the driver** to make it live, or reboot. Separate, because it
   ends running games.

This widget only asks and shows; every change is made by the controller's
worker through ``core`` (rule #12), and a measurement that is interrupted
puts the original setting back before it reports.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ...core.gpu_affinity import selection
from ...core.gpu_affinity.scan import ScanOutcome, ScanSettings, estimate_seconds
from ...core.gpu_affinity.service import Overview
from ...core.optimization.tweak import Outcome
from ..controllers.affinity_controller import AffinityController
from . import theme, widgets
from .affinity_results import ResultsTable, summary_text

_INTRO = (
    "Что делает вкладка. 1) Проверяет по очереди физические ядра процессора "
    "(кроме основного): закрепляет за каждым прерывания видеокарты и меряет "
    "частоту кадров тестовой сценой — как AutoGpuAffinity. 2) Лучшее ядро вы "
    "закрепляете за видеокартой — как в GoInterruptPolicy: режим прерываний "
    "«Specified Processors», приоритет «High». Во время замера видеодрайвер "
    "перезапускается для каждого ядра, экран гаснет на несколько секунд; "
    "закройте игры и не пользуйтесь ПК. Прежние настройки вернутся сами."
)

class AffinityTab(QWidget):
    def __init__(
        self, parent: QWidget | None = None, controller: AffinityController | None = None
    ) -> None:
        super().__init__(parent)
        self._controller = controller or AffinityController(self)
        self._overview: Overview | None = None
        self._checks: list[tuple[QCheckBox, object]] = []
        self._outcome: ScanOutcome | None = None
        self._ranked: list[selection.CoreResult] = []
        self._pinned = False
        self._loaded = False

        self._build()
        c = self._controller
        c.overview_ready.connect(self._on_overview)
        c.progress.connect(self._on_progress)
        c.scan_done.connect(self._on_scan_done)
        c.planned.connect(self._on_planned)
        c.applied.connect(self._on_applied)
        c.restarted.connect(self._on_restarted)
        c.failed.connect(self._on_failed)

    # -- construction ------------------------------------------------------------

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 18, 28, 24)
        outer.setSpacing(14)

        intro = widgets.label(_INTRO, "Subtitle")
        intro.setWordWrap(True)
        outer.addWidget(intro)

        body = QHBoxLayout()
        body.setSpacing(18)
        left = QVBoxLayout()
        left.setSpacing(14)
        left.addWidget(self._build_adapters())
        left.addWidget(self._build_scan())
        left.addStretch(1)
        body.addLayout(left, stretch=4)
        body.addWidget(self._build_results(), stretch=5)
        outer.addLayout(body, stretch=1)

    def _build_adapters(self):
        card = widgets.card()
        self._adapters_box = QVBoxLayout(card)
        self._adapters_box.setContentsMargins(20, 16, 20, 16)
        self._adapters_box.setSpacing(6)
        self._adapters_box.addWidget(widgets.label("ВИДЕОКАРТА", "SectionHeading"))
        self._adapter_rows = QVBoxLayout()
        self._adapter_rows.setSpacing(8)
        self._adapters_box.addLayout(self._adapter_rows)
        self._blockers = widgets.label("", "MetricDetail")
        self._blockers.setWordWrap(True)
        self._blockers.setStyleSheet(f"color: {theme.WARNING};")
        self._adapters_box.addWidget(self._blockers)
        return card

    def _build_scan(self):
        card = widgets.card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(8)
        layout.addWidget(widgets.label("ЗАМЕР", "SectionHeading"))

        row = QHBoxLayout()
        row.addWidget(widgets.label("Длительность замера на ядро, с", "MetricName"))
        row.addStretch(1)
        self._seconds = QSpinBox()
        self._seconds.setRange(10, 120)
        self._seconds.setValue(ScanSettings().benchmark_s)
        self._seconds.valueChanged.connect(self._update_estimate)
        row.addWidget(self._seconds)
        layout.addLayout(row)

        self._pin_scene = QCheckBox("Запускать сцену на том же ядре (как в AutoGpuAffinity)")
        self._pin_scene.setChecked(True)
        layout.addWidget(self._pin_scene)

        self._estimate = widgets.label("", "MetricDetail")
        self._estimate.setWordWrap(True)
        layout.addWidget(self._estimate)

        self._work = QProgressBar()
        self._work.setTextVisible(False)
        self._work.hide()
        layout.addWidget(self._work)
        self._status = widgets.label("", "MetricDetail")
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        buttons = QHBoxLayout()
        self._start = QPushButton("Начать замер")
        self._start.setObjectName("Primary")
        self._start.clicked.connect(self._on_start)
        buttons.addWidget(self._start, stretch=2)
        self._stop = QPushButton("Остановить")
        self._stop.setObjectName("Secondary")
        self._stop.clicked.connect(self._on_stop)
        buttons.addWidget(self._stop, stretch=1)
        layout.addLayout(buttons)
        return card

    def _build_results(self):
        card = widgets.card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(8)
        layout.addWidget(widgets.label("РЕЗУЛЬТАТ, FPS", "SectionHeading"))

        self._table = ResultsTable()
        self._table.itemSelectionChanged.connect(self._sync)
        layout.addWidget(self._table, stretch=1)

        self._summary = widgets.label(
            "Здесь появится таблица после замера. Выберите строку — это ядро и "
            "будет закреплено за видеокартой.", "MetricDetail",
        )
        self._summary.setWordWrap(True)
        layout.addWidget(self._summary)

        buttons = QHBoxLayout()
        self._apply = QPushButton("Закрепить за ядром")
        self._apply.setObjectName("Primary")
        self._apply.clicked.connect(self._on_apply)
        buttons.addWidget(self._apply, stretch=2)
        self._restart = QPushButton("Перезапустить видеодрайвер")
        self._restart.setObjectName("Secondary")
        self._restart.clicked.connect(self._on_restart)
        buttons.addWidget(self._restart, stretch=2)
        layout.addLayout(buttons)

        hint = widgets.label(
            "Изменение откатывается в «Истории изменений». Вступает в силу "
            "после перезапуска видеодрайвера или перезагрузки.", "MetricDetail",
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self._sync()
        return card

    # -- loading -------------------------------------------------------------------

    def showEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        if not self._loaded:
            self._loaded = True
            self.refresh()

    def refresh(self) -> None:
        self._controller.load_overview()

    def _on_overview(self, overview: Overview) -> None:
        self._overview = overview
        previous = {
            info.instance_id: box.isChecked() for box, info in self._checks
        }
        while self._adapter_rows.count():
            item = self._adapter_rows.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self._checks = []
        for info in overview.adapters:
            row = QVBoxLayout()
            row.setSpacing(0)
            box = QCheckBox(info.name)
            box.setChecked(previous.get(info.instance_id, True))
            box.toggled.connect(self._sync)
            detail = widgets.label(
                f"Сейчас: {info.policy.label}  ·  MSI: {info.msi_label}", "MetricDetail"
            )
            detail.setWordWrap(True)
            detail.setContentsMargins(26, 0, 0, 0)
            holder = QWidget()
            holder.setStyleSheet("background: transparent;")
            inner = QVBoxLayout(holder)
            inner.setContentsMargins(0, 0, 0, 0)
            inner.setSpacing(0)
            inner.addWidget(box)
            inner.addWidget(detail)
            if info.msi_on is not True:
                warn = widgets.label(
                    "MSI не включён: закрепление прерываний может не подействовать. "
                    "Программа не включает MSI сама — на драйвере без его поддержки "
                    "устройство остаётся без прерываний.", "MetricDetail",
                )
                warn.setWordWrap(True)
                warn.setContentsMargins(26, 2, 0, 0)
                warn.setStyleSheet(f"color: {theme.WARNING};")
                inner.addWidget(warn)
            self._adapter_rows.addWidget(holder)
            self._checks.append((box, info))
        self._blockers.setText("\n".join(f"• {b}" for b in overview.blockers))
        self._update_estimate()
        self._sync()

    def _update_estimate(self) -> None:
        if self._overview is None:
            return
        cpus = self._overview.candidates
        if not cpus:
            self._estimate.setText(self._overview.main_core_note)
            return
        minutes = estimate_seconds(
            len(cpus), ScanSettings(benchmark_s=self._seconds.value())
        ) / 60
        listed = ", ".join(str(n) for n in cpus)
        self._estimate.setText(
            f"{self._overview.main_core_note}\nБудет проверено ядер: {len(cpus)} "
            f"(CPU {listed}), примерно {minutes:.0f} мин."
        )

    # -- state of the buttons ---------------------------------------------------------------

    def _selected_adapters(self) -> list[tuple[str, str]]:
        return [info.pair for box, info in self._checks if box.isChecked()]  # type: ignore[attr-defined]

    def _selected_cpu(self) -> int | None:
        return self._table.selected_cpu()

    def _sync(self) -> None:
        busy = self._controller.busy
        ready = (
            self._overview is not None
            and not self._overview.blockers
            and bool(self._selected_adapters())
        )
        self._start.setEnabled(ready and not busy)
        self._stop.setEnabled(self._controller.scanning)
        cpu = self._selected_cpu()
        self._apply.setEnabled(cpu is not None and bool(self._selected_adapters()) and not busy)
        self._apply.setText(
            "Закрепить за ядром" if cpu is None else f"Закрепить за CPU {cpu}"
        )
        self._restart.setEnabled(self._pinned and not busy)
        for box, _info in self._checks:
            box.setEnabled(not busy)
        self._seconds.setEnabled(not busy)
        self._pin_scene.setEnabled(not busy)

    def _begin(self, text: str) -> None:
        self._status.setText(text)
        self._work.setRange(0, 0)
        self._work.show()
        self._sync()

    def _end(self) -> None:
        self._status.setText("")
        self._work.hide()

    def _on_progress(self, event: object) -> None:
        self._status.setText(event.text)  # type: ignore[attr-defined]
        fraction = event.fraction  # type: ignore[attr-defined]
        if fraction is None:
            self._work.setRange(0, 0)
        else:
            self._work.setRange(0, 1000)
            self._work.setValue(round(fraction * 1000))
        self._work.show()

    def blocks_close(self) -> str | None:
        """Why the window must not close now, or ``None``."""
        if self._controller.changing:
            return "Идёт замер ядер GPU или изменение видеокарты."
        return None

    def shutdown(self) -> None:
        self._controller.shutdown()

    # -- measuring --------------------------------------------------------------------------------

    def _on_start(self) -> None:
        if self._overview is None:
            return
        minutes = estimate_seconds(
            len(self._overview.candidates), ScanSettings(benchmark_s=self._seconds.value())
        ) / 60
        answer = QMessageBox.question(
            self, "Начать замер ядер GPU?",
            f"Замер займёт примерно {minutes:.0f} мин. Для каждого ядра видеодрайвер "
            "перезапускается: экран несколько раз погаснет, а запущенные игры "
            "и приложения с видео закроются.\n\nЗакройте игры и не пользуйтесь "
            "ПК до конца. Прежняя настройка прерываний вернётся сама, даже "
            "если замер остановить.\n\nНачать?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._outcome = None
        self._begin("Подготовка…")
        self._controller.start_scan(
            self._selected_adapters(),
            ScanSettings(
                benchmark_s=self._seconds.value(), pin_scene=self._pin_scene.isChecked()
            ),
        )
        self._sync()

    def _on_stop(self) -> None:
        self._stop.setEnabled(False)
        self._status.setText("Останавливаю: дождусь конца шага и верну прежние настройки…")
        self._controller.stop_scan()

    def _on_scan_done(self, outcome: ScanOutcome) -> None:
        self._end()
        self._outcome = outcome
        self._fill(outcome)
        self._sync()
        self.refresh()
        if outcome.restore_error:
            QMessageBox.critical(
                self, "Прежние настройки не вернулись",
                "Замер закончен, но вернуть прежнюю политику прерываний или "
                f"перезапустить видеокарту не удалось:\n\n{outcome.restore_error}\n\n"
                "Откройте «Историю изменений» или перезагрузите ПК и проверьте "
                "эту вкладку.",
            )

    def _fill(self, outcome: ScanOutcome) -> None:
        self._ranked = selection.ranked(outcome.results)
        self._table.show_results(self._ranked)
        self._summary.setText(summary_text(self._ranked, outcome))

    # -- choosing -----------------------------------------------------------------------------------

    def _on_apply(self) -> None:
        cpu = self._selected_cpu()
        if cpu is None:
            return
        self._begin("Готовлю предпросмотр…")
        self._controller.plan(self._selected_adapters(), cpu)
        self._sync()

    def _on_planned(self, plan: object) -> None:
        self._end()
        self._sync()
        if not plan.applicable:  # type: ignore[attr-defined]
            reasons = "\n".join(
                f"• {c.summary}" + (f" ({c.skip_reason})" if c.skip_reason else "")
                for c in plan.changes  # type: ignore[attr-defined]
            )
            QMessageBox.information(self, "Менять нечего", reasons or "Изменений не требуется.")
            return
        lines = "\n".join(f"• {c.summary}" for c in plan.applicable)  # type: ignore[attr-defined]
        answer = QMessageBox.question(
            self, "Закрепить прерывания видеокарты?",
            f"{lines}\n\nУровень риска: средний. Перед изменением создаётся "
            "резервная запись, после — проверка чтением из реестра. Откатить "
            "можно в «Истории изменений». Вступит в силу после перезапуска "
            "видеодрайвера или перезагрузки.\n\nПрименить?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._begin("Применяю…")
        self._controller.apply(plan)
        self._sync()

    def _on_applied(self, payload: object) -> None:
        report, notes = payload  # type: ignore[misc]
        self._end()
        result = report.results[0] if report.results else None
        ok = result is not None and result.outcome is Outcome.SUCCESS
        self._pinned = ok
        self._sync()
        self.refresh()
        if ok:
            QMessageBox.information(
                self, "Прерывания закреплены",
                f"{result.detail}\n\nЧтобы заработало сейчас, нажмите «Перезапустить "
                "видеодрайвер» (экран на несколько секунд погаснет) или "
                "перезагрузите ПК.",
            )
        else:
            detail = result.detail if result else "Изменение не выполнено."
            QMessageBox.warning(
                self, "Не удалось закрепить",
                f"{detail}\n\nИзменения откачены, прежняя политика на месте.",
            )

    # -- making it live ---------------------------------------------------------------------------------

    def _on_restart(self) -> None:
        answer = QMessageBox.question(
            self, "Перезапустить видеодрайвер?",
            "Экран на несколько секунд погаснет, а запущенные игры и "
            "приложения с видео закроются.\n\nПерезапустить?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._begin("Перезапуск видеодрайвера…")
        self._controller.restart_adapters(self._selected_adapters())
        self._sync()

    def _on_restarted(self, problems: list[str]) -> None:
        self._end()
        self._sync()
        if problems:
            QMessageBox.warning(
                self, "Видеокарта не вернулась",
                "\n".join(problems) + "\n\nПерезагрузите ПК.",
            )

    def _on_failed(self, message: str) -> None:
        self._end()
        self._sync()
        QMessageBox.warning(self, "Ошибка", message)
