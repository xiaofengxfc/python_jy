"""Automated strategy page: configure, start/stop and watch the runner."""

from __future__ import annotations

import logging
from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ...context import AppContext
from ...core.types import TIMEFRAMES, MarketType, RunMode
from ...engine.auto import AutoTrader, AutoTraderConfig
from ..theme import DOWN, TEXT_DIM, UP
from ..widgets.common import Card, mono_font
from ..widgets.tables import DataTable

log = logging.getLogger(__name__)


class AutoPage(QWidget):
    status = Signal(str)

    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._trader: AutoTrader | None = None
        self._param_widgets: dict[str, QWidget] = {}
        self._build_ui()

    # -------------------------------------------------------------------- ui
    def _build_ui(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        side = QVBoxLayout()
        side.setSpacing(10)
        side.addWidget(self._build_config_group())
        side.addWidget(self._build_control_group())
        side.addStretch(1)
        root.addLayout(side)

        right = QVBoxLayout()
        right.setSpacing(10)
        self.state_card = Card()
        self.state_label = QLabel("未运行")
        self.state_label.setObjectName("Title")
        self.signal_label = QLabel("目标仓位：--")
        self.signal_label.setObjectName("Muted")
        self.state_card.body().addWidget(self.state_label)
        self.state_card.body().addWidget(self.signal_label)
        right.addWidget(self.state_card)

        log_title = QLabel("运行日志")
        log_title.setObjectName("Title")
        right.addWidget(log_title)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFont(mono_font(10))
        self.log_view.setMaximumBlockCount(2000)
        right.addWidget(self.log_view, 1)

        self.orders_table = DataTable(["时间", "交易对", "方向", "数量", "价格", "模式"])
        self.orders_table.setMaximumHeight(160)
        right.addWidget(self.orders_table)
        root.addLayout(right, 1)

    def _build_config_group(self) -> QWidget:
        group = QGroupBox("自动交易配置")
        layout = QFormLayout(group)

        self.market_combo = QComboBox()
        for market in MarketType:
            self.market_combo.addItem(market.label, market.value)
        self.market_combo.setCurrentIndex(self.market_combo.findData(self.ctx.market_type.value))
        self.market_combo.currentIndexChanged.connect(self._on_market_changed)
        layout.addRow("市场", self.market_combo)

        self.symbol_edit = QComboBox()
        self.symbol_edit.setEditable(True)
        self.symbol_edit.setCurrentText(self.ctx.settings.last_symbol)
        layout.addRow("交易对", self.symbol_edit)

        self.timeframe_combo = QComboBox()
        self.timeframe_combo.addItems(TIMEFRAMES)
        self.timeframe_combo.setCurrentText(self.ctx.settings.timeframe)
        layout.addRow("周期", self.timeframe_combo)

        self.strategy_combo = QComboBox()
        for meta in self.ctx.strategies.meta_list():
            self.strategy_combo.addItem(meta["name"], meta["key"])
        self.strategy_combo.currentIndexChanged.connect(self._on_strategy_changed)
        layout.addRow("策略", self.strategy_combo)

        self.params_host = QWidget()
        self.params_form = QFormLayout(self.params_host)
        self.params_form.setContentsMargins(0, 0, 0, 0)
        layout.addRow(self.params_host)

        self.mode_combo = QComboBox()
        self.mode_combo.addItem("纸面交易", RunMode.PAPER.value)
        self.mode_combo.addItem("实盘", RunMode.LIVE.value)
        layout.addRow("模式", self.mode_combo)

        self.position_spin = QDoubleSpinBox()
        self.position_spin.setRange(1, 100)
        self.position_spin.setValue(100)
        self.position_spin.setSuffix(" %")
        layout.addRow("每次开仓比例", self.position_spin)

        self.leverage_spin = QDoubleSpinBox()
        self.leverage_spin.setRange(1, 125)
        self.leverage_spin.setValue(1)
        self.leverage_spin.setSuffix(" x")
        layout.addRow("杠杆", self.leverage_spin)

        self.poll_spin = QSpinBox()
        self.poll_spin.setRange(5, 600)
        self.poll_spin.setValue(15)
        self.poll_spin.setSuffix(" 秒")
        layout.addRow("轮询间隔", self.poll_spin)
        return group

    def _build_control_group(self) -> QWidget:
        group = QGroupBox("控制")
        layout = QVBoxLayout(group)
        self.start_btn = QPushButton("启动自动交易")
        self.start_btn.setObjectName("Primary")
        self.start_btn.setMinimumHeight(36)
        self.start_btn.clicked.connect(self._start)
        layout.addWidget(self.start_btn)

        self.stop_btn = QPushButton("停止")
        self.stop_btn.setObjectName("Danger")
        self.stop_btn.setMinimumHeight(32)
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._stop)
        layout.addWidget(self.stop_btn)

        self.kill_btn = QPushButton("紧急停止（全局急停）")
        self.kill_btn.setObjectName("Danger")
        self.kill_btn.clicked.connect(self._kill_switch)
        layout.addWidget(self.kill_btn)

        note = QLabel(
            "说明：自动交易只在新 K 线收盘后计算信号；所有订单都会经过风控检查。"
            "实盘首次使用请先用纸面模式验证。"
        )
        note.setObjectName("Muted")
        note.setWordWrap(True)
        layout.addWidget(note)
        return group

    # ------------------------------------------------------------- strategies
    def _on_market_changed(self) -> None:
        futures = MarketType(self.market_combo.currentData()).is_futures
        self.leverage_spin.setEnabled(futures)

    def _on_strategy_changed(self) -> None:
        while self.params_form.rowCount():
            self.params_form.removeRow(0)
        self._param_widgets.clear()
        try:
            strategy = self.ctx.strategies.get(self.strategy_combo.currentData())
        except (KeyError, TypeError):
            return
        for spec in strategy.params_spec:
            if spec.kind == "bool":
                widget = QCheckBox()
                widget.setChecked(bool(spec.default))
            elif spec.kind == "choice":
                widget = QComboBox()
                for choice in spec.choices:
                    widget.addItem(str(choice), choice)
                widget.setCurrentIndex(max(0, widget.findData(spec.default)))
            elif spec.kind == "int":
                widget = QSpinBox()
                widget.setRange(int(spec.minimum or 0), int(spec.maximum or 1_000_000))
                widget.setValue(int(spec.default))
            else:
                widget = QDoubleSpinBox()
                widget.setRange(float(spec.minimum or 0), float(spec.maximum or 1e9))
                widget.setDecimals(3)
                widget.setValue(float(spec.default))
            self._param_widgets[spec.name] = widget
            self.params_form.addRow(spec.label, widget)

    def _collect_params(self) -> dict:
        params = {}
        for name, widget in self._param_widgets.items():
            if isinstance(widget, QCheckBox):
                params[name] = widget.isChecked()
            elif isinstance(widget, QComboBox):
                params[name] = widget.currentData()
            elif isinstance(widget, (QSpinBox, QDoubleSpinBox)):
                params[name] = widget.value()
        return params

    # ---------------------------------------------------------------- control
    def _start(self) -> None:
        if self._trader and self._trader.running:
            return
        mode = RunMode(self.mode_combo.currentData())
        market = MarketType(self.market_combo.currentData())
        symbol = self.symbol_edit.currentText().strip()
        if not symbol:
            QMessageBox.information(self, "提示", "请填写交易对。")
            return
        if mode is RunMode.LIVE and self.ctx.network.value == "mainnet":
            answer = QMessageBox.question(
                self,
                "启动实盘自动交易",
                f"即将在实盘对 {symbol} 启动自动交易。\n"
                f"策略：{self.strategy_combo.currentText()}\n"
                f"仓位比例：{self.position_spin.value():.0f}%\n\n"
                "这会使用真实资金，确认继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        try:
            strategy = self.ctx.strategies.get(self.strategy_combo.currentData())
        except KeyError as exc:
            QMessageBox.warning(self, "策略错误", str(exc))
            return

        config = AutoTraderConfig(
            symbol=symbol,
            market_type=market,
            timeframe=self.timeframe_combo.currentText(),
            mode=mode,
            strategy_key=strategy.key,
            params=strategy.validate_params(self._collect_params()),
            position_pct=self.position_spin.value() / 100,
            leverage=self.leverage_spin.value() if market.is_futures else 1.0,
            poll_seconds=float(self.poll_spin.value()),
        )
        self._trader = AutoTrader(config, strategy, self.ctx.trading, self.ctx.klines, self)
        self._trader.log_event.connect(self._on_log)
        self._trader.signal_changed.connect(self._on_signal)
        self._trader.state_changed.connect(self._on_state)
        self._trader.start()
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)

    def _stop(self) -> None:
        if self._trader:
            self._trader.stop()
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)

    def _kill_switch(self) -> None:
        risk = self.ctx.settings.risk
        risk.kill_switch = True
        self.ctx.save_settings()
        self.ctx.risk.settings.kill_switch = True
        if self._trader:
            self._trader.stop()
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self._append("ERROR", "已触发全局急停：所有下单请求都会被风控拒绝。")
        self.status.emit("全局急停已开启，请到设置中解除")

    # ----------------------------------------------------------------- events
    def _on_log(self, level: str, message: str) -> None:
        self._append(level, message)

    def _append(self, level: str, message: str) -> None:
        color = {"ERROR": DOWN, "WARN": "#f0b90b", "TRADE": UP}.get(level, TEXT_DIM)
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_view.appendHtml(
            f'<span style="color:{TEXT_DIM}">{stamp}</span> '
            f'<span style="color:{color}">[{level}]</span> {message}'
        )

    def _on_signal(self, target: float, reason: str) -> None:
        text = {1.0: "做多", -1.0: "做空", 0.0: "空仓"}.get(target, f"{target:+.2f}")
        color = UP if target > 0 else DOWN if target < 0 else TEXT_DIM
        self.signal_label.setText(f"目标仓位：{text}　（{reason}）")
        self.signal_label.setStyleSheet(f"color: {color};")
        self._append("SIGNAL", f"信号变化 → {text}（{reason}）")
        if self.ctx.settings.notification_sound:
            from PySide6.QtWidgets import QApplication

            QApplication.beep()

    def _on_state(self, running: bool) -> None:
        self.state_label.setText("运行中" if running else "已停止")
        self.state_label.setStyleSheet(f"color: {UP if running else TEXT_DIM};")

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self._trader:
            self._trader.stop()
        super().closeEvent(event)
