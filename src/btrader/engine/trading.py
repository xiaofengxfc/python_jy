"""Order execution used by manual trading and the automated engine.

The service is the single place where an order can leave the application. It
applies risk checks and precision rounding, then hands the order to the venue
configured by the current network (mainnet or testnet).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

from ..core.config import AppSettings
from ..core.storage import Database
from ..core.types import MarketType, Network, OrderSide, OrderType, RunMode
from ..exchange.rest import BinanceREST, ExchangeError
from ..exchange.symbols import format_amount
from ..notify import WeChatNotifier
from .risk import RiskDecision, RiskManager

log = logging.getLogger(__name__)

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
    # Protective exits, handed to the venue as reduce-only stop orders.
    stop_loss: float | None = None
    take_profit: float | None = None


@dataclass
class OrderResult:
    order: dict
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
        db: Database,
        notifier: "WeChatNotifier | None" = None,
    ) -> None:
        self.settings = settings
        self.rest = rest
        self.risk = risk
        self.db = db
        # Optional: pushes fills, protective exits and breaker trips to WeChat.
        # Always best-effort, never on the order path's critical section.
        self.notifier = notifier
        # Last breaker reason already pushed to WeChat (dedupe across exits).
        self._halt_announced = ""
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
        USD), so the returned base quantity is the rounded value the risk
        checks should use. Spot and linear markets keep the base quantity
        itself as the exchange amount.

        ``offline=True`` is used by the order preview: it never touches the
        network, so the ticket still shows a size estimate while the venue is
        unreachable. Contract specs already fetched by the market page are
        reused from the cache.

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
            # A preview never places anything: round exactly the way the venue
            # would (inverse contracts are whole numbers, never up).
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

        The venue reports its total balance; when it is unavailable the
        percentage cap simply does not apply (the fixed ceiling still does).

        ccxt groups balances as ``{"total": {"USDT": 1234.5}}`` — the inner
        value, not the mapping, is the number. Reading ``balance["total"]``
        directly always yielded zero, so in live mode ``max_order_pct`` never
        took effect.
        """
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

        A failed query returns ``None`` rather than zero, because zero silently
        disables the position cap — the order would be checked as if nothing
        were held. Coin-margined positions are reported in contracts, so they go
        through the contract spec to produce a USD notional.
        """
        if not self.rest.has_credentials:
            # No key means no position to protect: a fresh testnet account is
            # genuinely flat, and refusing every order would be useless.
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
        price = req.price or 0.0
        if req.order_type == OrderType.MARKET.value and price <= 0:
            raise OrderRejected("市价单需要最新成交价作为参考。")

        # Callers always express size in base coins; convert it to whatever the
        # exchange expects (coins for spot/linear, contracts for coin-M).
        exchange_amount, base_qty = self.exchange_amount(
            req.market_type,
            req.symbol,
            req.amount,
            price,
        )
        if exchange_amount <= 0:
            spec = self.rest.contract_spec(req.market_type, req.symbol)
            raise OrderRejected(
                f"数量精度处理后为 0，请增加下单数量。（{spec.describe()}）"
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
            )
        )
        if not decision:
            raise OrderRejected(decision.reason)

        if not self.rest.has_credentials:
            raise OrderRejected(
                f"尚未配置 API Key：{self.network.value} 网络无法下单。"
            )

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
        self.db.save_order(self._account_id(), req.market_type.value, order, RunMode.LIVE.value)

        protection: list[dict] = []
        warnings: list[str] = []
        if not req.reduce_only:
            protection, warnings = self._arm_live_protection(req, order, exchange_amount)
            if warnings:
                self._notify(
                    WeChatNotifier.format_warning(
                        title=f"{req.symbol} 保护单未完全挂上",
                        detail="\n".join(warnings[:5]),
                    ),
                    kind="warning",
                )
        self.notify_fill(order, market=req.market_type.value)
        log.info(
            "实盘下单 %s %s %s %s @ %s -> %s",
            req.symbol, req.side, format_amount(base_qty), req.order_type, price,
            order.get("id"),
        )
        return OrderResult(
            order, quote, base_qty, exchange_amount, protection, warnings
        )

    def _arm_live_protection(
        self, req: OrderRequest, order: dict, exchange_amount: float
    ) -> tuple[list[dict], list[str]]:
        """Place venue-side stops for an entry.

        The guard must live at the exchange, otherwise closing the app leaves
        the position naked. A failure here must not undo the entry that already
        filled, so each order is attempted separately and reported back as a
        warning.
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
                    self._account_id(), req.market_type.value, item, RunMode.LIVE.value
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
        market: str | None = None,
    ) -> dict:
        """Cancel a resting order and return its final state.

        The cancel goes to the venue; the order row is written back to SQLite
        so the UI and the history agree on the state.
        """
        order = self.rest.cancel_order(market_type, order_id, symbol)
        try:
            self.db.save_order(
                self._account_id(), market or market_type.value, order, RunMode.LIVE.value
            )
        except Exception:  # a failed history write must not lose the cancel
            log.warning("撤单状态写库失败", exc_info=True)
        log.info("撤单 %s %s", symbol, order_id)
        return order

    # ------------------------------------------------------------ notifications
    def _notify(self, text: str, *, kind: str) -> None:
        """Hand a message to the WeChat notifier. Never raises.

        Notifications are additive: a missing OpenClaw install, a logged-out
        plugin or a full queue must not turn a filled order into an error.
        """
        notifier = self.notifier
        if notifier is None:
            return
        try:
            notifier.notify(text, kind=kind)
        except Exception:  # pragma: no cover - notification is best effort
            log.debug("微信通知入队失败", exc_info=True)

    def notify_fill(self, order: dict, *, market: str = "") -> None:
        """Push one executed order.

        The heading says whether the money was real: on the testnet the same
        fill is real code but fake funds, and announcing it as "实盘" would be
        a lie the reader cannot check.
        """
        if not order:
            return
        status = str(order.get("status") or "").lower()
        if status not in ("closed", "partially_filled"):
            return
        try:
            # ``filled`` is the traded size, ``amount`` the requested one; the
            # two differ on a partial fill. A triggered stop carries ``price`` 0
            # and only the crossed level in ``triggerPrice``.
            amount = float(order.get("filled") or order.get("amount") or 0.0)
            price = float(
                order.get("average")
                or order.get("price")
                or order.get("triggerPrice")
                or 0.0
            )
        except (TypeError, ValueError):
            return
        if amount <= 0 or price <= 0:
            return
        # A fee on an entry is a cost, but announcing "已实现 -3.99" on a plain
        # buy reads as a loss on a trade that has not closed anything yet: only
        # report a closed round.
        closed = float(order.get("closedQty") or 0.0)
        realized = order.get("realized") if closed > 1e-12 else None
        text = WeChatNotifier.format_fill(
            symbol=str(order.get("symbol") or ""),
            side=str(order.get("side") or ""),
            amount=amount,
            price=price,
            mode="testnet" if self.network is Network.TESTNET else "live",
            realized=float(realized) if realized is not None else None,
            market=market or str(order.get("market") or ""),
        )
        self._notify(text, kind="fill")

    def notify_risk_halt(self) -> None:
        """Push a breaker trip, if one is active and has not been announced yet.

        Called after every recorded exit; re-sending the same reason on each one
        would turn a breaker into a phone spammer. The last announced reason is
        remembered, and cleared once the breaker is.

        Both breaker shapes are covered. A consecutive-loss trip lives in
        ``halted_reason``; a daily-loss or daily-trade cap has no reason field
        and only shows up as a refused entry, so it is derived from the state
        the same way :meth:`RiskManager.precheck` derives it.
        """
        notifier = self.notifier
        if notifier is None or not notifier.settings.enabled:
            return
        state = self.risk.state
        risk = self.risk.settings
        reason = state.halted_reason or ""
        if not reason:
            if risk.max_daily_loss and state.day_pnl <= -abs(risk.max_daily_loss):
                reason = f"已达当日最大亏损（{risk.max_daily_loss:.2f} USDT）"
            elif risk.max_daily_trades and state.day_trades >= risk.max_daily_trades:
                reason = f"已达当日最大交易次数（{risk.max_daily_trades} 笔）"
        if not reason:
            self._halt_announced = ""
            return
        if reason == self._halt_announced:
            return
        self._halt_announced = reason
        self._notify(
            WeChatNotifier.format_halt(reason=reason, day_pnl=state.day_pnl),
            kind="risk",
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

        An exit comes back from the exchange long after the order was
        submitted, and a stop-loss fired by the venue is never seen by the
        submitting process at all. Without this the daily-loss and
        losing-streak breakers would stay silent — configurable, but unable to
        ever fire.

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
        # One exchange order can come back as several fills (a partially
        # executed market order, or a stop that sweeps). Pushing each slice
        # turned one close into a burst of near-identical WeChat messages, so
        # the slices are announced once per order, with the merged PnL.
        announced: dict[str, dict] = {}

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
                        self._merge_exit(announced, trade, result, market.value)
                    try:
                        self.db.save_trade(self._account_id(), trade, RunMode.LIVE.value)
                    except Exception:  # pragma: no cover - history is best effort
                        log.warning("实盘成交写库失败", exc_info=True)

        for summary in announced.values():
            self._notify(
                WeChatNotifier.format_fill(
                    symbol=summary["symbol"],
                    side=summary["side"],
                    amount=summary["amount"],
                    price=summary["price"],
                    mode="testnet" if self.network is Network.TESTNET else "live",
                    realized=summary["realized"],
                    market=summary["market"],
                ),
                kind="fill",
            )
        if booked or seen != set(ledger["seen"]):
            ledger["seen"] = list(seen)[-LIVE_TRADE_LIMIT:]
            ledger["books"] = books
            self._save_live_ledger(ledger)
        return booked

    def _merge_exit(self, announced: dict, trade: dict, result: dict, market: str) -> None:
        """Fold one closing slice into the message that will be sent for its order.

        Keyed by exchange order id so the slices of one order become a single
        message. Price is share-weighted, which is the only meaningful average
        once several fills at different prices have been merged.
        """
        key = str(
            trade.get("orderId")
            or trade.get("order")
            or (trade.get("info") or {}).get("orderId")
            or trade.get("id")
            or ""
        )
        qty = float(result.get("closedQty") or 0.0)
        price = float(result.get("price") or trade.get("price") or 0.0)
        if not key or qty <= 0 or price <= 0:
            return
        entry = announced.get(key)
        if entry is None:
            announced[key] = {
                "symbol": str(result.get("symbol") or trade.get("symbol") or ""),
                "side": str(trade.get("side") or ""),
                "amount": qty,
                "price": price,
                "realized": float(result.get("realized") or 0.0),
                "market": market,
            }
            return
        merged = entry["amount"] + qty
        entry["price"] = (entry["price"] * entry["amount"] + price * qty) / merged
        entry["amount"] = merged
        entry["realized"] = float(entry["realized"]) + float(result.get("realized") or 0.0)

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
