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

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import pandas as pd

from ..backtest import BacktestConfig, grid_search, run_backtest
from ..core.config import AIAutoSettings, RiskSettings
from ..core.credentials import Credentials
from ..core.types import (
    TIMEFRAME_SECONDS,
    MarketType,
    Network,
    timeframe_seconds,
)
from ..ai_pipeline import DEPTHS
from ..engine.ai_trader import AITrader, AITraderConfig, TradeBudget, TradeLease
from ..engine.auto import AutoTrader, AutoTraderConfig
from ..exchange import mirror
from ..exchange.rest import PLATFORMS, SUPPORTED_PLATFORMS, BinanceREST
from ..notify import FORMATS, Notifier, NotifyError, check_webhook_url
from ..strategy.candidates import CandidateError, default_store
from ..strategy.source import (
    StrategySourceError,
    rename_source,
    restore_builtin,
    save_source,
)
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
        ctx.notifier.notify(Notifier.format_task(title, lines), kind="task")
    except Exception:  # pragma: no cover - defensive
        log.debug("任务通知发送失败", exc_info=True)


def _market(value: str) -> MarketType:
    try:
        return MarketType(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"未知市场：{value}") from exc


def _strategy_params(strategy, params: dict[str, Any]) -> dict[str, Any]:
    """校验策略参数，坏值报 400 并点名是哪个参数。

    ``Strategy.validate_params`` 只负责把数字夹到范围内：``int("abc")`` /
    ``float(None)`` 会抛 ValueError / TypeError，裸调用时整个请求变成 500，用户
    看到的是 Internal Server Error，还得自己猜是哪个框填错了。
    """
    for spec in getattr(strategy, "params_spec", ()) or ():
        if spec.kind not in ("int", "float"):
            continue
        value = params.get(spec.name, spec.default)
        try:
            int(value) if spec.kind == "int" else float(value)
        except (TypeError, ValueError, OverflowError):
            raise HTTPException(
                status_code=400,
                detail=f"策略参数 {spec.name}（{spec.label}）不是数字：{value!r}",
            ) from None
    return strategy.validate_params(params)


# A candidate is addressed as ``cand:<id>`` everywhere a strategy key is
# accepted. It is loaded into a throwaway registry, never into the shared one,
# so testing an unapproved candidate cannot change the live strategy list.
CANDIDATE_PREFIX = "cand:"


def _candidate_id(key: str) -> str:
    return key[len(CANDIDATE_PREFIX):] if key.startswith(CANDIDATE_PREFIX) else ""


def _strategy_for(state, key: str):
    """Resolve a strategy key, or a ``cand:<id>`` candidate, into an instance."""
    candidate_id = _candidate_id(key)
    if not candidate_id:
        try:
            return state.ctx.strategies.get(key)
        except KeyError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        return default_store().load_strategy(candidate_id)
    except CandidateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _record_candidate_backtest(key: str, metrics: dict[str, Any], *, symbol: str, timeframe: str) -> None:
    """Keep the candidate's own record in step with a lab/backtest run."""
    candidate_id = _candidate_id(key)
    if not candidate_id or not metrics:
        return
    payload = dict(metrics)
    payload.update({"symbol": symbol, "timeframe": timeframe, "source": "btrader"})
    try:
        default_store().record_backtest(candidate_id, payload)
    except Exception:  # a failed bookkeeping write must not lose the result
        log.warning("写入候选回测记录失败", exc_info=True)


def _strategy_in_use(auto, ai_ctl, key: str, action: str = "重命名") -> str:
    """Why changing ``key`` right now is refused, or an empty string.

    A running engine keeps its own strategy instance, so swapping the file
    underneath it would change what the next tick trades without the user
    noticing. ``action`` is the verb that finishes "请先停止再…".
    """
    try:
        status = auto.status() or {}
    except Exception:  # pragma: no cover - defensive
        status = {}
    if status.get("running") and str((status.get("config") or {}).get("strategy") or "") == key:
        return f"自动交易正在使用该策略，请先停止再{action}"
    try:
        ai_status = ai_ctl.status() or {}
    except Exception:  # pragma: no cover - defensive
        ai_status = {}
    if str(ai_status.get("state") or "stopped") in ("running", "paused"):
        if str((ai_status.get("config") or {}).get("strategy") or "") == key:
            return f"AI 交易正在使用该策略，请先停止再{action}"
    return ""

def _engine_clash(auto_running: bool, ai_state: str, *, starting: str) -> str:
    """另一个引擎占着账户的原因，没有冲突时返回空串。

    量化执行引擎（auto）和 AI 交易共用同一个交易所账户：两个 poller
    同时下单只会互相把仓位改回去，所以启动前互斥（INV-14）。
    """
    if starting == "auto" and ai_state in ("running", "paused"):
        return "AI 交易正在运行，请先停止再启动量化引擎"
    if starting == "ai" and auto_running:
        return "量化执行引擎正在运行，请先停止再启动 AI 交易"
    return ""


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
    platform: str | None = None
    network: str | None = None
    market_type: str | None = None
    last_symbol: str | None = None
    timeframe: str | None = None

    proxy: str | None = None
    theme: str | None = None
    notification_sound: bool | None = None
    risk: dict[str, Any] | None = None
    notify: dict[str, Any] | None = None
    ai: dict[str, Any] | None = None
    ai_auto: dict[str, Any] | None = None


class AITraderStartPayload(BaseModel):
    """启动 AI 交易。主网必须显式 confirm_live，测试网不需要。"""

    market: str = ""
    symbol: str = ""
    timeframe: str = ""
    strategy: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    confirm_live: bool = False
    max_order_quote: float | None = Field(default=None, ge=0)
    max_position_quote: float | None = Field(default=None, ge=0)
    # 决策深度：quick / standard / deep。空表示用设置里的值。
    depth: str = ""


class AITraderPausePayload(BaseModel):
    paused: bool = True


class AITraderHaltPayload(BaseModel):
    reason: str = "人工干预"


class NotifyTestPayload(BaseModel):
    """设置页「测试」按钮的一次性请求体。

    可选字段用来在保存之前先验证地址和格式，留空就用已保存的值。
    """

    message: str = ""
    webhook_url: str = ""
    msg_format: str = ""
    webhook_headers: str = ""
    webhook_template: str = ""


class CredentialsPayload(BaseModel):
    market: str = "spot"
    network: str | None = None
    platform: str = ""
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


class StrategyRenamePayload(BaseModel):
    new_key: str
    new_name: str = ""


class StrategySourcePayload(BaseModel):
    code: str = ""


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
        market = _market(payload.market)
        # 交易所没有的周期在这里就拦住。以前不校验：7m 能启动成功，然后每一轮
        # ``timeframe_seconds`` 抛 ValueError 被循环吞成「循环异常」，界面显示
        # 在跑却永远不下单。AI 交易和回测都校验了，就这条漏了。
        if payload.timeframe not in TIMEFRAME_SECONDS:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"不支持的周期：{payload.timeframe}"
                    f"（可用：{'、'.join(TIMEFRAME_SECONDS)}）"
                ),
            )

        # The runner always trades the configured venue; the only question is
        # whether the user is allowed to arm it. Testnet needs no confirmation —
        # rehearsing is the point of it — mainnet does.
        if not ctx.rest_for(market).has_credentials:
            raise HTTPException(status_code=400, detail="自动交易需要先配置 API Key")
        if ctx.network is Network.MAINNET and not payload.confirm_live:
            raise HTTPException(
                status_code=400, detail="主网自动交易需要显式确认（confirm_live=true）"
            )

        try:
            strategy = ctx.strategies.get(payload.strategy)
        except KeyError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        params = _strategy_params(strategy, payload.params)

        with self._lock:
            if self._trader is not None and self._trader.running:
                raise HTTPException(status_code=409, detail="自动交易已在运行")

            if ctx.market_type is not market:
                # 只借不落盘：部署顺手把市场写进 config.json，等于替用户改了设置，
                # 写盘失败还会让整个部署变成 500。界面（内存里的市场）仍然跟着引擎
                # 走，回滚这一条要等引擎停下来才谈得上。
                ctx.set_market_type(market, persist=False)

            config = AutoTraderConfig(
                symbol=payload.symbol,
                market_type=market,
                timeframe=payload.timeframe,
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
                "network": ctx.network.value,
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
        venue = "实盘" if ctx.network is Network.MAINNET else "测试网"
        _notify_task(
            ctx,
            "自动交易已启动" if running else "自动交易已停止",
            [label, f"环境：{venue}"],
        )
        if not running:
            self._error_notified_at = 0.0

    def _on_log(self, ctx, level: str, message: str) -> None:
        """循环异常写日志。同一条错误只推一次，避免刷屏。"""
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
                mode="实盘" if ctx.network is Network.MAINNET else "测试网",
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



class AITraderController:
    """单例：同一时刻只允许一个 AI 交易实例（INV-14）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._trader: AITrader | None = None
        self.lease = TradeLease()
        # 轮询和「立即决策」都从 _build 出去，budget 由控制器持有：当日决策次数、
        # 每小时下单数、冷却时间是同一个账户的账，不能跟着实例走一遍。
        self.budget = TradeBudget()

    @property
    def trader(self) -> AITrader | None:
        with self._lock:
            return self._trader

    def _build(self, state, payload: AITraderStartPayload) -> AITrader:
        """把请求 + 已保存设置变成一个「还没启动」的实例。

        start（轮询）和 decide（立即决策一次）共用这一段：启用检查、凭据、
        策略参数校验只写一份，两个入口就不会慢慢长出两套规则。
        """
        ctx = state.ctx
        cfg = ctx.settings.ai_auto
        market = _market(payload.market) if payload.market else ctx.market_type
        symbol = (payload.symbol or cfg.symbol or ctx.settings.last_symbol).strip()
        timeframe = payload.timeframe or cfg.timeframe or ctx.settings.timeframe
        if not symbol:
            raise HTTPException(status_code=400, detail="请先选择交易对")
        if timeframe not in TIMEFRAME_SECONDS:
            raise HTTPException(status_code=400, detail=f"不支持的周期：{timeframe}")
        if not ctx.settings.ai.enabled:
            raise HTTPException(status_code=400, detail="请先在设置中启用 AI 助手")
        strategy_key = (payload.strategy or "").strip()
        if not strategy_key:
            keys = ctx.strategies.keys
            strategy_key = keys[0] if keys else ""
        if not strategy_key:
            raise HTTPException(status_code=400, detail="AI 交易需要先选择一个策略")
        try:
            signal_strategy = ctx.strategies.get(strategy_key)
        except KeyError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # 这里没有「允许实盘」总闸：配了凭据就是能签名下单，没凭据下面就拦住。
        # 主网多要一句 confirm_live，让调用方把「这是真钱」说出口；测试网的钱是假的，
        # 排练一次不该多花一次点击。
        if not ctx.rest_for(market).has_credentials:
            raise HTTPException(status_code=400, detail="AI 交易需要先配置 API Key")
        if ctx.network is Network.MAINNET and not payload.confirm_live:
            raise HTTPException(
                status_code=400, detail="主网 AI 交易需要显式确认（confirm_live=true）"
            )

        depth = (payload.depth or cfg.depth or "standard").strip()
        if depth not in DEPTHS:
            raise HTTPException(status_code=400, detail=f"未知的决策深度：{depth}")
        config = AITraderConfig(
            enabled=True,
            confirm_live=payload.confirm_live,
            network=ctx.network.value,
            symbol=symbol,
            strategy_key=strategy_key,
            market_type=market.value,
            timeframe=timeframe,
            leverage=cfg.leverage,
            max_order_quote=(
                cfg.max_order_quote if payload.max_order_quote is None else payload.max_order_quote
            ),
            max_position_quote=(
                cfg.max_position_quote
                if payload.max_position_quote is None
                else payload.max_position_quote
            ),
            max_decisions_per_day=cfg.max_decisions_per_day,
            max_orders_per_hour=cfg.max_orders_per_hour,
            min_confidence=cfg.min_confidence,
            decision_ttl_sec=cfg.decision_ttl_sec,
            max_price_drift_pct=cfg.max_price_drift_pct,
            cooldown_sec=cfg.cooldown_sec,
            poll_sec=cfg.poll_sec,
            require_stop=cfg.require_stop,
            depth=depth,
        )
        trader = AITrader(
            config,
            trading=ctx.trading,
            llm=ctx.ai,
            risk=ctx.trading.risk,
            lease=self.lease,
            budget=self.budget,
            frame_provider=lambda: _closed_only(
                _market_frame(state, ctx, market, symbol, timeframe, 300), timeframe
            ),
            portfolio_provider=lambda: _ai_portfolio(state, symbol),
            signal_provider=lambda: _strategy_signal(
                state,
                ctx,
                market,
                symbol,
                timeframe,
                signal_strategy,
                signal_strategy.validate_params(payload.params or {}),
            ),
            # 新闻只喂给流水线当参考，拉不到就当没有，不影响决策。
            news_provider=lambda: _news_block(ctx, symbol),
        )
        return trader

    def start(self, state, payload: AITraderStartPayload) -> dict[str, Any]:
        trader = self._build(state, payload)
        with self._lock:
            previous = self._trader
        # 改参数要重启。旧实例还握着共享租约，不停掉新实例就拿不到，
        # 报错是「交易权已被占用」。
        if previous is not None and previous is not trader:
            previous.stop()
        ok, message = trader.start()
        if not ok:
            raise HTTPException(status_code=400, detail=message)
        with self._lock:
            self._trader = trader
        return trader.status()

    def decide(self, state, payload: AITraderStartPayload) -> dict[str, Any]:
        """立即决策一次：同样的闸门和记账，不启动轮询线程。

        「立即决策」和测试共用 AITrader.decide_once，走同一条路径。
        """
        with self._lock:
            trader_in_use = (
                self._trader is not None and self._trader.state in ("running", "paused")
            )
        if trader_in_use:
            raise HTTPException(
                status_code=409, detail="AI 交易正在运行，请先停止再用「立即决策」"
            )
        trader = self._build(state, payload)
        return trader.decide_once() or {}

    def stop(self) -> dict[str, Any]:
        with self._lock:
            trader, self._trader = self._trader, None
        if trader is not None:
            trader.stop()
        return self.status()

    def pause(self, paused: bool) -> dict[str, Any]:
        trader = self.trader
        if trader is not None:
            trader.pause(paused)
        return self.status()

    def halt(self, reason: str) -> dict[str, Any]:
        trader = self.trader
        if trader is not None:
            trader.halt(reason)
        return self.status()

    def kill(self, reason: str = "急停") -> dict[str, Any]:
        with self._lock:
            trader, self._trader = self._trader, None
        if trader is not None:
            trader.kill(reason)
        return {"state": "halted", "reason": reason}

    def status(self, since: int = 0) -> dict[str, Any]:
        trader = self.trader
        if trader is None:
            return {
                "state": "stopped",
                "paused": False,
                "halted_reason": "",
                "config": {},
                "counters": {},
                "last_decision": None,
                "events": [],
                "seq": 0,
            }
        return trader.status(since)


def build_router(get_state: Callable[[], Any]) -> APIRouter:
    router = APIRouter()
    auto = AutoController()
    ai_ctl = AITraderController()
    jobs = JobStore()

    # ------------------------------------------------------------ portfolio
    @router.get("/api/portfolio")
    def portfolio(market: str = "") -> dict[str, Any]:
        """The venue account, plus the equity curve this process has sampled.

        Same snapshot the trading panel reads, so the two pages cannot quote a
        different equity for one account. The curve starts at the recorded
        baseline: a fresh install has no history, and inventing one would make
        the first flattering point look like profit.
        """
        state = get_state()
        ctx = state.ctx
        mt = _market(market) if market else ctx.market_type
        snapshot = ctx.account.snapshot(mt, refresh=True)
        equity = snapshot.equity
        # Sample the equity curve at most every 20s: the panel polls every 4s
        # and a point per poll would make the chart unreadable.
        now = time.time()
        if now - state.equity_stamp >= 20:
            state.equity_stamp = now
            if not state.equity_history:
                state.equity_history.append(
                    {
                        "t": int((now - 1) * 1000),
                        "equity": float(snapshot.baseline or equity),
                    }
                )
            state.equity_history.append({"t": int(now * 1000), "equity": float(equity)})
            del state.equity_history[:-720]
        positions = [dict(item) for item in snapshot.positions]
        realized = dict(snapshot.realized_by_symbol)
        for item in positions:
            realized.setdefault(item["symbol"], float(item.get("realized_pnl") or 0.0))
        return {
            "market": mt.value,
            "quote_asset": snapshot.quote_asset,
            "cash": snapshot.cash,
            "starting_balance": snapshot.baseline,
            "equity": equity,
            "realized_pnl": snapshot.realized_pnl,
            "realized_pnl_by_symbol": realized,
            "positions": positions,
            "wallet_value": snapshot.wallet_value,
            "orders": [dict(item) for item in snapshot.orders],
            "trades": [dict(item) for item in snapshot.trades],
            "equity_history": list(state.equity_history),
            "has_credentials": snapshot.has_credentials,
            "error": snapshot.error,
        }

    # ------------------------------------------------------------- settings
    @router.get("/api/settings")
    def get_settings() -> dict[str, Any]:
        ctx = get_state().ctx
        return {
            "settings": ctx.settings.to_dict(),
            "platform": ctx.platform,
            "platforms": PLATFORMS,
            "market": ctx.market_type.value,
            "network": ctx.network.value,
            "has_credentials": ctx.rest.has_credentials,
            "data_dir": str(paths.data_dir()),
        }

    @router.put("/api/settings")
    def update_settings(payload: SettingsPayload) -> dict[str, Any]:
        ctx = get_state().ctx
        changed = apply_settings(ctx, payload)
        log.info("设置已保存：%s", "、".join(changed) or "无改动")
        return {"ok": True, "settings": ctx.settings.to_dict()}

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

    @router.post("/api/risk/kill")
    def risk_kill(payload: AITraderHaltPayload) -> dict[str, Any]:
        """急停：打开全局风控开关，停掉 AI 交易和自动交易（INV-8）。"""
        state = get_state()
        ctx = state.ctx
        ctx.settings.risk.kill_switch = True
        ctx.save_settings()
        auto.stop()
        ai_ctl.kill(payload.reason or "急停")
        log.warning("全局急停已开启")
        return {
            "ok": True,
            "risk": ctx.trading.risk.snapshot(),
            "ai_trader": ai_ctl.status(),
        }

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
            "只回复两个字：可用", system="这是一个连通性测试，只回复要求的内容。", kind="test"
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
            snapshot = ctx.account.snapshot(market, symbol)
            for item in snapshot.positions:
                if item.get("symbol") != symbol:
                    continue
                contracts = float(item.get("contracts") or 0.0)
                if abs(contracts) <= 1e-12:
                    continue
                position = {
                    "side": "long" if contracts >= 0 else "short",
                    "contracts": contracts,
                    "entry_price": float(item.get("entry_price") or 0.0),
                    "mark_price": float(item.get("last") or 0.0),
                    "unrealized_pnl": float(item.get("unrealized") or 0.0),
                }
                break
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
            try:
                snapshot = ctx.account.snapshot(market, symbol)
            except Exception:  # the second opinion must survive a read failure
                snapshot = None
            for item in (snapshot.positions if snapshot else []):
                if item.get("symbol") != symbol:
                    continue
                note = (
                    f"当前持有 {float(item.get('contracts') or 0):.6f} 份，"
                    f"开仓价 {float(item.get('entry_price') or 0):,.4f}。"
                )
                break
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
        params = _strategy_params(strategy, payload.params)
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
        """配置状态和上一次结果。设置页拿它显示真实情况，不假装成功。"""
        return get_state().ctx.notifier.status()

    @router.post("/api/notify/test")
    def notify_test(payload: NotifyTestPayload) -> dict[str, Any]:
        """同步发一条，让用户当场看到真实结果。

        请求体里的字段只覆盖这一次发送，用来在保存之前先验证地址和格式。
        覆盖走副本，已保存的设置不会被测试改掉。
        """
        ctx = get_state().ctx
        notifier = ctx.notifier
        text = payload.message.strip() or "biTrader 通知测试：收到这条说明推送正常。"
        candidate = replace(
            notifier.settings,
            webhook_url=check_webhook_url(payload.webhook_url) or notifier.settings.webhook_url,
            msg_format=(payload.msg_format or "").strip() or notifier.settings.msg_format,
            webhook_headers=(payload.webhook_headers or "").strip()
            or notifier.settings.webhook_headers,
            webhook_template=(payload.webhook_template or "").strip()
            or notifier.settings.webhook_template,
        )
        scratch = Notifier(candidate, timeout=notifier.timeout, queue_size=1)
        try:
            result = scratch.send_now(text)
        except NotifyError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # 测试成功也算当前通知器的成功，状态面板要跟着变。
        notifier.last_error = ""
        notifier.last_error_at = 0.0
        notifier.last_success_at = scratch.last_success_at
        return result
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
        platform = payload.platform or ctx.platform
        if platform not in SUPPORTED_PLATFORMS:
            raise HTTPException(status_code=400, detail="暂不支持这个平台。现在只有币安能下真单。")
        key = payload.api_key.strip()
        secret = payload.api_secret.strip()
        if not key or not secret:
            raise HTTPException(status_code=400, detail="API Key 与 Secret 都不能为空")

        creds = Credentials(key, secret)
        if payload.verify:
            try:
                with state.locks[market]:
                    rest = BinanceREST(network, creds, platform=platform)
                    rest.fetch_balance(market)
            except Exception as exc:
                raise HTTPException(
                    status_code=400, detail=f"连接测试失败：{exc}"
                ) from exc

        if not ctx.credentials.save(network, market, creds, platform):
            raise HTTPException(
                status_code=500,
                detail="无法写入系统凭据库（keyring），请检查系统钥匙串是否可用",
            )
        if network is ctx.network and market is ctx.market_type and platform == ctx.platform:
            ctx.creds = creds
            ctx.rest.credentials = creds
            ctx.rest.reset()
        return {"ok": True, "market": market.value, "network": network.value}

    @router.delete("/api/credentials")
    def delete_credentials(
        market: str = "spot", network: str | None = None, platform: str = ""
    ) -> dict[str, Any]:
        ctx = get_state().ctx
        platform = platform or ctx.platform
        market_type = _market(market)
        target = _network(network) or ctx.network
        ctx.credentials.delete(target, market_type, platform)
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
                    # "builtin": a package implementation of this key exists,
                    # overridden or not. "overridden": the user file wins.
                    "builtin": ctx.strategies.is_builtin(meta["key"]),
                    "overridden": ctx.strategies.is_overridden(meta["key"]),
                }
            )
        return {"items": items}

    @router.post("/api/strategies/{key}/rename")
    def strategy_rename(key: str, payload: StrategyRenamePayload) -> dict[str, Any]:
        """Rename a user strategy: new key (and file name), optional display name.

        Renaming is refused while an engine is trading this strategy: the loop
        holds its own instance bound to the old key, and the rename would make
        the next restart silently run something else.
        """
        state = get_state()
        ctx = state.ctx
        blocked = _strategy_in_use(auto, ai_ctl, key)
        if blocked:
            raise HTTPException(status_code=409, detail=blocked)
        try:
            result = rename_source(
                ctx.strategies,
                key=key,
                new_key=payload.new_key,
                new_name=payload.new_name,
            )
        except StrategySourceError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        new_key = result["key"]
        # Everything that remembers the old key by name has to follow it.
        moved = 0
        try:
            moved = default_store().rename_registered_key(key, new_key)
        except Exception:
            log.warning("更新候选记录的注册键失败", exc_info=True)
        try:
            touched = False
            saved = dict(getattr(ctx.settings.ai_auto, "strategy_params", {}) or {})
            if key in saved:
                saved[new_key] = saved.pop(key)
                ctx.settings.ai_auto.strategy_params = saved
                touched = True
            if touched:
                ctx.save_settings()
        except Exception:
            log.warning("迁移策略引用失败", exc_info=True)
        return {**result, "candidates_updated": moved}

    @router.put("/api/strategies/{key}/source")
    def strategy_source_save(key: str, payload: StrategySourcePayload) -> dict[str, Any]:
        """Save the editor buffer as a user file.

        Saving a built-in key writes an override into the user directory; the
        package source is never touched. Refused while an engine trades it.
        """
        ctx = get_state().ctx
        blocked = _strategy_in_use(auto, ai_ctl, key, "修改")
        if blocked:
            raise HTTPException(status_code=409, detail=blocked)
        try:
            return save_source(ctx.strategies, key=key, code=payload.code)
        except StrategySourceError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc

    @router.post("/api/strategies/{key}/restore")
    def strategy_restore(key: str) -> dict[str, Any]:
        """Delete the user file that overrides a built-in, restoring the original.

        Only an active override can be restored; anything else is a 400 with a
        message the button can show as is.
        """
        ctx = get_state().ctx
        blocked = _strategy_in_use(auto, ai_ctl, key, "还原")
        if blocked:
            raise HTTPException(status_code=409, detail=blocked)
        try:
            return restore_builtin(ctx.strategies, key)
        except StrategySourceError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc

    # ------------------------------------------------------- automated trade
    @router.get("/api/auto/status")
    def auto_status(since: int = 0) -> dict[str, Any]:
        return auto.status(since)

    @router.post("/api/auto/start")
    def auto_start(payload: AutoStartPayload) -> dict[str, Any]:
        state = get_state()
        clash = _engine_clash(
            auto.running, str(ai_ctl.status().get("state") or ""), starting="auto"
        )
        if clash:
            raise HTTPException(status_code=409, detail=clash)
        auto.start(state.ctx, payload)
        return {"ok": True, "status": auto.status()}

    @router.post("/api/auto/stop")
    def auto_stop() -> dict[str, Any]:
        auto.stop()
        return {"ok": True, "status": auto.status()}

    # ------------------------------------------------------------ AI 交易
    @router.get("/api/ai/trader/status")
    def ai_trader_status(since: int = 0) -> dict[str, Any]:
        return ai_ctl.status(since)

    @router.post("/api/ai/trader/start")
    def ai_trader_start(payload: AITraderStartPayload) -> dict[str, Any]:
        state = get_state()
        clash = _engine_clash(auto.running, "", starting="ai")
        if clash:
            raise HTTPException(status_code=409, detail=clash)
        return {"ok": True, "status": ai_ctl.start(state, payload)}

    @router.post("/api/ai/trader/decide")
    def ai_trader_decide(payload: AITraderStartPayload) -> dict[str, Any]:
        """立即决策一次（不启动轮询），用于人工催促或验证配置。"""
        state = get_state()
        clash = _engine_clash(auto.running, "", starting="ai")
        if clash:
            raise HTTPException(status_code=409, detail=clash)
        return {"ok": True, "decision": ai_ctl.decide(state, payload)}

    @router.post("/api/ai/trader/stop")
    def ai_trader_stop() -> dict[str, Any]:
        return {"ok": True, "status": ai_ctl.stop()}

    @router.post("/api/ai/trader/pause")
    def ai_trader_pause(payload: AITraderPausePayload) -> dict[str, Any]:
        return {"ok": True, "status": ai_ctl.pause(payload.paused)}

    @router.post("/api/ai/trader/halt")
    def ai_trader_halt(payload: AITraderHaltPayload) -> dict[str, Any]:
        return {"ok": True, "status": ai_ctl.halt(payload.reason or "人工干预")}

    @router.post("/api/ai/trader/kill")
    def ai_trader_kill(payload: AITraderHaltPayload) -> dict[str, Any]:
        """急停：AI 交易 + 自动交易全部停止，并打开全局风控急停。"""
        state = get_state()
        ctx = state.ctx
        ctx.settings.risk.kill_switch = True
        ctx.save_settings()
        auto.stop()
        result = ai_ctl.kill(payload.reason or "急停")
        log.warning("AI 交易急停：%s", result.get("reason"))
        return {"ok": True, "ai_trader": result, "risk": ctx.trading.risk.snapshot()}

    @router.get("/api/ai/trader/audit")
    def ai_trader_audit(limit: int = 100) -> dict[str, Any]:
        path = paths.logs_dir() / "ai-trade-audit.jsonl"
        if not path.exists():
            return {"items": []}
        count = max(1, min(limit, 500))
        rows: deque[dict[str, Any]] = deque(maxlen=count)
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"读取审计日志失败：{exc}") from exc
        return {"items": list(rows)}

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


def apply_settings(ctx: Any, payload: "SettingsPayload") -> list[str]:
    """把设置写进上下文，返回改动过的字段名。

    所有字段先在一份草稿上校验，全部通过才动内存和磁盘。老实现里切网络、切市场
    会先落盘：后面的字段一旦报 400，磁盘上就留下一半新一半旧的配置。
    """
    settings = ctx.settings
    target_platform = None
    if payload.platform:
        if payload.platform not in SUPPORTED_PLATFORMS:
            raise HTTPException(
                status_code=400,
                detail="暂不支持这个平台。现在只有币安能下真单。",
            )
        target_platform = payload.platform
    target_network = _network(payload.network)
    target_market = None
    if payload.market_type:
        try:
            target_market = MarketType(payload.market_type)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail=f"未知市场：{payload.market_type}"
            ) from exc
    if payload.timeframe is not None and payload.timeframe not in TIMEFRAME_SECONDS:
        raise HTTPException(status_code=400, detail=f"不支持的周期：{payload.timeframe}")
    if payload.theme is not None and payload.theme not in ("light", "dark"):
        raise HTTPException(status_code=400, detail="主题只有亮色和暗色两种。")

    # 草稿副本：任何一项校验失败都不影响线上对象。
    risk_draft = replace(settings.risk)
    notify_draft = replace(settings.notify)
    ai_draft = replace(settings.ai)
    ai_auto_draft = replace(settings.ai_auto)
    if payload.risk is not None:
        _apply_risk(risk_draft, payload.risk)
    if payload.notify is not None:
        _apply_notify(notify_draft, payload.notify)
    if payload.ai is not None:
        _apply_ai(ai_draft, payload.ai)
    if payload.ai_auto is not None:
        _apply_ai_auto(ai_auto_draft, payload.ai_auto)

    # 字段都合法了，开始落地。切换类操作不落盘，统一在最后保存一次。
    # 落地前留一份快照：save_settings() 会因为配置文件坏了直接拒绝保存
    # （ConfigError → 409），那时内存里已经换了一半、磁盘还是旧的，
    # 界面显示的网络/市场与重启后对不上。保存失败就整体回滚。
    snapshot = replace(settings)
    old_platform, old_network, old_market = ctx.platform, ctx.network, ctx.market_type
    try:
        changed: list[str] = []
        if target_platform is not None and target_platform != ctx.platform:
            ctx.set_platform(target_platform, persist=False)
            changed.append("platform")
        if target_network is not None and target_network is not ctx.network:
            ctx.set_network(target_network, persist=False)
            changed.append("network")
        if target_market is not None and target_market is not ctx.market_type:
            ctx.set_market_type(target_market, persist=False)
            changed.append("market_type")
        for name in ("last_symbol", "timeframe"):
            value = getattr(payload, name)
            if value is not None and getattr(settings, name) != value:
                setattr(settings, name, value)
                changed.append(name)
        if payload.proxy is not None and payload.proxy.strip() != settings.proxy:
            # 重建客户端：ccxt 只读一次 proxies，旧客户端会继续直连。
            ctx.apply_proxy(payload.proxy)
            changed.append("proxy")
        if payload.theme is not None and settings.theme != payload.theme:
            settings.theme = payload.theme
            changed.append("theme")
        if payload.notification_sound is not None and settings.notification_sound != payload.notification_sound:
            settings.notification_sound = payload.notification_sound
            changed.append("notification_sound")
        for name, draft in (
            ("risk", risk_draft),
            ("notify", notify_draft),
            ("ai", ai_draft),
            ("ai_auto", ai_auto_draft),
        ):
            if getattr(settings, name) != draft:
                setattr(settings, name, draft)
                changed.append(name)

        ctx.save_settings()
    except Exception:
        # 切换类操作反向执行一遍，再把草稿之前的那份设置整体放回去。
        ctx.set_platform(old_platform, persist=False)
        ctx.set_network(old_network, persist=False)
        ctx.set_market_type(old_market, persist=False)
        if settings.proxy != snapshot.proxy:
            ctx.apply_proxy(snapshot.proxy)
        for name in (
            "platform",
            "network",
            "market_type",
            "last_symbol",
            "timeframe",
            "proxy",
            "theme",
            "notification_sound",
            "risk",
            "notify",
            "ai",
            "ai_auto",
        ):
            setattr(settings, name, getattr(snapshot, name))
        raise
    return changed


def _apply_risk(risk: RiskSettings, values: dict[str, Any]) -> None:
    """Only fields the dataclass declares; unknown keys are ignored."""
    for key, value in values.items():
        if not hasattr(risk, key):
            log.debug("忽略未知风控字段: %s", key)
            continue
        current = getattr(risk, key)
        if value is None and isinstance(current, (bool, str)):
            # null 不是「清空」：以前 bool(None) 会悄悄关掉急停开关，
            # str(None) 会把文本字段写成字符串 "None"。当作没提供。
            log.debug("忽略 null 风控字段: %s", key)
            continue
        try:
            setattr(risk, key, bool(value) if isinstance(current, bool) else type(current)(value))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"风控字段 {key} 的值无效")


def _apply_notify(target: Any, values: dict[str, Any]) -> None:
    """同 :func:`_apply_risk` 的规矩，对象是通知设置。

    开启通知却没填地址，消息会永远发不出去，所以这一组合直接拒绝，
    不留到第一次成交时才失败。
    """
    for key, value in values.items():
        if not hasattr(target, key):
            log.debug("忽略未知通知字段: %s", key)
            continue
        current = getattr(target, key)
        if value is None and isinstance(current, (bool, str)):
            # null 不是「清空」：以前 msg_format 会 400、webhook_url 会被清成空串、
            # 开关会被悄悄关掉。当作没提供，保住已保存的值。
            log.debug("忽略 null 通知字段: %s", key)
            continue
        if key == "msg_format":
            fmt = str(value or "").strip().lower()
            if fmt not in FORMATS:
                raise HTTPException(
                    status_code=400,
                    detail="消息格式只能是 auto、qyapi_markdown、qyapi_text 或 custom",
                )
            target.msg_format = fmt
            continue
        if key == "webhook_url":
            try:
                target.webhook_url = check_webhook_url(str(value or ""))
            except NotifyError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            continue
        current = getattr(target, key)
        try:
            setattr(target, key, bool(value) if isinstance(current, bool) else type(current)(value))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"通知字段 {key} 的值无效") from None
    if target.enabled and not str(target.webhook_url or "").strip():
        raise HTTPException(
            status_code=400,
            detail="开启通知前先填 Webhook 地址（企业微信群机器人的地址）。",
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
        if value is None and isinstance(current, (bool, str)):
            # null 不是「清空」：以前会把 model/base_url 写成字符串 "None"，
            # 之后每次调用都失败，界面上还看不出哪里错了。当作没提供。
            log.debug("忽略 null AI 字段: %s", key)
            continue
        if isinstance(current, bool):
            setattr(target, key, bool(value))
            continue
        try:
            setattr(target, key, type(current)(value))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"AI 字段 {key} 的值无效") from None


def _apply_ai_auto(target: AIAutoSettings, values: dict[str, Any]) -> None:
    """AI 交易设置：只接受 dataclass 声明的字段，类型就地转换。"""
    for key, value in values.items():
        if not hasattr(target, key):
            log.debug("忽略未知 AI 自动交易字段: %s", key)
            continue
        if key == "strategy_params":
            if not isinstance(value, dict):
                raise HTTPException(
                    status_code=400,
                    detail="ai_auto.strategy_params 必须是对象",
                )
            cleaned: dict[str, dict[str, Any]] = {}
            for strategy_key, params in value.items():
                if not isinstance(params, dict):
                    raise HTTPException(
                        status_code=400,
                        detail=f"ai_auto.strategy_params.{strategy_key} 必须是对象",
                    )
                cleaned[str(strategy_key)] = dict(params)
            target.strategy_params = cleaned
            continue
        current = getattr(target, key)
        if value is None and isinstance(current, (bool, str)):
            # 同 _apply_ai：null 不该把 symbol/depth 写成 "None"，也不该悄悄关开关。
            log.debug("忽略 null AI 自动交易字段: %s", key)
            continue
        if isinstance(current, bool):
            setattr(target, key, bool(value))
            continue
        try:
            setattr(target, key, type(current)(value))
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400, detail=f"AI 自动交易字段 {key} 的值无效"
            ) from None
    for name in ("max_order_quote", "max_position_quote", "max_price_drift_pct"):
        if getattr(target, name) < 0:
            raise HTTPException(status_code=400, detail=f"ai_auto.{name} 不能为负")
    if not (0.0 <= target.min_confidence <= 1.0):
        raise HTTPException(status_code=400, detail="ai_auto.min_confidence 必须在 0 到 1 之间")


def _news_block(ctx: Any, symbol: str, limit: int = 8) -> str:
    """把最近的新闻标题拼成一段，喂给 AI 交易流水线。

    只给标题和时间，正文太长会把上下文挤满。拉不到就当没有新闻，
    流水线自己会跳过消息面那一段。
    """
    try:
        data = ctx.news.snapshot(symbol=symbol, limit=limit)
    except Exception as exc:
        log.debug("读取新闻失败：%s", exc)
        return ""
    lines = []
    for item in data.get("items") or []:
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        stamp = str(item.get("published_at") or "")[:16].replace("T", " ")
        source = str(item.get("source") or "").strip()
        lines.append(f"- {stamp} {source}｜{title}".strip())
    return "\n".join(lines)


def _ai_portfolio(state, symbol: str) -> dict[str, Any]:
    """AI 交易用的组合视图：权益、该交易对持仓、最新价、风控状态。"""
    ctx = state.ctx
    market = ctx.market_type
    price = _last_price(state, market, symbol)
    snapshot = ctx.account.snapshot(market, symbol)
    position_quote = 0.0
    for item in snapshot.positions:
        if item.get("symbol") != symbol:
            continue
        position_quote = abs(float(item.get("quote_value") or 0.0))
        break
    equity = snapshot.equity
    risk = ctx.trading.risk
    return {
        "price": price,
        "position_quote": position_quote,
        "equity": equity,
        "kill_switch": bool(risk.settings.kill_switch),
        "halted_reason": risk.state.halted_reason,
    }


def _strategy_signal(
    state,
    ctx,
    market,
    symbol: str,
    timeframe: str,
    strategy,
    params: dict[str, Any],
) -> float:
    """Latest target of the selected strategy on closed candles.

    Positive means long, negative means short, zero means stay flat. The AI
    layer is only allowed to confirm/veto this direction; it cannot originate
    a trade without a signal.
    """
    frame = _closed_only(_market_frame(state, ctx, market, symbol, timeframe, 300), timeframe)
    if frame is None or getattr(frame, "empty", True):
        return 0.0
    result = strategy.generate(frame, params)
    if result is None or getattr(result, "empty", True) or "target" not in result:
        return 0.0
    try:
        target = float(result["target"].iloc[-1])
    except (TypeError, ValueError, IndexError):
        return 0.0
    # Spot cannot hold a negative position; treat a short signal as flat.
    if target < 0 and not getattr(market, "is_futures", False):
        return 0.0
    return max(-1.0, min(1.0, target))


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
                status_code=503, detail=f"无法获取 K 线：{primary}。备用源：{exc}"
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
    strategy = _strategy_for(state, payload.strategy)
    params = _strategy_params(strategy, payload.params)
    # 现货没有杠杆，也不能做空。原样透传会让现货回测跑出「做空 + 10 倍杠杆」这种
    # 根本不存在的曲线（``BacktestConfig.deploy = position_pct * max(1, leverage)``）。
    # 自动交易那条路径（``AutoController.start``）早就按市场收了，这里漏了同一句话；
    # 前端碰巧传 1，直调接口就绕过去了。
    config = BacktestConfig(
        initial_capital=payload.initial_capital,
        fee_rate=payload.fee_rate,
        slippage_bps=payload.slippage_bps,
        position_pct=payload.position_pct,
        leverage=payload.leverage if market.is_futures else 1.0,
        allow_short=bool(payload.allow_short) and market.is_futures,
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
            _record_candidate_backtest(
                payload.strategy,
                metrics,
                symbol=payload.symbol,
                timeframe=payload.timeframe,
            )
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

