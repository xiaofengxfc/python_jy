"""Order execution used by manual trading and the automated engine.

The service is the single place where an order can leave the application. It
applies risk checks and precision rounding, then hands the order to the venue
configured by the current network (mainnet or testnet).
"""

from __future__ import annotations

import logging
import math
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from ..core.config import AppSettings
from ..core.credentials import account_id
from ..core.storage import Database
from ..core.types import (
    MarketType,
    Network,
    OrderSide,
    OrderType,
    RunMode,
    market_for_symbol,
)
from ..exchange.rest import DEFAULT_PLATFORM, BinanceREST, ExchangeError
from ..exchange.symbols import format_amount
from ..notify import Notifier
from .risk import RiskDecision, RiskManager

log = logging.getLogger(__name__)

# 交易所拒单的英文原文对用户没有指导意义，能认出来的换成一句能照做的中文。
HEDGE_MODE_HINT = (
    "交易所账户开着「双向持仓」（hedge mode），本程序只支持单向持仓："
    "请到币安合约的持仓模式里改成「单向持仓」再下单。"
)


def venue_rejection_message(exc: Exception) -> str:
    """把交易所的拒单翻成一句用户能照着做的中文。

    -4061 / "position side" 是双向持仓账户在不带 positionSide 时的拒绝，
    这条以前直接 500 抛出去，用户只能看到一段英文错误码。
    """
    text = str(exc)
    if "-4061" in text or "position side" in text.lower():
        return HEDGE_MODE_HINT
    return f"交易所拒绝下单：{text}"

# Live fills already folded into the risk manager, plus the per-symbol cost
# ledger derives closing PnL. It lives in SQLite so a restart does not
# charge the same exit twice.
LIVE_LEDGER_KEY = "live.ledger.v1"
LIVE_TRADE_LIMIT = 800


def ledger_key(network: Network) -> str:
    """成交账本的存储 key，按网络分开。

    主网与测试网的成交编号各成一套、仓位也各自存在。共用一份 books 时，切到测试网
    看到的是主网的持仓配上测试网的价格，未实现盈亏这类数字就这样凭空出现；
    seen 里又只有成交 id，现货与合约撞号的那一笔会被当成「已经记过」直接跳过。
    """
    return f"{LIVE_LEDGER_KEY}:{network.value}"


def _symbol_market(symbol: str) -> MarketType:
    """Which market one ccxt symbol belongs to: spot has no settle part.

    判定只有一份，在 core.types.market_for_symbol（交割合约的到期日先切掉再判）；
    这里以前自己又写了一份，和 web 那两份各说各话。
    """
    return market_for_symbol(symbol)


class OrderRejected(RuntimeError):
    pass


def _min_size_note(spec: Any) -> str:
    """最小下单量怎么说：单位 + （币本位才有）「1 张 = 多少 USD」。

    现货与 U 本位合约的 amount 就是币本身，没有「张」这回事。以前一律带上
    spec.describe()，现货拒单时说出「下单量 0.00000417 低于最小下单量 0.0001。
    （1 张 = 1 ETH）」——1 币被当成 1 张，用户看不出该下多少。币本位才是真按张
    成交的，那句话得留着：1 张 = 100 USD。
    """
    if spec.inverse:
        return f"{format_amount(spec.min_amount)} 张。（{spec.describe()}）"
    return f"{format_amount(spec.min_amount)} {spec.base or '币'}。"


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
        notifier: "Notifier | None" = None,
        rest_for: Callable[[MarketType], Any] | None = None,
    ) -> None:
        self.settings = settings
        self.rest = rest
        # Resolver for the client allowed to sign for a given market. The web
        # context hands over AppContext.rest_for; without it every market falls
        # back to self.rest (offline previews, unit tests).
        self._rest_for = rest_for
        # One order at a time, whoever submits it (INV-14). Every controller
        # ends up in submit(), so the lock lives here instead of in each caller.
        self.account_lock = threading.RLock()
        self.risk = risk
        self.db = db
        # Optional: pushes fills, protective exits and breaker trips to WeChat.
        # Always best-effort, never on the order path's critical section.
        self.notifier = notifier
        # Last breaker reason already pushed to WeChat (dedupe across exits).
        self._halt_announced = ""
        self.network = Network(settings.network)
        # 杠杆不再按交易对缓存「推过就不用推」：在币安 App 上手动改过杠杆之后，
        # 那张缓存会让程序继续按旧值开仓，而且只有重启才发现。多一次签名请求换
        # 「不会按别人的杠杆开仓」，这笔账明显划算（下单频率本来就不高）。
        # Pairs this process has actually sent an order for, per market. The fill
        # sync can only poll pairs it can name: spot has no position object, and
        # the settings pair usually points at a contract, so a freshly bought
        # spot pair was invisible and its fills never reached the local ledger.
        self._traded: dict[MarketType, set[str]] = {}

    # ------------------------------------------------------------------ sizing
    def amount_from_quote(self, market_type: MarketType, symbol: str, quote: float, price: float) -> float:
        if price <= 0:
            raise OrderRejected("价格无效，无法计算下单数量。")
        raw = quote / price
        return self.rest_for(market_type).amount_to_precision(market_type, symbol, raw)

    def rest_for(self, market_type: MarketType) -> Any:
        """The client that may sign for this market type.

        Credentials live in one key slot per margined type, so signing a spot
        order with the current futures client makes Binance answer -2015.
        Callers used to borrow the global market for the length of one request,
        which leaked a market switch into every concurrent settings reader. A
        per-market client keeps the global market untouched.
        """
        resolve = self._rest_for
        if resolve is None:
            return self.rest
        try:
            client = resolve(market_type)
        except Exception:  # a broken resolver must not lose the order path
            log.warning(
                "按市场取交易所客户端失败，退回当前客户端：%s",
                market_type.value,
                exc_info=True,
            )
            return self.rest
        return client if client is not None else self.rest

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
        rest = self.rest_for(market_type)
        # 账本里的数量是浮点累加出来的（0.00012 + 0.0003 = 0.00041999999999999996），
        # 直接交给交易所截位会掉一个最小步长（0.00041），全平之后账本上还剩下
        # 0.00001 的尘埃，低于最小下单额再也卖不掉。先收敛掉浮点噪声再截。
        base_qty = round(float(base_qty), 12)
        spec = rest.contract_spec(
            market_type, symbol, allow_network=not offline
        )
        raw = spec.base_to_contracts(base_qty, price)
        if raw <= 0:
            raise OrderRejected("数量无效，无法计算下单量。")
        if spec.min_amount > 0 and raw < spec.min_amount - 1e-12:
            raise OrderRejected(
                f"下单量 {format_amount(raw)} 低于最小下单量 {_min_size_note(spec)}"
            )
        if offline:
            # A preview never places anything: round exactly the way the venue
            # would (inverse contracts are whole numbers, never up).
            exchange_amount = math.floor(raw + 1e-12) if spec.inverse else raw
        else:
            exchange_amount = rest.amount_to_precision(market_type, symbol, raw)
        if exchange_amount <= 0:
            # 现货没有「张」这个东西，别把 describe() 那句塞进来。
            detail = f"（{spec.describe()}）" if spec.inverse else ""
            raise OrderRejected(
                f"下单量 {format_amount(raw)} 无法满足交易所的最小下单单位。{detail}"
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
        rest = self.rest_for(req.market_type)
        if not rest.has_credentials:
            return 0.0
        try:
            balance = rest.fetch_balance(req.market_type)
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
        rest = self.rest_for(req.market_type)
        if not rest.has_credentials:
            # No key means no position to protect: a fresh testnet account is
            # genuinely flat, and refusing every order would be useless.
            return 0.0
        if req.market_type is MarketType.SPOT:
            # 现货没有持仓端点，钱包里的币就是能卖的东西——但里面混着用户自己充提
            # 的币。仓位上限量的是「本程序买了多少」：数量取本地成交账本里这一对的
            # 持仓，再按钱包余额封顶（账本比钱包多说明币已经被提走，那部分不再是敞口）。
            # 以前直接拿钱包余额当持仓：用户自己有 1 BTC（约 8.1 万）而上限是 5000，
            # 于是任何现货买单都回「持仓金额将达 81225.76，超过上限 5000.00」，
            # 现货一个都买不进来。合约侧读的是交易所那份持仓，本来就是对的。
            booked = self._spot_booked_base(req.symbol)
            if booked is None:
                return None
            try:
                balance = rest.fetch_balance(req.market_type)
            except Exception:
                log.warning("读取现货余额失败，无法校验持仓上限", exc_info=True)
                return None
            totals = balance.get("total") if isinstance(balance, dict) else None
            if not isinstance(totals, dict):
                return None
            base = req.symbol.split("/")[0]
            try:
                wallet = max(0.0, float(totals.get(base) or 0.0))
            except (TypeError, ValueError):
                return None
            return min(booked, wallet) * price
        try:
            positions = rest.fetch_positions(req.market_type, [req.symbol])
        except Exception:
            log.warning("读取实盘持仓失败，无法校验持仓上限", exc_info=True)
            return None
        spec = rest.contract_spec(req.market_type, req.symbol)
        total = 0.0
        for position in positions:
            contracts = float(position.get("contracts") or 0)
            mark = float(position.get("markPrice") or price or 0)
            total += spec.notional(contracts, mark)
        return total

    def _spot_booked_base(self, symbol: str) -> float | None:
        """本程序在这一对上还持有多少（本地成交账本，正数=多头）。

        读不到账本返回 ``None`` 而不是 0：0 会让仓位上限看起来「什么都没持有」，
        等于把闸门关掉。没记过这一对是另一回事——那确实是我们一个币都没买。
        """
        try:
            books = self._live_ledger()["books"] or {}
            book = books.get(symbol) or {}
            return max(0.0, float(book.get("contracts") or 0.0))
        except Exception:
            log.warning("读取本地成交账本失败，无法校验现货持仓上限", exc_info=True)
            return None

    # ------------------------------------------------------------------ submit
    def submit(self, req: OrderRequest) -> OrderResult:
        """Place one order, one order at a time.

        The ticket, AutoTrader and the AI trader all reach the venue through
        here (INV-1), so this is the single place where a shared lock can
        serialise them. Only the order itself is inside it: model calls, kline
        downloads and strategy runs belong to the callers and stay outside,
        otherwise a slow model would freeze the ticket.
        """
        with self.account_lock:
            return self._submit_locked(req)

    def _submit_locked(self, req: OrderRequest) -> OrderResult:
        rest = self.rest_for(req.market_type)
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
            spec = rest.contract_spec(req.market_type, req.symbol)
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

        if not rest.has_credentials:
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
                canceled = rest.cancel_symbol_orders(req.market_type, req.symbol)
                if canceled:
                    log.info("平仓前撤销 %s 的 %d 笔挂单", req.symbol, canceled)
            except ExchangeError as exc:
                log.warning("平仓前撤单失败 %s：%s", req.symbol, exc)
        try:
            if req.order_type == OrderType.MARKET.value:
                order = rest.create_order(
                    req.market_type, req.symbol, "market", req.side, exchange_amount, None, params
                )
            else:
                order = rest.create_order(
                    req.market_type,
                    req.symbol,
                    "limit",
                    req.side,
                    exchange_amount,
                    rest.price_to_precision(req.market_type, req.symbol, price),
                    params,
                )
        except ExchangeError as exc:
            # 交易所的拒单以前原样抛到路由，变成 500 + 一段英文（"binanceusdm {"code":-4061..."）。
            # 用户看不出该改什么；换成 400 加一句能照做的中文。
            raise OrderRejected(venue_rejection_message(exc)) from exc
        # 记住这一对：成交同步（sync_live_fills）只能去问它能叫出名字的交易对，
        # 现货又没有任何持仓对象可读（见 _live_symbols）。
        self._traded.setdefault(req.market_type, set()).add(req.symbol)
        quote = abs(base_qty) * price
        self.risk.record_order()
        try:
            self.db.save_order(
                self._account_id(), req.market_type.value, order, RunMode.LIVE.value
            )
        except Exception:
            # The order is already at the venue. Reporting a history write
            # failure as a failed order invites a retry and a second position.
            log.warning("订单写库失败", exc_info=True)

        protection: list[dict] = []
        warnings: list[str] = []
        if not req.reduce_only:
            protection, warnings = self._arm_live_protection(req, order, exchange_amount)
            if warnings:
                self._notify(
                    Notifier.format_warning(
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

        rest = self.rest_for(req.market_type)
        exit_side = "sell" if req.side == OrderSide.BUY.value else "buy"
        # Size the guard by what actually filled. A stop larger than the
        # position is rejected by the venue as reduce-only, which left the
        # filled part unprotected while the entry itself looked successful.
        size = exchange_amount
        try:
            filled = float(order.get("filled") or 0.0)
        except (TypeError, ValueError):
            filled = 0.0
        if 0 < filled < exchange_amount:
            size = filled
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
                        rest.create_take_profit_market(
                            req.market_type, req.symbol, exit_side, size, price
                        )
                    )
                else:
                    placed.append(
                        rest.create_stop_market(
                            req.market_type, req.symbol, exit_side, size, price
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

    def save_external_order(self, market_type: MarketType, order: dict) -> None:
        """Record an order that was placed outside :meth:`submit`.

        Protective stops and standalone trigger orders are armed by the routes,
        not by an :class:`OrderRequest`, but the history table should still know
        about them — otherwise "全部委托" silently loses every guard.
        """
        try:
            self.db.save_order(
                self._account_id(), market_type.value, order, RunMode.LIVE.value
            )
        except Exception:  # pragma: no cover - history is best effort
            log.warning("外部挂单写库失败", exc_info=True)

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
        order = self.rest_for(market_type).cancel_order(market_type, order_id, symbol)
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
        text = Notifier.format_fill(
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
            Notifier.format_halt(reason=reason, day_pnl=state.day_pnl),
            kind="risk",
        )


    # -------------------------------------------------------- live accounting
    def _ledger_key(self) -> str:
        return ledger_key(self.network)

    def _live_ledger(self) -> dict:
        key = self._ledger_key()
        data = self.db.get_json(key)
        if not isinstance(data, dict):
            # 0.6.3 的账本只存一份、没有网络后缀。升级上来先搬一次：不搬的话老用户
            # 的现货仓位与成交记录会凭空消失（界面显示 0，平仓还报「账本里没有这一对」）。
            self.db.adopt_legacy_key(LIVE_LEDGER_KEY, key)
            data = self.db.get_json(key)
        if not isinstance(data, dict):
            data = {}
        return {
            "seen": [str(item) for item in data.get("seen", [])][-LIVE_TRADE_LIMIT:],
            "books": dict(data.get("books") or {}),
        }

    def _save_live_ledger(self, ledger: dict) -> None:
        try:
            self.db.set_json(
                self._ledger_key(),
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
        return self._sync_pairs(self._sync_targets(markets))

    def sync_symbol_fills(self, market: MarketType, symbol: str) -> list[dict]:
        """Pull one pair's fills right now, without waiting for the poller.

        成交同步是 5 秒一轮的：刚买完就点「平仓」，账本还是上一轮的数字——轻则少平
        一截，重则账本还空着，直接回「账本里没有这一对」。平仓本身已经要发签名请求，
        这里多一次不改变延迟量级，换来的是「平的就是账本上那个数」。
        """
        if not self.rest_for(market).has_credentials:
            return []
        return self._sync_pairs([(market, symbol)])

    def _sync_targets(self, markets: list[MarketType] | None) -> list[tuple[MarketType, str]]:
        """Every (market, symbol) the poller should ask the venue about."""
        pairs: list[tuple[MarketType, str]] = []
        for market in markets or list(MarketType):
            # 每个市场用自己的客户端：拿 U 本位的 Key 去问现货成交，币安回
            # -2015（那把 Key 没有现货权限），这里以前 catch 掉只留一行 debug，
            # 于是现货成交从来没进过账本，风控也收不到现货平仓的盈亏。
            client = self.rest_for(market)
            if not client.has_credentials:
                log.debug("实盘成交同步跳过 %s：未配置该市场的 API Key", market.value)
                continue
            try:
                symbols = self._live_symbols(market)
            except Exception as exc:
                log.debug("实盘成交同步跳过 %s：%s", market.value, exc)
                continue
            pairs.extend((market, symbol) for symbol in symbols)
        return pairs

    def _sync_pairs(self, pairs: list[tuple[MarketType, str]]) -> list[dict]:
        ledger = self._live_ledger()
        seen = set(ledger["seen"])
        books = ledger["books"]
        booked: list[dict] = []
        # One exchange order can come back as several fills (a partially
        # executed market order, or a stop that sweeps). Pushing each slice
        # turned one close into a burst of near-identical WeChat messages, so
        # the slices are announced once per order, with the merged PnL.
        announced: dict[str, dict] = {}
        for market, symbol in pairs:
            client = self.rest_for(market)
            try:
                trades = client.fetch_my_trades(market, symbol, limit=50)
            except ExchangeError as exc:
                log.debug("拉取成交失败 %s %s：%s", market.value, symbol, exc)
                continue
            except Exception:  # network hiccup: try again next round
                log.debug("拉取成交异常 %s %s", market.value, symbol, exc_info=True)
                continue
            for trade in sorted(trades, key=lambda item: item.get("timestamp") or 0):
                trade_id = str(trade.get("id") or "")
                # 去重键要带市场：现货与合约的成交编号各自从 1 数起，只按 id 记的话
                # 合约那一笔会被当成「现货那笔已经记过」，账本少成交，风控也收不到
                # 这次平仓的盈亏。
                seen_id = f"{market.value}:{trade_id}" if trade_id else ""
                if not seen_id or seen_id in seen:
                    continue
                seen.add(seen_id)
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
                    self.db.save_trade(
                        self._account_id(), trade, RunMode.LIVE.value, market.value
                    )
                except Exception:  # pragma: no cover - history is best effort
                    log.warning("实盘成交写库失败", exc_info=True)

        for summary in announced.values():
            self._notify(
                Notifier.format_fill(
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
        """Symbols worth polling: open orders, positions, the active pair, and
        everything this program has already traded in that market.

        最后一条是现货能不能平仓的关键：现货没有持仓对象，`settings.last_symbol`
        通常指向合约，一个刚买进来的现货对既不在挂单里也不在持仓里，于是
        `fetch_my_trades` 根本不会被问到那一对——本地成交账本永远是空的，不带 size
        的现货平仓只能回「账本里没有这一对」，风控也收不到现货平仓的已实现盈亏。
        账本里记着的对（跨重启）和本进程下过单的对（重启后第一笔之前）合起来才是
        「本程序自己的」交易对。
        """
        client = self.rest_for(market)
        symbols: set[str] = set()
        for name in self._ledger_symbols(market):
            symbols.add(name)
        symbols.update(self._traded.get(market, set()))
        try:
            for order in client.fetch_open_orders(market):
                symbol = order.get("symbol")
                if symbol:
                    symbols.add(str(symbol))
        except Exception:
            log.debug("读取实盘挂单失败 %s", market.value, exc_info=True)
        if market.is_futures:
            try:
                for position in client.fetch_positions(market):
                    contracts = abs(float(position.get("contracts") or 0.0))
                    if contracts > 0 and position.get("symbol"):
                        symbols.add(str(position["symbol"]))
            except Exception:
                log.debug("读取实盘持仓失败 %s", market.value, exc_info=True)
        if market is self.settings_market_type():
            symbols.add(self.settings.last_symbol)
        return sorted(symbols)

    def _ledger_symbols(self, market: MarketType) -> list[str]:
        """Pairs the local cost ledger still holds in this market."""
        try:
            books = self._live_ledger()["books"]
        except Exception:  # pragma: no cover - reading our own ledger should work
            log.debug("读取本地成交账本失败", exc_info=True)
            return []
        return [str(name) for name in books if _symbol_market(str(name)) is market]

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
        elif (old > 0) == (signed > 0):
            # A fill that adds to the leg re-averages its cost.
            books[symbol] = {
                "contracts": new,
                "entry": (entry * old + price * signed) / new,
            }
        else:
            # A fill that only reduces the leg leaves the cost of what is still
            # held alone. Re-averaging it with the exit price shrank the basis,
            # so the next exit reported a profit that never happened.
            books[symbol] = {"contracts": new, "entry": entry}

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
        """Push the requested leverage to the venue, on every entry order.

        The exchange sizes a position with its own leverage setting; the value
        in the order payload is only validated locally. Skipping this call left
        live orders running at the account default regardless of what the panel
        showed. A failure is fatal for an entry: trading at an unknown leverage
        is exactly the surprise this is meant to prevent.

        每次都推，不按交易对缓存：杠杆是账户级的、可以被币安 App 或网页改掉，
        缓存一命中就意味着「按别人设的杠杆开仓」，而且只有重启才恢复。
        """
        if not market_type.is_futures:
            # 现货没有杠杆这回事，也不该去问现货客户端要 setLeverage（它会回
            # 「不支持设置杠杆」，把「现货不挂交易所侧条件单」那句准确的话盖掉）。
            return
        # 交易所的杠杆是整数。ccxt 把 1.0 序列化成 "1.0"，币安回
        # -1102 "leverage ... malformed"，于是每张合约单都 400「设置杠杆 1x 失败」，
        # 面板上一张单都发不出去。整数值就按整数发，非整数直接说清楚。
        if abs(leverage - round(leverage)) > 1e-9:
            raise OrderRejected(
                f"合约杠杆只能是整数：现在是 {leverage:g}x，请改成整数再下单。"
            )
        venue_leverage = int(round(leverage))
        if venue_leverage < 1:
            raise OrderRejected("合约杠杆至少 1x。")
        try:
            self.rest_for(market_type).set_leverage(symbol, venue_leverage, market_type)
        except ExchangeError as exc:
            raise OrderRejected(f"设置杠杆 {venue_leverage}x 失败：{exc}") from exc

    def ensure_leverage(
        self, market_type: MarketType, symbol: str, leverage: float
    ) -> None:
        """把交易所侧这一对的杠杆对齐，给不走 submit 的路径用（条件单）。

        条件单直接调交易所的算法单接口，绕开了 _submit_locked，于是它以前是唯一
        一条完全没人推杠杆的下单路径：面板上写着 10 倍，挂上去的是账户当时的杠杆。
        规则与 submit 完全一致：整数、至少 1、失败即抛 OrderRejected。
        """
        self._ensure_leverage(market_type, symbol, leverage)

    def change_network(self, network: Network, credentials) -> None:
        self.network = network
        self.settings.network = network.value
        self.rest.network = network
        self.rest.credentials = credentials
        self.rest.reset()


    def _account_id(self) -> str:
        # 平台跟着设置走，和 AccountService 用同一个 account_id：写库与读历史
        # 各算一份时，界面上一条成交都读不出来，而且不报错。
        return account_id(self.network, self.settings.platform or DEFAULT_PLATFORM)
