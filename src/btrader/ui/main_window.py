"""Main window: navigation, header and global status bar."""

from __future__ import annotations

import logging

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from .. import APP_NAME, __version__
from ..context import AppContext
from ..core.types import Network, RunMode
from .pages.auto import AutoPage
from .pages.backtest import BacktestPage
from .pages.portfolio import PortfolioPage
from .pages.settings import SettingsPage
from .pages.trading import TradingPage
from .theme import ACCENT, BG_ALT, BORDER, DOWN, TEXT, TEXT_DIM, UP
from .widgets.common import Badge


class MainWindow(QMainWindow):
    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self.setWindowTitle(f"{APP_NAME} · 币安交易工作台 v{__version__}")
        self.resize(1440, 900)
        self.setMinimumSize(1120, 720)
        self._build_ui()
        self._build_status_bar()

    # -------------------------------------------------------------------- ui
    def _build_ui(self) -> None:
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layout.addWidget(self._build_header())

        self.stack = QStackedWidget()
        self.trading_page = TradingPage(self.ctx)
        self.portfolio_page = PortfolioPage(self.ctx)
        self.backtest_page = BacktestPage(self.ctx)
        self.auto_page = AutoPage(self.ctx)
        self.settings_page = SettingsPage(self.ctx)

        for page in (
            self.trading_page,
            self.portfolio_page,
            self.backtest_page,
            self.auto_page,
            self.settings_page,
        ):
            self.stack.addWidget(page)

        for page in (self.trading_page, self.portfolio_page, self.auto_page, self.settings_page):
            page.status.connect(self._set_status)

        self.backtest_page.status.connect(self._set_status)
        self.backtest_page.promote.connect(self._promote_backtest)
        self.settings_page.network_changed.connect(self._on_network_changed)

        layout.addWidget(self.stack, 1)
        self.setCentralWidget(central)

    def _build_header(self) -> QWidget:
        header = QFrame()
        header.setFixedHeight(56)
        header.setStyleSheet(f"background: {BG_ALT}; border-bottom: 1px solid {BORDER};")
        layout = QHBoxLayout(header)
        layout.setContentsMargins(16, 0, 16, 0)
        layout.setSpacing(8)

        brand = QLabel(APP_NAME)
        brand.setStyleSheet(f"font-size: 18px; font-weight: 800; color: {ACCENT};")
        layout.addWidget(brand)
        layout.addSpacing(12)

        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self.nav_buttons: list[QPushButton] = []
        for index, (label, hint) in enumerate(
            [
                ("交易", "行情与手动下单"),
                ("资产", "余额与持仓"),
                ("回测", "策略回测与优化"),
                ("自动交易", "策略自动执行"),
                ("设置", "API、风控与插件"),
            ]
        ):
            button = QPushButton(label)
            button.setCheckable(True)
            button.setToolTip(hint)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setStyleSheet(
                f"""
                QPushButton {{
                    background: transparent; border: none; padding: 8px 18px;
                    color: {TEXT_DIM}; font-size: 14px;
                }}
                QPushButton:hover {{ color: {TEXT}; }}
                QPushButton:checked {{
                    color: {TEXT}; border-bottom: 2px solid {ACCENT};
                    font-weight: 600;
                }}
                """
            )
            button.clicked.connect(lambda _=False, i=index: self._navigate(i))
            self.nav_group.addButton(button, index)
            self.nav_buttons.append(button)
            layout.addWidget(button)
        self.nav_buttons[0].setChecked(True)

        layout.addStretch(1)

        self.network_badge = Badge("主网", ACCENT)
        layout.addWidget(self.network_badge)
        self.mode_badge = Badge("纸面", TEXT_DIM)
        layout.addWidget(self.mode_badge)

        self.credential_label = QLabel("")
        layout.addWidget(self.credential_label)
        return header

    def _build_status_bar(self) -> None:
        bar = QStatusBar()
        self.status_label = QLabel("就绪")
        bar.addWidget(self.status_label, 1)
        self.risk_label = QLabel("")
        bar.addPermanentWidget(self.risk_label)
        self.setStatusBar(bar)
        self._refresh_header()
        self._refresh_risk_label()

        self.status_timer = QTimer(self)
        self.status_timer.setInterval(5000)
        self.status_timer.timeout.connect(self._refresh_risk_label)
        self.status_timer.start()

    # ---------------------------------------------------------------- routing
    def _navigate(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        self.nav_buttons[index].setChecked(True)

    def _promote_backtest(self, result, params: dict) -> None:
        self._navigate(3)
        self.auto_page.symbol_edit.setCurrentText(self.backtest_page.symbol_edit.text().strip())
        self.auto_page.timeframe_combo.setCurrentText(
            self.backtest_page.timeframe_combo.currentText()
        )
        market_index = self.auto_page.market_combo.findData(
            self.backtest_page.market_combo.currentData()
        )
        if market_index >= 0:
            self.auto_page.market_combo.setCurrentIndex(market_index)
        strategy_index = self.auto_page.strategy_combo.findData(
            self.backtest_page.strategy_combo.currentData()
        )
        if strategy_index >= 0:
            self.auto_page.strategy_combo.setCurrentIndex(strategy_index)
        for name, value in params.items():
            widget = self.auto_page._param_widgets.get(name)
            if widget is None:
                continue
            from PySide6.QtWidgets import QCheckBox, QComboBox, QDoubleSpinBox, QSpinBox

            if isinstance(widget, QCheckBox):
                widget.setChecked(bool(value))
            elif isinstance(widget, QComboBox):
                index = widget.findData(value)
                if index >= 0:
                    widget.setCurrentIndex(index)
            elif isinstance(widget, (QSpinBox, QDoubleSpinBox)):
                widget.setValue(value)
        self.auto_page.mode_combo.setCurrentIndex(0)  # always paper first
        self._set_status("已把回测参数带入自动交易，请确认后以纸面模式启动")

    # ----------------------------------------------------------------- header
    def _refresh_header(self) -> None:
        network = self.ctx.network
        self.network_badge.setText(network.label)
        self.network_badge.set_color(ACCENT if network is Network.MAINNET else "#4f9dff")
        mode = RunMode.PAPER
        self.mode_badge.setText(mode.label)
        self.mode_badge.set_color(TEXT_DIM)
        if self.ctx.rest.has_credentials:
            self.credential_label.setText(f"API {self.ctx.creds.masked_key}")
            self.credential_label.setStyleSheet(f"color: {TEXT_DIM}; font-size: 11px;")
        else:
            self.credential_label.setText("未配置 API")
            self.credential_label.setStyleSheet("color: #ef5350; font-size: 11px;")

    def _refresh_risk_label(self) -> None:
        state = self.ctx.risk.state
        settings = self.ctx.settings.risk
        parts = []
        if settings.kill_switch:
            parts.append("急停已开启")
        if state.halted_reason:
            parts.append(f"熔断：{state.halted_reason}")
        parts.append(f"今日成交 {state.trades} 笔")
        if state.realized_pnl:
            parts.append(f"今日盈亏 {state.realized_pnl:+.2f}")
        text = " · ".join(parts)
        self.risk_label.setText(text)
        self.risk_label.setStyleSheet(
            f"color: {DOWN if (settings.kill_switch or state.halted_reason) else TEXT_DIM};"
        )

    def _set_status(self, message: str) -> None:
        self.status_label.setText(message)

    def _on_network_changed(self) -> None:
        self._refresh_header()
        self._set_status(f"环境已切换：{self.ctx.network.label}")

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        try:
            self.auto_page.close()
            for page in (
                self.trading_page,
                self.portfolio_page,
                self.backtest_page,
                self.settings_page,
            ):
                runner = getattr(page, "runner", None)
                if runner is not None:
                    runner.shutdown(2000)
            worker = getattr(self.backtest_page, "_worker", None)
            if worker is not None and worker.isRunning():
                worker.wait(2000)
            self.ctx.shutdown()
        finally:
            super().closeEvent(event)
