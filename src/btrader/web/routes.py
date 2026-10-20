"""API routes beyond market data: portfolio, settings, credentials, strategy,
automated trading and backtesting.

Kept out of :mod:`btrader.web.app` so that module stays a thin composition
root. ``build_router`` takes the state factory as an argument (instead of
importing it) to keep the dependency one-way and avoid a circular import.
"""

from __future__ import annotations

import logging
import json
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, replace
from datetime import datetime
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field

import pandas as pd

from ..backtest import BacktestConfig, grid_search, run_backtest
from ..core.config import RiskSettings
from ..core.credentials import Credentials
from ..core.types import TIMEFRAME_SECONDS, MarketType, Network, RunMode, timeframe_seconds
from ..engine.auto import AutoTrader, AutoTraderConfig
from ..exchange import mirror
from ..exchange.rest import BinanceREST
from ..installer import InstallOptions, restart_gateway_process
from ..notify import WeChatNotifier
from .. import ai, llm
from .. import paths

log = logging.getLogger(__name__)

# Backtest jobs are short-lived; keep the newest N so a long session cannot
# grow the process without bound.
MAX_JOBS = 40
# A repeating loop error must not become a stream of push messages.
ERROR_NOTIFY_INTERVAL = 600.0


def _notify_task(ctx, title: str, lines: list[str] | None = None) -> None:
    """Push a task event (启动/结束/失败) to WeChat. Never raises.

    A task notification must not be able to fail the task itself: the gateway
    can be down, the CLI can be missing, and the backtest still has to finish.
    """
    try:
        ctx.notifier.notify(WeChatNotifier.format_task(title, lines), kind="task")
    except Exception:  # pragma: no cover - defensive
        log.debug("任务通知发送失败", exc_info=True)


def _market(value: str) -> MarketType:
    try:
        return MarketType(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"未知市场：{value}") from exc


def _explain_async(ctx, events, *, kind: str, detail: str, context: str = "") -> None:
    """Ask the model what probably happened, in a thread, into the event log.

    Deliberately fire-and-forget: the alert has already been sent, so an
    explanation is a bonus, and waiting for it would delay whatever raised the
    alert. Silently does nothing when the assistant is off.
    """
    if not ctx.settings.ai.enabled:
        return

    def run() -> None:
        result = ai.explain_anomaly(ctx.ai, kind=kind, detail=detail, context=context)
        if result["ok"] and result["text"]:
            events.add("ai", "AI", result["text"][:600])

    threading.Thread(target=run, name="ai-explain", daemon=True).start()


def _network(value: str | None) -> Network | None:
    if value is None:
        return None
    try:
        return Network(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"未知网络：{value}") from exc


def _number(value: Any) -> float | None:
    """JSON-safe float: NaN/inf are not valid JSON."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


# -------------------------------------------------------------------- payloads
class SettingsPayload(BaseModel):
    network: str | None = None
    market_type: str | None = None
    last_symbol: str | None = None
    timeframe: str | None = None
    default_order_quote: float | None = Field(default=None, gt=0)
    kline_limit: int | None = Field(default=None, ge=100, le=1000)
    proxy: str | None = None
    theme: str | None = None
    notification_sound: bool | None = None
    risk: dict[str, Any] | None = None
    notify: dict[str, Any] | None = None
    ai: dict[str, Any] | None = None


class NotifyTestPayload(BaseModel):
    """One-off message body for the settings page's “测试” button.

    The optional overrides let the page verify a channel/target/path before it
    saves them; an empty field keeps the saved value.
    """

    message: str = ""
    channel: str = ""
    target: str = ""
    account: str = ""
    command: str = ""


class InstallPayload(BaseModel):
    """Options for installing OpenClaw from the settings page.

    ``with_wechat`` is the only switch with a real consequence, and it is on by
    default: a gateway without the plugin cannot deliver the messages this
    feature exists for.
    """

    with_wechat: bool = True
    registry: str = ""
    node_path: str = ""
    npm_path: str = ""


class CredentialsPayload(BaseModel):
    market: str = "spot"
    network: str | None = None
    api_key: str = ""
    api_secret: str = ""
    verify: bool = True


class AIKeyPayload(BaseModel):
    key: str = ""


class AIAnalyzePayload(BaseModel):
    symbol: str
    market: str = "spot"
    timeframes: list[str] = Field(default_factory=lambda: ["15m", "1h", "4h"])
    bars: int = Field(default=200, ge=50, le=1000)


class AINewsPayload(BaseModel):
    symbol: str = ""
    limit: int = Field(default=15, ge=3, le=40)


class AIReviewPayload(BaseModel):
    symbol: str
    market: str = "spot"
    timeframe: str = "1h"
    mode: str = "paper"


class AIDescribePayload(BaseModel):
    strategy: str
    params: dict[str, Any] = Field(default_factory=dict)


class AISuggestPayload(BaseModel):
    idea: str
    symbol: str = ""
    market: str = "spot"
    timeframe: str = "1h"


class AIPrejudgePayload(BaseModel):
    symbol: str
    market: str = "spot"
    timeframe: str = "1h"
    side: str = "buy"
    quote: float = Field(default=0.0, ge=0)


class AutoStartPayload(BaseModel):
    symbol: str
    market: str = "spot"
    timeframe: str = "15m"
    strategy: str
    params: dict[str, Any] = Field(default_factory=dict)
    position_pct: float = Field(default=1.0, gt=0, le=1)
    leverage: float = Field(default=1.0, ge=1.0, le=125.0)
    poll_seconds: float = Field(default=10.0, ge=1.0)
    mode: str = "paper"
    confirm_live: bool = False


class BacktestPayload(BaseModel):
    market: str = "spot"
    symbol: str
    timeframe: str = "1h"
    strategy: str
    params: dict[str, Any] = Field(default_factory=dict)
    bars: int = Field(default=800, ge=60, le=5000)
    initial_capital: float = Field(default=10_000.0, gt=0)
    fee_rate: float = Field(default=0.0004, ge=0, le=0.01)
    slippage_bps: float = Field(default=1.0, ge=0, le=100)
    position_pct: float = Field(default=1.0, gt=0, le=1)
    leverage: float = Field(default=1.0, ge=1.0, le=125.0)
    allow_short: bool = True
    stop_loss_pct: float = Field(default=0.0, ge=0, le=1)
    take_profit_pct: float = Field(default=0.0, ge=0, le=1)
    save: bool = True


class OptimizePayload(BacktestPayload):
    grid: dict[str, list[Any]] = Field(default_factory=dict)
    metric: str = "sharpe"
    top_n: int = Field(default=30, ge=1, le=200)


class AIFilterPayload(BacktestPayload):
    """A backtest plus the cap on how many entries the model may review.

    Every review is one paid call, so the cap is part of the request rather than
    a hidden constant: the page shows the number it is about to spend.
    """

    limit: int = Field(default=40, ge=1, le=200)


# ------------------------------------------------------------------ event log
class EventLog:
    """Bounded, thread-safe feed for the automated-trading panel."""

    def __init__(self, capacity: int = 500) -> None:
        self._items: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._seq = 0

    def add(self, kind: str, level: str, message: str) -> None:
        with self._lock:
            self._seq += 1
            self._items.append(
                {
                    "seq": self._seq,
                    "kind": kind,
                    "level": level,
                    "message": message,
                    "when": datetime.now().strftime("%H:%M:%S"),
                }
            )

    def since(self, seq: int) -> tuple[list[dict[str, Any]], int]:
        with self._lock:
            return [item for item in self._items if item["seq"] > seq], self._seq


# ---------------------------------------------------------------- auto trading
class AutoController:
    """Single-slot owner of the automated trader."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._trader: AutoTrader | None = None
        self.events = EventLog()
        self.last_signal: dict[str, Any] | None = None
        self.config: dict[str, Any] | None = None
        # Loop errors repeat every poll; the phone should hear about a broken
        # strategy once, not once per tick.
        self._error_notified_at = 0.0
        # AI briefings run here, never on the trading thread: a model that takes
        # 20 s must not delay the next candle check.
        self.briefs = ai.BriefQueue()

    @property
    def running(self) -> bool:
        with self._lock:
            return self._trader is not None and self._trader.running

    def start(self, ctx, payload: AutoStartPayload) -> None:
        if payload.mode not in ("paper", "live"):
            raise HTTPException(status_code=400, detail="模式必须是 paper 或 live")
        mode = RunMode(payload.mode)
        market = _market(payload.market)

        if mode is RunMode.LIVE:
            if ctx.network is not Network.MAINNET:
                raise HTTPException(
                    status_code=400, detail="测试网不支持实盘自动交易，请切换到主网"
                )
            if not ctx.rest_for(market).has_credentials:
                raise HTTPException(status_code=400, detail="实盘自动交易需要配置 API Key")
            if not payload.confirm_live:
                raise HTTPException(
                    status_code=400, detail="实盘自动交易需要显式确认（confirm_live=true）"
                )

        try:
            strategy = ctx.strategies.get(payload.strategy)
        except KeyError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        params = strategy.validate_params(payload.params)

        with self._lock:
            if self._trader is not None and self._trader.running:
                raise HTTPException(status_code=409, detail="自动交易已在运行")

            if ctx.market_type is not market:
                ctx.set_market_type(market)

            config = AutoTraderConfig(
                symbol=payload.symbol,
                market_type=market,
                timeframe=payload.timeframe,
                mode=mode,
                strategy_key=payload.strategy,
                params=params,
                position_pct=payload.position_pct,
                leverage=payload.leverage if market.is_futures else 1.0,
                poll_seconds=payload.poll_seconds,
            )
            trader = AutoTrader(config, strategy, ctx.trading, ctx.klines)
            trader.on_log(lambda level, message: self._on_log(ctx, level, message))
            trader.on_signal(self._on_signal)
            trader.on_state(lambda running: self._on_state(ctx, running))
            trader.on_candle(lambda bars, target, changed, price: self._on_candle(
                ctx, config, bars, target, changed, price
            ))
            self._trader = trader
            self.config = {
                "symbol": config.symbol,
                "market": market.value,
                "timeframe": config.timeframe,
                "strategy": config.strategy_key,
                "params": params,
                "mode": mode.value,
                "position_pct": config.position_pct,
                "leverage": config.leverage,
            }
            self.last_signal = None
            trader.start()

    def _on_signal(self, target: float, reason: str) -> None:
        self.last_signal = {
            "target": target,
            "reason": reason,
            "when": datetime.now().strftime("%H:%M:%S"),
        }
        self.events.add("signal", "SIGNAL", f"目标仓位 {target:+.0%} · {reason}")

    def stop(self) -> None:
        with self._lock:
            trader = self._trader
        if trader is not None and trader.running:
            trader.stop()

    def status(self, since: int = 0) -> dict[str, Any]:
        items, seq = self.events.since(since)
        return {
            "running": self.running,
            "config": self.config,
            "last_signal": self.last_signal,
            "seq": seq,
            "events": items,
        }

    # ------------------------------------------------------------- notifications
    def _on_state(self, ctx, running: bool) -> None:
        """自动交易启停：写事件日志，同时推送手机。"""
        self.events.add(
            "state", "INFO", "自动交易已启动" if running else "自动交易已停止"
        )
        config = self.config or {}
        label = (
            f"{config.get('symbol', '')} {config.get('timeframe', '')}"
            f" · {config.get('strategy', '')}"
        ).strip()
        mode = "实盘" if config.get("mode") == "live" else "纸面"
        _notify_task(
            ctx,
            "自动交易已启动" if running else "自动交易已停止",
            [label, f"模式：{mode}"],
        )
        if not running:
            self._error_notified_at = 0.0

    def _on_log(self, ctx, level: str, message: str) -> None:
        """循环异常写日志；同一条错误只推一次，避免刷屏。"""
        self.events.add("log", level, message)
        if level != "ERROR":
            return
        now = time.time()
        if now - self._error_notified_at < ERROR_NOTIFY_INTERVAL:
            return
        self._error_notified_at = now
        _notify_task(ctx, "自动交易异常", [message[:300]])
        # ...and let the model explain it once, next to the alert.
        _explain_async(ctx, self.events, kind="自动交易循环异常", detail=message)

    def _on_candle(self, ctx, config, bars, target: float, changed: bool, price: float) -> None:
        """One AI briefing per closed candle, off the trading thread.

        ``submit`` returns immediately: the queue owns its own worker, so a slow
        model delays a message, never the next candle check.
        """
        settings = ctx.settings.ai
        if not settings.enabled or not settings.auto_brief:
            return

        def run() -> None:
            result = ai.briefing(
                ctx.ai,
                symbol=config.symbol,
                timeframe=config.timeframe,
                frame=bars,
                target=target,
                target_delta=changed,
                mode=config.mode.label,
            )
            if not result["ok"]:
                log.debug("AI 简报失败：%s", result.get("error"))
                return
            self.events.add("ai", "AI", result["text"][:600])
            safe = target >= 0
            _notify_task(
                ctx,
                "AI 收盘简报",
                [f"{config.symbol} {config.timeframe} · 收盘价 {price:,.4f}"]
                + [line for line in result["text"].splitlines() if line.strip()][:6],
            )
            _ = safe

        if not self.briefs.submit({"run": run}):
            log.debug("AI 简报队列已满，跳过这一根")


# ------------------------------------------------------------------ backtests
class JobStore:
    """Tiny in-memory job table for backtest / optimisation runs."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._order: deque[str] = deque()

    def create(self, kind: str) -> str:
        job_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._jobs[job_id] = {
                "id": job_id,
                "kind": kind,
                "status": "running",
                "progress": 0.0,
                "started": time.time(),
                "result": None,
                "error": None,
            }
            self._order.append(job_id)
            while len(self._order) > MAX_JOBS:
                self._jobs.pop(self._order.popleft(), None)
        return job_id

    def update(self, job_id: str, **fields: Any) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job.update(fields)

    def get(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise HTTPException(status_code=404, detail="任务不存在或已过期")
            return dict(job)


def _metric_text(
    metrics: dict, key: str, unit: str = "", *, signed: bool = True
) -> str:
    """Format one backtest metric for a phone screen: short and honest.

    A missing metric prints ``—`` rather than ``0``: "回测完成 · 收益 0%" is a
    claim, while "—" says the run had nothing to report.
    """
    try:
        value = float(metrics.get(key))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "—"
    if unit == "%":
        return f"{value * 100:+.2f}%" if signed else f"{value * 100:.2f}%"
    return f"{value:g}{unit}"


def _serialize_result(result, params: dict[str, Any], limit_points: int = 1200) -> dict:
    equity = result.equity
    if len(equity) > limit_points:
        step = max(1, len(equity) // limit_points)
        equity = equity.iloc[::step]
    points = [
        {
            "t": int(row.timestamp),
            "equity": _number(row.equity),
            "drawdown": _number(row.drawdown),
            "position": _number(row.position),
            "target": _number(row.target),
            "close": _number(row.close),
        }
        for row in equity.itertuples(index=False)
    ]
    trades = [trade.as_dict() for trade in result.trades[-500:]]
    return {
        "metrics": {key: _number(value) for key, value in result.metrics.items()},
        "params": params,
        "equity": points,
        "trades": trades,
    }


def _install_options(payload: InstallPayload) -> InstallOptions:
    """Translate the request body into installer options.

    Kept apart from the routes so the mapping (and its empty-string handling) is
    testable without an HTTP client.
    """
    return InstallOptions(
        with_wechat=bool(payload.with_wechat),
        registry=(payload.registry or "").strip(),
        node_path=(payload.node_path or "").strip(),
        npm_path=(payload.npm_path or "").strip(),
    )


def build_router(get_state: Callable[[], Any]) -> APIRouter:
    router = APIRouter()
    auto = AutoController()
    jobs = JobStore()

    # ------------------------------------------------------------ portfolio
    @router.get("/api/portfolio")
    def portfolio() -> dict[str, Any]:
        state = get_state()
        ctx = state.ctx
        market = ctx.market_type
        with state.account_lock:
            account = ctx.paper.account
            positions = []
            for position in account.positions.values():
                if abs(position.contracts) <= 1e-12:
                    continue
                last = _last_price(state, market, position.symbol) or position.entry_price
                positions.append(
                    {
                        "symbol": position.symbol,
                        "contracts": position.contracts,
                        "side": position.side,
                        "entry_price": position.entry_price,
                        "last": last,
                        "unrealized": (last - position.entry_price) * position.contracts,
                        "quote_value": last * position.contracts,
                        "realized_pnl": position.realized_pnl,
                        "fees": position.fees,
                    }
                )
            holdings_value = sum(item["quote_value"] for item in positions)
            equity = account.cash + holdings_value
            # Sample the equity curve at most every 20s: the panel polls every
            # 4s and a point per poll would make the chart unreadable.
            now = time.time()
            if now - state.equity_stamp >= 20:
                state.equity_stamp = now
                if not state.equity_history:
                    state.equity_history.append(
                        {
                            "t": int((now - 1) * 1000),
                            "equity": float(account.starting_balance),
                        }
                    )
                state.equity_history.append({"t": int(now * 1000), "equity": float(equity)})
                del state.equity_history[:-720]
            trades = [dict(trade) for trade in account.trades[-100:]][::-1]
            # Full order history, newest first: the drawer shows open orders,
            # the portfolio page shows everything including fills and cancels.
            orders = [dict(order) for order in account.orders[-200:]][::-1]
            return {
                "market": market.value,
                "quote_asset": account.quote_asset,
                "cash": account.cash,
                "starting_balance": account.starting_balance,
                "equity": equity,
                "realized_pnl": sum(p.realized_pnl for p in account.positions.values()),
                "positions": positions,
                "orders": orders,
                "trades": trades,
                "equity_history": list(state.equity_history),
            }

    # ------------------------------------------------------------- settings
    @router.get("/api/settings")
    def get_settings() -> dict[str, Any]:
        ctx = get_state().ctx
        return {
            "settings": ctx.settings.to_dict(),
            "market": ctx.market_type.value,
            "network": ctx.network.value,
            "has_credentials": ctx.rest.has_credentials,
            "data_dir": str(paths.data_dir()),
        }

    @router.put("/api/settings")
    def update_settings(payload: SettingsPayload) -> dict[str, Any]:
        state = get_state()
        ctx = state.ctx

        target_network = _network(payload.network)
        if target_network is not None and target_network is not ctx.network:
            ctx.set_network(target_network)

        target_market = None
        if payload.market_type:
            try:
                target_market = MarketType(payload.market_type)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=f"未知市场：{payload.market_type}") from exc
        if target_market is not None and target_market is not ctx.market_type:
            ctx.set_market_type(target_market)

        settings = ctx.settings
        if payload.last_symbol is not None:
            settings.last_symbol = payload.last_symbol
        if payload.timeframe is not None:
            if payload.timeframe not in TIMEFRAME_SECONDS:
                raise HTTPException(status_code=400, detail=f"不支持的周期：{payload.timeframe}")
            settings.timeframe = payload.timeframe
        if payload.default_order_quote is not None:
            settings.default_order_quote = payload.default_order_quote
        if payload.kline_limit is not None:
            settings.kline_limit = payload.kline_limit
        if payload.proxy is not None:
            # Rebuilds the clients and drops their caches; ccxt reads
            # ``proxies`` once, so an old client would keep going direct.
            ctx.apply_proxy(payload.proxy)
        if payload.theme in ("light", "dark"):
            settings.theme = payload.theme
        if payload.notification_sound is not None:
            settings.notification_sound = payload.notification_sound
        if payload.risk is not None:
            _apply_risk(settings.risk, payload.risk)
        if payload.notify is not None:
            # Validate on a scratch copy first: rejecting the request halfway
            # through must not leave the live object half-updated.
            candidate = replace(settings.notify)
            _apply_notify(candidate, payload.notify)
            settings.notify = candidate
        if payload.ai is not None:
            candidate_ai = replace(settings.ai)
            _apply_ai(candidate_ai, payload.ai)
            settings.ai = candidate_ai

        ctx.save_settings()
        return {"ok": True, "settings": settings.to_dict()}

    # ---------------------------------------------------------------- risk
    @router.get("/api/risk")
    def risk_state() -> dict[str, Any]:
        """Live breaker state, so the UI can show why orders are refused."""
        ctx = get_state().ctx
        return ctx.trading.risk.snapshot()

    @router.post("/api/risk/resume")
    def risk_resume() -> dict[str, Any]:
        """Acknowledge a halt and resume with a fresh daily allowance.

        Rejected while the global kill switch is on: that switch has its own
        off button and mixing the two would hide which one was the cause.
        """
        ctx = get_state().ctx
        risk = ctx.trading.risk
        if risk.settings.kill_switch:
            raise HTTPException(
                status_code=400, detail="全局急停已开启，请先在设置中关闭急停。"
            )
        risk.clear_halt()
        log.warning("风控熔断已手动解除，日内基准重置")
        return risk.snapshot()

    # --------------------------------------------------------------- ai
    @router.get("/api/ai/status")
    def ai_status() -> dict[str, Any]:
        """Provider, model, whether a key is set, and today's usage."""
        return get_state().ctx.ai.status()

    @router.post("/api/ai/key")
    def ai_set_key(payload: AIKeyPayload) -> dict[str, Any]:
        """Store the API key in the OS keyring, never in ``config.json``."""
        ctx = get_state().ctx
        if not ctx.ai.set_key(payload.key):
            raise HTTPException(
                status_code=400, detail="写入系统密钥环失败，请检查系统凭据服务是否可用。"
            )
        return ctx.ai.status()

    @router.post("/api/ai/test")
    def ai_test() -> dict[str, Any]:
        """One tiny round trip, so "配置对不对" is answered before it matters."""
        ctx = get_state().ctx
        result = ctx.ai.ask(
            "只回复两个字：可用", system="你是一个连通性测试，只回复用户要求的内容。", kind="test"
        )
        if not result["ok"]:
            raise HTTPException(status_code=400, detail=result["error"])
        return result

    @router.post("/api/ai/analyze")
    def ai_analyze(payload: AIAnalyzePayload) -> dict[str, Any]:
        """Multi-timeframe read of one symbol, plus the levels the chart draws.

        The levels are range-checked against the same candles that were sent, so
        a line that appears on the chart is always inside the traded range.
        """
        ctx = get_state().ctx
        state = get_state()
        market = _market(payload.market)
        symbol = payload.symbol.strip()
        if not symbol:
            raise HTTPException(status_code=400, detail="请先选择交易对")
        timeframes = payload.timeframes or ["15m", "1h", "4h"]
        frames: dict[str, Any] = {}
        for timeframe in timeframes[:3]:
            if timeframe not in TIMEFRAME_SECONDS:
                raise HTTPException(status_code=400, detail=f"不支持的周期：{timeframe}")
            frame = _market_frame(state, ctx, market, symbol, timeframe, payload.bars)
            if frame is not None and not frame.empty:
                frames[timeframe] = _closed_only(frame, timeframe)
        if not frames:
            raise HTTPException(status_code=400, detail="没有取到 K 线，请检查交易对与周期")
        result = ai.analyze_klines(
            ctx.ai, symbol=symbol, frames=frames, market_label=market.label
        )
        if not result["ok"]:
            raise HTTPException(status_code=400, detail=result["error"])
        result["symbol"] = symbol
        result["market"] = market.value
        result["price"] = float(frames[next(iter(frames))]["close"].iloc[-1])
        return result

    @router.post("/api/ai/news")
    def ai_news(payload: AINewsPayload) -> dict[str, Any]:
        """Score the headlines the news panel already fetched."""
        ctx = get_state().ctx
        snapshot = ctx.news.snapshot(payload.symbol, limit=payload.limit)
        result = ai.analyze_news(
            ctx.ai, symbol=payload.symbol or "市场", items=snapshot.get("items") or []
        )
        if not result["ok"]:
            raise HTTPException(status_code=400, detail=result["error"])
        return result

    @router.post("/api/ai/review")
    def ai_review(payload: AIReviewPayload) -> dict[str, Any]:
        """Is the open position consistent with the risk settings?

        Reading the position is only done when the user allowed it: balances and
        P/L are private, and this endpoint is a manual click, not a background
        job.
        """
        ctx = get_state().ctx
        state = get_state()
        market = _market(payload.market)
        symbol = payload.symbol.strip()
        if not symbol:
            raise HTTPException(status_code=400, detail="请先选择交易对")
        if not ctx.settings.ai.share_position:
            raise HTTPException(
                status_code=400,
                detail="持仓与盈亏属于隐私数据：请先在设置里允许“把持仓发送给模型”。",
            )
        position: dict[str, Any] = {}
        try:
            account = ctx.trading.paper.account if payload.mode != "live" else None
            if account is not None:
                held = account.positions.get(symbol)
                if held:
                    position = {
                        "side": "long" if held.contracts >= 0 else "short",
                        "contracts": held.contracts,
                        "entry_price": held.entry_price,
                        "mark_price": held.entry_price,
                        "unrealized_pnl": 0.0,
                    }
        except Exception:  # a missing position is not an error
            log.debug("读取持仓失败", exc_info=True)
        frame = None
        try:
            frame = _closed_only(
                _market_frame(state, ctx, market, symbol, payload.timeframe, 200),
                payload.timeframe,
            )
        except HTTPException:
            frame = None
        result = ai.review_position(
            ctx.ai,
            symbol=symbol,
            position=position,
            risk=ctx.trading.risk.snapshot(),
            frame=frame,
            timeframe=payload.timeframe,
        )
        if not result["ok"]:
            raise HTTPException(status_code=400, detail=result["error"])
        return result

    @router.post("/api/ai/prejudge")
    def ai_prejudge(payload: AIPrejudgePayload) -> dict[str, Any]:
        """A second opinion on one order, cached per candle and size bucket.

        Never in the order path: the ticket has already been filled in when this
        is clicked, and the order goes out whether or not the model answers.
        """
        ctx = get_state().ctx
        state = get_state()
        market = _market(payload.market)
        symbol = payload.symbol.strip()
        if not symbol:
            raise HTTPException(status_code=400, detail="请先选择交易对")
        frame = _closed_only(
            _market_frame(state, ctx, market, symbol, payload.timeframe, 200),
            payload.timeframe,
        )
        if frame is None or frame.empty:
            raise HTTPException(status_code=400, detail="没有取到 K 线，请检查交易对与周期")
        note = ""
        if ctx.settings.ai.share_position:
            held = getattr(ctx.trading.paper.account, "positions", {})
            item = held.get(symbol) if isinstance(held, dict) else None
            if item:
                note = (
                    f"当前持有 {getattr(item, 'contracts', 0):.6f} 份，"
                    f"开仓价 {getattr(item, 'entry_price', 0):,.4f}。"
                )
        result = ai.prejudge_order(
            ctx.ai,
            symbol=symbol,
            timeframe=payload.timeframe,
            side=payload.side,
            quote=payload.quote,
            frame=frame,
            position_note=note,
        )
        if not result["ok"]:
            raise HTTPException(status_code=400, detail=result["error"])
        return result

    @router.get("/api/ai/audit")
    def ai_audit(limit: int = 50) -> dict[str, Any]:
        """The audit trail, newest first. Backs the 命中率 panel."""
        return {"items": llm.audit_tail(max(1, min(limit, 200)))}

    @router.post("/api/ai/describe")
    def ai_describe(payload: AIDescribePayload) -> dict[str, Any]:
        """Explain one strategy's code: when it earns, when it bleeds."""
        ctx = get_state().ctx
        try:
            strategy = ctx.strategies.get(payload.strategy)
        except KeyError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        params = strategy.validate_params(payload.params)
        source = _strategy_source(strategy)
        result = ai.describe_strategy(
            ctx.ai,
            key=strategy.key,
            name=strategy.meta.name,
            description=strategy.meta.description,
            source=source,
            params=params,
        )
        if not result["ok"]:
            raise HTTPException(status_code=400, detail=result["error"])
        return result

    @router.post("/api/ai/suggest")
    def ai_suggest(payload: AISuggestPayload) -> dict[str, Any]:
        """Turn a plain-language idea into a strategy key + params.

        Never code: the reply is validated against the strategies that already
        exist, so nothing new is imported into this process.
        """
        ctx = get_state().ctx
        state = get_state()
        idea = payload.idea.strip()
        if not idea:
            raise HTTPException(status_code=400, detail="请先描述你的想法")
        market = _market(payload.market)
        frames: dict[str, Any] = {}
        if payload.symbol:
            try:
                frame = _market_frame(
                    state, ctx, market, payload.symbol, payload.timeframe, 200
                )
                frames[payload.timeframe] = _closed_only(frame, payload.timeframe)
            except HTTPException:
                frames = {}
        catalogue = ctx.strategies.meta_list()
        # The route serves ``params`` (the JSON-friendly form) while the registry
        # hands back dataclasses; normalise to the shape the prompt expects.
        for item in catalogue:
            item["params_spec"] = [asdict(spec) for spec in item.get("params_spec") or []]
        result = ai.suggest_strategy(
            ctx.ai, idea=idea, strategies=catalogue, frames=frames
        )
        if not result["ok"]:
            raise HTTPException(status_code=400, detail=result["error"])
        # Coerce the model's numbers through the strategy's own spec so an
        # out-of-range value is clamped rather than silently applied.
        try:
            strategy = ctx.strategies.get(result["strategy"])
            result["params"] = strategy.validate_params(result["params"])
        except KeyError:
            pass
        return result

    # ------------------------------------------------------- wechat notify
    @router.get("/api/notify")
    def notify_status() -> dict[str, Any]:
        """Whether the OpenClaw CLI can be found, plus the last outcome.

        The settings page shows this instead of claiming success: a missing CLI
        or an empty target is the ordinary failure, and the user needs the real
        reason to fix it.
        """
        return get_state().ctx.notifier.status()

    @router.post("/api/notify/test")
    def notify_test(payload: NotifyTestPayload) -> dict[str, Any]:
        """Send one message synchronously so the user sees the real error.

        The body may override channel/target/command for this single call, so
        the user can try a value before saving it. Overrides are applied to a
        copy — the saved settings are never mutated by a test.
        """
        ctx = get_state().ctx
        notifier = ctx.notifier
        text = payload.message.strip() or (
            "biTrader 企业微信通知测试：如果你收到这条消息，说明推送已打通。"
        )
        candidate = replace(
            notifier.settings,
            channel=(payload.channel or "").strip() or notifier.settings.channel,
            target=(payload.target or "").strip() or notifier.settings.target,
            account=(payload.account or "").strip() or notifier.settings.account,
            command=(payload.command or "").strip() or notifier.settings.command,
        )
        scratch = WeChatNotifier(
            candidate, timeout=notifier.timeout, queue_size=1
        )
        try:
            result = scratch.send_now(text)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # A successful test is also a success for the live notifier's status.
        notifier.last_error = ""
        notifier.last_error_at = 0.0
        notifier.last_success_at = scratch.last_success_at
        return result

    # ------------------------------------------------------ openclaw install
    @router.get("/api/install/openclaw")
    def install_status() -> dict[str, Any]:
        """Node/npm availability plus the state and log of the last install.

        The page polls this while a run is in flight; the log tail is part of
        the same payload so a slow npm cannot show up as a frozen button with no
        explanation.
        """
        return get_state().ctx.installer.status()

    @router.post("/api/install/openclaw/plan")
    def install_plan(payload: InstallPayload) -> dict[str, Any]:
        """Preview the exact command, so nothing runs unseen."""
        ctx = get_state().ctx
        options = _install_options(payload)
        try:
            argv = ctx.installer.plan(options)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"command": argv, "options": asdict(options)}

    @router.post("/api/install/openclaw")
    def install_start(payload: InstallPayload) -> dict[str, Any]:
        """Start ``npm install --global ...`` in the background.

        Installing software is the one action on this page that changes the
        machine, so it is a deliberate, separate call — never a side effect of
        saving settings or of a status poll.
        """
        ctx = get_state().ctx
        options = _install_options(payload)
        try:
            return ctx.installer.start(options)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/api/install/openclaw/cancel")
    def install_cancel() -> dict[str, Any]:
        """Stop the running install and the npm tree it spawned."""
        return get_state().ctx.installer.cancel()

    @router.get("/api/install/openclaw/channel")
    def channel_status() -> dict[str, Any]:
        """Whether the gateway knows the 企业微信 channel and is connected.

        Read from the gateway rather than inferred from files on disk, because a
        plugin can be installed and still be disabled or unconfigured. The
        gateway's own process state rides along: the long connection lives in
        that process, so a channel is useless when the gateway is down and the
        page has to say which of the two is wrong.
        """
        ctx = get_state().ctx
        status = ctx.installer.channel_status()
        status["gateway"] = ctx.gateway.status()
        return status

    @router.post("/api/install/openclaw/channel")
    def channel_register() -> dict[str, Any]:
        """Register the 企业微信 plugin with the gateway, streamed to the log."""
        try:
            return get_state().ctx.installer.register_channel()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/api/install/openclaw/gateway")
    def gateway_start() -> dict[str, Any]:
        """Start the local gateway without restarting the app."""
        return get_state().ctx.gateway.start()

    @router.post("/api/install/openclaw/gateway/restart")
    def gateway_restart() -> dict[str, Any]:
        """Restart the gateway process.

        A channel session lives inside that process: after a scan or an unbind,
        the running gateway still holds the state it booted with, and only a
        restart makes the new binding (or its removal) take effect.

        A gateway this app did not spawn is not ours to kill, so it is restarted
        through the CLI instead; otherwise the button would quietly do nothing
        on exactly the machine where it matters.
        """
        ctx = get_state().ctx
        if ctx.gateway.status().get("managed"):
            ctx.gateway.stop()
            return ctx.gateway.start()
        code, output = restart_gateway_process()
        status = ctx.gateway.status()
        if not status.get("reachable"):
            # The CLI can refuse — "listener changed before restart" is what it
            # says when its own bookkeeping is stale — so fall back to launching
            # a gateway ourselves instead of leaving push dead.
            status = ctx.gateway.start()
        if not status.get("reachable"):
            status = {
                **status,
                "error": output.strip()[-300:] or f"openclaw 返回退出码 {code}",
            }
        return status

    # ------------------------------------------------------------------ news
    @router.get("/api/news")
    def news(symbol: str = "", limit: int = 20) -> dict[str, Any]:
        """Headlines for one pair from the keyless public feeds.

        Never raises for an upstream failure: the panel renders the message
        instead of an empty feed that looks like "no news".
        """
        ctx = get_state().ctx
        return ctx.news.snapshot(symbol, limit=limit)

    # ---------------------------------------------------------- credentials
    @router.get("/api/credentials")
    def credentials(network: str | None = None) -> dict[str, Any]:
        """Which keys are configured, for one network.

        ``network`` must be honoured: the settings page has a 主网/测试网 switch,
        and reading it from ``ctx.network`` instead made the 主网 tab show the
        测试网 keys — the user then overwrote a real key believing they were
        looking at a different slot.
        """
        ctx = get_state().ctx
        network_obj = _network(network) or ctx.network
        items = []
        for market in MarketType:
            creds = ctx.credentials.load(network_obj, market)
            items.append(
                {
                    "market": market.value,
                    "label": market.label,
                    "configured": creds.is_set,
                    "masked": creds.masked_key,
                    "current": market is ctx.market_type and network_obj is ctx.network,
                }
            )
        return {"network": network_obj.value, "items": items}

    @router.post("/api/credentials")
    def save_credentials(payload: CredentialsPayload) -> dict[str, Any]:
        state = get_state()
        ctx = state.ctx
        market = _market(payload.market)
        network = _network(payload.network) or ctx.network
        key = payload.api_key.strip()
        secret = payload.api_secret.strip()
        if not key or not secret:
            raise HTTPException(status_code=400, detail="API Key 与 Secret 都不能为空")

        creds = Credentials(key, secret)
        if payload.verify:
            try:
                with state.locks[market]:
                    rest = BinanceREST(network, creds)
                    rest.fetch_balance(market)
            except Exception as exc:
                raise HTTPException(
                    status_code=400, detail=f"连接测试失败：{exc}"
                ) from exc

        if not ctx.credentials.save(network, market, creds):
            raise HTTPException(
                status_code=500,
                detail="无法写入系统凭据库（keyring），请检查系统钥匙串是否可用",
            )
        if network is ctx.network and market is ctx.market_type:
            ctx.creds = creds
            ctx.rest.credentials = creds
            ctx.rest.reset()
        return {"ok": True, "market": market.value, "network": network.value}

    @router.delete("/api/credentials")
    def delete_credentials(market: str = "spot", network: str | None = None) -> dict[str, Any]:
        ctx = get_state().ctx
        market_type = _market(market)
        target = _network(network) or ctx.network
        ctx.credentials.delete(target, market_type)
        if target is ctx.network and market_type is ctx.market_type:
            ctx.creds = Credentials()
            ctx.rest.credentials = ctx.creds
            ctx.rest.reset()
        return {"ok": True}

    # ----------------------------------------------------------- strategies
    @router.get("/api/strategies")
    def strategies() -> dict[str, Any]:
        ctx = get_state().ctx
        items = []
        for meta in ctx.strategies.meta_list():
            items.append(
                {
                    **{k: v for k, v in meta.items() if k != "params_spec"},
                    "params": [asdict(spec) for spec in meta["params_spec"]],
                    "defaults": ctx.strategies.get(meta["key"]).default_params(),
                }
            )
        return {"items": items}

    # ------------------------------------------------------- automated trade
    @router.get("/api/auto/status")
    def auto_status(since: int = 0) -> dict[str, Any]:
        return auto.status(since)

    @router.post("/api/auto/start")
    def auto_start(payload: AutoStartPayload) -> dict[str, Any]:
        auto.start(get_state().ctx, payload)
        return {"ok": True, "status": auto.status()}

    @router.post("/api/auto/stop")
    def auto_stop() -> dict[str, Any]:
        auto.stop()
        return {"ok": True, "status": auto.status()}

    # ------------------------------------------------------------- backtest
    @router.post("/api/backtest/run")
    def backtest_run(payload: BacktestPayload) -> dict[str, Any]:
        return _start_backtest(get_state(), jobs, payload)

    @router.post("/api/backtest/optimize")
    def backtest_optimize(payload: OptimizePayload) -> dict[str, Any]:
        return _start_optimize(get_state(), jobs, payload)

    @router.post("/api/backtest/ai-filter")
    def backtest_ai_filter(payload: AIFilterPayload) -> dict[str, Any]:
        """Replay a backtest's entries through the model and compare both runs.

        The backtest itself is re-run (it is deterministic and cheap) instead of
        caching frames between requests: that keeps this endpoint stateless, and
        the trades it judges are exactly the trades the original run produced.
        """
        return _start_ai_filter(get_state(), jobs, payload)

    @router.get("/api/backtest/jobs/{job_id}")
    def backtest_job(job_id: str) -> dict[str, Any]:
        return jobs.get(job_id)

    @router.get("/api/backtest/runs")
    def backtest_runs(limit: int = 30) -> dict[str, Any]:
        state = get_state()
        rows = state.ctx.db.recent_backtests(max(1, min(limit, 200)))
        items = []
        for row in rows:
            item = dict(row)
            # The table stores these as JSON text; the UI wants objects.
            for key in ("params", "metrics"):
                try:
                    item[key] = json.loads(item[key]) if item.get(key) else {}
                except (TypeError, ValueError):
                    item[key] = {}
            items.append(item)
        return {"items": items}

    return router


# ------------------------------------------------------------------- helpers
def _last_price(state, market: MarketType, symbol: str) -> float | None:
    try:
        with state.locks[market]:
            raw = state.ctx.market_rest_for(market).fetch_ticker(market, symbol)
        price = float(raw.get("last") or 0.0)
        return price or None
    except Exception:
        return None


def _apply_risk(risk: RiskSettings, values: dict[str, Any]) -> None:
    """Only fields the dataclass declares; unknown keys are ignored."""
    for key, value in values.items():
        if not hasattr(risk, key):
            log.debug("忽略未知风控字段: %s", key)
            continue
        current = getattr(risk, key)
        try:
            setattr(risk, key, bool(value) if isinstance(current, bool) else type(current)(value))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"风控字段 {key} 的值无效")


def _apply_notify(target: Any, values: dict[str, Any]) -> None:
    """Same contract as :func:`_apply_risk`, for the WeChat push settings.

    ``enabled`` without a target would queue messages that can never be
    delivered, so that combination is rejected up front instead of failing
    silently on the first fill.
    """
    for key, value in values.items():
        if not hasattr(target, key):
            log.debug("忽略未知通知字段: %s", key)
            continue
        current = getattr(target, key)
        try:
            setattr(target, key, bool(value) if isinstance(current, bool) else type(current)(value))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"通知字段 {key} 的值无效")
    if target.enabled and not str(target.target or "").strip():
        raise HTTPException(
            status_code=400,
            detail=(
                "开启企业微信通知前，请先填写接收人 userid"
                "（企业微信通讯录里的成员账号，且该成员要先给机器人发过一条消息）。"
            ),
        )


def _apply_ai(target: Any, values: dict[str, Any]) -> None:
    """Same contract as :func:`_apply_notify`, for the LLM assistant.

    The API key is deliberately *not* one of these fields: it is written to the
    OS keyring by ``/api/ai/key`` so it never lands in ``config.json``.
    """
    for key, value in values.items():
        if not hasattr(target, key):
            log.debug("忽略未知 AI 字段: %s", key)
            continue
        current = getattr(target, key)
        if isinstance(current, bool):
            setattr(target, key, bool(value))
            continue
        try:
            setattr(target, key, type(current)(value))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"AI 字段 {key} 的值无效") from None
    if target.daily_limit < 0:
        raise HTTPException(status_code=400, detail="每日调用上限不能为负")


def _market_frame(state, ctx, market, symbol: str, timeframe: str, limit: int):
    """Candles for analysis, with the same fallback the chart uses.

    The AI endpoints must not fail on exactly the network where the chart still
    works: Binance's futures domains are unreachable on some connections and
    ``/api/klines`` silently switches to a perpetual mirror. Analysis of an empty
    result would be worse than analysis of a slightly different feed — the digest
    states which prices it was given.
    """
    try:
        with state.locks[market]:
            return ctx.klines.fetch(market, symbol, timeframe, limit=limit)
    except Exception as primary:
        if not mirror.supports(market, timeframe):
            raise HTTPException(status_code=503, detail=str(primary)) from primary
        log.warning("币安行情不可用，AI 分析改用备用源：%s", primary)
        try:
            rows = mirror.fetch_ohlcv(market, symbol, timeframe, limit=limit)
        except Exception as exc:
            raise HTTPException(
                status_code=503, detail=f"无法获取 K 线：{primary}；备用源：{exc}"
            ) from exc
        frame = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
        if frame.empty:
            return frame
        frame["timestamp"] = frame["timestamp"].astype("int64")
        return frame


def _closed_only(frame, timeframe: str):
    """Drop the still-forming candle.

    Everything the model sees must be a finished bar: an open candle's high/low
    move after the fact, so an analysis that includes it can be contradicted by
    the next refresh.
    """
    if frame is None or getattr(frame, "empty", True):
        return frame
    step = timeframe_seconds(timeframe) * 1000
    last_open = int(frame["timestamp"].iloc[-1])
    if last_open + step <= int(time.time() * 1000):
        return frame
    return frame.iloc[:-1].reset_index(drop=True)


def _strategy_source(strategy) -> str:
    """The strategy's own file, for the "explain this strategy" prompt."""
    try:
        import inspect

        path = inspect.getsourcefile(strategy.__class__)
        if path:
            return paths_read(path)
    except Exception:
        log.debug("读取策略源码失败", exc_info=True)
    return ""


def paths_read(path: str) -> str:
    try:
        from pathlib import Path

        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _validate_backtest(state, payload: BacktestPayload):
    market = _market(payload.market)
    if payload.timeframe not in TIMEFRAME_SECONDS:
        raise HTTPException(status_code=400, detail=f"不支持的周期：{payload.timeframe}")
    try:
        strategy = state.ctx.strategies.get(payload.strategy)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    params = strategy.validate_params(payload.params)
    config = BacktestConfig(
        initial_capital=payload.initial_capital,
        fee_rate=payload.fee_rate,
        slippage_bps=payload.slippage_bps,
        position_pct=payload.position_pct,
        leverage=payload.leverage,
        allow_short=payload.allow_short,
        stop_loss_pct=payload.stop_loss_pct,
        take_profit_pct=payload.take_profit_pct,
        timeframe=payload.timeframe,
    )
    return market, strategy, params, config


def _start_backtest(state, jobs: JobStore, payload: BacktestPayload) -> dict[str, Any]:
    market, strategy, params, config = _validate_backtest(state, payload)
    job_id = jobs.create("backtest")

    def work() -> None:
        try:
            jobs.update(job_id, progress=0.15)
            with state.locks[market]:
                frame = state.ctx.klines.fetch(
                    market, payload.symbol, payload.timeframe, limit=payload.bars
                )
            jobs.update(job_id, progress=0.55)
            if frame.empty:
                raise ValueError("没有取到 K 线，请检查交易对与周期")
            result = run_backtest(frame, strategy, params, config)
            jobs.update(job_id, progress=0.9)
            serialized = _serialize_result(result, params)
            if payload.save:
                try:
                    state.ctx.db.save_backtest(
                        {
                            "id": job_id,
                            "created_at": datetime.now().isoformat(timespec="seconds"),
                            "symbol": payload.symbol,
                            "market_type": market.value,
                            "timeframe": payload.timeframe,
                            "strategy": payload.strategy,
                            "params": params,
                            "start": datetime.fromtimestamp(
                                result.equity["timestamp"].iloc[0] / 1000
                            ).isoformat(timespec="minutes"),
                            "end": datetime.fromtimestamp(
                                result.equity["timestamp"].iloc[-1] / 1000
                            ).isoformat(timespec="minutes"),
                            "metrics": serialized["metrics"],
                        }
                    )
                except Exception:  # a failed history write must not lose the result
                    log.warning("保存回测记录失败", exc_info=True)
            jobs.update(job_id, status="done", progress=1.0, result=serialized)
            metrics = serialized.get("metrics") or {}
            _notify_task(
                state.ctx,
                "回测完成",
                [
                    f"{payload.symbol} {payload.timeframe} · {payload.strategy}",
                    f"收益 {_metric_text(metrics, 'total_return', '%')}"
                    f" · 回撤 {_metric_text(metrics, 'max_drawdown', '%', signed=False)}"
                    f" · 胜率 {_metric_text(metrics, 'win_rate', '%', signed=False)}",
                    f"交易 {int(metrics.get('trades') or 0)} 笔",
                ],
            )
        except Exception as exc:
            log.warning("回测失败: %s", exc, exc_info=True)
            jobs.update(job_id, status="error", error=str(exc) or exc.__class__.__name__)
            _notify_task(
                state.ctx,
                "回测失败",
                [f"{payload.symbol} {payload.timeframe} · {payload.strategy}", str(exc)],
            )

    threading.Thread(target=work, name=f"backtest-{job_id}", daemon=True).start()
    return {"job_id": job_id, "status": "running"}


def _start_optimize(state, jobs: JobStore, payload: OptimizePayload) -> dict[str, Any]:
    market, strategy, _params, config = _validate_backtest(state, payload)
    if not payload.grid:
        raise HTTPException(status_code=400, detail="请至少设置一个参数的搜索范围")
    job_id = jobs.create("optimize")

    def progress(index: int, total: int, _params: dict) -> None:
        jobs.update(job_id, progress=0.1 + 0.85 * index / max(1, total))

    def work() -> None:
        try:
            with state.locks[market]:
                frame = state.ctx.klines.fetch(
                    market, payload.symbol, payload.timeframe, limit=payload.bars
                )
            if frame.empty:
                raise ValueError("没有取到 K 线，请检查交易对与周期")
            table = grid_search(
                frame,
                strategy,
                payload.grid,
                config,
                metric=payload.metric,
                top_n=payload.top_n,
                progress=progress,
            )
            rows = [
                {key: _number(value) if not isinstance(value, str) else value
                 for key, value in row.items()}
                for row in table.to_dict(orient="records")
            ]
            jobs.update(job_id, status="done", progress=1.0, result={"rows": rows})
            best = rows[0] if rows else {}
            best_text = ", ".join(
                f"{key}={best[key]}" for key in list(best)[:4]
            )
            _notify_task(
                state.ctx,
                "参数优化完成",
                [
                    f"{payload.symbol} {payload.timeframe} · {payload.strategy}",
                    f"共 {len(rows)} 组参数，最优：{best_text or '无结果'}",
                    f"目标指标：{payload.metric}",
                ],
            )
        except Exception as exc:
            log.warning("参数优化失败: %s", exc, exc_info=True)
            jobs.update(job_id, status="error", error=str(exc) or exc.__class__.__name__)
            _notify_task(
                state.ctx,
                "参数优化失败",
                [f"{payload.symbol} {payload.timeframe} · {payload.strategy}", str(exc)],
            )

    threading.Thread(target=work, name=f"optimize-{job_id}", daemon=True).start()
    return {"job_id": job_id, "status": "running"}


def _start_ai_filter(state, jobs: JobStore, payload: AIFilterPayload) -> dict[str, Any]:
    """Backtest, then re-judge each entry with the model, then compare.

    The model never runs inside ``run_backtest``: it only sees the bars *before*
    each entry, so the two runs are the same strategy on the same data with one
    difference — which trades are allowed. That is what makes the comparison
    mean something.
    """
    market, strategy, params, config = _validate_backtest(state, payload)
    ctx = state.ctx
    if not ctx.settings.ai.enabled:
        raise HTTPException(status_code=400, detail="AI 助手未启用，请先在设置里打开。")
    job_id = jobs.create("ai-filter")

    def work() -> None:
        try:
            jobs.update(job_id, progress=0.1)
            frame = _market_frame(
                state, ctx, market, payload.symbol, payload.timeframe, payload.bars
            )
            if frame.empty:
                raise ValueError("没有取到 K 线，请检查交易对与周期")
            result = run_backtest(frame, strategy, params, config)
            original = {key: _number(value) for key, value in result.metrics.items()}
            jobs.update(job_id, progress=0.35)

            def progress(index: int, total: int) -> None:
                jobs.update(job_id, progress=0.35 + 0.6 * index / max(1, total))

            judged = ai.filter_trades(
                ctx.ai,
                symbol=payload.symbol,
                timeframe=payload.timeframe,
                frame=frame,
                trades=[trade.as_dict() for trade in result.trades],
                progress=progress,
                limit=payload.limit,
            )
            if not judged["ok"]:
                raise ValueError(judged.get("error") or "AI 复核失败")

            filtered = ai.trade_metrics(
                judged["kept"], float(config.initial_capital)
            )
            # Same shape as the original metrics so the table lines up; only the
            # trade-derived numbers are meaningful for the filtered run.
            filtered_view = {
                "trades": filtered["trades"],
                "total_return": filtered["total_return"],
                "max_drawdown": filtered["max_drawdown"],
                "win_rate": filtered["win_rate"],
                "profit_factor": filtered["profit_factor"],
                "avg_trade": filtered["avg_trade"],
                "net_pnl": filtered["net_pnl"],
            }
            original_view = {
                "trades": original.get("trades", 0.0),
                "total_return": original.get("total_return", 0.0),
                "max_drawdown": original.get("max_drawdown", 0.0),
                "win_rate": original.get("win_rate", 0.0),
                "profit_factor": original.get("profit_factor", 0.0),
                "avg_trade": original.get("avg_trade", 0.0),
                "net_pnl": filtered_view["net_pnl"] + 0.0,
            }
            # ``net_pnl`` is trade-derived on both sides, so compute it the same
            # way for the original run instead of mixing units.
            original_view["net_pnl"] = sum(
                float(trade.pnl) for trade in result.trades
            )
            comparison = ai.compare_runs(original_view, filtered_view)

            jobs.update(
                job_id,
                status="done",
                progress=1.0,
                result={
                    "symbol": payload.symbol,
                    "timeframe": payload.timeframe,
                    "strategy": payload.strategy,
                    "params": params,
                    "original": original_view,
                    "filtered": filtered_view,
                    "comparison": comparison,
                    "judged": judged["judged"],
                    "total": judged["total"],
                    "kept": judged["kept_count"],
                    "failures": judged["failures"],
                    "truncated": judged["truncated"],
                },
            )
            _notify_task(
                ctx,
                "AI 过滤回测完成",
                [
                    f"{payload.symbol} {payload.timeframe} · {payload.strategy}",
                    f"复核 {judged['total']} 笔，保留 {judged['kept_count']} 笔",
                    f"收益 {original_view['total_return']:+.2%} → "
                    f"{filtered_view['total_return']:+.2%}",
                    comparison["verdict"],
                ],
            )
        except Exception as exc:
            log.warning("AI 过滤回测失败: %s", exc, exc_info=True)
            jobs.update(job_id, status="error", error=str(exc) or exc.__class__.__name__)
            _notify_task(
                ctx,
                "AI 过滤回测失败",
                [f"{payload.symbol} {payload.timeframe} · {payload.strategy}", str(exc)],
            )

    threading.Thread(target=work, name=f"ai-filter-{job_id}", daemon=True).start()
    return {"job_id": job_id, "status": "running"}

