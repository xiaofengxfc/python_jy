"""FastAPI backend for the local web UI.

The desktop app and this server share one engine: everything under
``core`` / ``exchange`` / ``engine`` is Qt-free, so this module is a thin HTTP
adapter over :class:`btrader.context.AppContext`.

The server binds to localhost only. It is started by the Electron shell as a
child process (see ``btrader.web.__main__``) and also runs standalone through
``run_web.py`` for frontend development.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..context import AppContext
from ..core.config import ConfigError
from ..core.types import (
    TIMEFRAME_SECONDS,
    MarketType,
    Network,
    market_for_symbol,
)
from ..engine.trading import OrderRejected, OrderRequest
from ..exchange.symbols import format_amount, load_symbols
from ..exchange import mirror
from ..exchange.rest import ExchangeError, ticker_bid_ask
from ..logging_setup import error_buffer
from .routes import build_router
from .routes_lab import build_lab_router
from .routes_strategy import build_strategy_router
from .stream import StreamHub

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
FONT_DIR = Path(__file__).resolve().parents[1] / "assets" / "fonts"

# Shown when the exchange cannot be reached, so the UI still has something to
# render while offline.
_FALLBACK_SYMBOLS: dict[str, list[str]] = {
    "spot": ["BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT", "XRP/USDT"],
    "usdm": ["BTC/USDT:USDT", "ETH/USDT:USDT", "BNB/USDT:USDT", "SOL/USDT:USDT"],
    "coinm": ["BTC/USD:BTC", "ETH/USD:ETH"],
}

MARKET_LABELS = {
    "spot": "现货",
    "usdm": "U 本位合约",
    "coinm": "币本位合约",
}

# Estimate-only numbers for the order ticket. The venue charges its own
# commission at fill time; these exist so the ticket can show "约" figures
# before anything is sent, not to predict the actual fee.
TAKER_FEE_RATE = 0.0004
SLIPPAGE_BPS = 1.0


# --------------------------------------------------------------------- state
class _State:
    """Process-wide services plus the locks that guard ccxt clients."""

    def __init__(self) -> None:
        self.ctx = AppContext()
        # ccxt exchanges are not thread safe; one lock per market keeps a slow
        # kline download from corrupting a concurrent ticker call.
        self.locks = {market: threading.RLock() for market in MarketType}
        self.loading = {market: threading.Lock() for market in MarketType}
        self.symbols: dict[str, list[dict[str, Any]]] = {}
        # The same lock the trading service takes around a submit, so a web
        # route and an engine poll can never place an order at the same time.
        self.account_lock = self.ctx.trading.account_lock
        self.equity_history: list[dict[str, Any]] = []
        self.equity_stamp = 0.0
        self.stream: StreamHub | None = None
        self.stop_reconcile = threading.Event()


_state: _State | None = None
_state_lock = threading.Lock()


def get_state() -> _State:
    global _state
    if _state is None:
        with _state_lock:
            if _state is None:
                _state = _State()
    return _state


def _market(value: str) -> MarketType:
    try:
        return MarketType(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"未知市场：{value}") from exc


def _mirror_candles(
    market: MarketType,
    symbol: str,
    timeframe: str,
    limit: int,
    since: int | None,
    primary: Exception,
) -> list[list[float]] | None:
    """备用合约行情（Gate 永续），取不到就返回 ``None``。

    Only futures: spot already has a public Binance mirror, so a second source
    there would add disagreement without adding reachability.
    """
    if not mirror.supports(market, timeframe):
        return None
    log.warning("币安行情不可用，改用备用源：%s", primary)
    try:
        rows = mirror.fetch_ohlcv(market, symbol, timeframe, since=since, limit=limit)
    except Exception as exc:
        log.warning("备用行情也不可用：%s", exc)
        # Both failures are worth reporting: the first says why the primary is
        # down, the second says the fallback could not rescue it either. 但状态码
        # 由原始错误定：交易对根本不存在时，两个源都查不到，这是 400，不是 503。
        raise _venue_http_error(
            primary, _market_data_hint(market, f"{primary}。备用源：{exc}"), symbol
        ) from exc
    if not rows:
        return None
    return rows


def _market_data_hint(market: MarketType, detail: str) -> str:
    """Name the usual cause of a contract-data failure instead of echoing ccxt."""
    text = str(detail)
    looks_like_network = any(
        key in text.lower()
        for key in ("timeout", "timed out", "connection", "network", "ssl", "proxy", "exchangeinfo")
    )
    if market.is_futures and looks_like_network:
        return (
            "无法连接币安合约行情接口（fapi.binance.com / dapi.binance.com），"
            "常见原因是 DNS 污染或网络限制。可以这样处理。一是在设置里填代理。"
            "二是让合约图表改用 Gate.io 永续行情，那边也失败时才会显示这条错误。"
            f"原始错误：{text[:300]}"
        )
    return text


# ---------------------------------------------------------------- serialising
def _symbol_payload(info: Any) -> dict[str, Any]:
    return {
        "symbol": info.symbol,
        "display": info.display,
        "base": info.base,
        "quote": info.quote,
        "price_precision": info.price_precision,
        "amount_precision": info.amount_precision,
        "min_amount": info.min_amount,
        "min_cost": info.min_cost,
        "is_inverse": info.is_inverse,
    }


def _fallback_items(market: MarketType, error: str) -> list[dict[str, Any]]:
    items = []
    for symbol in _FALLBACK_SYMBOLS[market.value]:
        base, _, tail = symbol.partition("/")
        quote = tail.split(":")[0]
        items.append(
            {
                "symbol": symbol,
                "display": symbol,
                "base": base,
                "quote": quote,
                "price_precision": 2,
                "amount_precision": 6,
                "min_amount": 0.0,
                "min_cost": 0.0,
                "is_inverse": market is MarketType.COINM,
                "degraded": True,
                "error": error,
            }
        )
    return items


def _symbols_for(state: _State, market: MarketType) -> tuple[list[dict[str, Any]], str | None]:
    """Return cached symbols, loading them once per market.

    A failed load is not cached: the next request retries the exchange and the
    caller gets a small fallback list in the meantime.
    """
    key = market.value
    cached = state.symbols.get(key)
    if cached is not None:
        return cached, None

    with state.loading[market]:
        cached = state.symbols.get(key)
        if cached is not None:
            return cached, None
        try:
            with state.locks[market]:
                infos = load_symbols(state.ctx.market_rest_for(market), market)
            items = [_symbol_payload(info) for info in infos]
            if not items:
                raise RuntimeError("交易所返回了空的交易对列表")
            state.symbols[key] = items
            return items, None
        except Exception as exc:  # network, geo block, ccxt error …
            message = str(exc) or exc.__class__.__name__
            log.warning("加载 %s 交易对失败: %s", key, message)
            return _fallback_items(market, message), message


# -------------------------------------------------------------------- payload
class OrderPayload(BaseModel):
    """下单请求。

    数量、价格这些字段不在模型上判 gt=0：pydantic 拦下来返回的是 422 加一句英文
    「Input should be greater than 0」，界面没有中文可显示。交给路由用中文说明
    拒绝，状态码统一 400，和其余失败场景一致。
    """

    market: str = "spot"
    symbol: str
    side: str
    order_type: str = "limit"
    size_mode: str = "amount"
    size: float
    price: float | None = None
    # Protective exits for a new entry (absolute prices). Omit to trade bare.
    stop_loss: float | None = None
    take_profit: float | None = None
    # Mainnet requires an explicit acknowledgement; testnet ignores it.
    confirm_live: bool = False


class PreviewPayload(BaseModel):
    market: str = "spot"
    symbol: str
    side: str = "buy"
    order_type: str = "limit"
    size_mode: str = "amount"
    size: float
    price: float | None = None


class ConditionalPayload(BaseModel):
    """A standalone trigger order: nothing is sent until the price touches.

    ``direction`` is the ticket's own wording (below = 跌破, above = 涨破).
    It is optional because the trigger compared with the mark price already
    says the same thing, but when it is supplied it must agree — a ticket
    that says 跌破 while the trigger sits above the market is a typo, and
    arming it would fire on the next tick.
    """

    market: str = "spot"
    symbol: str
    side: str
    order_type: str = "market"
    size_mode: str = "amount"
    size: float
    trigger: float
    price: float | None = None
    direction: str | None = None
    reduce_only: bool = False
    confirm_live: bool = False


class ProtectionPayload(BaseModel):
    """Arm / replace the exchange-side guard of a position that already exists.

    Both levels are optional (setting only a stop is normal), ``market`` is
    inferred from the symbol when omitted.
    """

    symbol: str
    market: str | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    confirm_live: bool = False

class ClosePayload(BaseModel):
    """Close (or reduce) a position with a market order.

    ``size`` is in base coins. Omitted means the whole position on futures; on
    spot it means the size booked in the local fill ledger, and an unbooked pair
    is refused (400) instead of selling the coin balance — that balance also
    holds what the user deposited, so "the whole position" is not a thing spot
    can infer. ``market`` is optional: when a caller omits it the venue is
    inferred from the ccxt symbol, because defaulting to spot made a futures
    position look absent ("当前没有该交易对的持仓") and the exit impossible.
    """

    symbol: str
    market: str | None = None
    size: float | None = None
    confirm_live: bool = False


def _market_for_symbol(symbol: str) -> MarketType:
    """Infer the market from a ccxt symbol (shared with the pricing path)."""
    return market_for_symbol(symbol)


def _balance_free(balance: Any, asset: str) -> float:
    """Free balance of one asset from a ccxt balance payload."""
    if not isinstance(balance, dict):
        return 0.0
    entry = balance.get(asset.upper())
    if isinstance(entry, dict):
        for key in ("free", "total"):
            try:
                value = float(entry.get(key) or 0.0)
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
        return 0.0
    free = balance.get("free")
    if isinstance(free, dict):
        try:
            return float(free.get(asset.upper()) or 0.0)
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def _live_spot_available(state: _State, symbol: str, market: MarketType) -> float:
    """Free base balance on the exchange, for capping a live spot sell.

    A failed read returns 0: the caller then reports "no position", which is
    the safe outcome — it never lets an oversell through.
    """
    base = symbol.split("/")[0].upper()
    try:
        with state.locks[market]:
            balance = state.ctx.rest_for(market).fetch_balance(market)
    except Exception:
        log.warning("读取实盘余额失败 %s", symbol, exc_info=True)
        return 0.0
    return _balance_free(balance, base)


def _require_live_ready(
    *,
    signed: bool,
    confirm_live: bool,
    network: Network,
    market: MarketType | None = None,
    stop_loss: float | None = None,
    take_profit: float | None = None,
) -> None:
    """Gate an order behind credentials and, on mainnet, a second confirm.

    Testnet is a real venue with fake funds, so it needs no extra click.
    Mainnet keeps the acknowledgement because there a typo costs money.

    A futures order may carry a stop / take-profit: they are placed as
    STOP_MARKET / TAKE_PROFIT_MARKET orders. A spot order may not. Binance has
    no reduce-only stop for spot balances, and arming nothing while reporting
    success would leave the position unprotected.
    """
    if not signed:
        raise HTTPException(status_code=400, detail="下单需要先配置该市场的 API Key。")
    if network is Network.MAINNET and not confirm_live:
        raise HTTPException(
            status_code=400,
            detail="主网下单需要二次确认（confirm_live=true）。测试网不需要。",
        )
    if (stop_loss or take_profit) and market is not None and not market.is_futures:
        raise HTTPException(
            status_code=400,
            detail="现货暂无交易所侧止损/止盈，请切换到合约市场或手动盯盘。",
        )


def _ledger_entry_price(state: _State, market: MarketType, symbol: str) -> float:
    """Average entry of the open leg for one pair, 0.0 when unknown.

    The venue reports size, not cost basis, so this comes from the app's own
    fill ledger. It is only used as a fallback reference when validating a
    protective level against a market that has already run past it.
    """
    try:
        snapshot = state.ctx.account.snapshot(market, symbol, refresh=True)
    except Exception:
        log.debug("读取持仓成本失败 %s", symbol, exc_info=True)
        return 0.0
    for position in getattr(snapshot, "positions", []) or []:
        if position.get("symbol") == symbol:
            try:
                return float(position.get("entry_price") or 0.0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def _booked_held(state: _State, symbol: str) -> float | None:
    """The app's own record of the open leg on ``symbol``, signed; None if none.

    ``sync_live_fills`` folds every live fill into a cost book per symbol, so the
    process knows what it is holding even at the moment the venue will not answer.
    Used only to answer "is this order an exit?": being wrong there costs a
    reduce-only order the venue rejects, never an unproven entry that slips
    through.
    """
    try:
        books = state.ctx.trading._live_ledger()["books"]
    except Exception:  # pragma: no cover - reading our own ledger should work
        log.debug("读取本地成交账本失败 %s", symbol, exc_info=True)
        return None
    book = books.get(symbol)
    if not isinstance(book, dict):
        return None
    try:
        return float(book.get("contracts") or 0.0)
    except (TypeError, ValueError):  # pragma: no cover - ledger writes numbers
        return None


def _venue_symbol(state: _State, market: MarketType, symbol: str) -> str:
    """The symbol the venue actually lists, from either spelling a caller uses.

    界面传的是持仓行上的 ccxt 符号（``ETH/USDT:USDT``），手敲接口的人传的是
    「币/计价」（``ETH/USDT``）。合约市场里这一对叫 ``ETH/USDT:USDT``：照原样
    拿去读持仓只会回「当前没有该交易对的持仓」，明明有仓。所以这里把两种写法
    都收进来，统一成交易所认的那一个。

    现货本来就是 ``BASE/QUOTE``，原样返回——不能给现货也补一个结算币出来。
    合约缺结算币时按市场的结算规则补：U 本位用计价币（USDT / USDC），币本位
    用基础币（``BTC/USD`` -> ``BTC/USD:BTC``）。补错了交易所会自己回错，路由
    已经把它翻成 400 加原因，不会变成「没有持仓」这种误导。
    """
    text = (symbol or "").strip()
    if not text or not market.is_futures or ":" in text:
        return text
    base, _, tail = text.partition("/")
    quote = tail.split(":")[0].upper()
    if market is MarketType.COINM:
        return f"{text}:{base.upper()}"
    settle = quote if quote in ("USDT", "USDC") else "USDT"
    return f"{text}:{settle}"


def _live_held(state: _State, symbol: str, market: MarketType) -> float | None:
    """Signed live position size: positive long, negative short.

    Spot has no position object, so what is held is the base balance. Futures
    report contracts with a side; the sign is what this function owes its
    callers, because the close route picks the opposite side from it.

    ``None`` means "nobody knows": the venue read failed and the app has no
    booked leg either. It is deliberately not 0.0 — a failed read used to come
    back as "flat", so a sell of a held position was judged as an entry and
    refused by the entry breakers, leaving the user locked in during a halt.
    """
    with state.locks[market]:
        rest = state.ctx.rest_for(market)
        if market.is_futures:
            try:
                positions = rest.fetch_positions(market, [symbol])
            except Exception:
                log.warning("读取实盘持仓失败 %s，改用本地成交账本", symbol, exc_info=True)
                return _booked_held(state, symbol)
            for item in positions:
                if item.get("symbol") != symbol:
                    continue
                contracts = float(item.get("contracts") or 0.0)
                if item.get("side") == "short":
                    contracts = -abs(contracts)
                return contracts
            return 0.0
        return _live_spot_available(state, symbol, market)


def _is_reducing(
    state: _State,
    market: MarketType,
    symbol: str,
    side: str,
    amount: float,
) -> bool:
    """Whether an order closes (part of) an existing position.

    A reduce order must not be judged by the entry limits (per-order notional,
    position cap) or by the entry breakers: selling 0.01 BTC is an exit, and
    refusing it with "单笔金额超过上限" — or with "风控熔断中" — is what made a
    held position impossible to sell.

    The position is read from the venue, because that is where the real
    exposure is; when that read fails, the app's own fill book answers instead
    (see :func:`_live_held`). Spot sells are always exits: spot cannot short, and
    the unheld-sell case is rejected earlier with its own message.
    """
    if market is MarketType.SPOT:
        held = _live_spot_available(state, symbol, market)
        return side == "sell" and held > 0 and amount <= held * (1 + 1e-6) + 1e-12
    held = _live_held(state, symbol, market)
    if held is None:
        # 持仓读不到、本地账本也没有：这笔单子证明不了是减仓。当作开仓处理，
        # 让 precheck 用「无法读取当前持仓」明确拒掉，别拿交易所的 reduceOnly
        # 去赌——赌输的代价是先把该交易对的挂单撤了。
        return False
    if side == "sell" and held > 0:
        return amount <= held * (1 + 1e-6) + 1e-12
    if side == "buy" and held < 0:
        return amount <= -held * (1 + 1e-6) + 1e-12
    return False


def _check_protection_levels(
    reference: float,
    stop_loss: float | None,
    take_profit: float | None,
    side: str = "buy",
) -> None:
    """Reject a stop/target that is on the wrong side of the entry price.

    A long exits below with the stop and above with the target; a short is the
    mirror image. A typo (or a swapped pair) would otherwise fire on the very
    next tick and close the position immediately.
    """
    if side == "sell":
        if stop_loss and stop_loss <= reference:
            raise HTTPException(
                status_code=400,
                detail=f"空单止损价 {stop_loss:g} 必须高于开仓价 {reference:g}",
            )
        if take_profit and take_profit >= reference:
            raise HTTPException(
                status_code=400,
                detail=f"空单止盈价 {take_profit:g} 必须低于开仓价 {reference:g}",
            )
        return
    if stop_loss and stop_loss >= reference:
        raise HTTPException(
            status_code=400,
            detail=f"止损价 {stop_loss:g} 必须低于开仓价 {reference:g}",
        )
    if take_profit and take_profit <= reference:
        raise HTTPException(
            status_code=400,
            detail=f"止盈价 {take_profit:g} 必须高于开仓价 {reference:g}",
        )


# ----------------------------------------------------------------------- app
_SYMBOL_MISSING_MARKERS: tuple[str, ...] = (
    "does not have market symbol",
    "invalid symbol",
    "market symbol not found",
    "invalid market",
)

def _unknown_symbol(exc: BaseException) -> bool:
    """True when the venue says this pair does not exist.

    ccxt raises BadSymbol for a typo'd pair, but the exchange layer flattens
    every failure into ExchangeError, so by the time a route sees it only the
    message is left. It matters: a 503 tells the caller to try again later,
    and this answer will never change until the symbol does.
    """
    text = str(exc).lower()
    return any(marker in text for marker in _SYMBOL_MISSING_MARKERS)


def _venue_http_error(
    exc: BaseException, prefix: str, symbol: str = ""
) -> HTTPException:
    """Map a venue failure onto the status the caller can act on."""
    if _unknown_symbol(exc):
        detail = f"没有这个交易对：{symbol}" if symbol else "这条市场没有这个交易对"
        return HTTPException(status_code=400, detail=detail)
    return HTTPException(status_code=503, detail=f"{prefix}：{exc}")


def _cancel_http_error(order_id: str, exc: BaseException) -> HTTPException:
    """A cancel that cannot match an order is a 400/404, never a 503."""
    if not str(order_id).strip().isdigit():
        return HTTPException(
            status_code=400,
            detail=f"订单号不合法：{order_id}。币安的订单号是纯数字。",
        )
    text = str(exc).lower()
    if "unknown order" in text or "-2011" in text:
        return HTTPException(status_code=404, detail=f"没有这个挂单：{order_id}")
    return _venue_http_error(exc, "撤单失败")

def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        state = get_state()
        state.stream = StreamHub(state.ctx.network)
        # Warm the symbol list in the background: the first paint must not wait
        # on a multi-second load_markets() call.
        threading.Thread(target=_warm, daemon=True).start()

        state.stop_reconcile.clear()
        threading.Thread(
            target=_reconcile_live_fills, args=(state,), name="live-reconciler", daemon=True
        ).start()
        try:
            yield
        finally:
            state.stop_reconcile.set()
            if state.stream is not None:
                await state.stream.close()
            try:
                state.ctx.shutdown()
            except Exception:  # pragma: no cover - best effort on exit
                log.debug("关闭上下文失败", exc_info=True)

    app = FastAPI(title="biTrader Web", lifespan=lifespan)

    @app.exception_handler(ConfigError)
    async def _config_error(_request: Request, exc: ConfigError) -> JSONResponse:
        """配置文件写不进去时给 409：这是用户的文件出了问题，不是服务端崩了。"""
        return JSONResponse(status_code=409, content={"detail": str(exc)})
    app.include_router(build_router(get_state))
    # Candidate review and multi-round parameter reflection.
    app.include_router(build_lab_router(get_state))
    # Source editor for user strategy plugins.
    app.include_router(build_strategy_router(get_state))

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    if FONT_DIR.is_dir():
        app.mount("/fonts", StaticFiles(directory=FONT_DIR), name="fonts")

    # ------------------------------------------------------------------ pages
    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(
            STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"}
        )

    # ------------------------------------------------------------------- meta
    @app.get("/api/health")
    def health() -> dict[str, Any]:
        state = get_state()
        return {
            "ok": True,
            "market": state.ctx.market_type.value,
            "network": state.ctx.network.value,
            "has_credentials": state.ctx.rest.has_credentials,
            "markets": [
                {"value": m.value, "label": MARKET_LABELS[m.value]}
                for m in MarketType
            ],
        }

    @app.get("/api/symbols")
    def api_symbols(market: str = "spot") -> dict[str, Any]:
        state = get_state()
        items, error = _symbols_for(state, _market(market))
        return {"items": items, "degraded": error is not None, "error": error}

    @app.get("/api/klines")
    def api_klines(
        market: str = "spot",
        symbol: str = "BTC/USDT",
        timeframe: str = "15m",
        limit: int = 400,
        before: int | None = None,
    ) -> dict[str, Any]:
        state = get_state()
        mt = _market(market)
        if timeframe not in TIMEFRAME_SECONDS:
            raise HTTPException(status_code=400, detail=f"不支持的周期：{timeframe}")
        limit = max(20, min(int(limit), 1000))
        step = TIMEFRAME_SECONDS[timeframe] * 1000
        since = max(0, int(before) - (limit + 1) * step) if before is not None else None
        source = "Binance"
        try:
            with state.locks[mt]:
                frame = state.ctx.klines.fetch(
                    mt, symbol, timeframe, limit=limit + 1 if before is not None else limit,
                    since=since,
                )
            if before is not None:
                # Scrolling past the left edge asks for the window that ends at
                # the oldest candle the chart already holds. One extra step
                # covers the boundary bar itself.
                frame = frame[frame["timestamp"] < int(before)].tail(limit)
        except Exception as exc:
            # 币安合约域名在部分网络会被 DNS 污染，界面就只剩「无法查看合约 K 线」。
            # 现货有公共镜像，合约没有；此时改用 Gate 永续行情，并把来源一起返回，
            # 让界面能标明「这不是币安的价格」。
            rows = _mirror_candles(mt, symbol, timeframe, limit, since, exc)
            if rows is None:
                # 不存在的交易对不是「服务暂时不可用」：备用源也查不到就是 400。
                raise _venue_http_error(
                    exc, _market_data_hint(mt, exc), symbol
                ) from exc
            return {
                "symbol": symbol,
                "timeframe": timeframe,
                "candles": [
                    {"t": t, "o": o, "h": h, "l": l, "c": c, "v": v}
                    for t, o, h, l, c, v in rows
                ],
                "source": mirror.source_label(),
                "degraded": True,
            }
        candles = [
            {
                "t": int(row.timestamp),
                "o": float(row.open),
                "h": float(row.high),
                "l": float(row.low),
                "c": float(row.close),
                "v": float(row.volume),
            }
            for row in frame.itertuples(index=False)
        ]
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "candles": candles,
            "source": source,
            "degraded": False,
        }

    @app.get("/api/ticker")
    def api_ticker(market: str = "spot", symbol: str = "BTC/USDT") -> dict[str, Any]:
        state = get_state()
        mt = _market(market)
        try:
            with state.locks[mt]:
                rest = state.ctx.market_rest_for(mt)
                raw = rest.fetch_ticker(mt, symbol)
                bid, ask = ticker_bid_ask(raw)
                if not bid or not ask:
                    # 币安合约的 24 小时 ticker 里没有买一卖一，去 bookTicker 补一档。
                    # 补不上就留 0：前端据此显示「—」，不要编一个价出来。
                    try:
                        bid, ask = ticker_bid_ask(
                            raw, *rest.fetch_bid_ask(mt, symbol)
                        )
                    except Exception as exc:
                        log.warning("读取买卖一失败 %s：%s", symbol, exc)
        except Exception as exc:
            raise _venue_http_error(exc, "无法获取行情", symbol) from exc
        return {
            "symbol": symbol,
            "last": float(raw.get("last") or 0.0),
            "bid": bid,
            "ask": ask,
            "high": float(raw.get("high") or 0.0),
            "low": float(raw.get("low") or 0.0),
            "base_volume": float(raw.get("baseVolume") or 0.0),
            "quote_volume": float(raw.get("quoteVolume") or 0.0),
            "change_pct": float(raw.get("percentage") or 0.0),
            "timestamp": int(raw.get("timestamp") or 0),
        }

    @app.get("/api/orderbook")
    def api_orderbook(
        market: str = "spot", symbol: str = "BTC/USDT", limit: int = 12
    ) -> dict[str, Any]:
        state = get_state()
        mt = _market(market)
        limit = max(5, min(int(limit), 50))
        try:
            with state.locks[mt]:
                book = state.ctx.market_rest_for(mt).fetch_order_book(mt, symbol, limit)
        except Exception as exc:
            raise _venue_http_error(exc, "无法获取盘口", symbol) from exc
        return {
            "bids": [[float(p), float(q)] for p, q in (book.get("bids") or [])[:limit]],
            "asks": [[float(p), float(q)] for p, q in (book.get("asks") or [])[:limit]],
        }

    # --------------------------------------------------------------- account
    @app.get("/api/account")
    def api_account(market: str = "spot", symbol: str | None = None) -> dict[str, Any]:
        """Balances, positions, open orders and the fill ledger for one market.

        Everything here comes from the venue. There is no simulated account any
        more, so the trading panel, the portfolio page and the AI panel all read
        this one snapshot — the numbers cannot dissagree between pages because
        there is only one place they come from.

        The service caches the snapshot for a few seconds: the panels poll, and
        signing balance + positions + open orders per poll would burn the
        request weight budget for nothing.

        A missing key is not an error: the page still renders, and "尚未配置
        API Key" is a message, not a blank screen.
        """
        state = get_state()
        snapshot = state.ctx.account.snapshot(_market(market), symbol)
        return snapshot.as_payload()

    @app.post("/api/orders/preview")
    def api_preview_order(payload: PreviewPayload) -> dict[str, Any]:
        """Dry-run an order: sizing, fees and the risk verdict, nothing sent.

        Sizing is rounded offline so the panel still works without network
        access to the contract spec endpoint; a market order falls back to the
        last trade price as its reference.
        """
        if payload.size <= 0:
            raise HTTPException(status_code=400, detail="下单数量必须大于 0")
        if payload.price is not None and payload.price <= 0:
            raise HTTPException(status_code=400, detail="下单价格必须大于 0")
        state = get_state()
        mt = _market(payload.market)
        reference = payload.price if payload.order_type == "limit" else None
        if reference is None:
            # A preview is an estimate for the ticket; quoting it from the
            # charting feed keeps it consistent with the chart beside it. The
            # real order re-reads the price from its own venue.
            quote = state.ctx.market_rest_for(mt)
            try:
                with state.locks[mt]:
                    reference = float(
                        quote.fetch_ticker(mt, payload.symbol).get("last") or 0.0
                    )
            except Exception as exc:
                raise _venue_http_error(
                    exc, "无法获取最新价", payload.symbol
                ) from exc
        if not reference or reference <= 0:
            raise HTTPException(status_code=400, detail="缺少有效的参考价格")

        amount = (
            payload.size / reference if payload.size_mode == "quote" else payload.size
        )
        if amount <= 0:
            raise HTTPException(status_code=400, detail="下单数量必须大于 0")

        trading = state.ctx.trading
        notional = abs(amount) * reference
        base = {
            "symbol": payload.symbol,
            "market": mt.value,
            "side": payload.side,
            "order_type": payload.order_type,
            "price": reference,
            "amount_base": amount,
            "quote_notional": notional,
            "fee_rate": TAKER_FEE_RATE,
            "slippage_bps": SLIPPAGE_BPS,
        }

        try:
            exchange_amount, base_qty = trading.exchange_amount(
                mt, payload.symbol, amount, reference, offline=True
            )
        except OrderRejected as exc:
            return {
                **base,
                "allowed": False,
                "reason": str(exc),
                "exchange_amount": 0.0,
                "fee_estimate": 0.0,
            }
        except Exception as exc:
            return {
                **base,
                "allowed": False,
                "reason": f"无法校验下单量：{exc}",
                "exchange_amount": 0.0,
                "fee_estimate": 0.0,
            }

        decision = trading.precheck(
            OrderRequest(
                symbol=payload.symbol,
                side=payload.side,
                order_type=payload.order_type,
                amount=base_qty,
                price=reference,
                reduce_only=_is_reducing(
                    state, mt, payload.symbol, payload.side, base_qty
                ),
                market_type=mt,
            )
        )
        return {
            **base,
            "allowed": bool(decision),
            "reason": decision.reason,
            "amount_base": base_qty,
            "exchange_amount": exchange_amount,
            "quote_notional": abs(base_qty) * reference,
            "fee_estimate": abs(base_qty) * reference * TAKER_FEE_RATE,
        }

    @app.post("/api/orders")
    def api_place_order(payload: OrderPayload) -> dict[str, Any]:
        if payload.side not in ("buy", "sell"):
            raise HTTPException(status_code=400, detail="方向必须是 buy 或 sell")
        if payload.order_type not in ("limit", "market"):
            raise HTTPException(status_code=400, detail="订单类型必须是 limit 或 market")
        if payload.size_mode not in ("amount", "quote"):
            raise HTTPException(status_code=400, detail="数量模式必须是 amount 或 quote")
        # 这几条以前靠 pydantic 的 gt=0 拦，返回的是 422 加英文；界面上没有中文可显示，
        # 而且限价单不填价格时会被下面「按现价取参考价」的逻辑悄悄改成现价挂单。
        if payload.size <= 0:
            raise HTTPException(status_code=400, detail="下单数量必须大于 0")
        if payload.order_type == "limit" and payload.price is None:
            raise HTTPException(
                status_code=400,
                detail="限价单必须填写价格：不填会按现价挂出去，可能立刻成交。",
            )
        if payload.price is not None and payload.price <= 0:
            raise HTTPException(status_code=400, detail="下单价格必须大于 0")
        if (payload.stop_loss is not None and payload.stop_loss <= 0) or (
            payload.take_profit is not None and payload.take_profit <= 0
        ):
            raise HTTPException(status_code=400, detail="止损 / 止盈价格必须大于 0")

        state = get_state()
        mt = _market(payload.market)

        # Every order is real now, so the gates are always on: the account must
        # be able to sign, and on mainnet the caller must acknowledge that this
        # leaves the building. Testnet asks for no ceremony — rehearsing should
        # not require extra clicks.
        with state.locks[mt]:
            signed = state.ctx.rest_for(mt).has_credentials
        _require_live_ready(
            signed=signed,
            confirm_live=payload.confirm_live,
            network=state.ctx.network,
            market=mt,
            stop_loss=payload.stop_loss,
            take_profit=payload.take_profit,
        )

        # An order is always priced by the venue that will execute it; quoting
        # it from the charting feed would send a testnet order with a
        # mainnet-only price.
        quote = state.ctx.quote_rest(mt)
        reference = payload.price if payload.order_type == "limit" else None
        if reference is None:
            try:
                with state.locks[mt]:
                    reference = float(
                        quote.fetch_ticker(mt, payload.symbol).get("last") or 0.0
                    )
            except Exception as exc:
                raise _venue_http_error(
                    exc, "无法获取最新价", payload.symbol
                ) from exc
        if not reference or reference <= 0:
            raise HTTPException(status_code=400, detail="缺少有效的参考价格")

        amount = (
            payload.size / reference if payload.size_mode == "quote" else payload.size
        )
        if amount <= 0:
            raise HTTPException(status_code=400, detail="下单数量必须大于 0")

        # Spot sells are capped by the coins actually held. "100%" is budgeted
        # from a price that ticks between render and submit, so the quantity
        # derived from it can exceed the balance by a fraction of a percent;
        # clamp that instead of rejecting what is clearly a full exit.
        adjusted = False
        if mt is MarketType.SPOT and payload.side == "sell":
            available = _live_spot_available(state, payload.symbol, mt)
            if available <= 0:
                raise HTTPException(
                    status_code=400,
                    detail=f"当前没有 {payload.symbol} 持仓，现货无法做空。",
                )
            if amount > available:
                if amount <= available * 1.005:
                    amount = available
                    adjusted = True
                else:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"现货不能做空：当前持有 {available:.8f}，"
                            f"无法卖出 {amount:.8f}。"
                        ),
                    )

        request = OrderRequest(
            symbol=payload.symbol,
            side=payload.side,
            order_type=payload.order_type,
            amount=amount,
            price=reference,
            # A sell of coins actually held is an exit. Marking it here is
            # what lets a halted or emergency-stopped account still flatten:
            # the entry breakers are skipped for reduce-only orders.
            reduce_only=_is_reducing(
                state, mt, payload.symbol, payload.side, amount
            ),
            market_type=mt,
        )
        if payload.stop_loss or payload.take_profit:
            # Spot is long-only: a sell there is an exit, never a new short.
            # Futures may open a short with a sell, and its stop/target sit on
            # the opposite side of the entry.
            if payload.side == "sell" and not mt.is_futures:
                raise HTTPException(
                    status_code=400,
                    detail="现货卖出是平仓，止损/止盈请在开仓时设置。",
                )
            _check_protection_levels(
                reference, payload.stop_loss, payload.take_profit, payload.side
            )
            request.stop_loss = payload.stop_loss
            request.take_profit = payload.take_profit

        # 订单落在哪个市场由 OrderRequest.market_type 决定，签名的 Key 也由
        # TradingService 按同一个市场去取，全局市场全程不动：以前这里要先把
        # ctx.market_type 借成请求的市场，并发读设置会看到另一个市场。
        # 提交段自己在 account_lock 里串行，跨市场两张单子不会互相盖。
        try:
            result = state.ctx.trading.submit(request)
        except OrderRejected as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            log.exception("下单失败")
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        # 以前借全局市场时会顺手 invalidate 掉账户快照，成交之后面板立刻是新的。
        # 借市场没了，这一步要显式做，否则刚下的单在缓存里最多还是 10 秒前的账。
        state.ctx.account.invalidate()

        return {
            "ok": True,
            "order": result.order,
            "quote_notional": result.quote_notional,
            "amount_base": result.amount_base,
            "amount_exchange": result.amount_exchange,
            "network": state.ctx.network.value,
            "adjusted": adjusted,
            "protection": result.protection,
            "warnings": result.warnings,
        }

    # ---------------------------------------------------------------- cancel
    @app.delete("/api/orders/{order_id}")
    def api_cancel_order(
        order_id: str,
        symbol: str,
        market: str | None = None,
    ) -> dict[str, Any]:
        """Cancel a resting order.

        The response carries the final order state so the UI can update the row
        without a refetch.
        """
        if not symbol:
            raise HTTPException(status_code=400, detail="缺少交易对")
        if not str(order_id).strip().isdigit():
            # 币安的订单号是纯数字。带着 "nope" 去签名，交易所只会回 -1102，
            # 那串东西再往上抛就是 503，用户以为交易所挂了。
            raise HTTPException(
                status_code=400,
                detail=f"订单号不合法：{order_id}。币安的订单号是纯数字。",
            )
        state = get_state()
        mt = _market(market) if market else _market_for_symbol(symbol)
        symbol = _venue_symbol(state, mt, symbol)
        try:
            with state.account_lock:
                order = state.ctx.trading.cancel(mt, symbol, order_id, market=mt.value)
        except OrderRejected as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise _cancel_http_error(order_id, exc) from exc
        return {"ok": True, "order": order}

    # ---------------------------------------------------------------- closing
    @app.post("/api/positions/close")
    def api_close_position(payload: ClosePayload) -> dict[str, Any]:
        """Market-close a position (optionally a partial size).

        Kept separate from /api/orders so the caller does not have to know the
        signed size or the direction: the server reads the position from the
        venue and sends the opposite side with reduceOnly.

        Spot is sized from the local fill ledger, never from the coin balance:
        the wallet holds what the user deposited too, and one size-less spot
        close used to sell all of it.
        """
        # An omitted market is inferred from the symbol: a futures pair carries
        # its settle currency ("BTC/USDT:USDT"), spot does not. Defaulting to
        # spot here made every futures exit fail as "没有该交易对的持仓".
        mt = _market(payload.market) if payload.market else _market_for_symbol(payload.symbol)
        if payload.size is not None and payload.size <= 0:
            raise HTTPException(status_code=400, detail="平仓数量必须大于 0")
        state = get_state()

        with state.locks[mt]:
            signed = state.ctx.rest_for(mt).has_credentials
        _require_live_ready(
            signed=signed,
            confirm_live=payload.confirm_live,
            network=state.ctx.network,
        )
        # 两种写法都收：内部一律用交易所认的那个符号，报错文案也跟着它走。
        symbol = _venue_symbol(state, mt, payload.symbol)
        held = _live_held(state, symbol, mt)
        if held is None:
            raise HTTPException(
                status_code=503,
                detail="无法读取当前持仓，暂时无法平仓，请稍后再试。",
            )
        if abs(held) <= 1e-12:
            raise HTTPException(status_code=400, detail="当前没有该交易对的持仓")
        if payload.size is not None:
            size = min(abs(payload.size), abs(held))
        elif mt.is_futures:
            # 合约的「持仓」就是这台机器在该市场上的敞口，全平就是全平。
            size = abs(held)
        else:
            # 现货的「持仓」是币余额，里面混着用户自己充进来的钱：不带 size 时只平
            # 本程序成交账本记着的数量。以前这里拿钱包余额当全部持仓，一次不带
            # size 的平仓把钱包里本来就有的币一起卖了（t33 实测：1 BTC 全没了）。
            booked = _booked_held(state, symbol)
            if booked is None or abs(booked) <= 1e-12:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"现货平仓只平本程序记录的数量：本地成交账本里没有 {symbol}"
                        f" 的持仓记录，请显式指定要卖的 size（钱包可用 {abs(held):g}）。"
                    ),
                )
            size = min(abs(booked), abs(held))
        try:
            quote = state.ctx.quote_rest(mt)
            with state.locks[mt]:
                reference = float(
                    quote.fetch_ticker(mt, symbol).get("last") or 0.0
                )
        except Exception as exc:
            raise _venue_http_error(exc, "无法获取最新价", symbol) from exc
        if reference <= 0:
            raise HTTPException(status_code=400, detail="无法获取有效的参考价格")

        side = "sell" if held > 0 else "buy"
        request = OrderRequest(
            symbol=symbol,
            side=side,
            order_type="market",
            amount=size,
            price=reference,
            reduce_only=True,
            market_type=mt,
        )
        try:
            with state.account_lock:
                result = state.ctx.trading.submit(request)
        except OrderRejected as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            log.exception("平仓失败")
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {
            "ok": True,
            "order": result.order,
            "price": reference,
            "size": size,
            "side": side,
        }

    # ------------------------------------------------- conditional orders
    def _reference_price(state: _State, mt: MarketType, symbol: str) -> float:
        """Mark price from the venue that will execute, never from the chart."""
        try:
            with state.locks[mt]:
                value = float(
                    state.ctx.quote_rest(mt).fetch_ticker(mt, symbol).get("last") or 0.0
                )
        except Exception as exc:
            raise _venue_http_error(exc, "无法获取最新价", symbol) from exc
        if value <= 0:
            raise HTTPException(status_code=400, detail="无法获取有效的参考价格")
        return value

    @app.post("/api/orders/conditional")
    def api_place_conditional(payload: ConditionalPayload) -> dict[str, Any]:
        """Arm a trigger order at the exchange (跌破 / 涨破 才真正下单).

        Spot has no venue-side conditional order, so it is refused with a
        message that says what to do instead. Turning it into a plain market
        order would fill immediately at a price the user never agreed to.

        Binance splits these by direction: for a BUY, a trigger above the mark
        is a stop (breakout) and below it a take-profit (dip entry); a SELL is
        the mirror image. The route picks the type from that rule, so the
        caller only has to send side + trigger.
        """
        if payload.side not in ("buy", "sell"):
            raise HTTPException(status_code=400, detail="方向必须是 buy 或 sell")
        if payload.size_mode not in ("amount", "quote"):
            raise HTTPException(status_code=400, detail="数量模式必须是 amount 或 quote")
        if payload.direction not in (None, "below", "above"):
            raise HTTPException(status_code=400, detail="触发方向必须是 below 或 above")
        if payload.size <= 0:
            raise HTTPException(status_code=400, detail="下单数量必须大于 0")
        if payload.trigger <= 0:
            raise HTTPException(status_code=400, detail="触发价必须大于 0")
        if payload.price is not None and payload.price <= 0:
            raise HTTPException(status_code=400, detail="委托价必须大于 0")

        mt = _market(payload.market)
        if not mt.is_futures:
            raise HTTPException(
                status_code=400,
                detail="现货没有交易所侧条件单：请切到合约市场，或用币安自带的条件单。",
            )
        state = get_state()
        with state.locks[mt]:
            signed = state.ctx.rest_for(mt).has_credentials
        _require_live_ready(
            signed=signed,
            confirm_live=payload.confirm_live,
            network=state.ctx.network,
        )

        symbol = _venue_symbol(state, mt, payload.symbol)
        reference = _reference_price(state, mt, symbol)
        above = payload.trigger > reference
        if payload.direction and (payload.direction == "above") != above:
            asked = "涨破" if payload.direction == "above" else "跌破"
            where = "上方" if above else "下方"
            raise HTTPException(
                status_code=400,
                detail=f"触发方向对不上：触发价 {payload.trigger} 在现价 {reference} 的{where}，但选择的是{asked}。",
            )
        take_profit = (payload.side == "buy") != above

        amount = (
            payload.size / payload.trigger
            if payload.size_mode == "quote"
            else payload.size
        )
        try:
            exchange_amount, base_qty = state.ctx.trading.exchange_amount(
                mt, symbol, amount, payload.trigger
            )
        except OrderRejected as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        try:
            with state.locks[mt]:
                order = state.ctx.rest_for(mt).create_conditional(
                    mt,
                    symbol,
                    payload.side,
                    exchange_amount,
                    payload.trigger,
                    take_profit=take_profit,
                    reduce_only=payload.reduce_only,
                )
        except ExchangeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            log.exception("条件单挂单失败")
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        state.ctx.trading.save_external_order(mt, order)
        state.ctx.account.invalidate()
        return {
            "ok": True,
            "order": order,
            "price": reference,
            "trigger": payload.trigger,
            "kind": "take_profit" if take_profit else "stop",
            "amount_base": base_qty,
            "amount_exchange": exchange_amount,
        }

    # ------------------------------------------------------- protections
    @app.put("/api/positions/protection")
    def api_set_protection(payload: ProtectionPayload) -> dict[str, Any]:
        """Arm, replace or clear the venue-side guard of an existing position.

        Kept apart from ordering so a position opened bare (or a stop that has
        to move as the trade develops) can be protected without touching the
        entry. Futures only: spot has no reduce-only trigger order, and
        pretending otherwise would leave the position naked.
        """
        mt = (
            _market(payload.market)
            if payload.market
            else _market_for_symbol(payload.symbol)
        )
        if not mt.is_futures:
            raise HTTPException(
                status_code=400,
                detail="现货没有交易所侧止损/止盈：现货仓位请手动卖出，或用币安自带的止损。",
            )
        if payload.stop_loss is None and payload.take_profit is None:
            raise HTTPException(status_code=400, detail="至少要填止损或止盈其中之一")
        if (payload.stop_loss is not None and payload.stop_loss <= 0) or (
            payload.take_profit is not None and payload.take_profit <= 0
        ):
            raise HTTPException(status_code=400, detail="止损 / 止盈价格必须大于 0")

        state = get_state()
        with state.locks[mt]:
            signed = state.ctx.rest_for(mt).has_credentials
        _require_live_ready(
            signed=signed,
            confirm_live=payload.confirm_live,
            network=state.ctx.network,
            market=mt,
        )

        symbol = _venue_symbol(state, mt, payload.symbol)
        held = _live_held(state, symbol, mt)
        if held is None:
            raise HTTPException(
                status_code=503,
                detail="无法读取当前持仓，暂时无法挂保护单，请稍后再试。",
            )
        if abs(held) <= 1e-12:
            raise HTTPException(status_code=400, detail="当前没有该交易对的持仓")
        long = held > 0
        exit_side = "sell" if long else "buy"
        entry_side = "buy" if long else "sell"

        reference = _reference_price(state, mt, symbol)
        try:
            _check_protection_levels(
                reference, payload.stop_loss, payload.take_profit, entry_side
            )
        except HTTPException:
            # The market may already have run past the level the user wants.
            # The position entry is the honest reference for "is this a stop?",
            # so only reject when the level is wrong for that too.
            entry = _ledger_entry_price(state, mt, symbol)
            if entry <= 0:
                raise
            _check_protection_levels(
                entry, payload.stop_loss, payload.take_profit, entry_side
            )

        try:
            size_exchange, _ = state.ctx.trading.exchange_amount(
                mt, symbol, abs(held), reference
            )
        except OrderRejected as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        rest = state.ctx.rest_for(mt)
        try:
            with state.locks[mt]:
                cleared = rest.cancel_protective_orders(mt, symbol)
        except ExchangeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        placed: list[dict] = []
        warnings: list[str] = []
        for level, take_profit, label in (
            (payload.stop_loss, False, "止损"),
            (payload.take_profit, True, "止盈"),
        ):
            if not level:
                continue
            try:
                with state.locks[mt]:
                    if take_profit:
                        order = rest.create_take_profit_market(
                            mt, symbol, exit_side, size_exchange, level
                        )
                    else:
                        order = rest.create_stop_market(
                            mt, symbol, exit_side, size_exchange, level
                        )
            except ExchangeError as exc:
                warnings.append(f"{label}单挂单失败：{exc}")
                continue
            placed.append(order)
            state.ctx.trading.save_external_order(mt, order)
        state.ctx.account.invalidate()
        if not placed:
            raise HTTPException(
                status_code=400,
                detail="。".join(warnings) if warnings else "止损/止盈都没有挂上",
            )
        return {
            "ok": True,
            "protection": {
                "symbol": symbol,
                "stop_loss": payload.stop_loss or 0.0,
                "take_profit": payload.take_profit or 0.0,
                "orders": [item.get("id") for item in placed],
            },
            "price": reference,
            "size": abs(held),
            "cleared": cleared,
            "warnings": warnings,
        }

    @app.delete("/api/positions/protection")
    def api_clear_protection(
        symbol: str, market: str | None = None
    ) -> dict[str, Any]:
        """Disarm the stop-loss / take-profit guard of one symbol."""
        if not symbol:
            raise HTTPException(status_code=400, detail="缺少交易对")
        mt = _market(market) if market else _market_for_symbol(symbol)
        if not mt.is_futures:
            raise HTTPException(
                status_code=400,
                detail="现货没有交易所侧止损/止盈，无需撤销。",
            )
        state = get_state()
        # 撤单也要用交易所认的符号：传裸写法撤的是另一个名字，等于没撤。
        symbol = _venue_symbol(state, mt, symbol)
        try:
            with state.locks[mt]:
                cleared = state.ctx.rest_for(mt).cancel_protective_orders(mt, symbol)
        except ExchangeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        state.ctx.account.invalidate()
        return {"ok": True, "cleared": cleared}

    # -------------------------------------------------------------- logs

    @app.get("/api/logs")
    def api_logs(limit: int = 200) -> dict[str, Any]:
        """Recent warnings/errors for the in-app log viewer."""
        buffer = error_buffer()
        records = buffer.records()
        limit = max(1, min(int(limit), buffer.capacity))
        errors, warnings = buffer.counts()
        return {
            "revision": buffer.revision,
            "errors": errors,
            "warnings": warnings,
            "items": [
                {
                    "level": item.level_name,
                    "logger": item.logger,
                    "message": item.message,
                    "detail": item.detail,
                    "when": item.when,
                    "text": item.as_text(),
                }
                for item in records[-limit:][::-1]
            ],
        }

    @app.post("/api/logs/clear")
    def api_logs_clear() -> dict[str, Any]:
        error_buffer().clear()
        return {"ok": True}

    # ---------------------------------------------------------- websocket
    @app.websocket("/ws/market")
    async def ws_market(ws: WebSocket) -> None:
        await ws.accept()
        state = get_state()
        hub = state.stream
        if hub is None:  # pragma: no cover - only if lifespan did not run
            await ws.close(code=1011)
            return

        queue: asyncio.Queue | None = None
        key: tuple[str, str] | None = None
        try:
            command = await ws.receive_json()
            while True:
                action = str(command.get("action", "subscribe"))
                if action == "ping":
                    await ws.send_json({"type": "pong"})
                    command = await ws.receive_json()
                    continue
                if action != "subscribe":
                    command = await ws.receive_json()
                    continue

                try:
                    market = _market(str(command.get("market", "spot")))
                except HTTPException as exc:
                    await ws.send_json({"type": "error", "message": exc.detail})
                    command = await ws.receive_json()
                    continue

                symbol = str(command.get("symbol") or "")
                if not symbol:
                    await ws.send_json({"type": "error", "message": "缺少交易对"})
                    command = await ws.receive_json()
                    continue

                # Switching symbol cancels the old watchers: they are refcounted
                # per (market, symbol), so another client keeps them alive.
                if queue is not None and key is not None:
                    await hub.unsubscribe(key, queue)
                    queue = None
                    key = None
                queue, key = await hub.subscribe(market, symbol)
                await ws.send_json(
                    {"type": "subscribed", "market": market.value, "symbol": symbol}
                )

                nxt = await _stream_until_command(ws, queue)
                if nxt is None:  # client disconnected mid-stream
                    return
                command = nxt
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # a broken socket must not take the app down
            log.debug("行情 WebSocket 关闭: %s", exc)
        finally:
            if queue is not None and key is not None:
                await hub.unsubscribe(key, queue)

    return app


async def _stream_until_command(ws: WebSocket, queue) -> dict[str, Any] | None:
    """Forward market frames until the client sends another command.

    Returns the parsed command, or ``None`` when the socket closed. The receive
    task outlives each queue read, so a command that arrives while frames are
    flowing is still seen instead of being buffered until the next tick.
    """
    receiver = asyncio.create_task(ws.receive_json())
    try:
        while True:
            getter = asyncio.create_task(queue.get())
            done, _pending = await asyncio.wait(
                {receiver, getter}, return_when=asyncio.FIRST_COMPLETED
            )
            if receiver in done:
                getter.cancel()
                with suppress(asyncio.CancelledError):
                    await getter
                try:
                    return receiver.result()
                except WebSocketDisconnect:
                    return None
            frame = getter.result()
            await ws.send_json(frame)
    finally:
        receiver.cancel()
        with suppress(asyncio.CancelledError):
            await receiver


def _warm() -> None:
    try:
        state = get_state()
        _symbols_for(state, state.ctx.market_type)
    except Exception:  # pragma: no cover - warm-up is best effort
        log.debug("预热交易对失败", exc_info=True)


# Live fills feed the daily-loss and losing-streak breakers, and a stop-loss
# fired by the venue is only visible through them. Each poll costs a signed
# request per market, so five seconds keeps the breakers close to real time
# while staying far below Binance's request weight budget. The account snapshot
# is refreshed on the same tick, which is what keeps the panels current without
# every page polling the venue itself.
LIVE_SYNC_SECONDS = 5.0


def _reconcile_live_fills(state: _State) -> None:
    while not state.stop_reconcile.wait(LIVE_SYNC_SECONDS):
        try:
            # 这里刻意不拿 account_lock：那把锁现在包的是「提交这一下」，
            # 而一次成交同步要对每个市场逐个交易对发签名请求，可能好几秒。
            # 拿它等于把下单框挡在成交轮询后面。账本只有这一个线程在写。
            booked = state.ctx.trading.sync_live_fills()
            if booked:
                log.info("交易所成交已回填风控 %d 笔", len(booked))
        except Exception:  # the poller must survive a bad exchange
            log.debug("成交同步异常", exc_info=True)
        try:
            state.ctx.account.refresh_all()
        except Exception:  # pragma: no cover - snapshot refresh is best effort
            log.debug("账户快照刷新异常", exc_info=True)
