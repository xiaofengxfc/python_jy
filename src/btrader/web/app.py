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

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..context import AppContext
from ..core.types import (
    TIMEFRAME_SECONDS,
    MarketType,
    Network,
    market_for_symbol,
)
from ..engine.trading import OrderRejected, OrderRequest
from ..exchange.symbols import format_amount, load_symbols
from ..exchange import mirror
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
        self.account_lock = threading.RLock()
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
        # down, the second says the fallback could not rescue it either.
        raise HTTPException(
            status_code=503,
            detail=_market_data_hint(market, f"{primary}；备用源：{exc}"),
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
            "常见原因是 DNS 污染或网络限制。可采取：1) 在设置里填写代理；"
            "2) 合约图表会自动改用 Gate.io 永续行情（若其也失败则会显示这条错误）。"
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
    market: str = "spot"
    symbol: str
    side: str
    order_type: str = "limit"
    size_mode: str = "amount"
    size: float = Field(gt=0)
    price: float | None = None
    # Protective exits for a new entry (absolute prices). Omit to trade bare.
    stop_loss: float | None = Field(default=None, gt=0)
    take_profit: float | None = Field(default=None, gt=0)
    # Mainnet requires an explicit acknowledgement; testnet ignores it.
    confirm_live: bool = False


class PreviewPayload(BaseModel):
    market: str = "spot"
    symbol: str
    side: str = "buy"
    order_type: str = "limit"
    size_mode: str = "amount"
    size: float = Field(gt=0)
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
    size: float = Field(gt=0)
    trigger: float = Field(gt=0)
    price: float | None = Field(default=None, gt=0)
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
    stop_loss: float | None = Field(default=None, gt=0)
    take_profit: float | None = Field(default=None, gt=0)
    confirm_live: bool = False

class ClosePayload(BaseModel):
    """Close (or reduce) a position with a market order.

    ``size`` is in base coins; omitted means the whole position. ``market`` is
    optional: when a caller omits it the venue is inferred from the ccxt
    symbol, because defaulting to spot made a futures position look absent
    ("当前没有该交易对的持仓") and the exit impossible.
    """

    symbol: str
    market: str | None = None
    size: float | None = Field(default=None, gt=0)
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

    Testnet is a real venue with fake funds, so it needs no extra click: the
    whole point of it is to rehearse without ceremony. Mainnet keeps the
    acknowledgement because there a typo costs money.

    Protective exits used to be refused here because the exchange side could
    not honour them. They are now placed as STOP_MARKET / TAKE_PROFIT_MARKET
    orders, so a futures order may carry them. Spot still cannot: Binance has
    no reduce-only stop for spot balances, and pretending to arm one would
    leave the position unhedged.
    """
    if not signed:
        raise HTTPException(status_code=400, detail="下单需要先配置该市场的 API Key。")
    if network is Network.MAINNET and not confirm_live:
        raise HTTPException(
            status_code=400,
            detail="主网下单需要二次确认（confirm_live=true）；测试网不需要。",
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


def _live_held(state: _State, symbol: str, market: MarketType) -> float:
    """Signed live position size: positive long, negative short.

    Spot has no position object, so what is held is the base balance. Futures
    report contracts with a side; the sign is what this function owes its
    callers, because the close route picks the opposite side from it.
    """
    with state.locks[market]:
        rest = state.ctx.rest_for(market)
        if market.is_futures:
            try:
                positions = rest.fetch_positions(market, [symbol])
            except Exception:
                log.warning("读取实盘持仓失败 %s", symbol, exc_info=True)
                return 0.0
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
    exposure is. Spot sells are always exits: spot cannot short, and the
    unheld-sell case is rejected earlier with its own message.
    """
    if market is MarketType.SPOT:
        held = _live_spot_available(state, symbol, market)
        return side == "sell" and held > 0 and amount <= held * (1 + 1e-6) + 1e-12
    held = _live_held(state, symbol, market)
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
def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        state = get_state()
        state.stream = StreamHub(state.ctx.network)
        # Warm the symbol list in the background: the first paint must not wait
        # on a multi-second load_markets() call.
        threading.Thread(target=_warm, daemon=True).start()
        # Bring the OpenClaw gateway up in the background. WeChat push needs it,
        # and nothing else was starting it; doing it off-thread keeps a slow or
        # missing gateway from delaying the first paint.
        threading.Thread(
            target=state.ctx.gateway.start, name="openclaw-gateway", daemon=True
        ).start()
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
    app.include_router(build_router(get_state))
    # Phase 4-B: candidate review + multi-round parameter reflection.
    app.include_router(build_lab_router(get_state))
    # Phase 5-A: source editor for user strategy plugins.
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
            # 现货有公共镜像，合约没有；此时用 Gate 永续兜底，并且把来源带回去，
            # 让界面能标明「这不是币安的价格」。
            rows = _mirror_candles(mt, symbol, timeframe, limit, since, exc)
            if rows is None:
                raise HTTPException(
                    status_code=503, detail=_market_data_hint(mt, exc)
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
                raw = state.ctx.market_rest_for(mt).fetch_ticker(mt, symbol)
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {
            "symbol": symbol,
            "last": float(raw.get("last") or 0.0),
            "bid": float(raw.get("bid") or 0.0),
            "ask": float(raw.get("ask") or 0.0),
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
            raise HTTPException(status_code=503, detail=str(exc)) from exc
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
                raise HTTPException(
                    status_code=503, detail=f"无法获取最新价：{exc}"
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
                raise HTTPException(
                    status_code=503, detail=f"无法获取最新价：{exc}"
                ) from exc
        if not reference or reference <= 0:
            raise HTTPException(status_code=400, detail="缺少有效的参考价格")

        amount = (
            payload.size / reference if payload.size_mode == "quote" else payload.size
        )
        if amount <= 0:
            raise HTTPException(status_code=400, detail="下单数量必须大于 0")

        if state.ctx.market_type is not mt:
            state.ctx.set_market_type(mt)

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
        try:
            with state.account_lock:
                result = state.ctx.trading.submit(request)
        except OrderRejected as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            log.exception("下单失败")
            raise HTTPException(status_code=500, detail=str(exc)) from exc

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
        state = get_state()
        mt = _market(market) if market else _market_for_symbol(symbol)
        try:
            with state.account_lock:
                order = state.ctx.trading.cancel(mt, symbol, order_id, market=mt.value)
        except OrderRejected as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"ok": True, "order": order}

    # ---------------------------------------------------------------- closing
    @app.post("/api/positions/close")
    def api_close_position(payload: ClosePayload) -> dict[str, Any]:
        """Market-close a position (optionally a partial size).

        Kept separate from /api/orders so the caller does not have to know the
        signed size or the direction: the server reads the position from the
        venue and sends the opposite side with reduceOnly.
        """
        # An omitted market is inferred from the symbol: a futures pair carries
        # its settle currency ("BTC/USDT:USDT"), spot does not. Defaulting to
        # spot here made every futures exit fail as "没有该交易对的持仓".
        mt = _market(payload.market) if payload.market else _market_for_symbol(payload.symbol)
        state = get_state()

        with state.locks[mt]:
            signed = state.ctx.rest_for(mt).has_credentials
        _require_live_ready(
            signed=signed,
            confirm_live=payload.confirm_live,
            network=state.ctx.network,
        )
        held = _live_held(state, payload.symbol, mt)
        if abs(held) <= 1e-12:
            raise HTTPException(status_code=400, detail="当前没有该交易对的持仓")
        size = abs(held) if payload.size is None else min(abs(payload.size), abs(held))
        try:
            quote = state.ctx.quote_rest(mt)
            with state.locks[mt]:
                reference = float(
                    quote.fetch_ticker(mt, payload.symbol).get("last") or 0.0
                )
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"无法获取最新价：{exc}") from exc
        if reference <= 0:
            raise HTTPException(status_code=400, detail="无法获取有效的参考价格")

        side = "sell" if held > 0 else "buy"
        request = OrderRequest(
            symbol=payload.symbol,
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
            raise HTTPException(status_code=503, detail=f"无法获取最新价：{exc}") from exc
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

        reference = _reference_price(state, mt, payload.symbol)
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
                mt, payload.symbol, amount, payload.trigger
            )
        except OrderRejected as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        try:
            with state.locks[mt]:
                order = state.ctx.rest_for(mt).create_conditional(
                    mt,
                    payload.symbol,
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

        state = get_state()
        with state.locks[mt]:
            signed = state.ctx.rest_for(mt).has_credentials
        _require_live_ready(
            signed=signed,
            confirm_live=payload.confirm_live,
            network=state.ctx.network,
            market=mt,
        )

        held = _live_held(state, payload.symbol, mt)
        if abs(held) <= 1e-12:
            raise HTTPException(status_code=400, detail="当前没有该交易对的持仓")
        long = held > 0
        exit_side = "sell" if long else "buy"
        entry_side = "buy" if long else "sell"

        reference = _reference_price(state, mt, payload.symbol)
        try:
            _check_protection_levels(
                reference, payload.stop_loss, payload.take_profit, entry_side
            )
        except HTTPException:
            # The market may already have run past the level the user wants.
            # The position entry is the honest reference for "is this a stop?",
            # so only reject when the level is wrong for that too.
            entry = _ledger_entry_price(state, mt, payload.symbol)
            if entry <= 0:
                raise
            _check_protection_levels(
                entry, payload.stop_loss, payload.take_profit, entry_side
            )

        try:
            size_exchange, _ = state.ctx.trading.exchange_amount(
                mt, payload.symbol, abs(held), reference
            )
        except OrderRejected as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        rest = state.ctx.rest_for(mt)
        try:
            with state.locks[mt]:
                cleared = rest.cancel_protective_orders(mt, payload.symbol)
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
                            mt, payload.symbol, exit_side, size_exchange, level
                        )
                    else:
                        order = rest.create_stop_market(
                            mt, payload.symbol, exit_side, size_exchange, level
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
                detail="；".join(warnings) if warnings else "止损/止盈都没有挂上",
            )
        return {
            "ok": True,
            "protection": {
                "symbol": payload.symbol,
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
            with state.account_lock:
                booked = state.ctx.trading.sync_live_fills()
            if booked:
                log.info("交易所成交已回填风控 %d 笔", len(booked))
        except Exception:  # the poller must survive a bad exchange
            log.debug("成交同步异常", exc_info=True)
        try:
            state.ctx.account.refresh_all()
        except Exception:  # pragma: no cover - snapshot refresh is best effort
            log.debug("账户快照刷新异常", exc_info=True)
