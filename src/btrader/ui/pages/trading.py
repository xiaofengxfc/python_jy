"""Trading workstation: chart, order book and manual order ticket."""

from __future__ import annotations

import logging

import pandas as pd
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ...context import AppContext
from ...core.models import OrderBook, Ticker
from ...core.types import TIMEFRAMES, MarketType, OrderSide, OrderType, RunMode
from ...engine.trading import OrderRejected, OrderRequest
from ...exchange.rest import ExchangeError
from ...exchange.symbols import format_amount, ticker_from_ccxt
from ..theme import DOWN, TEXT_DIM, UP
from ..widgets.chart import CandleChart
from ..widgets.common import Card, mono_font, pnl_color
from ..widgets.orderbook import OrderBookWidget
from ..widgets.tables import DataTable
from ..workers import TaskRunner

log = logging.getLogger(__name__)


class TradingPage(QWidget):
    status = Signal(str)

    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.runner = TaskRunner(self, max_threads=3)
        self._symbols: list[str] = []
        self._ticker: Ticker | None = None
        self._df = pd.DataFrame()
        self._loading = False

        self._build_ui()

        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(2000)
        self.refresh_timer.timeout.connect(self._refresh_live)
        self.refresh_timer.start()

        self.book_timer = QTimer(self)
        self.book_timer.setInterval(1500)
        self.book_timer.timeout.connect(self._refresh_book)
        self.book_timer.start()

        QTimer.singleShot(50, self._bootstrap)

    # --------------------------------------------------------------- building
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)
        root.addWidget(self._build_symbol_bar())

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(6)
        self.chart = CandleChart()
        left_layout.addWidget(self.chart, 1)
        left_layout.addWidget(self._build_bottom_tabs())
        splitter.addWidget(left)

        right = QWidget()
        right.setMinimumWidth(300)
        right.setMaximumWidth(420)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(8)
        right_layout.addWidget(self._build_book_card(), 3)
        right_layout.addWidget(self._build_order_ticket(), 4)
        right_layout.addStretch(1)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 1)
        root.addWidget(splitter, 1)

    def _build_symbol_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("Panel")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(10)

        self.market_combo = QComboBox()
        for market in MarketType:
            self.market_combo.addItem(market.label, market.value)
        self.market_combo.setFixedWidth(120)
        self.market_combo.currentIndexChanged.connect(self._on_market_changed)
        layout.addWidget(self.market_combo)

        self.symbol_combo = QComboBox()
        self.symbol_combo.setEditable(True)
        self.symbol_combo.setMinimumWidth(200)
        self.symbol_combo.setEditText(self.ctx.settings.last_symbol)
        self.symbol_combo.lineEdit().returnPressed.connect(self._on_symbol_changed)
        self.symbol_combo.activated.connect(lambda _: self._on_symbol_changed())
        layout.addWidget(self.symbol_combo)

        self.price_label = QLabel("--")
        self.price_label.setObjectName("SymbolPrice")
        layout.addWidget(self.price_label)

        self.change_label = QLabel("")
        layout.addWidget(self.change_label)

        layout.addStretch(1)

        self.high_label = QLabel("")
        self.high_label.setObjectName("Muted")
        self.low_label = QLabel("")
        self.low_label.setObjectName("Muted")
        self.volume_label = QLabel("")
        self.volume_label.setObjectName("Muted")
        layout.addWidget(self.high_label)
        layout.addWidget(self.low_label)
        layout.addWidget(self.volume_label)

        self.timeframe_combo = QComboBox()
        self.timeframe_combo.addItems(TIMEFRAMES)
        self.timeframe_combo.setCurrentText(self.ctx.settings.timeframe)
        self.timeframe_combo.setFixedWidth(80)
        self.timeframe_combo.currentTextChanged.connect(self._on_timeframe_changed)
        layout.addWidget(self.timeframe_combo)

        self.refresh_btn = QPushButton("刷新")
        self.refresh_btn.setObjectName("Flat")
        self.refresh_btn.clicked.connect(lambda: self._load_klines(reset_zoom=True))
        layout.addWidget(self.refresh_btn)
        return bar

    def _build_book_card(self) -> QWidget:
        card = Card()
        header = QHBoxLayout()
        title = QLabel("订单簿")
        title.setObjectName("Title")
        header.addWidget(title)
        header.addStretch(1)
        self.book_spread = QLabel("")
        self.book_spread.setObjectName("Muted")
        header.addWidget(self.book_spread)
        card.body().addLayout(header)
        self.book = OrderBookWidget()
        card.body().addWidget(self.book, 1)
        return card

    def _build_order_ticket(self) -> QWidget:
        card = Card()
        title = QLabel("手动下单")
        title.setObjectName("Title")
        card.body().addWidget(title)

        self.mode_combo = QComboBox()
        self.mode_combo.addItem("纸面交易", RunMode.PAPER.value)
        self.mode_combo.addItem("实盘", RunMode.LIVE.value)
        if self.ctx.network.value == "testnet":
            self.mode_combo.addItem("测试网实盘", RunMode.LIVE.value)
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("模式"))
        mode_row.addWidget(self.mode_combo, 1)
        card.body().addLayout(mode_row)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setSpacing(6)

        self.side_combo = QComboBox()
        self.side_combo.addItem("买入 / 做多", OrderSide.BUY.value)
        self.side_combo.addItem("卖出 / 做空", OrderSide.SELL.value)
        form.addRow("方向", self.side_combo)

        self.type_combo = QComboBox()
        self.type_combo.addItem("市价", OrderType.MARKET.value)
        self.type_combo.addItem("限价", OrderType.LIMIT.value)
        self.type_combo.currentIndexChanged.connect(self._on_order_type_changed)
        form.addRow("类型", self.type_combo)

        self.price_spin = QDoubleSpinBox()
        self.price_spin.setDecimals(6)
        self.price_spin.setRange(0, 1e12)
        self.price_spin.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
        self.price_spin.setEnabled(False)
        form.addRow("价格", self.price_spin)

        self.size_mode = QComboBox()
        self.size_mode.addItem("按金额 (USDT)", "quote")
        self.size_mode.addItem("按数量", "amount")
        self.size_mode.currentIndexChanged.connect(self._on_size_mode_changed)
        form.addRow("下单方式", self.size_mode)

        self.amount_spin = QDoubleSpinBox()
        self.amount_spin.setDecimals(6)
        self.amount_spin.setRange(0, 1e12)
        self.amount_spin.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
        self.amount_spin.setValue(self.ctx.settings.default_order_quote)
        form.addRow("数量", self.amount_spin)

        self.leverage_spin = QDoubleSpinBox()
        self.leverage_spin.setDecimals(0)
        self.leverage_spin.setRange(1, 125)
        self.leverage_spin.setValue(1)
        self.leverage_spin.setEnabled(False)
        self.leverage_spin.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
        form.addRow("杠杆", self.leverage_spin)

        self.reduce_check = QCheckBox("只减仓")
        self.reduce_check.setEnabled(False)
        form.addRow("", self.reduce_check)

        card.body().addLayout(form)

        self.estimate_label = QLabel("预计金额：--")
        self.estimate_label.setObjectName("Muted")
        card.body().addWidget(self.estimate_label)

        buttons = QHBoxLayout()
        self.buy_btn = QPushButton("买入 / 做多")
        self.buy_btn.setObjectName("Buy")
        self.buy_btn.setMinimumHeight(36)
        self.buy_btn.clicked.connect(lambda: self._submit_order(OrderSide.BUY.value))
        self.sell_btn = QPushButton("卖出 / 做空")
        self.sell_btn.setObjectName("Sell")
        self.sell_btn.setMinimumHeight(36)
        self.sell_btn.clicked.connect(lambda: self._submit_order(OrderSide.SELL.value))
        buttons.addWidget(self.buy_btn)
        buttons.addWidget(self.sell_btn)
        card.body().addLayout(buttons)

        self.mode_hint = QLabel("")
        self.mode_hint.setObjectName("Muted")
        self.mode_hint.setWordWrap(True)
        card.body().addWidget(self.mode_hint)

        self.price_spin.valueChanged.connect(self._update_estimate)
        self.amount_spin.valueChanged.connect(self._update_estimate)
        self.size_mode.currentIndexChanged.connect(self._update_estimate)
        self.leverage_spin.valueChanged.connect(self._update_estimate)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        self._on_mode_changed()
        return card

    def _build_bottom_tabs(self) -> QWidget:
        self.bottom_tabs = QTabWidget()
        self.orders_table = DataTable(["时间", "交易对", "方向", "类型", "价格", "数量", "状态"])
        self.trades_table = DataTable(["时间", "交易对", "方向", "价格", "数量", "手续费"])
        self.bottom_tabs.addTab(self.orders_table, "当前委托 / 历史")
        self.bottom_tabs.addTab(self.trades_table, "成交记录")
        self.bottom_tabs.setFixedHeight(180)
        return self.bottom_tabs

    # ------------------------------------------------------------- data loads
    def _bootstrap(self) -> None:
        index = self.market_combo.findData(self.ctx.market_type.value)
        if index >= 0:
            self.market_combo.blockSignals(True)
            self.market_combo.setCurrentIndex(index)
            self.market_combo.blockSignals(False)
        # Show the configured symbol immediately; the full list arrives async.
        self.symbol_combo.setEditText(self.ctx.settings.last_symbol)
        self._set_futures_controls()
        self._load_symbols()

    def _load_symbols(self) -> None:
        market = self._market()
        self.status.emit(f"正在加载 {market.label} 交易对…")
        runner = self.runner

        def fetch():
            from ...exchange.symbols import load_symbols

            return load_symbols(self.ctx.rest_for(market), market)

        def done(infos):
            self._symbols = [info.symbol for info in infos]
            self.symbol_combo.blockSignals(True)
            self.symbol_combo.clear()
            preferred = [s for s in self._symbols if s.endswith("/USDT")]
            ordered = preferred + [s for s in self._symbols if s not in preferred]
            self.symbol_combo.addItems(ordered)
            self.symbol_combo.blockSignals(False)
            target = self.ctx.settings.last_symbol
            if target not in ordered and ordered:
                target = ordered[0]
            index = self.symbol_combo.findText(target)
            if index >= 0:
                self.symbol_combo.setCurrentIndex(index)
            else:
                self.symbol_combo.setEditText(target)
            self.status.emit(f"已加载 {len(ordered)} 个交易对")
            self._on_symbol_changed()

        runner.run(fetch, done, self._show_error)

    def _load_klines(self, reset_zoom: bool = False) -> None:
        if self._loading:
            return
        market = self._market()
        symbol = self.current_symbol()
        timeframe = self.timeframe_combo.currentText()
        if not symbol:
            return
        self._loading = True
        self.status.emit(f"加载 {symbol} {timeframe} K 线…")
        limit = max(300, self.ctx.settings.kline_limit)

        def fetch():
            return self.ctx.klines.fetch(market, symbol, timeframe, limit=limit)

        def done(frame: pd.DataFrame) -> None:
            self._loading = False
            self._df = frame
            self.chart.set_data(frame, reset_zoom=reset_zoom)
            self.chart.zoom_to_last(220)
            self._update_estimate()
            self.status.emit(f"{symbol} 已加载 {len(frame)} 根 K 线")

        def failed(message: str) -> None:
            self._loading = False
            self._show_error(message)

        self.runner.run(fetch, done, failed)

    def _refresh_live(self) -> None:
        market = self._market()
        symbol = self.current_symbol()
        if not symbol:
            return

        def fetch():
            return self.ctx.rest_for(market).fetch_ticker(market, symbol)

        self.runner.run(fetch, lambda raw: self._apply_ticker(symbol, raw), lambda _m: None)

    def _refresh_book(self) -> None:
        market = self._market()
        symbol = self.current_symbol()
        if not symbol or self.bottom_tabs.currentIndex() == 1:
            return

        def fetch():
            return self.ctx.rest_for(market).fetch_order_book(market, symbol, 20)

        self.runner.run(fetch, self._apply_book, lambda _m: None)

    def _apply_ticker(self, symbol: str, raw: dict) -> None:
        ticker = ticker_from_ccxt(symbol, raw)
        self._ticker = ticker
        self.price_label.setText(f"{ticker.last:,.4f}")
        self.change_label.setText(f"{ticker.change_pct:+.2f}%")
        color = UP if ticker.change_pct >= 0 else DOWN
        self.change_label.setStyleSheet(f"color: {color}; font-weight: 600;")
        self.high_label.setText(f"24h 高 {ticker.high:,.4f}")
        self.low_label.setText(f"24h 低 {ticker.low:,.4f}")
        self.volume_label.setText(f"24h 额 {ticker.quote_volume:,.0f}")
        if self.type_combo.currentData() == OrderType.MARKET.value:
            self.price_spin.blockSignals(True)
            self.price_spin.setValue(ticker.last)
            self.price_spin.blockSignals(False)
        self.chart.set_price_line(ticker.last)
        self._update_estimate()

    def _apply_book(self, raw: dict) -> None:
        bids = [(float(p), float(a)) for p, a, *_ in raw.get("bids", [])]
        asks = [(float(p), float(a)) for p, a, *_ in raw.get("asks", [])]
        book = OrderBook(symbol=raw.get("symbol", ""), bids=bids, asks=asks)
        self.book.update_book(book)
        if book.best_bid and book.best_ask:
            spread = book.best_ask - book.best_bid
            pct = spread / book.best_bid * 100
            self.book_spread.setText(f"价差 {spread:.4f} ({pct:.3f}%)")

    # ----------------------------------------------------------------- events
    def current_symbol(self) -> str:
        return self.symbol_combo.currentText().strip()

    def _market(self) -> MarketType:
        return MarketType(self.market_combo.currentData())

    def _on_market_changed(self) -> None:
        market = self._market()
        self.ctx.set_market_type(market)
        self._set_futures_controls()
        self._load_symbols()

    def _on_symbol_changed(self) -> None:
        symbol = self.current_symbol()
        if not symbol:
            return
        self.ctx.settings.last_symbol = symbol
        self.ctx.save_settings()
        self._load_klines(reset_zoom=True)
        self._refresh_live()
        self._refresh_book()

    def _on_timeframe_changed(self, timeframe: str) -> None:
        self.ctx.settings.timeframe = timeframe
        self.ctx.save_settings()
        if not self._df.empty:
            self._load_klines(reset_zoom=True)

    def _on_order_type_changed(self) -> None:
        is_limit = self.type_combo.currentData() == OrderType.LIMIT.value
        self.price_spin.setEnabled(is_limit)
        if is_limit and self._ticker:
            self.price_spin.setValue(self._ticker.last)

    def _on_size_mode_changed(self) -> None:
        if self.size_mode.currentData() == "amount":
            self.amount_spin.setValue(0.001)
        else:
            self.amount_spin.setValue(self.ctx.settings.default_order_quote)
        self._update_estimate()

    def _on_mode_changed(self) -> None:
        mode = self.mode_combo.currentData()
        if mode == RunMode.LIVE.value:
            self.mode_hint.setText("⚠ 实盘模式将使用真实资金下单，请确认风控设置。")
            self.mode_hint.setStyleSheet("color: #f0b90b;")
        else:
            self.mode_hint.setText("纸面交易使用模拟资金，行情为真实行情。")
            self.mode_hint.setStyleSheet(f"color: {TEXT_DIM};")

    def _set_futures_controls(self) -> None:
        futures = self._market().is_futures
        self.leverage_spin.setEnabled(futures)
        self.reduce_check.setEnabled(futures)

    def _update_estimate(self) -> None:
        price = self.price_spin.value()
        if price <= 0 and self._ticker:
            price = self._ticker.last
        if self.size_mode.currentData() == "quote":
            quote = self.amount_spin.value()
            amount = quote / price if price > 0 else 0.0
        else:
            amount = self.amount_spin.value()
            quote = amount * price
        leverage = self.leverage_spin.value() if self._market().is_futures else 1.0
        self.estimate_label.setText(
            f"预计：{quote:,.2f} USDT  |  数量 {format_amount(amount)}  |  杠杆 {leverage:g}x"
        )

    # --------------------------------------------------------------- ordering
    def _submit_order(self, side: str) -> None:
        symbol = self.current_symbol()
        market = self._market()
        mode = RunMode(self.mode_combo.currentData())
        price = self.price_spin.value()
        if price <= 0 and self._ticker:
            price = self._ticker.last
        if price <= 0:
            self._show_error("尚未获取到有效价格。")
            return

        if self.size_mode.currentData() == "quote":
            quote = self.amount_spin.value()
            if quote <= 0:
                self._show_error("请输入下单金额。")
                return
            amount = quote / price
        else:
            amount = self.amount_spin.value()
            quote = amount * price
            if amount <= 0:
                self._show_error("请输入下单数量。")
                return

        estimated = quote
        if mode is RunMode.LIVE and self.ctx.network.value == "mainnet":
            if self.ctx.settings.risk.require_confirmation:
                answer = QMessageBox.question(
                    self,
                    "确认实盘下单",
                    f"即将在【实盘】提交 {side} 订单\n\n"
                    f"交易对：{symbol}\n方向：{OrderSide(side).label}\n"
                    f"数量：{format_amount(amount)}\n价格：{price:,.4f}\n"
                    f"预计金额：{estimated:,.2f} USDT\n\n确认提交？",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return

        market_type = market
        order_type = self.type_combo.currentData()
        leverage = self.leverage_spin.value() if market.is_futures else 1.0
        reduce_only = self.reduce_check.isChecked() and market.is_futures

        self.buy_btn.setEnabled(False)
        self.sell_btn.setEnabled(False)

        def submit():
            req = OrderRequest(
                symbol=symbol,
                side=side,
                order_type=order_type,
                amount=amount,
                price=price,
                reduce_only=reduce_only,
                leverage=leverage,
                market_type=market_type,
                mode=mode,
            )
            return self.ctx.trading.submit(req)

        def done(result) -> None:
            self.buy_btn.setEnabled(True)
            self.sell_btn.setEnabled(True)
            self.status.emit(
                f"下单成功：{symbol} {OrderSide(side).label} {format_amount(amount)} @ {price:,.4f}"
            )
            self._refresh_orders()
            self._refresh_trades()

        def failed(message: str) -> None:
            self.buy_btn.setEnabled(True)
            self.sell_btn.setEnabled(True)
            self._show_error(message)

        self.runner.run(submit, done, failed)

    def _refresh_orders(self) -> None:
        rows = self.ctx.db.recent_orders(100)
        self.orders_table.setRowCount(0)
        for row, record in enumerate(rows):
            from datetime import datetime

            ts = datetime.fromtimestamp((record["created_at"] or 0) / 1000)
            self.orders_table.set_row(
                row,
                [
                    ts.strftime("%m-%d %H:%M:%S"),
                    record["symbol"],
                    "买入" if record["side"] == "buy" else "卖出",
                    "市价" if record["type"] == "market" else "限价",
                    f"{record['price'] or 0:,.4f}",
                    format_amount(record["amount"] or 0),
                    record["status"] or "",
                ],
            )

    def _refresh_trades(self) -> None:
        mode = RunMode(self.mode_combo.currentData())
        if mode is RunMode.PAPER:
            rows = list(reversed(self.ctx.paper.account.trades[-100:]))
            self.trades_table.setRowCount(0)
            from datetime import datetime

            for row, trade in enumerate(rows):
                ts = datetime.fromtimestamp(trade["timestamp"] / 1000)
                self.trades_table.set_row(
                    row,
                    [
                        ts.strftime("%m-%d %H:%M:%S"),
                        trade["symbol"],
                        "买入" if trade["side"] == "buy" else "卖出",
                        f"{trade['price']:,.4f}",
                        format_amount(trade["amount"]),
                        f"{trade['fee']:.6f}",
                    ],
                )

    def _show_error(self, message: str) -> None:
        log.warning("交易页错误: %s", message)
        self.status.emit(f"错误：{message}")
        QMessageBox.warning(self, "操作失败", message)
