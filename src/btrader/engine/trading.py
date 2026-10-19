"""Order execution used by manual trading and the automated engine.

The service is the single place where an order can leave the application. It
applies risk checks, precision rounding and (in paper mode) the paper broker.
"""

from __future__ import annotations

import logging
import math
import uuid
from dataclasses import dataclass

from ..core.config import AppSettings
from ..core.models import now_ms
from ..core.storage import Database
from ..core.types import MarketType, Network, OrderSide, OrderType, RunMode
from ..exchange.rest import BinanceREST, ExchangeError
from ..exchange.symbols import format_amount
from .paper import PaperBroker
from .risk import RiskDecision, RiskManager

log = logging.getLogger(__name__)


class OrderRejected(RuntimeError):
    pass


@dataclass
class OrderRequest:
    symbol: str
    side: str
    order_type: str
    amount: float
    price: float | None = None
    quote_amount: float | None = None
    reduce_only: bool = False
    leverage: float = 1.0
    market_type: MarketType = MarketType.SPOT
    mode: RunMode = RunMode.PAPER


@dataclass
class OrderResult:
    order: dict
    mode: RunMode
    quote_notional: float
    amount_base: float = 0.0
    amount_exchange: float = 0.0


class TradingService:
    def __init__(
        self,
        settings: AppSettings,
        rest: BinanceREST,
        risk: RiskManager,
        paper: PaperBroker,
        db: Database,
    ) -> None:
        self.settings = settings
        self.rest = rest
        self.risk = risk
        self.paper = paper
        self.db = db
        self.network = Network(settings.network)

    # ------------------------------------------------------------------ sizing
    def amount_from_quote(self, market_type: MarketType, symbol: str, quote: float, price: float) -> float:
        if price <= 0:
            raise OrderRejected("价格无效，无法计算下单数量。")
        raw = quote / price
        return self.rest.amount_to_precision(market_type, symbol, raw)

    def exchange_amount(
        self,
        market_type: MarketType,
        symbol: str,
        base_qty: float,
        price: float,
        *,
        offline: bool = False,
    ) -> tuple[float, float]:
        """Convert a base-coin quantity into the exchange's amount unit.

        Returns ``(exchange_amount, base_qty)``. For inverse contracts the
        exchange amount is a number of contracts (each worth ``contractSize``
        USD), so the returned base quantity is the rounded value that the paper
        account and the risk checks should use. Spot and linear markets keep
        the base quantity itself as the exchange amount.

        ``offline=True`` is used for paper trading: it never touches the
        network, so a simulated order cannot be refused just because the
        exchange is unreachable. Contract specs already fetched by the market
        page are reused from the cache.

        Raises ``OrderRejected`` when the rounded size falls below the market
        minimum (e.g. less than one contract on coin-M).
        """
        spec = self.rest.contract_spec(
            market_type, symbol, allow_network=not offline
        )
        raw = spec.base_to_contracts(base_qty, price)
        if raw <= 0:
            raise OrderRejected("数量无效，无法计算下单量。")
        if spec.min_amount > 0 and raw < spec.min_amount - 1e-12:
            raise OrderRejected(
                f"下单量 {format_amount(raw)} 低于最小下单量 "
                f"{format_amount(spec.min_amount)}。（{spec.describe()}）"
            )
        if offline:
            # Paper fills are simulated locally: round exactly the way the
            # venue would (inverse contracts are whole numbers, never up).
            exchange_amount = math.floor(raw + 1e-12) if spec.inverse else raw
        else:
            exchange_amount = self.rest.amount_to_precision(market_type, symbol, raw)
        if exchange_amount <= 0:
            raise OrderRejected(
                f"下单量 {format_amount(raw)} 无法满足交易所的最小下单单位。"
                f"（{spec.describe()}）"
            )
        rounded_base = spec.contracts_to_base(exchange_amount, price)
        return float(exchange_amount), rounded_base

    def estimate_cost(self, amount: float, price: float) -> float:
        return abs(amount) * price

    # --------------------------------------------------------------- validate
    def precheck(self, req: OrderRequest, current_position_quote: float | None = None) -> RiskDecision:
        price = req.price or 0.0
        notional = abs(req.amount) * price
        if req.amount <= 0:
            return RiskDecision(False, "下单数量必须大于 0。")
        if notional <= 0:
            return RiskDecision(False, "无法确定订单金额（缺少价格）。")
        if current_position_quote is None:
            current_position_quote = self._current_position_quote(req, price)
        decision = self.risk.check_market(req.market_type)
        if not decision:
            return decision
        return self.risk.check_order(
            quote_notional=notional,
            current_position_quote=current_position_quote,
            added_quote=notional,
            leverage=req.leverage,
            entry=not req.reduce_only,
        )

    def _current_position_quote(self, req: OrderRequest, price: float) -> float:
        """Best-effort current exposure used by the position limit.

        Paper mode is exact; live mode queries the exchange and falls back to 0
        (i.e. only the new order is checked) if the query fails. Coin-margined
        positions are reported in contracts, so they go through the contract
        spec to produce a USD notional.
        """
        if req.mode is RunMode.PAPER:
            held = self.paper.account.positions.get(req.symbol)
            return abs(held.contracts) * price if held and held.contracts else 0.0
        if not self.rest.has_credentials:
            return 0.0
        try:
            positions = self.rest.fetch_positions(req.market_type, [req.symbol])
        except Exception:
            return 0.0
        spec = self.rest.contract_spec(req.market_type, req.symbol)
        total = 0.0
        for position in positions:
            contracts = float(position.get("contracts") or 0)
            mark = float(position.get("markPrice") or price or 0)
            total += spec.notional(contracts, mark)
        return total

    # ------------------------------------------------------------------ submit
    def submit(self, req: OrderRequest) -> OrderResult:
        if req.mode is RunMode.BACKTEST:
            raise OrderRejected("回测模式不允许下单。")

        price = req.price or 0.0
        if req.order_type == OrderType.MARKET.value and price <= 0:
            raise OrderRejected("市价单需要最新成交价作为参考。")

        # Callers always express size in base coins; convert it to whatever the
        # exchange expects (coins for spot/linear, contracts for coin-M).
        # Paper orders are converted offline: a simulated fill must not depend
        # on the venue being reachable.
        exchange_amount, base_qty = self.exchange_amount(
            req.market_type,
            req.symbol,
            req.amount,
            price,
            offline=req.mode is RunMode.PAPER,
        )
        if exchange_amount <= 0:
            spec = self.rest.contract_spec(req.market_type, req.symbol)
            raise OrderRejected(
                f"数量精度处理后为 0，请增加下单数量。（{spec.describe()}）"
            )

        if (
            req.market_type is MarketType.SPOT
            and req.side == OrderSide.SELL.value
            and req.mode is RunMode.PAPER
        ):
            held = self.paper.account.positions.get(req.symbol)
            available = held.contracts if held else 0.0
            if base_qty > available + 1e-12:
                raise OrderRejected(
                    f"现货不能做空：当前持有 {format_amount(available)}，无法卖出 {format_amount(base_qty)}。"
                    "如需做空请切换到合约市场。"
                )

        decision = self.precheck(
            OrderRequest(
                symbol=req.symbol,
                side=req.side,
                order_type=req.order_type,
                amount=base_qty,
                price=price,
                reduce_only=req.reduce_only,
                leverage=req.leverage,
                market_type=req.market_type,
                mode=req.mode,
            )
        )
        if not decision:
            raise OrderRejected(decision.reason)

        if req.mode is RunMode.PAPER:
            order = self.paper.place_order(
                symbol=req.symbol,
                side=req.side,
                order_type=req.order_type,
                amount=base_qty,
                price=price,
                reduce_only=req.reduce_only,
                allow_short=req.market_type.is_futures,
            )
            quote = abs(base_qty) * price
            self.risk.record_order()
            self.db.save_order("paper", req.market_type.value, order, req.mode.value)
            log.info(
                "纸面下单 %s %s %s %s @ %s",
                req.symbol, req.side, format_amount(base_qty), req.order_type, price,
            )
            return OrderResult(order, req.mode, quote, base_qty, exchange_amount)

        if self.network is Network.MAINNET and not self.rest.has_credentials:
            raise OrderRejected("实盘下单需要先配置 API Key。")

        params: dict = {}
        if req.reduce_only and req.market_type.is_futures:
            params["reduceOnly"] = True
        if req.order_type == OrderType.MARKET.value:
            order = self.rest.create_order(
                req.market_type, req.symbol, "market", req.side, exchange_amount, None, params
            )
        else:
            order = self.rest.create_order(
                req.market_type,
                req.symbol,
                "limit",
                req.side,
                exchange_amount,
                self.rest.price_to_precision(req.market_type, req.symbol, price),
                params,
            )
        quote = abs(base_qty) * price
        self.risk.record_order()
        self.db.save_order(self._account_id(), req.market_type.value, order, req.mode.value)
        log.info(
            "实盘下单 %s %s %s %s @ %s -> %s",
            req.symbol, req.side, format_amount(base_qty), req.order_type, price,
            order.get("id"),
        )
        return OrderResult(order, req.mode, quote, base_qty, exchange_amount)

    # ------------------------------------------------------------------ cancel
    def cancel(self, market_type: MarketType, symbol: str, order_id: str, mode: RunMode) -> None:
        if mode is RunMode.PAPER:
            self.paper.cancel(order_id)
            return
        self.rest.cancel_order(market_type, order_id, symbol)

    # --------------------------------------------------------------- hierarchy
    def change_network(self, network: Network, credentials) -> None:
        self.network = network
        self.settings.network = network.value
        self.rest.network = network
        self.rest.credentials = credentials
        self.rest.reset()

    def _account_id(self) -> str:
        return f"binance:{self.network.value}"

    def new_client_order_id(self) -> str:
        return f"bt{uuid.uuid4().hex[:16]}"
