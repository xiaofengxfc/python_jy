"""FastAPI backend for the local web UI.

The desktop app and this server share one engine: everything under
``core`` / ``exchange`` / ``engine`` is Qt-free, so this module is a thin HTTP
adapter over :class:`btrader.context.AppContext`.

This is a development tool for iterating on the interface quickly. It binds to
localhost, trades paper money only, and is not a deployment target.
"""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..context import AppContext
from ..core.types import TIMEFRAME_SECONDS, MarketType, RunMode
from ..engine.trading import OrderRejected, OrderRequest
from ..exchange.symbols import load_symbols

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
                infos = load_symbols(state.ctx.rest_for(market), market)
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


# ----------------------------------------------------------------------- app
def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # Warm the symbol list in the background: the first paint must not wait
        # on a multi-second load_markets() call.
        threading.Thread(target=_warm, daemon=True).start()
        try:
            yield
        finally:
            try:
                get_state().ctx.shutdown()
            except Exception:  # pragma: no cover - best effort on exit
                log.debug("关闭上下文失败", exc_info=True)

    app = FastAPI(title="biTrader Web", lifespan=lifespan)

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
    ) -> dict[str, Any]:
        state = get_state()
        mt = _market(market)
        if timeframe not in TIMEFRAME_SECONDS:
            raise HTTPException(status_code=400, detail=f"不支持的周期：{timeframe}")
        limit = max(20, min(int(limit), 1000))
        try:
            with state.locks[mt]:
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
                raw = state.ctx.rest_for(mt).fetch_ticker(mt, symbol)
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
                book = state.ctx.rest_for(mt).fetch_order_book(mt, symbol, limit)
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

        reference = payload.price if payload.order_type == "limit" else None
        if reference is None:
            try:
                with state.locks[mt]:
                    reference = float(
                        state.ctx.rest_for(mt).fetch_ticker(mt, payload.symbol).get("last")
                        or 0.0
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

        request = OrderRequest(
            symbol=payload.symbol,
            side=payload.side,
            order_type=payload.order_type,
            amount=amount,
            price=reference,
            market_type=mt,
            mode=RunMode.PAPER,
        )
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
        }

    @app.post("/api/paper/reset")
    def api_reset_paper(balance: float | None = None) -> dict[str, Any]:
        state = get_state()
        with state.account_lock:
            state.ctx.paper.reset(balance)
        return {"ok": True, "cash": state.ctx.paper.account.cash}

    return app


def _warm() -> None:
    try:
        state = get_state()
        _symbols_for(state, state.ctx.market_type)
    except Exception:  # pragma: no cover - warm-up is best effort
        log.debug("预热交易对失败", exc_info=True)
