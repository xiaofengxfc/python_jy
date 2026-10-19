"""Portfolio overview: balances, positions and PnL."""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ...context import AppContext
from ...core.types import MarketType
from ...exchange.symbols import format_amount
from .. import theme
from ..widgets.common import Card, MetricCard, PageHeader, money_text, pnl_color
from ..widgets.tables import DataTable
from ..workers import TaskRunner

log = logging.getLogger(__name__)


class PortfolioPage(QWidget):
    status = Signal(str)

    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.runner = TaskRunner(self, max_threads=2)
        self._prices: dict[str, float] = {}
        self._build_ui()
        self.timer = QTimer(self)
        self.timer.setInterval(8000)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        QTimer.singleShot(200, self.refresh)

    def _build_ui(self) -> None:
        # The page scrolls: at small window heights the tables and metric
        # cards no longer fit, and a plain layout would clip the bottom rows
        # with no way to reach them.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        content = QWidget()
        root = QVBoxLayout(content)
        root.setContentsMargins(*theme.PAGE_MARGIN)
        root.setSpacing(theme.PAGE_GAP)

        header = PageHeader("资产与持仓", "账户余额、合约持仓与纸面账户表现")
        self.mode_label = QLabel("")
        self.mode_label.setObjectName("Subtitle")
        header.actions.addWidget(self.mode_label)
        self.refresh_btn = QPushButton("刷新")
        self.refresh_btn.setObjectName("Flat")
        self.refresh_btn.clicked.connect(self.refresh)
        header.add_action(self.refresh_btn)
        root.addWidget(header)

        cards = QHBoxLayout()
        cards.setSpacing(theme.PAGE_GAP)
        self.equity_card = MetricCard("总权益 (USDT)")
        self.available_card = MetricCard("可用余额")
        self.pnl_card = MetricCard("未实现盈亏")
        self.realized_card = MetricCard("已实现盈亏")
        for card in (self.equity_card, self.available_card, self.pnl_card, self.realized_card):
            cards.addWidget(card)
        root.addLayout(cards)

        # Left: how the account is allocated. Right: what it holds.
        tables = QHBoxLayout()
        tables.setSpacing(theme.PAGE_GAP)

        left_column = QVBoxLayout()
        left_column.setSpacing(theme.PAGE_GAP)

        balances_card = Card()
        balances_head = QHBoxLayout()
        balances_title = QLabel("账户余额")
        balances_title.setObjectName("Title")
        balances_head.addWidget(balances_title)
        balances_head.addStretch(1)
        self.balances_count = QLabel("")
        self.balances_count.setObjectName("Subtitle")
        balances_head.addWidget(self.balances_count)
        balances_card.body().addLayout(balances_head)
        self.balances_table = DataTable(["币种", "可用", "冻结", "合计"])
        self.balances_table.setMinimumHeight(180)
        balances_card.body().addWidget(self.balances_table, 1)
        left_column.addWidget(balances_card, 3)

        paper_card = Card()
        paper_title = QLabel("纸面账户")
        paper_title.setObjectName("Title")
        paper_card.body().addWidget(paper_title)
        self.paper_summary = QLabel("--")
        self.paper_summary.setObjectName("Subtitle")
        self.paper_summary.setWordWrap(True)
        paper_card.body().addWidget(self.paper_summary)
        paper_card.body().addStretch(1)
        reset_btn = QPushButton("重置纸面账户")
        reset_btn.setObjectName("Danger")
        reset_btn.clicked.connect(self._reset_paper)
        paper_card.body().addWidget(reset_btn)
        left_column.addWidget(paper_card, 2)
        tables.addLayout(left_column, 2)

        positions_card = Card()
        positions_head = QHBoxLayout()
        positions_title = QLabel("持仓")
        positions_title.setObjectName("Title")
        positions_head.addWidget(positions_title)
        positions_head.addStretch(1)
        self.positions_count = QLabel("")
        self.positions_count.setObjectName("Subtitle")
        positions_head.addWidget(self.positions_count)
        positions_card.body().addLayout(positions_head)
        self.positions_table = DataTable(
            ["交易对", "方向", "数量", "开仓价", "标记价", "未实现盈亏", "杠杆", "强平价"]
        )
        self.positions_table.setMinimumHeight(240)
        positions_card.body().addWidget(self.positions_table, 1)
        tables.addWidget(positions_card, 3)

        root.addLayout(tables, 1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(content)
        outer.addWidget(scroll)

    # ------------------------------------------------------------------ data
    def refresh(self) -> None:
        self._refresh_paper()
        self._refresh_paper_prices()
        if not self.ctx.rest.has_credentials:
            self.mode_label.setText("未配置 API Key，仅显示纸面数据")
            self.positions_table.setRowCount(0)
            self.balances_table.setRowCount(0)
            self.equity_card.set_title("总权益 (USDT)")
            self.available_card.set_title("可用余额")
            self.equity_card.set_value("--")
            self.available_card.set_value("--")
            self.pnl_card.set_value("--")
            return
        self.mode_label.setText(f"实盘账户 · {self.ctx.network.label}")
        market = self.ctx.market_type
        rest = self.ctx.rest_for(market)

        def fetch():
            balance = rest.fetch_balance(market)
            positions = rest.fetch_positions(market) if market.is_futures else []
            specs = {}
            for position in positions:
                symbol = position.get("symbol") or ""
                if symbol:
                    specs[symbol] = rest.contract_spec(market, symbol)
            settle = ""
            if market is MarketType.COINM:
                coin_spec = rest.contract_spec(market, self.ctx.settings.last_symbol)
                settle = coin_spec.settle or coin_spec.base or "BTC"
            return balance, positions, specs, settle

        self.runner.run(fetch, self._apply_account, self._show_error)

    def _refresh_paper(self) -> None:
        paper = self.ctx.paper
        prices = self._prices
        unrealized = paper.unrealized_pnl(prices)
        equity = paper.account.cash + sum(
            (pos.contracts * prices.get(symbol, pos.entry_price))
            for symbol, pos in paper.account.positions.items()
            if pos.contracts
        )
        self.paper_summary.setText(
            f"起始资金 {paper.account.starting_balance:,.2f} USDT　现金 {paper.account.cash:,.2f}　"
            f"权益 {equity:,.2f}　已实现 {paper.realized_pnl():+,.2f}　"
            f"未实现 {unrealized:+,.2f}　持仓品种 {sum(1 for p in paper.account.positions.values() if p.contracts)}"
        )
        if not self.ctx.rest.has_credentials:
            self.equity_card.set_value(money_text(equity))
            self.available_card.set_value(money_text(paper.account.cash))
            self.pnl_card.set_value(f"{unrealized:+,.2f}", pnl_color(unrealized))
            self.realized_card.set_value(f"{paper.realized_pnl():+,.2f}", pnl_color(paper.realized_pnl()))

    def _refresh_paper_prices(self) -> None:
        """Fetch prices for open paper positions in the background."""
        paper = self.ctx.paper
        symbols = [s for s, p in paper.account.positions.items() if p.contracts]
        if not symbols:
            self._prices = {}
            return
        market = self.ctx.market_type
        rest = self.ctx.rest_for(market)

        def fetch():
            prices = {}
            for symbol in symbols:
                try:
                    raw = rest.fetch_ticker(market, symbol)
                    prices[symbol] = float(raw.get("last") or 0.0)
                except Exception:
                    continue
            return prices

        self.runner.run(fetch, self._apply_paper_prices, lambda _m: None)

    def _apply_paper_prices(self, prices: dict) -> None:
        self._prices = {k: v for k, v in prices.items() if v}
        self._refresh_paper()

    def _apply_account(self, payload) -> None:
        balance, positions, specs, settle_asset = payload
        market = self.ctx.market_type
        totals = balance.get("total") or {}
        frees = balance.get("free") or {}
        total = float(totals.get("USDT") or 0)
        free = float(frees.get("USDT") or 0)
        unrealized = 0.0

        self.positions_table.setRowCount(0)
        row = 0
        for position in positions:
            contracts = float(position.get("contracts") or 0)
            if not contracts:
                continue
            pnl = float(position.get("unrealizedPnl") or 0)
            unrealized += pnl
            side = "多" if (position.get("side") or "long") == "long" else "空"
            mark = float(position.get("markPrice") or 0)
            p_spec = specs.get(position.get("symbol", ""))
            if p_spec is not None and p_spec.is_inverse and mark > 0:
                # Binance reports coin-M sizes in contracts; show both units.
                qty = (
                    f"{format_amount(abs(contracts))} 张"
                    f" ({format_amount(p_spec.contracts_to_base(abs(contracts), mark))} {p_spec.base})"
                )
            else:
                qty = format_amount(abs(contracts))
            self.positions_table.set_row(
                row,
                [
                    position.get("symbol", ""),
                    side,
                    qty,
                    f"{float(position.get('entryPrice') or 0):,.4f}",
                    f"{mark:,.4f}",
                    f"{pnl:+,.2f}",
                    f"{float(position.get('leverage') or 1):g}x",
                    f"{float(position.get('liquidationPrice') or 0):,.4f}",
                ],
                colors=[None, theme.current().up if side == "多" else theme.current().down,
                        None, None, None, pnl_color(pnl), None, None],
            )
            row += 1

        if market is MarketType.COINM:
            # Coin-M wallets hold the margin coin, not USDT.
            asset = settle_asset or "BTC"
            coin_total = float(totals.get(asset) or 0)
            coin_free = float(frees.get(asset) or 0)
            self.equity_card.set_title(f"总权益 ({asset})")
            self.available_card.set_title(f"可用 ({asset})")
            self.equity_card.set_value(
                format_amount(coin_total), f"未实现 {unrealized:+,.2f} USDT"
            )
            self.available_card.set_value(format_amount(coin_free))
        else:
            self.equity_card.set_title("总权益 (USDT)")
            self.available_card.set_title("可用余额")
            self.equity_card.set_value(money_text(total))
            self.available_card.set_value(money_text(free))
        self.pnl_card.set_value(f"{unrealized:+,.2f}", pnl_color(unrealized))
        self.realized_card.set_value("--")
        self.positions_count.setText(f"{row} 个持仓" if row else "无持仓")

        self.balances_table.setRowCount(0)
        totals = balance.get("total") or {}
        frees = balance.get("free") or {}
        useds = balance.get("used") or {}
        rows = sorted(
            ((asset, float(value or 0)) for asset, value in totals.items() if value),
            key=lambda item: -item[1],
        )
        self.balances_count.setText(f"{len(rows)} 个币种" if rows else "无余额")
        for row, (asset, value) in enumerate(rows):
            self.balances_table.set_row(
                row,
                [
                    asset,
                    format_amount(float(frees.get(asset) or 0)),
                    format_amount(float(useds.get(asset) or 0)),
                    format_amount(value),
                ],
            )

    def _reset_paper(self) -> None:
        answer = QMessageBox.question(
            self, "重置纸面账户", "确认清空纸面持仓与成交并恢复初始资金？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.ctx.paper.reset(10_000.0)
            self.refresh()
            self.status.emit("纸面账户已重置")

    def _show_error(self, message: str) -> None:
        log.warning("资产页错误: %s", message)
        self.status.emit(f"资产获取失败：{message}")
