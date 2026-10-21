"""HTTP bridge for the Freqtrade sidecar.

The browser never talks to Freqtrade directly: the local API uses generated
credentials, and exposing it to the renderer would leak them. These endpoints
are a narrow, typed facade over `btrader.freqtrade`.
"""

from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..freqtrade import FreqtradeError


class StartPayload(BaseModel):
    strategy: str = ""
    dry_run: bool | None = None


class StrategyPayload(BaseModel):
    code: str = ""


class BacktestPayload(BaseModel):
    strategy: str
    timerange: str = ""
    timeframe: str = ""
    pairs: list[str] | None = None


class DataPayload(BaseModel):
    pair: str = "BTC/USDT"
    timeframe: str = "1h"
    market: str = "spot"
    limit: int = 1000


def build_freqtrade_router(get_state: Callable[[], Any]) -> APIRouter:
    router = APIRouter()

    def bridge():
        return get_state().ctx.freqtrade

    def fail(exc: Exception) -> HTTPException:
        return HTTPException(status_code=400, detail=str(exc))

    @router.get("/api/ft/status")
    def status() -> dict[str, Any]:
        return bridge().status()

    @router.post("/api/ft/start")
    def start(payload: StartPayload) -> dict[str, Any]:
        item = bridge()
        try:
            if payload.dry_run is not None:
                item.settings.dry_run = payload.dry_run
            if payload.strategy:
                item.settings.strategy = payload.strategy
            result = item.start(payload.strategy)
            get_state().ctx.config.save()
            return result
        except FreqtradeError as exc:
            raise fail(exc) from exc

    @router.post("/api/ft/stop")
    def stop() -> dict[str, Any]:
        try:
            return bridge().stop()
        except FreqtradeError as exc:
            raise fail(exc) from exc

    @router.get("/api/ft/strategies")
    def strategies() -> dict[str, Any]:
        return {"items": bridge().strategies()}

    @router.get("/api/ft/strategies/{name}/source")
    def strategy_source(name: str) -> dict[str, Any]:
        try:
            code = bridge().strategy_source(name)
            return {"name": name, "code": code}
        except FreqtradeError as exc:
            raise fail(exc) from exc

    @router.put("/api/ft/strategies/{name}")
    def save_strategy(name: str, payload: StrategyPayload) -> dict[str, Any]:
        try:
            return bridge().save_strategy(name, payload.code)
        except FreqtradeError as exc:
            raise fail(exc) from exc

    @router.get("/api/ft/profit")
    def profit() -> dict[str, Any]:
        try:
            return bridge().client().request("GET", "/api/v1/profit")
        except FreqtradeError as exc:
            raise fail(exc) from exc

    @router.get("/api/ft/trades")
    def trades() -> dict[str, Any]:
        try:
            return {"items": bridge().client().request("GET", "/api/v1/status")}
        except FreqtradeError as exc:
            raise fail(exc) from exc

    @router.post("/api/ft/forceexit")
    def forceexit(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            body = payload or {"tradeid": "all"}
            return bridge().client().request("POST", "/api/v1/forceexit", body)
        except FreqtradeError as exc:
            raise fail(exc) from exc

    @router.post("/api/ft/data")
    def sync_data(payload: DataPayload) -> dict[str, Any]:
        from ..core.types import MarketType

        ctx = get_state().ctx
        try:
            market = MarketType(payload.market)
            frame = ctx.klines.fetch(
                market,
                payload.pair,
                payload.timeframe,
                limit=max(60, min(5000, payload.limit)),
            )
            return bridge().write_ohlcv(payload.pair, payload.timeframe, frame)
        except Exception as exc:  # network + validation failures are user-facing
            raise fail(exc) from exc

    @router.post("/api/ft/backtest")
    def backtest(payload: BacktestPayload) -> dict[str, Any]:
        try:
            return bridge().backtest(
                payload.strategy,
                timerange=payload.timerange,
                timeframe=payload.timeframe,
                pairs=payload.pairs,
            )
        except FreqtradeError as exc:
            raise fail(exc) from exc

    @router.get("/api/ft/backtest/status")
    def backtest_status() -> dict[str, Any]:
        try:
            return bridge().backtest_status()
        except FreqtradeError as exc:
            raise fail(exc) from exc

    return router
