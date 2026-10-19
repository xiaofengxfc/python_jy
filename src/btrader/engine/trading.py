"""Order execution used by manual trading and the automated engine.

The service is the single place where an order can leave the application. It
applies risk checks, precision rounding and (in paper mode) the paper broker.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

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


def _is_filled(order: dict) -> bool:
    """Whether an order already has exposure to protect.

    ccxt reports ``status == "closed"`` for a filled order and also mirrors the
    raw ``filled``/``amount`` pair. Both are checked: some venues answer ACK
    with no status, and a filled market entry must still get its stop.
    """
    status = str(order.get("status") or "").lower()
    if status == "closed":
        return True
    if status in ("open", "new", "partially_filled", "canceled", "rejected", "expired"):
        return status == "partially_filled"
    filled = order.get("filled")
    amount = order.get("amount")
    try:
        return filled is not None and amount is not None and float(filled) > 0
    except (TypeError, ValueError):
        return False


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
    # Venue-side protective orders created alongside a live entry.
    protection: list[dict] = field(default_factory=list)
    # Non-fatal problems the caller must surface (a stop that did not arm).
    warnings: list[str] = field(default_factory=list)


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
        # Leverage already pushed to the venue, keyed by (market, symbol).
        # Setting it on every order would burn a signed request each time.
        self._leverage_applied: dict[tuple[MarketType, str], float] = {}

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
            resolved = self._current_position_quote(req, price)
            if resolved is None:
                # Reading the venue failed. Refuse to open, but never block a
                # reduce-only exit: being unable to close is the worse risk.
                if not req.reduce_only:
                    return RiskDecision(False, "无法读取当前持仓，为避免重复开仓已拒绝本次下单。")
                resolved = 0.0
            current_position_quote = resolved
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

        Paper mode is exact. Live mode reports the venue's total balance; when
        it is unavailable the percentage cap simply does not apply (the fixed
        ceiling still does).

        ccxt groups balances as ``{"total": {"USDT": 1234.5}}`` — the inner
        value, not the mapping, is the number. Reading ``balance["total"]``
        directly always yielded zero, so in live mode ``max_order_pct`` never
        took effect.
        """
        if req.mode is RunMode.PAPER:
            return self.paper.account.cash
        if not self.rest.has_credentials:
            return 0.0
        try:
            balance = self.rest.fetch_balance(req.market_type)
        except Exception:
            return 0.0
        # Coin-margined wallets hold the margin coin, so their total is marked
        # to USD before it can be compared with a quote-denominated cap.
        if req.market_type is MarketType.COINM:
            asset = "BTC"
            mark = price
        else:
            asset = "USDT"
            mark = 1.0
        totals = balance.get("total")
        value = totals.get(asset) if isinstance(totals, dict) else None
        if value is None:
            entry = balance.get(asset)  # older ccxt shapes
            value = entry.get("total") if isinstance(entry, dict) else entry
        try:
            return max(0.0, float(value or 0.0)) * mark
        except (TypeError, ValueError):
            return 0.0

    def _current_position_quote(self, req: OrderRequest, price: float) -> float | None:
        """Current exposure used by the position limit, or ``None`` if unknown.

        Paper mode is exact. Live mode queries the exchange; a failed query
        returns ``None`` rather than zero, because zero silently disables the
        position cap — the order would be checked as if nothing were held.
        Coin-margined positions are reported in contracts, so they go through
        the contract spec to produce a USD notional.
        """
        if req.mode is RunMode.PAPER:
            held = self.paper.account.positions.get(req.symbol)
            return abs(held.contracts) * price if held and held.contracts else 0.0
        if not self.rest.has_credentials:
            return 0.0
        try:
            positions = self.rest.fetch_positions(req.market_type, [req.symbol])
        except Exception:
            log.warning("读取实盘持仓失败，无法校验持仓上限", exc_info=True)
            return None
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

        if req.market_type.is_futures and req.leverage > 0 and not req.reduce_only:
            # The venue sizes a position with its own leverage, not the one in
            # our risk payload. Without this call the panel's choice was only
            # validated locally and the exchange kept its account default.
            # Exits skip it on purpose: being unable to close is worse than
            # closing at a stale leverage.
            self._ensure_leverage(req.market_type, req.symbol, req.leverage)

        params: dict = {}
        if req.reduce_only and req.market_type.is_futures:
            params["reduceOnly"] = True
            # A resting reduceOnly stop would be re-triggered by this exit and
            # flip the position instead of flattening it. Clearing the symbol
            # first is what makes "close" mean close.
            try:
                canceled = self.rest.cancel_symbol_orders(req.market_type, req.symbol)
                if canceled:
                    log.info("平仓前撤销 %s 的 %d 笔挂单", req.symbol, canceled)
            except ExchangeError as exc:
                log.warning("平仓前撤单失败 %s：%s", req.symbol, exc)
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

        protection: list[dict] = []
        warnings: list[str] = []
        if not req.reduce_only:
            protection, warnings = self._arm_live_protection(req, order, exchange_amount)
        log.info(
            "实盘下单 %s %s %s %s @ %s -> %s",
            req.symbol, req.side, format_amount(base_qty), req.order_type, price,
            order.get("id"),
        )
        return OrderResult(
            order, req.mode, quote, base_qty, exchange_amount, protection, warnings
        )

    def _arm_live_protection(
        self, req: OrderRequest, order: dict, exchange_amount: float
    ) -> tuple[list[dict], list[str]]:
        """Place venue-side stops for a live entry.

        Paper mode keeps its guards inside the app; a live position needs them
        at the exchange, otherwise closing the app leaves it naked. A failure
        here must not undo the entry that already filled, so each order is
        attempted separately and reported back as a warning.
        """
        if not (req.stop_loss or req.take_profit):
            return [], []
        if not req.market_type.is_futures:
            return [], [
                "现货暂不支持交易所侧止损/止盈，本单仅在合约市场自动挂保护单。"
            ]
        # A limit order that has not filled yet has no position to protect;
        # the exchange would reject a reduceOnly stop against zero exposure.
        if not _is_filled(order):
            return [], ["挂单尚未成交，成交后可再次设置止损/止盈。"]

        exit_side = "sell" if req.side == OrderSide.BUY.value else "buy"
        placed: list[dict] = []
        warnings: list[str] = []
        for price, take_profit, label in (
            (req.stop_loss, False, "止损"),
            (req.take_profit, True, "止盈"),
        ):
            if not price:
                continue
            try:
                if take_profit:
                    placed.append(
                        self.rest.create_take_profit_market(
                            req.market_type, req.symbol, exit_side, exchange_amount, price
                        )
                    )
                else:
                    placed.append(
                        self.rest.create_stop_market(
                            req.market_type, req.symbol, exit_side, exchange_amount, price
                        )
                    )
            except ExchangeError as exc:
                warnings.append(f"{label}单挂单失败：{exc}")
                log.warning("%s单挂单失败 %s：%s", label, req.symbol, exc)
        for item in placed:
            try:
                self.db.save_order(
                    self._account_id(), req.market_type.value, item, req.mode.value
                )
            except Exception:  # pragma: no cover - history is best effort
                log.warning("保护单写库失败", exc_info=True)
        return placed, warnings

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
    def _ensure_leverage(
        self, market_type: MarketType, symbol: str, leverage: float
    ) -> None:
        """Push the requested leverage to the venue, once per pair.

        The exchange sizes a position with its own leverage setting; the value
        in the order payload is only validated locally. Skipping this call left
        live orders running at the account default regardless of what the panel
        showed. A failure is fatal for an entry: trading at an unknown leverage
        is exactly the surprise this is meant to prevent.
        """
        key = (market_type, symbol)
        if abs(self._leverage_applied.get(key, 0.0) - leverage) < 1e-9:
            return
        try:
            self.rest.set_leverage(symbol, leverage, market_type)
        except ExchangeError as exc:
            raise OrderRejected(f"设置杠杆 {leverage:g}x 失败：{exc}") from exc
        self._leverage_applied[key] = leverage

    def change_network(self, network: Network, credentials) -> None:
        self.network = network
        self.settings.network = network.value
        self.rest.network = network
        self.rest.credentials = credentials
        self.rest.reset()
        # A new venue means the leverage stored there is unknown again.
        self._leverage_applied.clear()

    def _account_id(self) -> str:
        return f"binance:{self.network.value}"
