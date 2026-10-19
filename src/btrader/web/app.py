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
from ..core.types import TIMEFRAME_SECONDS, MarketType, RunMode
from ..engine.trading import OrderRejected, OrderRequest
from ..exchange.symbols import format_amount, load_symbols
from ..logging_setup import error_buffer
from .routes import build_router
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
    # Paper is the default; a live order must say so and confirm explicitly.
    mode: str = "paper"
    confirm_live: bool = False


class PreviewPayload(BaseModel):
    market: str = "spot"
    symbol: str
    side: str = "buy"
    order_type: str = "limit"
    size_mode: str = "amount"
    size: float = Field(gt=0)
    price: float | None = None


class ClosePayload(BaseModel):
    """Close (or reduce) a position with a market order.

    ``size`` is in base coins; omitted means the whole position. ``market`` is
    optional: when a caller omits it the venue is inferred from the ccxt
    symbol, because defaulting to spot made a futures position look absent
    ("当前没有该交易对的持仓") and the exit impossible.
    """

    symbol: str
    market: str | None = None
    mode: str = "paper"
    size: float | None = Field(default=None, gt=0)
    confirm_live: bool = False


class ProtectionPayload(BaseModel):
    """Arm or update the SL/TP guard of an existing position.

    ``0`` (or omitted) clears that leg; sending both as 0 disarms the guard.
    """

    symbol: str
    stop_loss: float = Field(default=0.0, ge=0)
    take_profit: float = Field(default=0.0, ge=0)


class ConditionalPayload(BaseModel):
    """Standalone stop/limit order armed before its trigger is reached.

    Used for "buy when it breaks 90k" and for an exit that has to exist
    before the entry fills. ``direction`` says which way the price must cross
    ``trigger``; the resulting order uses ``order_type`` and ``price``.
    """

    market: str = "spot"
    symbol: str
    side: str
    order_type: str = "market"
    size_mode: str = "amount"
    size: float = Field(gt=0)
    trigger: float = Field(gt=0)
    direction: str = "below"
    price: float | None = Field(default=None, gt=0)
    mode: str = "paper"
    # An exit that only reduces an existing position (never opens a short).
    reduce_only: bool = False


def _market_for_symbol(symbol: str) -> MarketType:
    """Infer the market from a ccxt symbol when the caller did not say.

    ``BTC/USDT`` -> spot, ``BTC/USDT:USDT`` -> USDT-margined,
    ``BTC/USD:BTC`` -> coin-margined.
    """
    if ":" not in symbol:
        return MarketType.SPOT
    settle = symbol.rsplit(":", 1)[1]
    return MarketType.USDM if settle.upper() in ("USDT", "USDC") else MarketType.COINM


def _paper_held(state: _State, symbol: str) -> float:
    """Base quantity of a paper position, as a positive number."""
    position = state.ctx.paper.account.positions.get(symbol)
    return abs(position.contracts) if position else 0.0


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
    market: MarketType | None = None,
    stop_loss: float | None = None,
    take_profit: float | None = None,
) -> None:
    """Gate a live order behind credentials and an explicit acknowledgement.

    Protective exits used to be refused here because the exchange side could
    not honour them. They are now placed as ``STOP_MARKET`` /
    ``TAKE_PROFIT_MARKET`` orders, so a futures order may carry them. Spot
    still cannot: Binance has no reduce-only stop for spot balances, and
    pretending to arm one would leave the position unhedged.
    """
    if not signed:
        raise HTTPException(status_code=400, detail="实盘下单需要先配置该市场的 API Key。")
    if not confirm_live:
        raise HTTPException(
            status_code=400,
            detail="实盘下单需要二次确认（confirm_live=true）。",
        )
    if (stop_loss or take_profit) and market is not None and not market.is_futures:
        raise HTTPException(
            status_code=400,
            detail="实盘现货暂不支持交易所侧止损/止盈，请切换到合约市场或手动盯盘。",
        )


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


def _empty_live_account(market: MarketType) -> dict[str, Any]:
    """Account shape used when the exchange cannot be read."""
    quote = "USDT" if market is not MarketType.COINM else "BTC"
    return {
        "quote_asset": quote,
        "cash": 0.0,
        "starting_balance": 0.0,
        "realized_pnl": 0.0,
        "positions": [],
        "orders": [],
        "trades": [],
        "protections": [],
        "conditional": [],
        "live": True,
    }


def _is_reducing(
    state: _State,
    market: MarketType,
    symbol: str,
    side: str,
    amount: float,
    mode: RunMode = RunMode.PAPER,
) -> bool:
    """Whether an order closes (part of) an existing position.

    A reduce order must not be judged by the entry limits (per-order notional,
    position cap) or by the entry breakers: selling 0.01 BTC is an exit, and
    refusing it with "单笔金额超过上限" — or with "风控熔断中" — is what made a
    held position impossible to sell.

    Paper reads the simulated book; live reads the venue, because that is
    where the real position is. Spot sells are always exits: spot cannot
    short, and the unheld-sell case is rejected earlier with its own message.
    """
    if market is MarketType.SPOT:
        if mode is RunMode.LIVE:
            held = _live_spot_available(state, symbol, market)
        else:
            position = state.ctx.paper.account.positions.get(symbol)
            held = position.contracts if position else 0.0
        return side == "sell" and held > 0 and amount <= held * (1 + 1e-6) + 1e-12

    if mode is RunMode.LIVE:
        held = _live_held(state, symbol, market)
    else:
        position = state.ctx.paper.account.positions.get(symbol)
        held = position.contracts if position else 0.0
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
            target=_reconcile_paper_orders, args=(state,), name="paper-reconciler", daemon=True
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
        try:
            with state.locks[mt]:
                if before is not None:
                    # Scrolling past the left edge asks for the window that
                    # ends at the oldest candle the chart already holds. One
                    # extra step covers the boundary bar itself.
                    step = TIMEFRAME_SECONDS[timeframe] * 1000
                    since = max(0, int(before) - (limit + 1) * step)
                    frame = state.ctx.klines.fetch(
                        mt, symbol, timeframe, limit=limit + 1, since=since
                    )
                    frame = frame[frame["timestamp"] < int(before)].tail(limit)
                else:
                    frame = state.ctx.klines.fetch(mt, symbol, timeframe, limit=limit)
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
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
        return {"symbol": symbol, "timeframe": timeframe, "candles": candles}

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
    def api_account() -> dict[str, Any]:
        state = get_state()
        with state.account_lock:
            account = state.ctx.paper.account
            positions = [
                {
                    "symbol": position.symbol,
                    "contracts": position.contracts,
                    "side": position.side,
                    "entry_price": position.entry_price,
                    "realized_pnl": position.realized_pnl,
                }
                for position in account.positions.values()
                if abs(position.contracts) > 1e-12
            ]
            orders = [dict(order) for order in account.orders[-50:]][::-1]
            trades = [dict(trade) for trade in account.trades[-50:]][::-1]
            return {
                "quote_asset": account.quote_asset,
                "cash": account.cash,
                "starting_balance": account.starting_balance,
                "realized_pnl": sum(p.realized_pnl for p in account.positions.values()),
                "positions": positions,
                "orders": orders,
                "trades": trades,
                "protections": [
                    dict(guard) for guard in account.protections.values()
                ],
                "conditional": [
                    {
                        "id": item.get("id"),
                        "symbol": item.get("symbol"),
                        "market": item.get("market") or "spot",
                        "side": item.get("side"),
                        "type": item.get("type") or "market",
                        "amount": item.get("amount") or 0.0,
                        "price": item.get("price") or 0.0,
                        "trigger": item.get("trigger") or 0.0,
                        "direction": item.get("direction") or "below",
                        "status": item.get("status") or "open",
                        "createdAt": item.get("createdAt") or 0,
                    }
                    for item in account.conditional
                    if item.get("status") in ("open", "triggered", "canceled", "rejected")
                ][-50:][::-1],
            }

    @app.get("/api/account/live")
    def api_live_account(market: str = "spot", symbol: str | None = None) -> dict[str, Any]:
        """Balances, positions and open orders straight from the exchange.

        The paper account is local and always available; this one is only as
        good as the API key and the network, so every read failure becomes an
        HTTP error the panel can show instead of an empty account that would
        look like "you own nothing".
        """
        state = get_state()
        mt = _market(market)
        with state.locks[mt]:
            rest = state.ctx.rest_for(mt)
            if not rest.has_credentials:
                raise HTTPException(status_code=400, detail=f"{mt.label}尚未配置 API Key。")
            try:
                balance = rest.fetch_balance(mt)
            except Exception as exc:
                raise HTTPException(status_code=503, detail=f"读取实盘余额失败：{exc}") from exc
            quote_asset = "BTC" if mt is MarketType.COINM else "USDT"
            result = _empty_live_account(mt)
            result["quote_asset"] = quote_asset
            result["cash"] = _balance_free(balance, quote_asset)

            positions: list[dict[str, Any]] = []
            if mt.is_futures:
                try:
                    raw_positions = rest.fetch_positions(mt, [symbol] if symbol else None)
                except Exception as exc:
                    log.warning("读取实盘持仓失败 %s", exc, exc_info=True)
                    raw_positions = []
                for item in raw_positions:
                    contracts = float(item.get("contracts") or 0.0)
                    if abs(contracts) <= 1e-12:
                        continue
                    positions.append(
                        {
                            "symbol": item.get("symbol") or "",
                            "contracts": contracts if item.get("side") != "short" else -contracts,
                            "side": "short" if item.get("side") == "short" else "long",
                            "entry_price": float(
                                item.get("entryPrice") or item.get("markPrice") or 0.0
                            ),
                            "realized_pnl": float(
                                (item.get("info") or {}).get("realizedPnl") or 0.0
                            ),
                        }
                    )
            elif symbol:
                # Spot has no position object: what is held is simply the base
                # balance, and only the pair being viewed is of interest.
                base = symbol.split("/")[0].upper()
                held = _balance_free(balance, base)
                if held > 0:
                    positions.append(
                        {
                            "symbol": symbol,
                            "contracts": held,
                            "side": "long",
                            "entry_price": 0.0,
                            "realized_pnl": 0.0,
                        }
                    )

            orders: list[dict[str, Any]] = []
            try:
                for order in rest.fetch_open_orders(mt, symbol):
                    orders.append(
                        {
                            "id": str(order.get("id") or ""),
                            "symbol": order.get("symbol") or "",
                            "market": mt.value,
                            "side": order.get("side") or "buy",
                            "type": order.get("type") or "limit",
                            "amount": float(order.get("amount") or 0.0),
                            "price": float(order.get("price") or 0.0),
                            "filled": float(order.get("filled") or 0.0),
                            "status": "open",
                            "timestamp": int(order.get("timestamp") or 0),
                        }
                    )
            except Exception as exc:
                log.warning("读取实盘挂单失败 %s", exc, exc_info=True)

            result["positions"] = positions
            result["orders"] = orders
            result["trades"] = []
            return result

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
        paper = state.ctx.paper
        notional = abs(amount) * reference
        base = {
            "symbol": payload.symbol,
            "market": mt.value,
            "side": payload.side,
            "order_type": payload.order_type,
            "price": reference,
            "amount_base": amount,
            "quote_notional": notional,
            "fee_rate": paper.fee_rate,
            "slippage_bps": paper.slippage_bps,
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
                mode=RunMode.PAPER,
            )
        )
        return {
            **base,
            "allowed": bool(decision),
            "reason": decision.reason,
            "amount_base": base_qty,
            "exchange_amount": exchange_amount,
            "quote_notional": abs(base_qty) * reference,
            "fee_estimate": abs(base_qty) * reference * paper.fee_rate,
        }

    @app.post("/api/orders")
    def api_place_order(payload: OrderPayload) -> dict[str, Any]:
        if payload.side not in ("buy", "sell"):
            raise HTTPException(status_code=400, detail="方向必须是 buy 或 sell")
        if payload.order_type not in ("limit", "market"):
            raise HTTPException(status_code=400, detail="订单类型必须是 limit 或 market")
        if payload.size_mode not in ("amount", "quote"):
            raise HTTPException(status_code=400, detail="数量模式必须是 amount 或 quote")
        if payload.mode not in ("paper", "live"):
            raise HTTPException(status_code=400, detail="模式必须是 paper 或 live")

        state = get_state()
        mt = _market(payload.market)
        mode = RunMode(payload.mode)

        if mode is RunMode.LIVE:
            # Two gates: the account must be able to sign, and the caller must
            # have acknowledged this leaves the building. A testnet order also
            # needs credentials, so the check is not network-specific.
            with state.locks[mt]:
                signed = state.ctx.rest_for(mt).has_credentials
            _require_live_ready(
                signed=signed,
                confirm_live=payload.confirm_live,
                market=mt,
                stop_loss=payload.stop_loss,
                take_profit=payload.take_profit,
            )

        # A paper fill is simulated against the charting feed; a live order must
        # be priced by the venue that will actually execute it.
        quote = state.ctx.quote_rest(mt, paper=mode is RunMode.PAPER)
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
            available = (
                _live_spot_available(state, payload.symbol, mt)
                if mode is RunMode.LIVE
                else _paper_held(state, payload.symbol)
            )
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
                state, mt, payload.symbol, payload.side, amount, mode
            ),
            market_type=mt,
            mode=mode,
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
            "mode": result.mode.value,
            "adjusted": adjusted,
            "protection": (
                state.ctx.paper.protections().get(payload.symbol)
                if mode is RunMode.PAPER
                else result.protection
            ),
            "warnings": result.warnings,
        }

    # ---------------------------------------------------- conditional orders
    @app.post("/api/orders/conditional")
    def api_place_conditional(payload: ConditionalPayload) -> dict[str, Any]:
        """Arm a standalone stop/limit order.

        Paper only: a live exchange conditional order needs its own endpoint
        (``STOP_MARKET`` etc.) and is not wired up yet, so this refuses rather
        than silently sending a plain market order.
        """
        if payload.mode != "paper":
            raise HTTPException(status_code=400, detail="条件单目前只支持纸面模式")
        if payload.side not in ("buy", "sell"):
            raise HTTPException(status_code=400, detail="方向必须是 buy 或 sell")
        if payload.direction not in ("below", "above"):
            raise HTTPException(status_code=400, detail="触发方向必须是 below 或 above")
        mt = _market(payload.market)
        state = get_state()
        amount = payload.size
        if payload.size_mode == "quote":
            if not payload.trigger:
                raise HTTPException(status_code=400, detail="按金额下单需要触发价")
            amount = payload.size / payload.trigger
        if amount <= 0:
            raise HTTPException(status_code=400, detail="条件单数量必须大于 0")
        try:
            with state.account_lock:
                item = state.ctx.paper.place_conditional(
                    payload.symbol,
                    payload.side,
                    amount,
                    payload.trigger,
                    direction=payload.direction,
                    order_type=payload.order_type,
                    price=payload.price,
                    market=mt.value,
                    allow_short=mt.is_futures,
                    reduce_only=payload.reduce_only,
                )
                state.ctx.trading.save_paper_state()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "conditional": item, "amount_base": amount}

    @app.delete("/api/orders/conditional/{order_id}")
    def api_cancel_conditional(order_id: str) -> dict[str, Any]:
        state = get_state()
        with state.account_lock:
            item = state.ctx.paper.cancel_conditional(order_id)
            if item is None:
                raise HTTPException(
                    status_code=404, detail="未找到可撤销的条件单（可能已触发或已撤销）。"
                )
            state.ctx.trading.save_paper_state()
        return {"ok": True, "conditional": item}

    @app.post("/api/paper/reset")
    def api_reset_paper(balance: float | None = None) -> dict[str, Any]:
        state = get_state()
        with state.account_lock:
            state.ctx.paper.reset(balance)
            state.ctx.trading.save_paper_state()
        return {"ok": True, "cash": state.ctx.paper.account.cash}

    # ---------------------------------------------------------------- cancel
    @app.delete("/api/orders/{order_id}")
    def api_cancel_order(
        order_id: str,
        symbol: str,
        market: str | None = None,
        mode: str = "paper",
    ) -> dict[str, Any]:
        """Cancel a resting order.

        Paper and live orders share the same route; ``mode`` decides where it
        goes. The response carries the final order state so the UI can update
        the row without a refetch.
        """
        if mode not in ("paper", "live"):
            raise HTTPException(status_code=400, detail="模式必须是 paper 或 live")
        if not symbol:
            raise HTTPException(status_code=400, detail="缺少交易对")
        state = get_state()
        mt = _market(market) if market else _market_for_symbol(symbol)
        try:
            with state.account_lock:
                order = state.ctx.trading.cancel(
                    mt, symbol, order_id, RunMode(mode), market=mt.value
                )
        except OrderRejected as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"ok": True, "order": order}

    # ---------------------------------------------------------------- closing
    @app.post("/api/positions/close")
    def api_close_position(payload: ClosePayload) -> dict[str, Any]:
        """Market-close a paper position (optionally a partial size).

        Kept separate from ``/api/orders`` so the caller does not have to know
        the signed size or the direction: the server reads the position and
        sends the opposite side with ``reduceOnly``. Live positions are read
        from the exchange for the same reason.
        """
        if payload.mode not in ("paper", "live"):
            raise HTTPException(status_code=400, detail="模式必须是 paper 或 live")
        # An omitted market is inferred from the symbol: a futures pair carries
        # its settle currency ("BTC/USDT:USDT"), spot does not. Defaulting to
        # spot here made every futures exit fail as "没有该交易对的持仓".
        mt = _market(payload.market) if payload.market else _market_for_symbol(payload.symbol)
        state = get_state()
        mode = RunMode(payload.mode)

        if mode is RunMode.LIVE:
            with state.locks[mt]:
                signed = state.ctx.rest_for(mt).has_credentials
            _require_live_ready(
                signed=signed,
                confirm_live=payload.confirm_live,
                stop_loss=None,
                take_profit=None,
            )
            held = _live_held(state, payload.symbol, mt)
        else:
            position = state.ctx.paper.account.positions.get(payload.symbol)
            held = position.contracts if position else 0.0
        if abs(held) <= 1e-12:
            raise HTTPException(status_code=400, detail="当前没有该交易对的持仓")
        size = abs(held) if payload.size is None else min(abs(payload.size), abs(held))
        try:
            quote = state.ctx.quote_rest(mt, paper=mode is RunMode.PAPER)
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
            mode=mode,
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

    @app.put("/api/positions/protection")
    def api_set_protection(payload: ProtectionPayload) -> dict[str, Any]:
        """Arm, update, or clear the guard of a position that already exists.

        Kept separate from ordering so a position opened bare (or a stop that
        needs moving as the trade develops) can be protected without touching
        the entry — and so spot positions can use it too.
        """
        state = get_state()
        with state.account_lock:
            position = state.ctx.paper.account.positions.get(payload.symbol)
            held = position.contracts if position else 0.0
            if abs(held) <= 1e-12:
                raise HTTPException(status_code=400, detail="当前没有该交易对的持仓")
            long = held > 0
            try:
                market = _market_for_symbol(payload.symbol)
                with state.locks[market]:
                    reference = float(
                        state.ctx.market_rest_for(market)
                        .fetch_ticker(market, payload.symbol)
                        .get("last")
                        or 0.0
                    )
            except Exception as exc:
                raise HTTPException(status_code=503, detail=f"无法获取最新价：{exc}") from exc
            if reference <= 0:
                # Falling back to the entry price keeps a stop usable offline.
                reference = position.entry_price
            try:
                _check_protection_levels(
                    reference,
                    payload.stop_loss or None,
                    payload.take_profit or None,
                    "buy" if long else "sell",
                )
            except HTTPException:
                # The market may have moved past the level already. The entry
                # price is the honest reference for "is this a stop?", so only
                # reject when the level is wrong for that too.
                _check_protection_levels(
                    position.entry_price,
                    payload.stop_loss or None,
                    payload.take_profit or None,
                    "buy" if long else "sell",
                )
            guard = state.ctx.paper.set_protection(
                payload.symbol, payload.stop_loss, payload.take_profit
            )
            state.ctx.trading.save_paper_state()
        return {"ok": True, "protection": guard or None}

    @app.delete("/api/positions/protection")
    def api_clear_protection(symbol: str) -> dict[str, Any]:
        """Disarm the stop-loss / take-profit guard of one symbol."""
        if not symbol:
            raise HTTPException(status_code=400, detail="缺少交易对")
        state = get_state()
        with state.account_lock:
            existed = state.ctx.paper.clear_protection(symbol)
            if existed:
                state.ctx.trading.save_paper_state()
        return {"ok": True, "cleared": existed}

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


# Paper limit orders have no exchange to match them, so a light poll does it:
# every couple of seconds the last price of each symbol with a resting order is
# fetched and any order the market has crossed is filled.
RECONCILE_SECONDS = 2.0

# Live fills feed the same breakers as paper ones, but each poll costs a
# signed request per market plus one per symbol. Five seconds keeps the loss
# breakers close to real time while staying far below Binance's request weight
# budget (a handful of calls per interval, not per symbol per second).
LIVE_SYNC_SECONDS = 5.0


def _reconcile_paper_orders(state: _State) -> None:
    last_live_sync = 0.0
    while not state.stop_reconcile.wait(RECONCILE_SECONDS):
        # Live fills feed the same breakers as paper ones, but a signed request
        # per symbol is far too expensive at the paper cadence.
        now = time.monotonic()
        if now - last_live_sync >= LIVE_SYNC_SECONDS:
            last_live_sync = now
            try:
                with state.account_lock:
                    booked = state.ctx.trading.sync_live_fills()
                if booked:
                    log.info("实盘成交已回填风控 %d 笔", len(booked))
            except Exception:  # the reconciler must survive a bad exchange
                log.debug("实盘成交同步异常", exc_info=True)
        try:
            resting = state.ctx.paper.open_orders()
            # Symbols guarded by a stop/target must be polled even when no
            # order rests on them: a market entry closes its order at once.
            guarded = state.ctx.paper.protections()
            conditional = state.ctx.paper.open_conditional()
            wanted = {(order.get("market") or "spot", order["symbol"]) for order in resting}
            for symbol in guarded:
                wanted.add((_market_for_symbol(symbol).value, symbol))
            for item in conditional:
                wanted.add((item.get("market") or "spot", item["symbol"]))
            for market_value, symbol in sorted(wanted):
                try:
                    market = MarketType(market_value)
                except ValueError:
                    continue
                try:
                    with state.locks[market]:
                        raw = state.ctx.market_rest_for(market).fetch_ticker(market, symbol)
                    price = float(raw.get("last") or 0.0)
                except Exception as exc:
                    log.debug("纸面撮合取价失败 %s: %s", symbol, exc)
                    continue
                if price <= 0:
                    continue
                for order in state.ctx.trading.settle_paper_orders(symbol, price):
                    if order.get("protection"):
                        log.info(
                            "纸面%s触发 %s %s @ %s",
                            "止损" if order["protection"] == "stop_loss" else "止盈",
                            symbol, format_amount(order["amount"]), order.get("average"),
                        )
                    else:
                        log.info(
                            "纸面限价成交 %s %s %s @ %s",
                            symbol, order["side"], format_amount(order["amount"]), order["price"],
                        )
        except Exception:  # keep the loop alive whatever one cycle does
            log.exception("纸面撮合循环异常")
