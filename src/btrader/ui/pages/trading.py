"""Trading workstation: chart, order book and manual order ticket."""

from __future__ import annotations

import logging

import pandas as pd
import qtawesome as qta
from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QButtonGroup,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
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
from .. import theme
from ..widgets.chart import CandleChart
from ..widgets.common import Card, PageHeader, divider
from ..widgets.orderbook import OrderBookWidget
from ..widgets.tables import DataTable
from ..workers import TaskRunner

log = logging.getLogger(__name__)


class OrderDrawer(QFrame):
    """Bottom dock for open orders / history / fills.

    Collapsed it is a single bar; clicking the bar slides the tab widget in
    and out, so the chart keeps the vertical space by default.
    """

    HEADER_H = 34
    EXPANDED_H = 236

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Drawer")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 8)
        layout.setSpacing(6)

        self.header = QPushButton()
        self.header.setObjectName("DrawerHeader")
        self.header.setCursor(Qt.CursorShape.PointingHandCursor)
        self.header.setFixedHeight(self.HEADER_H - 12)
        self.header.setToolTip("展开或收起委托面板")
        self.header.clicked.connect(self.toggle)
        layout.addWidget(self.header)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.currentChanged.connect(lambda _i: self.sync_header())
        layout.addWidget(self.tabs, 1)

        self._expanded = False
        self.set_expanded(False)
        theme.bus().changed.connect(lambda _p: self.sync_header())

    def toggle(self) -> None:
        self.set_expanded(not self._expanded)

    def set_expanded(self, expanded: bool) -> None:
        self._expanded = expanded
        self.tabs.setVisible(expanded)
        self.setFixedHeight(self.EXPANDED_H if expanded else self.HEADER_H + 14)
        self.sync_header()

    def sync_header(self) -> None:
        palette = theme.current()
        index = max(0, self.tabs.currentIndex())
        label = self.tabs.tabText(index) if self.tabs.count() else "委托"
        self.header.setText(f"  {label}")
        self.header.setIcon(
            qta.icon(
                "fa6s.chevron-up" if self._expanded else "fa6s.chevron-down",
                color=palette.text_dim,
            )
        )
        self.header.setIconSize(QSize(11, 11))


class TradingPage(QWidget):
    status = Signal(str)

    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.runner = TaskRunner(self, max_threads=3)
        self._symbols: list[str] = []
        self._ticker: Ticker | None = None
        self._spec = None  # ContractSpec for the selected symbol
        self._df = pd.DataFrame()
        self._loading = False
        self._live_available: float | None = None
        self._balance_pending = False
        self._pending_percent: int | None = None

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
        root.setContentsMargins(*theme.PAGE_MARGIN)
        root.setSpacing(theme.PAGE_GAP)
        root.addWidget(PageHeader("交易", "行情、订单簿与手动下单"))
        root.addWidget(self._build_symbol_bar())

        # The drawer exists before it is placed: its collapsed bar sits under
        # the chart and owns the order tabs.
        self._build_bottom_tabs()

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(theme.PAGE_GAP)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(theme.PAGE_GAP)

        # The chart owns all remaining height; orders live in the drawer bar.
        chart_card = Card(padding=8)
        self.chart = CandleChart()
        chart_card.body().addWidget(self.chart, 1)
        left_layout.addWidget(chart_card, 1)
        left_layout.addWidget(self.drawer)
        splitter.addWidget(left)

        right = QWidget()
        right.setMinimumWidth(320)
        right.setMaximumWidth(360)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(theme.PAGE_GAP)
        right_layout.addWidget(self._build_book_card(), 2)
        right_layout.addWidget(self._build_order_ticket(), 3)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        splitter.setSizes([1040, 330])
        root.addWidget(splitter, 1)

    def _stat(self, layout: QHBoxLayout, caption: str) -> QLabel:
        column = QVBoxLayout()
        column.setSpacing(1)
        label = QLabel(caption)
        label.setObjectName("Eyebrow")
        value = QLabel("--")
        value.setStyleSheet("font-size: 13px; font-weight: 600;")
        column.addWidget(label)
        column.addWidget(value)
        layout.addLayout(column)
        return value

    def _build_symbol_bar(self) -> QWidget:
        card = Card()
        layout = QHBoxLayout()
        layout.setSpacing(16)

        self.market_combo = QComboBox()
        for market in MarketType:
            self.market_combo.addItem(market.label, market.value)
        self.market_combo.setFixedWidth(116)
        self.market_combo.currentIndexChanged.connect(self._on_market_changed)
        layout.addWidget(self.market_combo)

        self.symbol_combo = QComboBox()
        self.symbol_combo.setEditable(True)
        self.symbol_combo.setMinimumWidth(180)
        self.symbol_combo.setEditText(self.ctx.settings.last_symbol)
        self.symbol_combo.lineEdit().returnPressed.connect(self._on_symbol_changed)
        self.symbol_combo.activated.connect(lambda _: self._on_symbol_changed())
        layout.addWidget(self.symbol_combo)

        separator = divider(horizontal=False)
        separator.setFixedHeight(theme.CONTROL_HEIGHT)
        layout.addWidget(separator)

        price_column = QVBoxLayout()
        price_column.setSpacing(0)
        price_row = QHBoxLayout()
        price_row.setSpacing(theme.CONTROL_GAP)
        self.price_label = QLabel("--")
        self.price_label.setObjectName("SymbolPrice")
        price_row.addWidget(self.price_label)
        self.change_label = QLabel("")
        price_row.addWidget(self.change_label)
        price_row.addStretch(1)
        price_column.addLayout(price_row)
        self.symbol_caption = QLabel("")
        self.symbol_caption.setObjectName("Subtitle")
        price_column.addWidget(self.symbol_caption)
        layout.addLayout(price_column)

        layout.addStretch(1)

        stats = QHBoxLayout()
        stats.setSpacing(22)
        self.high_label = self._stat(stats, "24H 最高")
        self.low_label = self._stat(stats, "24H 最低")
        self.volume_label = self._stat(stats, "24H 成交额")
        layout.addLayout(stats)

        self.timeframe_combo = QComboBox()
        self.timeframe_combo.addItems(TIMEFRAMES)
        self.timeframe_combo.setCurrentText(self.ctx.settings.timeframe)
        self.timeframe_combo.setFixedWidth(82)
        self.timeframe_combo.currentTextChanged.connect(self._on_timeframe_changed)
        layout.addWidget(self.timeframe_combo)

        self.refresh_btn = QPushButton("刷新")
        self.refresh_btn.setObjectName("Flat")
        self.refresh_btn.clicked.connect(lambda: self._load_klines(reset_zoom=True))
        layout.addWidget(self.refresh_btn)
        card.body().addLayout(layout)
        return card

    def _build_book_card(self) -> QWidget:
        card = Card()
        header = QHBoxLayout()
        title = QLabel("订单簿")
        title.setObjectName("Title")
        header.addWidget(title)
        header.addStretch(1)
        self.book_spread = QLabel("")
        self.book_spread.setObjectName("Subtitle")
        header.addWidget(self.book_spread)
        card.body().addLayout(header)
        self.book = OrderBookWidget()
        card.body().addWidget(self.book, 1)
        return card

    def _field_row(self, caption: str, widget: QWidget, extra: QWidget | None = None) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(theme.CONTROL_GAP)
        label = QLabel(caption)
        label.setObjectName("FieldLabel")
        label.setFixedWidth(36)
        row.addWidget(label)
        row.addWidget(widget, 1)
        if extra is not None:
            row.addWidget(extra)
        return row

    def _segment_row(self, options: list[tuple[str, str]], on_click) -> QHBoxLayout:
        """A two-state segmented control. Options are (label, value)."""
        row = QHBoxLayout()
        row.setSpacing(0)
        self.type_group = QButtonGroup(self)
        self.type_group.setExclusive(True)
        for index, (label, value) in enumerate(options):
            button = QPushButton(label)
            button.setObjectName("SegmentLeft" if index == 0 else "SegmentRight")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setProperty("segmentValue", value)
            self.type_group.addButton(button, index)
            row.addWidget(button)
        self.type_group.button(0).setChecked(True)
        self.type_group.idClicked.connect(lambda _index: on_click())
        return row

    def _size_toggle(self) -> QWidget:
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        self.size_group = QButtonGroup(self)
        self.size_group.setExclusive(True)
        for index, (label, value) in enumerate((("金额", "quote"), ("数量", "amount"))):
            button = QPushButton(label)
            button.setObjectName("MiniSegmentLeft" if index == 0 else "MiniSegmentRight")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setProperty("segmentValue", value)
            self.size_group.addButton(button, index)
            row.addWidget(button)
        self.size_group.button(0).setChecked(True)
        self.size_group.idClicked.connect(lambda _index: self._on_size_mode_changed())
        return holder

    def _order_type(self) -> str:
        button = self.type_group.checkedButton()
        return button.property("segmentValue") if button else OrderType.MARKET.value

    def _size_mode_value(self) -> str:
        button = self.size_group.checkedButton()
        return button.property("segmentValue") if button else "quote"

    def _build_order_ticket(self) -> QWidget:
        card = Card()

        header = QHBoxLayout()
        title = QLabel("手动下单")
        title.setObjectName("Title")
        header.addWidget(title)
        header.addStretch(1)
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("纸面交易", RunMode.PAPER.value)
        self.mode_combo.addItem("实盘", RunMode.LIVE.value)
        if self.ctx.network.value == "testnet":
            self.mode_combo.addItem("测试网实盘", RunMode.LIVE.value)
        self.mode_combo.setFixedWidth(120)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        header.addWidget(self.mode_combo)
        card.body().addLayout(header)
        card.body().addWidget(divider())

        # Step 1 — order type.
        card.body().addLayout(
            self._segment_row(
                [("市价", OrderType.MARKET.value), ("限价", OrderType.LIMIT.value)],
                self._on_order_type_changed,
            )
        )

        # Step 2 — price, then size with its unit toggle beside the field.
        self.price_spin = QDoubleSpinBox()
        self.price_spin.setDecimals(6)
        self.price_spin.setRange(0, 1e12)
        self.price_spin.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
        self.price_spin.setEnabled(False)
        self.price_spin.setAlignment(Qt.AlignmentFlag.AlignRight)
        card.body().addLayout(self._field_row("价格", self.price_spin))

        self.amount_spin = QDoubleSpinBox()
        self.amount_spin.setDecimals(6)
        self.amount_spin.setRange(0, 1e12)
        self.amount_spin.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
        self.amount_spin.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.amount_spin.setValue(self.ctx.settings.default_order_quote)
        card.body().addLayout(self._field_row("数量", self.amount_spin, self._size_toggle()))

        # Step 3 — quick sizing by share of available balance.
        self.percent_row = QHBoxLayout()
        self.percent_row.setSpacing(6)
        self.percent_buttons: list[QPushButton] = []
        for percent in (25, 50, 75, 100):
            button = QPushButton(f"{percent}%")
            button.setObjectName("Percent")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _checked=False, value=percent: self._apply_percent(value))
            self.percent_row.addWidget(button)
            self.percent_buttons.append(button)
        card.body().addLayout(self.percent_row)

        # Step 4 — read-back before committing.
        self.estimate_label = QLabel("预计金额：--")
        self.estimate_label.setObjectName("Subtitle")
        card.body().addWidget(self.estimate_label)

        self.unit_hint = QLabel("")
        self.unit_hint.setObjectName("Subtitle")
        self.unit_hint.setWordWrap(True)
        card.body().addWidget(self.unit_hint)

        self.leverage_spin = QDoubleSpinBox()
        self.leverage_spin.setDecimals(0)
        self.leverage_spin.setRange(1, 125)
        self.leverage_spin.setValue(1)
        self.leverage_spin.setEnabled(False)
        self.leverage_spin.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
        leverage_row = QHBoxLayout()
        leverage_row.setSpacing(theme.CONTROL_GAP)
        leverage_row.addLayout(self._field_row("杠杆", self.leverage_spin), 1)
        self.reduce_check = QCheckBox("只减仓")
        self.reduce_check.setEnabled(False)
        leverage_row.addWidget(self.reduce_check)
        card.body().addLayout(leverage_row)

        card.body().addWidget(divider())

        # Step 5 — the side is the last decision.
        buttons = QHBoxLayout()
        buttons.setSpacing(theme.CONTROL_GAP)
        self.buy_btn = QPushButton("买入 / 做多")
        self.buy_btn.setObjectName("Buy")
        self.buy_btn.setMinimumHeight(theme.CONTROL_HEIGHT + 4)
        self.buy_btn.clicked.connect(lambda: self._submit_order(OrderSide.BUY.value))
        self.sell_btn = QPushButton("卖出 / 做空")
        self.sell_btn.setObjectName("Sell")
        self.sell_btn.setMinimumHeight(theme.CONTROL_HEIGHT + 4)
        self.sell_btn.clicked.connect(lambda: self._submit_order(OrderSide.SELL.value))
        buttons.addWidget(self.buy_btn)
        buttons.addWidget(self.sell_btn)
        card.body().addLayout(buttons)

        self.mode_hint = QLabel("")
        self.mode_hint.setObjectName("Subtitle")
        self.mode_hint.setWordWrap(True)
        card.body().addWidget(self.mode_hint)

        self.price_spin.valueChanged.connect(self._update_estimate)
        self.amount_spin.valueChanged.connect(self._update_estimate)
        self.leverage_spin.valueChanged.connect(self._update_estimate)
        self._on_mode_changed()
        return card

    def _build_bottom_tabs(self) -> QWidget:
        self.drawer = OrderDrawer()
        self.bottom_tabs = self.drawer.tabs

        open_page = QWidget()
        open_layout = QVBoxLayout(open_page)
        open_layout.setContentsMargins(0, 0, 0, 0)
        open_layout.setSpacing(4)
        open_bar = QHBoxLayout()
        open_bar.addStretch(1)
        self.cancel_btn = QPushButton("撤销选中委托")
        self.cancel_btn.setObjectName("Flat")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._cancel_selected_order)
        open_bar.addWidget(self.cancel_btn)
        open_layout.addLayout(open_bar)
        self.open_orders_table = DataTable(
            ["时间", "交易对", "方向", "类型", "价格", "数量", "已成交", "状态", "ID"]
        )
        self.open_orders_table.setColumnHidden(8, True)
        self.open_orders_table.itemSelectionChanged.connect(
            lambda: self.cancel_btn.setEnabled(self.open_orders_table.currentRow() >= 0)
        )
        open_layout.addWidget(self.open_orders_table, 1)
        self._open_orders_page = open_page
        self.bottom_tabs.addTab(open_page, "当前委托")

        self.orders_table = DataTable(["时间", "交易对", "方向", "类型", "价格", "数量", "状态"])
        self.trades_table = DataTable(["时间", "交易对", "方向", "价格", "数量", "手续费"])
        self.bottom_tabs.addTab(self.orders_table, "历史委托")
        self.bottom_tabs.addTab(self.trades_table, "成交记录")
        self._open_orders: list[dict] = []
        return self.drawer

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
        if not symbol or self.bottom_tabs.currentIndex() == 2:
            return

        def fetch():
            return self.ctx.rest_for(market).fetch_order_book(market, symbol, 20)

        self.runner.run(fetch, self._apply_book, lambda _m: None)

    def _apply_ticker(self, symbol: str, raw: dict) -> None:
        ticker = ticker_from_ccxt(symbol, raw)
        self._ticker = ticker
        # Paper limit orders rest until the live price crosses them.
        self._fill_paper_limits(symbol, ticker.last)
        self.price_label.setText(f"{ticker.last:,.4f}")
        self.change_label.setText(f"{ticker.change_pct:+.2f}%")
        color = theme.current().up if ticker.change_pct >= 0 else theme.current().down
        self.change_label.setStyleSheet(f"color: {color}; font-weight: 600;")
        self.high_label.setText(f"{ticker.high:,.4f}")
        self.low_label.setText(f"{ticker.low:,.4f}")
        self.volume_label.setText(f"{ticker.quote_volume:,.0f}")
        self.symbol_caption.setText(f"{symbol} · 最新价")
        if self._order_type() == OrderType.MARKET.value:
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

    def _fill_paper_limits(self, symbol: str, price: float) -> None:
        """Fill resting paper limit orders when the live price crosses them."""
        if not price or price <= 0:
            return
        mode = self.mode_combo.currentData()
        if mode != RunMode.PAPER.value:
            return
        filled = self.ctx.paper.try_fill(symbol, price, price)
        if not filled:
            return
        market = self._market()
        for order in filled:
            self.ctx.db.save_order("paper", market.value, order, RunMode.PAPER.value)
            self.status.emit(
                f"限价委托成交：{symbol} {format_amount(float(order.get('amount') or 0))} "
                f"@ {float(order.get('average') or 0):,.4f}"
            )
        self._refresh_orders()
        self._refresh_trades()

    # ----------------------------------------------------------------- events
    def current_symbol(self) -> str:
        return self.symbol_combo.currentText().strip()

    def _market(self) -> MarketType:
        return MarketType(self.market_combo.currentData())

    def _on_market_changed(self) -> None:
        market = self._market()
        self.ctx.set_market_type(market)
        self._spec = None  # contract size differs per market
        self._set_futures_controls()
        self._load_symbols()

    def _on_symbol_changed(self) -> None:
        symbol = self.current_symbol()
        if not symbol:
            return
        self.ctx.settings.last_symbol = symbol
        self.ctx.save_settings()
        self._load_contract_spec()
        self._load_klines(reset_zoom=True)
        self._refresh_live()
        self._refresh_book()

    def _load_contract_spec(self) -> None:
        """Fetch how many coins one contract represents (coin-M only, cheap)."""
        symbol = self.current_symbol()
        market = self._market()
        self._spec = None
        if not symbol:
            return
        rest = self.ctx.rest_for(market)

        def fetch():
            return rest.contract_spec(market, symbol)

        def done(spec) -> None:
            self._spec = spec
            self._update_estimate()

        self.runner.run(fetch, done, lambda _m: None)

    def _unit_hint_text(self, spec) -> str:
        if spec is None:
            return ""
        if spec.is_inverse:
            return (
                f"币本位合约：{spec.describe()}。"
                "数量按币填写，下单时会自动换算成张数（不足 1 张会被拒绝）。"
            )
        return ""

    def _on_timeframe_changed(self, timeframe: str) -> None:
        self.ctx.settings.timeframe = timeframe
        self.ctx.save_settings()
        if not self._df.empty:
            self._load_klines(reset_zoom=True)

    def _on_order_type_changed(self) -> None:
        is_limit = self._order_type() == OrderType.LIMIT.value
        self.price_spin.setEnabled(is_limit)
        if is_limit and self._ticker:
            self.price_spin.setValue(self._ticker.last)

    def _on_size_mode_changed(self) -> None:
        if self._size_mode_value() == "amount":
            self.amount_spin.setDecimals(6)
            self.amount_spin.setValue(0.001)
        else:
            self.amount_spin.setDecimals(2)
            self.amount_spin.setValue(self.ctx.settings.default_order_quote)
        self._update_estimate()

    def _effective_price(self) -> float:
        """Limit price when set, otherwise the last traded price."""
        price = self.price_spin.value()
        if price <= 0 and self._ticker:
            price = self._ticker.last
        return price

    def _available_quote(self) -> float | None:
        """Quote balance backing the percentage shortcuts, None if unknown."""
        if RunMode(self.mode_combo.currentData()) is RunMode.PAPER:
            return max(0.0, self.ctx.paper.account.cash)
        return self._live_available

    def _request_balance(self) -> None:
        """Fetch the live quote balance once, then replay a pending shortcut."""
        if self._balance_pending:
            return
        market = self._market()
        rest = self.ctx.rest_for(market)
        if not rest.has_credentials:
            self._pending_percent = None
            self.unit_hint.setText("未配置 API 凭据，无法读取实盘可用余额。")
            return
        self._balance_pending = True
        self.status.emit("正在读取可用余额…")

        def fetch():
            return rest.fetch_balance(market)

        def done(balance: dict) -> None:
            self._balance_pending = False
            free = balance.get("free") or {}
            self._live_available = float(free.get("USDT") or 0.0)
            pending, self._pending_percent = self._pending_percent, None
            if pending:
                self._apply_percent(pending)

        def failed(message: str) -> None:
            self._balance_pending = False
            self._pending_percent = None
            self._show_error(message)

        self.runner.run(fetch, done, failed)

    def _apply_percent(self, percent: int) -> None:
        """Fill the size field with a share of the available balance."""
        available = self._available_quote()
        if available is None:
            self._pending_percent = percent
            self._request_balance()
            return
        quote = available * percent / 100.0
        if self._size_mode_value() == "quote":
            self.amount_spin.setValue(round(quote, 2))
            return
        price = self._effective_price()
        if price <= 0:
            self._show_error("尚未获取到有效价格。")
            return
        self.amount_spin.setValue(round(quote / price, 6))

    def _on_mode_changed(self) -> None:
        if hasattr(self, "_live_available"):
            self._live_available = None
        mode = self.mode_combo.currentData()
        if mode == RunMode.LIVE.value:
            self.mode_hint.setText("⚠ 实盘模式将使用真实资金下单，请确认风控设置。")
            self.mode_hint.setStyleSheet(f"color: {theme.current().accent};")
        else:
            self.mode_hint.setText("纸面交易使用模拟资金，行情为真实行情。")
            self.mode_hint.setStyleSheet(f"color: {theme.current().text_dim};")

    def _set_futures_controls(self) -> None:
        futures = self._market().is_futures
        self.leverage_spin.setEnabled(futures)
        self.reduce_check.setEnabled(futures)

    def _update_estimate(self) -> None:
        price = self._effective_price()
        if self._size_mode_value() == "quote":
            quote = self.amount_spin.value()
            amount = quote / price if price > 0 else 0.0
        else:
            amount = self.amount_spin.value()
            quote = amount * price
        leverage = self.leverage_spin.value() if self._market().is_futures else 1.0
        text = f"预计：{quote:,.2f} USDT  |  数量 {format_amount(amount)}  |  杠杆 {leverage:g}x"
        spec = self._spec
        if spec is not None and spec.is_inverse and price > 0 and amount > 0:
            contracts = spec.base_to_contracts(amount, price)
            text = (
                f"预计：{quote:,.2f} USDT  |  {format_amount(amount)} {spec.base} "
                f"≈ {format_amount(contracts)} 张  |  杠杆 {leverage:g}x"
            )
        self.estimate_label.setText(text)
        self.unit_hint.setText(self._unit_hint_text(spec))

    # --------------------------------------------------------------- ordering
    def _submit_order(self, side: str) -> None:
        symbol = self.current_symbol()
        market = self._market()
        mode = RunMode(self.mode_combo.currentData())
        price = self._effective_price()
        if price <= 0:
            self._show_error("尚未获取到有效价格。")
            return

        if self._size_mode_value() == "quote":
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
        order_type = self._order_type()
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
        from datetime import datetime

        self.orders_table.setRowCount(0)
        for row, record in enumerate(rows):
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
        self._refresh_open_orders()

    def _refresh_open_orders(self) -> None:
        """List resting orders: paper orders live in memory, live orders on Binance."""
        mode = RunMode(self.mode_combo.currentData())
        market = self._market()
        if mode is RunMode.PAPER:
            open_orders = [
                dict(order)
                for order in self.ctx.paper.account.orders
                if order.get("status") == "open"
            ]
            self._apply_open_orders(list(reversed(open_orders)))
            return
        if not self.ctx.rest.has_credentials:
            self._apply_open_orders([])
            return
        rest = self.ctx.rest_for(market)

        def fetch():
            return rest.fetch_open_orders(market)

        self.runner.run(fetch, self._apply_open_orders, lambda _m: self._apply_open_orders([]))

    def _apply_open_orders(self, orders: list[dict]) -> None:
        from datetime import datetime

        self._open_orders = list(orders)
        self.open_orders_table.setRowCount(0)
        for row, order in enumerate(self._open_orders):
            ts = datetime.fromtimestamp((order.get("timestamp") or 0) / 1000)
            self.open_orders_table.set_row(
                row,
                [
                    ts.strftime("%m-%d %H:%M:%S"),
                    order.get("symbol", ""),
                    "买入" if order.get("side") == "buy" else "卖出",
                    "市价" if order.get("type") == "market" else "限价",
                    f"{float(order.get('price') or 0):,.4f}",
                    format_amount(float(order.get("amount") or 0)),
                    format_amount(float(order.get("filled") or 0)),
                    order.get("status") or "",
                    str(order.get("id") or ""),
                ],
            )
        self.cancel_btn.setEnabled(False)
        index = self.bottom_tabs.indexOf(self._open_orders_page)
        if index >= 0:
            self.bottom_tabs.setTabText(index, f"当前委托 ({len(self._open_orders)})")
        self.drawer.sync_header()

    def _cancel_selected_order(self) -> None:
        row = self.open_orders_table.currentRow()
        if row < 0 or row >= len(self._open_orders):
            return
        order = self._open_orders[row]
        order_id = str(order.get("id") or "")
        symbol = order.get("symbol") or self.current_symbol()
        if not order_id:
            return
        mode = RunMode(self.mode_combo.currentData())
        market = self._market()
        if not QMessageBox.question(
            self,
            "撤销委托",
            f"确认撤销 {symbol} 的委托 {order_id}？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) == QMessageBox.StandardButton.Yes:
            return
        self.cancel_btn.setEnabled(False)

        def cancel():
            self.ctx.trading.cancel(market, symbol, order_id, mode)
            return True

        def done(_ok) -> None:
            self.status.emit(f"已撤销委托 {order_id}")
            self._refresh_orders()

        def failed(message: str) -> None:
            self.cancel_btn.setEnabled(True)
            self._show_error(message)

        self.runner.run(cancel, done, failed)

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
