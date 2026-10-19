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

# settings_kv key holding the serialized simulated account.
PAPER_STATE_KEY = "paper.account.v1"

# Live fills already folded into the risk manager, plus the per-symbol cost
# ledger used to derive closing PnL. Kept in SQLite so a restart does not
# charge the same exit twice.
LIVE_LEDGER_KEY = "live.ledger.v1"
LIVE_TRADE_LIMIT = 800


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
    # Protective exits, only meaningful for paper entries.
    stop_loss: float | None = None
    take_profit: float | None = None


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
            equity=self._equity(req, price),
        )

    def _equity(self, req: OrderRequest, price: float) -> float:
        """Account equity used by the percentage-based per-order cap.

        Paper mode is exact. Live mode reports the quote balance; when it is
        unavailable the percentage cap simply does not apply (the fixed
        ceiling still does).
        """
        if req.mode is RunMode.PAPER:
            return self.paper.account.cash
        if not self.rest.has_credentials:
            return 0.0
        try:
            balance = self.rest.fetch_balance(req.market_type)
        except Exception:
            return 0.0
        for key in ("USDT", "total"):
            value = balance.get(key)
            if isinstance(value, dict):
                value = value.get("free") or value.get("total")
            try:
                number = float(value or 0.0)
            except (TypeError, ValueError):
                continue
            if number > 0:
                return number
        return 0.0

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
                market=req.market_type.value,
                stop_loss=req.stop_loss,
                take_profit=req.take_profit,
            )
            quote = abs(base_qty) * price
            self.risk.record_order()
            # A market order (and a marketable exit in particular) closes
            # immediately, so its realized PnL never reaches the background
            # reconciler. Recording it here is what lets the daily-loss and
            # consecutive-loss breakers see exits at all.
            self._record_fill(order)
            self.db.save_order("paper", req.market_type.value, order, req.mode.value)
            self.save_paper_state()
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
    def cancel(
        self,
        market_type: MarketType,
        symbol: str,
        order_id: str,
        mode: RunMode,
        market: str | None = None,
    ) -> dict:
        """Cancel a resting order and return its final state.

        Paper orders are cancelled locally; live orders go to the exchange.
        The order row is written back to SQLite either way so the UI and the
        history agree on the state.
        """
        if mode is RunMode.PAPER:
            order = self.paper.cancel(order_id)
            if order is None:
                raise OrderRejected("未找到可撤销的委托（可能已成交或已撤销）。")
            self.db.save_order(
                "paper",
                market or market_type.value,
                order,
                mode.value,
            )
            self.save_paper_state()
            log.info("纸面撤单 %s %s", symbol, order_id)
            return order
        order = self.rest.cancel_order(market_type, order_id, symbol)
        try:
            self.db.save_order(
                self._account_id(), market or market_type.value, order, mode.value
            )
        except Exception:  # a failed history write must not lose the cancel
            log.warning("撤单状态写库失败", exc_info=True)
        log.info("实盘撤单 %s %s", symbol, order_id)
        return order

    # ------------------------------------------------------- paper execution
    def save_paper_state(self) -> None:
        """Persist the simulated account (cash, positions, resting orders, SL/TP).

        Best effort: a failed write must never roll back a fill that already
        happened in memory.
        """
        try:
            self.db.set_json(PAPER_STATE_KEY, self.paper.snapshot())
        except Exception:  # pragma: no cover - storage is best effort
            log.warning("保存纸面账户失败", exc_info=True)

    def _record_fill(self, order: dict) -> None:
        """Feed a paper fill's realized PnL into the risk manager.

        ``record_order`` already counted the order itself; this is the other
        half — the daily-loss and consecutive-loss circuit breakers only ever
        see exits, which is why they used to be dead code.
        """
        if not order or order.get("status") != "closed":
            return
        closed = float(order.get("closedQty") or 0.0)
        if closed <= 0:
            return
        realized = float(order.get("realized") or 0.0)
        self.risk.record_trade(realized)
        log.info(
            "纸面平仓 %s %s %s 已实现 %.4f",
            order.get("symbol"),
            order.get("side"),
            format_amount(closed),
            realized,
        )

    # -------------------------------------------------------- live accounting
    def _live_ledger(self) -> dict:
        data = self.db.get_json(LIVE_LEDGER_KEY)
        if not isinstance(data, dict):
            data = {}
        return {
            "seen": [str(item) for item in data.get("seen", [])][-LIVE_TRADE_LIMIT:],
            "books": dict(data.get("books") or {}),
        }

    def _save_live_ledger(self, ledger: dict) -> None:
        try:
            self.db.set_json(
                LIVE_LEDGER_KEY,
                {
                    "seen": ledger["seen"][-LIVE_TRADE_LIMIT:],
                    "books": ledger["books"],
                },
            )
        except Exception:  # pragma: no cover - storage is best effort
            log.warning("保存实盘成交账本失败", exc_info=True)

    def sync_live_fills(self, markets: list[MarketType] | None = None) -> list[dict]:
        """Pull recent live fills and feed closing PnL into the risk manager.

        Paper fills are accounted where they happen, but a live exit comes
        back from the exchange long after the order was submitted. Without
        this the daily-loss and losing-streak breakers stayed silent in live
        mode — they looked configurable but could never fire.

        Spot trades carry no realized PnL, so a weighted-average cost book per
        symbol is kept here: the first fill that reduces a position turns the
        difference against the average entry into a realized result. Contracts
        that already report ``realizedPnl`` use that figure instead of guessing.

        Everything is persisted and de-duplicated by trade id, so repeated
        polling (and restarts) never charge the same exit twice.
        """
        if not self.rest.has_credentials:
            return []
        ledger = self._live_ledger()
        seen = set(ledger["seen"])
        books = ledger["books"]
        booked: list[dict] = []

        for market in markets or list(MarketType):
            try:
                symbols = self._live_symbols(market)
            except Exception as exc:
                log.debug("实盘成交同步跳过 %s：%s", market.value, exc)
                continue
            for symbol in symbols:
                try:
                    trades = self.rest.fetch_my_trades(market, symbol, limit=50)
                except ExchangeError as exc:
                    log.debug("拉取成交失败 %s %s：%s", market.value, symbol, exc)
                    continue
                except Exception:  # network hiccup: try again next round
                    log.debug("拉取成交异常 %s %s", market.value, symbol, exc_info=True)
                    continue
                for trade in sorted(trades, key=lambda item: item.get("timestamp") or 0):
                    trade_id = str(trade.get("id") or "")
                    if not trade_id or trade_id in seen:
                        continue
                    seen.add(trade_id)
                    result = self._book_live_trade(trade, books)
                    if result is not None:
                        booked.append(result)
                        self.risk.record_trade(result["realized"])
                        log.info(
                            "实盘平仓 %s %s 已实现 %.4f",
                            symbol,
                            trade.get("side"),
                            result["realized"],
                        )
                    try:
                        self.db.save_trade(self._account_id(), trade, RunMode.LIVE.value)
                    except Exception:  # pragma: no cover - history is best effort
                        log.warning("实盘成交写库失败", exc_info=True)

        if booked or seen != set(ledger["seen"]):
            ledger["seen"] = list(seen)[-LIVE_TRADE_LIMIT:]
            ledger["books"] = books
            self._save_live_ledger(ledger)
        return booked

    def _live_symbols(self, market: MarketType) -> list[str]:
        """Symbols worth polling: open orders, positions and the active pair."""
        symbols: set[str] = set()
        try:
            for order in self.rest.fetch_open_orders(market):
                symbol = order.get("symbol")
                if symbol:
                    symbols.add(str(symbol))
        except Exception:
            log.debug("读取实盘挂单失败 %s", market.value, exc_info=True)
        if market.is_futures:
            try:
                for position in self.rest.fetch_positions(market):
                    contracts = abs(float(position.get("contracts") or 0.0))
                    if contracts > 0 and position.get("symbol"):
                        symbols.add(str(position["symbol"]))
            except Exception:
                log.debug("读取实盘持仓失败 %s", market.value, exc_info=True)
        if market is self.settings_market_type():
            symbols.add(self.settings.last_symbol)
        return sorted(symbols)

    def settings_market_type(self) -> MarketType:
        try:
            return MarketType(self.settings.market_type)
        except ValueError:
            return MarketType.SPOT

    @staticmethod
    def _book_live_trade(trade: dict, books: dict) -> dict | None:
        """Fold one fill into the symbol's cost book.

        Returns a summary when the fill closed (or reduced) a position, else
        ``None``. Leaving a book at exactly zero drops it, so a flat symbol
        starts clean the next time it is traded.
        """
        symbol = str(trade.get("symbol") or "")
        try:
            amount = abs(float(trade.get("amount") or 0.0))
            price = float(trade.get("price") or 0.0)
        except (TypeError, ValueError):
            return None
        if not symbol or amount <= 0 or price <= 0:
            return None
        side = str(trade.get("side") or "").lower()
        if side not in ("buy", "sell"):
            return None

        fee_info = trade.get("fee")
        if isinstance(fee_info, dict):
            fee = abs(float(fee_info.get("cost") or 0.0))
        else:
            fee = abs(float(trade.get("fee") or 0.0))

        book = books.get(symbol) or {"contracts": 0.0, "entry": 0.0}
        old = float(book.get("contracts") or 0.0)
        entry = float(book.get("entry") or 0.0)
        signed = amount if side == "buy" else -amount
        new = old + signed

        realized = 0.0
        closed = 0.0
        if old != 0 and (old > 0) != (signed > 0):
            closed = min(abs(signed), abs(old))
            direction = 1.0 if old > 0 else -1.0
            reported = trade.get("realizedPnl")
            if reported is None and isinstance(trade.get("info"), dict):
                reported = trade["info"].get("realizedPnl")
            try:
                realized = float(reported) - fee if reported is not None else (
                    (price - entry) * closed * direction - fee
                )
            except (TypeError, ValueError):
                realized = (price - entry) * closed * direction - fee

        if abs(new) < 1e-12:
            books.pop(symbol, None)
        elif old == 0 or (old > 0) != (new > 0):
            books[symbol] = {"contracts": new, "entry": price}
        else:
            books[symbol] = {
                "contracts": new,
                "entry": (entry * old + price * signed) / new,
            }

        if closed <= 0:
            return None
        return {
            "symbol": symbol,
            "side": side,
            "closedQty": closed,
            "price": price,
            "realized": realized,
        }

    def settle_paper_orders(self, symbol: str, price: float) -> list[dict]:
        """Fill paper limit orders against a fresh last price.

        Called by the backend's background reconciler. Returns the orders that
        filled so the caller can persist them.
        """
        filled = self.paper.mark(symbol, price)
        for order in filled:
            self._record_fill(order)
            try:
                self.db.save_order(
                    "paper", order.get("market") or "spot", order, RunMode.PAPER.value
                )
            except Exception:  # pragma: no cover - history is best effort
                log.warning("纸面成交写库失败", exc_info=True)
        if filled:
            self.save_paper_state()
        return filled

    def check_protections(self, symbol: str, price: float) -> list[dict]:
        """Trigger stop-loss / take-profit exits for one symbol."""
        orders = self.paper.check_protections(symbol, price)
        for order in orders:
            self._record_fill(order)
            try:
                self.db.save_order(
                    "paper", order.get("market") or "spot", order, RunMode.PAPER.value
                )
            except Exception:  # pragma: no cover - history is best effort
                log.warning("保护单写库失败", exc_info=True)
        if orders:
            self.save_paper_state()
        return orders

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
