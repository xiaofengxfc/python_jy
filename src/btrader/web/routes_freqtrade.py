"""HTTP bridge for the Freqtrade sidecar.

The browser never talks to Freqtrade directly: the local API uses generated
credentials, and exposing it to the renderer would leak them. These endpoints
are a narrow, typed facade over `btrader.freqtrade`.
"""

from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..core.types import MarketType
from ..freqtrade import FreqtradeError
from ..freqtrade_compat import ensure_adapter


class StartPayload(BaseModel):
    strategy: str = ""
    dry_run: bool | None = None


class StrategyPayload(BaseModel):
    code: str = ""


class BacktestPayload(BaseModel):
    # Either a Freqtrade strategy file name, or ``local_key``: a biTrader
    # strategy that is first rendered into an IStrategy adapter.
    strategy: str = ""
    timerange: str = ""
    timeframe: str = ""
    pairs: list[str] | None = None
    local_key: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    market: str = "spot"


class RenamePayload(BaseModel):
    new_name: str


class DataPayload(BaseModel):
    pair: str = "BTC/USDT"
    timeframe: str = "1h"
    market: str = "spot"
    limit: int = 1000


def _market(value: str) -> MarketType:
    try:
        return MarketType(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"未知市场：{value}") from exc


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

    @router.post("/api/ft/strategies/{name}/rename")
    def rename_strategy(name: str, payload: RenamePayload) -> dict[str, Any]:
        try:
            return bridge().rename_strategy(name, payload.new_name)
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

    @router.get("/api/ft/history")
    def history(limit: int = 50) -> dict[str, Any]:
        try:
            count = max(1, min(int(limit), 200))
            items = bridge().client().request(
                "GET", f"/api/v1/trades?limit={count}"
            )
            if isinstance(items, dict):
                items = items.get("trades") or []
            return {"items": items or []}
        except FreqtradeError as exc:
            raise fail(exc) from exc

    @router.get("/api/ft/daily")
    def daily(days: int = 30) -> dict[str, Any]:
        try:
            count = max(1, min(int(days), 30))
            data = bridge().client().request(
                "GET", f"/api/v1/daily?timescale={count}"
            )
            if isinstance(data, dict):
                data = data.get("data") or data.get("daily") or []
            return {"items": data or []}
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
        item = bridge()
        try:
            name = payload.strategy
            adapter = None
            warnings: list[str] = []
            if payload.local_key:
                # A local strategy is a ``Strategy.generate()`` plugin, not an
                # IStrategy: render the shim, then run it through the same CLI
                # path every Freqtrade backtest uses.
                market = _market(payload.market)
                adapter = ensure_adapter(
                    item,
                    get_state().ctx.strategies,
                    payload.local_key,
                    params=payload.params,
                    timeframe=payload.timeframe or "1h",
                    # The sidecar config is spot-only, and Freqtrade refuses a
                    # can_short strategy outside futures mode. Saying so beats
                    # pretending the short leg was measured.
                    allow_short=False,
                )
                name = adapter["name"]
                if market.is_futures:
                    warnings.append(
                        "Freqtrade 目前以现货模式回测：策略的做空段会被忽略，"
                        "要做空请用 biTrader 引擎回测。"
                    )
            if not name:
                raise FreqtradeError("请选择一个策略")
            result = item.backtest(
                name,
                timerange=payload.timerange,
                timeframe=payload.timeframe,
                pairs=payload.pairs,
            )
        except FreqtradeError as exc:
            raise fail(exc) from exc
        except KeyError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if adapter is not None:
            result["local_key"] = payload.local_key
            result["adapter"] = adapter["name"]
            result["params"] = {**(result.get("params") or {}), **adapter["params"]}
        if warnings:
            result["warnings"] = warnings
        return result

    @router.get("/api/ft/backtest/status")
    def backtest_status() -> dict[str, Any]:
        try:
            return bridge().backtest_status()
        except FreqtradeError as exc:
            raise fail(exc) from exc

    return router
