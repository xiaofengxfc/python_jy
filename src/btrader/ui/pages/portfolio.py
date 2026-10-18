"""Portfolio overview: balances, positions and PnL."""

from __future__ import annotations

import logging

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...context import AppContext
from ...core.types import MarketType
from ...exchange.symbols import format_amount
from ..theme import DOWN, UP
from ..widgets.common import Card, MetricCard, money_text, pnl_color
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
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        header = QHBoxLayout()
        title = QLabel("资产与持仓")
        title.setObjectName("Title")
        header.addWidget(title)
        self.mode_label = QLabel("")
        self.mode_label.setObjectName("Muted")
        header.addWidget(self.mode_label)
        header.addStretch(1)
        self.refresh_btn = QPushButton("刷新")
        self.refresh_btn.setObjectName("Flat")
        self.refresh_btn.clicked.connect(self.refresh)
        header.addWidget(self.refresh_btn)
        root.addLayout(header)

        cards = QHBoxLayout()
        cards.setSpacing(10)
        self.equity_card = MetricCard("总权益 (USDT)")
        self.available_card = MetricCard("可用余额")
        self.pnl_card = MetricCard("未实现盈亏")
        self.realized_card = MetricCard("已实现盈亏")
        for card in (self.equity_card, self.available_card, self.pnl_card, self.realized_card):
            cards.addWidget(card)
        root.addLayout(cards)

        self.paper_card = Card()
        paper_title = QLabel("纸面账户")
        paper_title.setObjectName("Title")
        self.paper_card.body().addWidget(paper_title)
        self.paper_summary = QLabel("--")
        self.paper_summary.setWordWrap(True)
        self.paper_card.body().addWidget(self.paper_summary)
        reset_btn = QPushButton("重置纸面账户")
        reset_btn.setObjectName("Flat")
        reset_btn.setFixedWidth(140)
        reset_btn.clicked.connect(self._reset_paper)
        self.paper_card.body().addWidget(reset_btn)
        root.addWidget(self.paper_card)

        positions_title = QLabel("持仓")
        positions_title.setObjectName("Title")
        root.addWidget(positions_title)
        self.positions_table = DataTable(
            ["交易对", "方向", "数量", "开仓价", "标记价", "未实现盈亏", "杠杆", "强平价"]
        )
        self.positions_table.setMinimumHeight(180)
        root.addWidget(self.positions_table, 1)

        balances_title = QLabel("账户余额")
        balances_title.setObjectName("Title")
        root.addWidget(balances_title)
        self.balances_table = DataTable(["币种", "可用", "冻结", "合计"])
        self.balances_table.setMinimumHeight(150)
        root.addWidget(self.balances_table, 1)

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
                colors=[None, UP if side == "多" else DOWN, None, None, None, pnl_color(pnl), None, None],
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

        self.balances_table.setRowCount(0)
        totals = balance.get("total") or {}
        frees = balance.get("free") or {}
        useds = balance.get("used") or {}
        rows = sorted(
            ((asset, float(value or 0)) for asset, value in totals.items() if value),
            key=lambda item: -item[1],
        )
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
