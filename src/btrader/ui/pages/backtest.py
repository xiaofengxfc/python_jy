"""Backtest, parameter optimisation and paper-trading promotion."""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass

import pandas as pd
from PySide6.QtCore import QDateTime, Qt, QThread, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ...backtest import BacktestConfig, BacktestResult, grid_search, run_backtest
from ...context import AppContext
from ...core.types import TIMEFRAMES, MarketType
from ...exchange.klines import KlineStore
from ...strategy.base import ParamSpec
from .. import theme
from ..widgets.chart import CandleChart
from ..widgets.common import Card, MetricCard, PageHeader, money_text, percent_text, pnl_color
from ..widgets.tables import DataTable
from ..workers import TaskRunner

log = logging.getLogger(__name__)


class BacktestWorker(QThread):
    progress = Signal(int, int, str)
    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(self, df: pd.DataFrame, strategy, params: dict, config: BacktestConfig) -> None:
        super().__init__()
        self.df = df
        self.strategy = strategy
        self.params = params
        self.config = config

    def run(self) -> None:  # pragma: no cover - thread body
        try:
            result = run_backtest(self.df, self.strategy, self.params, self.config)
        except Exception as exc:
            log.exception("回测失败")
            self.failed.emit(str(exc) or exc.__class__.__name__)
            return
        self.finished_ok.emit(result)


class GridWorker(QThread):
    progress = Signal(int, int, str)
    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        df: pd.DataFrame,
        strategy,
        grid: dict,
        config: BacktestConfig,
        metric: str,
    ) -> None:
        super().__init__()
        self.df = df
        self.strategy = strategy
        self.grid = grid
        self.config = config
        self.metric = metric
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:  # pragma: no cover - thread body
        try:
            frame = grid_search(
                self.df,
                self.strategy,
                self.grid,
                self.config,
                metric=self.metric,
                top_n=100,
                progress=self._on_progress,
                should_stop=lambda: self._stop,
            )
        except Exception as exc:
            log.exception("参数优化失败")
            self.failed.emit(str(exc) or exc.__class__.__name__)
            return
        self.finished_ok.emit(frame)

    def _on_progress(self, index: int, total: int, params: dict) -> None:
        self.progress.emit(index, total, ", ".join(f"{k}={v}" for k, v in params.items()))


class BacktestPage(QWidget):
    status = Signal(str)
    promote = Signal(object, dict)  # BacktestResult, params

    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.runner = TaskRunner(self, max_threads=2)
        self._df = pd.DataFrame()
        self._result: BacktestResult | None = None
        self._worker: BacktestWorker | None = None
        self._grid_worker: GridWorker | None = None
        self._param_widgets: dict[str, QWidget] = {}

        self._build_ui()
        self._load_strategies()

    # ------------------------------------------------------------------- ui
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(*theme.PAGE_MARGIN)
        outer.setSpacing(theme.PAGE_GAP)
        outer.addWidget(PageHeader("策略回测", "历史数据驱动的策略验证与参数优化"))

        root = QHBoxLayout()
        root.setSpacing(theme.PAGE_GAP)

        config_panel = QScrollArea()
        config_panel.setWidgetResizable(True)
        config_panel.setFixedWidth(340)
        config_panel.setFrameShape(QScrollArea.Shape.NoFrame)
        holder = QWidget()
        side = QVBoxLayout(holder)
        side.setContentsMargins(0, 0, 8, 0)
        side.setSpacing(theme.PAGE_GAP)

        side.addWidget(self._build_data_group())
        side.addWidget(self._build_strategy_group())
        side.addWidget(self._build_cost_group())
        side.addWidget(self._build_action_group())
        side.addStretch(1)
        config_panel.setWidget(holder)
        root.addWidget(config_panel)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(theme.PAGE_GAP)

        metrics = QGridLayout()
        metrics.setSpacing(theme.PAGE_GAP)
        self.m_return = MetricCard("总收益率")
        self.m_annual = MetricCard("年化收益")
        self.m_dd = MetricCard("最大回撤")
        self.m_sharpe = MetricCard("夏普比率")
        self.m_trades = MetricCard("交易次数")
        self.m_win = MetricCard("胜率")
        for i, card in enumerate(
            (self.m_return, self.m_annual, self.m_dd, self.m_sharpe, self.m_trades, self.m_win)
        ):
            metrics.addWidget(card, i // 3, i % 3)
        right_layout.addLayout(metrics)

        results_card = Card(padding=12)
        self.tabs = QTabWidget()
        self.chart = CandleChart()
        self.chart_tab = QWidget()
        chart_layout = QVBoxLayout(self.chart_tab)
        chart_layout.setContentsMargins(0, 0, 0, 0)
        chart_layout.addWidget(self.chart)

        self.trades_table = DataTable(
            ["#", "方向", "开仓时间", "平仓时间", "开仓价", "平仓价", "数量", "盈亏", "收益率", "持仓K", "原因"]
        )
        self.equity_table = DataTable(["时间", "权益", "回撤", "仓位"])

        self.optimize_panel = self._build_optimize_panel()
        self.tabs.addTab(self.chart_tab, "图表")
        self.tabs.addTab(self.trades_table, "交易明细")
        self.tabs.addTab(self.equity_table, "权益曲线")
        self.tabs.addTab(self.optimize_panel, "参数优化")
        results_card.body().addWidget(self.tabs, 1)
        right_layout.addWidget(results_card, 1)
        root.addWidget(right, 1)
        outer.addLayout(root, 1)

    def _build_data_group(self) -> QWidget:
        card = Card(padding=14)
        card.body().addWidget(self._section("数据"))
        form = QFormLayout()
        form.setVerticalSpacing(8)

        self.market_combo = QComboBox()
        for market in MarketType:
            self.market_combo.addItem(market.label, market.value)
        self.market_combo.setCurrentIndex(
            self.market_combo.findData(self.ctx.market_type.value)
        )
        form.addRow("市场", self.market_combo)

        self.symbol_edit = QLineEdit(self.ctx.settings.last_symbol)
        form.addRow("交易对", self.symbol_edit)

        self.timeframe_combo = QComboBox()
        self.timeframe_combo.addItems(TIMEFRAMES)
        self.timeframe_combo.setCurrentText(self.ctx.settings.timeframe)
        form.addRow("周期", self.timeframe_combo)

        self.bars_spin = QSpinBox()
        self.bars_spin.setRange(200, 100_000)
        self.bars_spin.setSingleStep(500)
        self.bars_spin.setValue(3000)
        form.addRow("K 线数量", self.bars_spin)

        self.load_btn = QPushButton("加载历史数据")
        self.load_btn.clicked.connect(self._load_data)
        form.addRow(self.load_btn)

        self.data_label = QLabel("尚未加载数据")
        self.data_label.setObjectName("Subtitle")
        self.data_label.setWordWrap(True)
        form.addRow(self.data_label)
        card.body().addLayout(form)
        return card

    def _section(self, title: str, subtitle: str = "") -> QWidget:
        """Card header: title plus an optional one-line hint."""
        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 4)
        layout.setSpacing(2)
        label = QLabel(title)
        label.setObjectName("Title")
        layout.addWidget(label)
        if subtitle:
            hint = QLabel(subtitle)
            hint.setObjectName("Subtitle")
            hint.setWordWrap(True)
            layout.addWidget(hint)
        return holder

    def _build_strategy_group(self) -> QWidget:
        card = Card(padding=14)
        card.body().addWidget(self._section("策略与参数"))
        self.strategy_combo = QComboBox()
        self.strategy_combo.currentIndexChanged.connect(self._on_strategy_changed)
        card.body().addWidget(self.strategy_combo)

        self.strategy_desc = QLabel("")
        self.strategy_desc.setObjectName("Subtitle")
        self.strategy_desc.setWordWrap(True)
        card.body().addWidget(self.strategy_desc)

        self.params_form_host = QWidget()
        self.params_form = QFormLayout(self.params_form_host)
        self.params_form.setContentsMargins(0, 4, 0, 0)
        self.params_form.setSpacing(8)
        card.body().addWidget(self.params_form_host)
        return card

    def _build_cost_group(self) -> QWidget:
        card = Card(padding=14)
        card.body().addWidget(self._section("资金与成本"))
        form = QFormLayout()
        form.setVerticalSpacing(8)

        self.capital_spin = QDoubleSpinBox()
        self.capital_spin.setRange(100, 1e9)
        self.capital_spin.setDecimals(0)
        self.capital_spin.setValue(10_000)
        form.addRow("初始资金", self.capital_spin)

        self.fee_spin = QDoubleSpinBox()
        self.fee_spin.setDecimals(4)
        self.fee_spin.setRange(0, 0.01)
        self.fee_spin.setSingleStep(0.0001)
        self.fee_spin.setValue(0.0004)
        self.fee_spin.setSuffix(" /边")
        form.addRow("手续费率", self.fee_spin)

        self.slippage_spin = QDoubleSpinBox()
        self.slippage_spin.setDecimals(2)
        self.slippage_spin.setRange(0, 100)
        self.slippage_spin.setValue(1.0)
        self.slippage_spin.setSuffix(" bps")
        form.addRow("滑点", self.slippage_spin)

        self.leverage_spin = QDoubleSpinBox()
        self.leverage_spin.setDecimals(0)
        self.leverage_spin.setRange(1, 125)
        self.leverage_spin.setValue(1)
        form.addRow("杠杆", self.leverage_spin)

        self.position_spin = QDoubleSpinBox()
        self.position_spin.setDecimals(0)
        self.position_spin.setRange(1, 100)
        self.position_spin.setValue(100)
        self.position_spin.setSuffix(" %")
        form.addRow("仓位比例", self.position_spin)

        self.allow_short_check = QCheckBox("允许做空")
        self.allow_short_check.setChecked(True)
        form.addRow("", self.allow_short_check)

        self.sl_spin = QDoubleSpinBox()
        self.sl_spin.setDecimals(1)
        self.sl_spin.setRange(0, 90)
        self.sl_spin.setValue(0)
        self.sl_spin.setSuffix(" %")
        form.addRow("止损", self.sl_spin)

        self.tp_spin = QDoubleSpinBox()
        self.tp_spin.setDecimals(1)
        self.tp_spin.setRange(0, 500)
        self.tp_spin.setValue(0)
        self.tp_spin.setSuffix(" %")
        form.addRow("止盈", self.tp_spin)
        card.body().addLayout(form)
        return card

    def _build_action_group(self) -> QWidget:
        card = Card(padding=14)
        card.body().addWidget(self._section("运行"))
        layout = QVBoxLayout()
        layout.setSpacing(8)
        self.run_btn = QPushButton("开始回测")
        self.run_btn.setObjectName("Primary")
        self.run_btn.setMinimumHeight(38)
        self.run_btn.clicked.connect(self._run)
        layout.addWidget(self.run_btn)

        self.progress = QProgressBar()
        self.progress.setValue(0)
        layout.addWidget(self.progress)

        self.promote_btn = QPushButton("用此参数开始纸面交易")
        self.promote_btn.setObjectName("Flat")
        self.promote_btn.setEnabled(False)
        self.promote_btn.clicked.connect(self._promote)
        layout.addWidget(self.promote_btn)
        card.body().addLayout(layout)
        return card

    def _build_optimize_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 8, 8, 8)

        hint = QLabel(
            "为每个参数填写候选值（逗号分隔），例如 5,10,20。组合数过多时会很慢。"
        )
        hint.setObjectName("Subtitle")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.grid_form_host = QWidget()
        self.grid_form = QFormLayout(self.grid_form_host)
        layout.addWidget(self.grid_form_host)

        controls = QHBoxLayout()
        self.metric_combo = QComboBox()
        for label, key in [
            ("夏普比率", "sharpe"),
            ("总收益率", "total_return"),
            ("最大回撤", "max_drawdown"),
            ("收益回撤比", "calmar"),
            ("胜率", "win_rate"),
        ]:
            self.metric_combo.addItem(label, key)
        controls.addWidget(QLabel("排序指标"))
        controls.addWidget(self.metric_combo)
        controls.addStretch(1)
        self.opt_run_btn = QPushButton("开始优化")
        self.opt_run_btn.clicked.connect(self._run_grid)
        controls.addWidget(self.opt_run_btn)
        layout.addLayout(controls)

        self.opt_progress = QProgressBar()
        layout.addWidget(self.opt_progress)

        self.opt_table = QTableWidget(0, 0)
        self.opt_table.verticalHeader().setVisible(False)
        self.opt_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.opt_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.opt_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.opt_table, 1)
        return panel

    # -------------------------------------------------------------- strategies
    def _load_strategies(self) -> None:
        self.strategy_combo.blockSignals(True)
        self.strategy_combo.clear()
        for meta in self.ctx.strategies.meta_list():
            self.strategy_combo.addItem(meta["name"], meta["key"])
        self.strategy_combo.blockSignals(False)
        if self.strategy_combo.count() == 0:
            self.status.emit("未找到任何策略，请检查 strategies 目录")
            return
        self._on_strategy_changed()

    def current_strategy(self):
        key = self.strategy_combo.currentData()
        return self.ctx.strategies.get(key)

    def _on_strategy_changed(self) -> None:
        while self.params_form.rowCount():
            self.params_form.removeRow(0)
        while self.grid_form.rowCount():
            self.grid_form.removeRow(0)
        self._param_widgets.clear()
        try:
            strategy = self.current_strategy()
        except KeyError:
            return
        self.strategy_desc.setText(strategy.description)
        for spec in strategy.params_spec:
            widget = self._make_param_widget(spec)
            self._param_widgets[spec.name] = widget
            self.params_form.addRow(spec.label, widget)

            grid_edit = QLineEdit()
            grid_edit.setPlaceholderText(f"默认 {spec.default}")
            self.grid_form.addRow(spec.label, grid_edit)
            grid_edit.setObjectName(f"grid_{spec.name}")

    def _make_param_widget(self, spec: ParamSpec) -> QWidget:
        if spec.kind == "bool":
            widget = QCheckBox()
            widget.setChecked(bool(spec.default))
            return widget
        if spec.kind == "choice":
            widget = QComboBox()
            for choice in spec.choices:
                widget.addItem(str(choice), choice)
            index = widget.findData(spec.default)
            if index >= 0:
                widget.setCurrentIndex(index)
            return widget
        if spec.kind == "int":
            widget = QSpinBox()
            widget.setRange(int(spec.minimum or 0), int(spec.maximum or 1_000_000))
            widget.setValue(int(spec.default))
            return widget
        widget = QDoubleSpinBox()
        widget.setDecimals(3)
        widget.setRange(float(spec.minimum or 0), float(spec.maximum or 1e9))
        widget.setSingleStep(float(spec.step or 0.1))
        widget.setValue(float(spec.default))
        return widget

    def _collect_params(self) -> dict:
        params = {}
        for spec in self.current_strategy().params_spec:
            widget = self._param_widgets.get(spec.name)
            if widget is None:
                continue
            if isinstance(widget, QCheckBox):
                params[spec.name] = widget.isChecked()
            elif isinstance(widget, QComboBox):
                params[spec.name] = widget.currentData()
            elif isinstance(widget, QSpinBox):
                params[spec.name] = widget.value()
            elif isinstance(widget, QDoubleSpinBox):
                params[spec.name] = widget.value()
        return params

    def _collect_grid(self) -> dict:
        grid: dict[str, list] = {}
        for spec in self.current_strategy().params_spec:
            edit = self.grid_form_host.findChild(QLineEdit, f"grid_{spec.name}")
            if edit is None or not edit.text().strip():
                continue
            raw = [part.strip() for part in edit.text().split(",") if part.strip()]
            values = []
            for token in raw:
                try:
                    if spec.kind == "bool":
                        values.append(token.lower() in ("1", "true", "yes", "是"))
                    elif spec.kind == "int":
                        values.append(int(float(token)))
                    elif spec.kind == "float":
                        values.append(float(token))
                    else:
                        values.append(token)
                except ValueError:
                    continue
            if values:
                grid[spec.name] = values
        return grid

    # ------------------------------------------------------------------ data
    def _market(self) -> MarketType:
        return MarketType(self.market_combo.currentData())

    def _config(self) -> BacktestConfig:
        return BacktestConfig(
            initial_capital=self.capital_spin.value(),
            fee_rate=self.fee_spin.value(),
            slippage_bps=self.slippage_spin.value(),
            position_pct=self.position_spin.value() / 100,
            leverage=self.leverage_spin.value(),
            allow_short=self.allow_short_check.isChecked(),
            stop_loss_pct=self.sl_spin.value() / 100,
            take_profit_pct=self.tp_spin.value() / 100,
            timeframe=self.timeframe_combo.currentText(),
        )

    def _load_data(self) -> None:
        market = self._market()
        symbol = self.symbol_edit.text().strip()
        timeframe = self.timeframe_combo.currentText()
        bars = self.bars_spin.value()
        if not symbol:
            return
        self.load_btn.setEnabled(False)
        self.data_label.setText("正在下载历史数据…")
        rest = self.ctx.rest_for(market)

        def fetch():
            store = KlineStore(rest, self.ctx.klines._root)
            from ...core.types import timeframe_seconds

            step = timeframe_seconds(timeframe) * 1000
            since = int(time.time() * 1000) - bars * step
            return store.fetch(market, symbol, timeframe, limit=bars, since=since)

        def done(frame: pd.DataFrame) -> None:
            self.load_btn.setEnabled(True)
            self._df = frame
            first = pd.Timestamp(int(frame["timestamp"].iloc[0]), unit="ms")
            last = pd.Timestamp(int(frame["timestamp"].iloc[-1]), unit="ms")
            self.data_label.setText(
                f"{symbol} {timeframe}｜{len(frame)} 根\n{first:%Y-%m-%d %H:%M} → {last:%Y-%m-%d %H:%M}"
            )
            self.chart.set_data(frame, reset_zoom=True)
            self.chart.zoom_to_last(300)
            self.status.emit(f"历史数据已就绪：{len(frame)} 根 K 线")

        def failed(message: str) -> None:
            self.load_btn.setEnabled(True)
            self.data_label.setText(f"加载失败：{message}")
            QMessageBox.warning(self, "数据加载失败", message)

        self.runner.run(fetch, done, failed)

    # ------------------------------------------------------------------ runs
    def _run(self) -> None:
        if self._df.empty:
            QMessageBox.information(self, "提示", "请先加载历史数据。")
            return
        if self._worker and self._worker.isRunning():
            return
        strategy = self.current_strategy()
        params = strategy.validate_params(self._collect_params())
        config = self._config()
        self.run_btn.setEnabled(False)
        self.progress.setRange(0, 0)
        self.status.emit("回测运行中…")

        self._worker = BacktestWorker(self._df, strategy, params, config)
        self._worker.finished_ok.connect(self._on_result)
        self._worker.failed.connect(self._on_run_failed)
        self._worker.finished.connect(lambda: self.run_btn.setEnabled(True))
        self._worker.start()

    def _on_result(self, result: BacktestResult) -> None:
        self._result = result
        self.progress.setRange(0, 100)
        self.progress.setValue(100)
        metrics = result.metrics
        self.m_return.set_value(percent_text(metrics["total_return"]), pnl_color(metrics["total_return"]))
        self.m_annual.set_value(percent_text(metrics["annual_return"]), pnl_color(metrics["annual_return"]))
        self.m_dd.set_value(
            percent_text(metrics["max_drawdown"]),
            theme.current().down if metrics["max_drawdown"] < 0 else None,
        )
        self.m_sharpe.set_value(f"{metrics['sharpe']:.2f}")
        self.m_trades.set_value(f"{int(metrics['trades'])}")
        self.m_win.set_value(percent_text(metrics["win_rate"], 1))

        equity = result.equity
        frame = self._df.copy()
        frame["timestamp"] = frame["timestamp"].astype("int64")
        self.chart.set_data(frame, reset_zoom=False)
        self.chart.set_trade_markers(result.trade_frame)
        self.chart.zoom_to_last(400)

        self._fill_trades(result)
        self._fill_equity(result)

        strategy_key = self.strategy_combo.currentData()
        self.ctx.db.save_backtest(
            {
                "id": uuid.uuid4().hex[:12],
                "created_at": int(time.time() * 1000),
                "symbol": self.symbol_edit.text().strip(),
                "market_type": self._market().value,
                "timeframe": self.timeframe_combo.currentText(),
                "strategy": strategy_key,
                "params": result.params,
                "start": str(int(equity["timestamp"].iloc[0])),
                "end": str(int(equity["timestamp"].iloc[-1])),
                "metrics": metrics,
            }
        )
        self.promote_btn.setEnabled(True)
        self.status.emit(
            f"回测完成：收益 {metrics['total_return']*100:.2f}% · 交易 {int(metrics['trades'])} 次"
        )

    def _on_run_failed(self, message: str) -> None:
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.status.emit(f"回测失败：{message}")
        QMessageBox.warning(self, "回测失败", message)

    def _fill_trades(self, result: BacktestResult) -> None:
        frame = result.trade_frame
        self.trades_table.setRowCount(0)
        for i, row in frame.iterrows():
            entry = pd.Timestamp(int(row["entry_time"]), unit="ms")
            exit_ = pd.Timestamp(int(row["exit_time"]), unit="ms")
            reasons = {
                "signal": "信号",
                "stop_loss": "止损",
                "take_profit": "止盈",
                "end_of_data": "数据结束",
            }
            self.trades_table.set_row(
                int(i),
                [
                    str(int(i) + 1),
                    "多" if row["side"] == "long" else "空",
                    entry.strftime("%m-%d %H:%M"),
                    exit_.strftime("%m-%d %H:%M"),
                    f"{row['entry_price']:,.4f}",
                    f"{row['exit_price']:,.4f}",
                    f"{abs(row['qty']):,.6f}",
                    f"{row['pnl']:+,.2f}",
                    percent_text(row["pnl_pct"]),
                    str(int(row["bars"])),
                    reasons.get(row["exit_reason"], row["exit_reason"]),
                ],
                colors=[None, theme.current().up if row["side"] == "long" else theme.current().down,
                        None, None, None, None, None,
                        pnl_color(row["pnl"]), pnl_color(row["pnl"]), None, None],
            )

    def _fill_equity(self, result: BacktestResult) -> None:
        equity = result.equity
        step = max(1, len(equity) // 1000)
        self.equity_table.setRowCount(0)
        row = 0
        for _, record in equity.iloc[::step].iterrows():
            ts = pd.Timestamp(int(record["timestamp"]), unit="ms")
            self.equity_table.set_row(
                row,
                [
                    ts.strftime("%Y-%m-%d %H:%M"),
                    money_text(record["equity"]),
                    percent_text(record["drawdown"]),
                    percent_text(record["position"], 1),
                ],
                colors=[None, None,
                        theme.current().down if record["drawdown"] < -0.001 else None, None],
            )
            row += 1

    # ------------------------------------------------------------- optimizing
    def _run_grid(self) -> None:
        if self._df.empty:
            QMessageBox.information(self, "提示", "请先加载历史数据。")
            return
        if self._grid_worker and self._grid_worker.isRunning():
            return
        grid = self._collect_grid()
        if not grid:
            QMessageBox.information(self, "提示", "请至少为一个参数填写候选值。")
            return
        strategy = self.current_strategy()
        config = self._config()
        self.opt_run_btn.setEnabled(False)
        self.opt_progress.setRange(0, 100)
        self.opt_progress.setValue(0)
        self.status.emit("参数优化运行中…")

        self._grid_worker = GridWorker(self._df, strategy, grid, config, self.metric_combo.currentData())
        self._grid_worker.progress.connect(self._on_grid_progress)
        self._grid_worker.finished_ok.connect(self._on_grid_done)
        self._grid_worker.failed.connect(self._on_grid_failed)
        self._grid_worker.finished.connect(lambda: self.opt_run_btn.setEnabled(True))
        self._grid_worker.start()

    def _on_grid_progress(self, index: int, total: int, params: str) -> None:
        self.opt_progress.setMaximum(total)
        self.opt_progress.setValue(index)
        self.opt_progress.setFormat(f"{index}/{total}  {params}")

    def _on_grid_done(self, frame: pd.DataFrame) -> None:
        self.opt_progress.setValue(self.opt_progress.maximum())
        if frame.empty:
            self.status.emit("参数优化没有有效结果")
            return
        labels = {
            "m_total_return": "总收益",
            "m_annual_return": "年化",
            "m_max_drawdown": "最大回撤",
            "m_sharpe": "夏普",
            "m_trades": "交易数",
            "m_win_rate": "胜率",
        }
        columns = [c for c in frame.columns if not c.startswith("m_")] + [
            c for c in frame.columns if c.startswith("m_")
        ]
        self.opt_table.setColumnCount(len(columns))
        self.opt_table.setHorizontalHeaderLabels([labels.get(c, c) for c in columns])
        self.opt_table.setRowCount(len(frame))
        for row, (_, record) in enumerate(frame.iterrows()):
            for col, name in enumerate(columns):
                value = record[name]
                if isinstance(value, float):
                    text = f"{value:.4f}" if name.startswith("m_") else f"{value:g}"
                else:
                    text = str(value)
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if name == "m_sharpe":
                    item.setForeground(QColor(theme.current().accent))
                self.opt_table.setItem(row, col, item)
        self.status.emit(f"参数优化完成：{len(frame)} 组结果")

    def _on_grid_failed(self, message: str) -> None:
        self.status.emit(f"优化失败：{message}")
        QMessageBox.warning(self, "优化失败", message)

    def _promote(self) -> None:
        if self._result is None:
            return
        self.promote.emit(self._result, self._collect_params())
