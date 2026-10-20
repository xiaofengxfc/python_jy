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

from ..backtest import BacktestConfig, grid_search, run_backtest
from ..core.config import RiskSettings
from ..core.credentials import Credentials
from ..core.types import TIMEFRAME_SECONDS, MarketType, Network, RunMode
from ..engine.auto import AutoTrader, AutoTraderConfig
from ..exchange.rest import BinanceREST
from ..installer import InstallOptions, restart_gateway_process, wechat_has_context
from ..notify import WeChatNotifier
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
    theme: str | None = None
    notification_sound: bool | None = None
    risk: dict[str, Any] | None = None
    notify: dict[str, Any] | None = None


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
            "biTrader 微信通知测试：如果你收到这条消息，说明推送已打通。"
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
        # ``sent`` only means the channel accepted the message. WeChat lets a bot
        # post solely inside a conversation the user opened, so without an inbound
        # message the phone stays silent — say that instead of implying success.
        if not wechat_has_context():
            result = {
                **result,
                "warning": (
                    "已提交给网关，但还没有会话上下文，手机可能收不到："
                    "请先在手机微信里给 ClawBot 发一条任意消息，然后再点一次发送测试。"
                ),
            }
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
        """Whether the gateway knows the WeChat channel and has an account.

        Read from the gateway rather than inferred from files on disk, because a
        plugin can be installed and still be disabled or unconfigured. The
        gateway's own process state rides along: a channel is useless when the
        gateway is down, and the page has to say which of the two is wrong.
        """
        ctx = get_state().ctx
        status = ctx.installer.channel_status()
        status["gateway"] = ctx.gateway.status()
        return status

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

    @router.post("/api/install/openclaw/channel")
    def channel_register() -> dict[str, Any]:
        """Register the WeChat plugin with the gateway, streamed to the log."""
        try:
            return get_state().ctx.installer.register_channel()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/api/install/openclaw/logout")
    def channel_logout(account: str = "") -> dict[str, Any]:
        """Unbind a WeChat account and delete the credential files it left.

        Without this the only way back to "未绑定" was editing the plugin's
        state directory by hand: the page could bind but never release.

        The notifier's own target is cleared with it — a stale recipient is
        exactly what turns "已解绑" into a wall of failed sends.
        """
        ctx = get_state().ctx
        try:
            result = ctx.installer.logout(account)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        notify = ctx.settings.notify
        if notify.target or notify.account or notify.enabled:
            ctx.settings.notify = replace(notify, enabled=False, target="", account="")
            ctx.save_settings()
        result["settings"] = ctx.settings.to_dict()
        return result

    @router.post("/api/install/openclaw/bind")
    def channel_bind(send_test: bool = True) -> dict[str, Any]:
        """Finish a scan in one step: persist the recipient, enable, verify.

        The scan only links the account in the gateway. The notifier carries its
        own settings, so the flow used to end with a bound channel whose push
        switch was still off and whose target was empty — "扫码成功了但收不到
        通知". Everything the scan already knows is applied here instead of
        asking the user to copy an id into another card and save.
        """
        ctx = get_state().ctx
        status = ctx.installer.channel_status()
        if not status.get("configured"):
            raise HTTPException(status_code=400, detail="尚未完成扫码绑定，请先扫码。")
        targets = status.get("targets") or []
        if not targets:
            raise HTTPException(status_code=400, detail="已绑定账号，但没有可用的接收目标。")
        primary = targets[0]
        settings = ctx.settings
        settings.notify = replace(
            settings.notify,
            enabled=True,
            target=primary["target"],
            account=primary["account"],
        )
        # ``save_settings`` also rebinds the running notifier, so the change
        # takes effect without a restart.
        ctx.save_settings()

        # A configured channel still delivers nothing while the gateway is down.
        gateway = ctx.gateway.status()
        if not gateway.get("reachable"):
            gateway = ctx.gateway.start()
        result: dict[str, Any] = {
            "ok": True,
            "target": primary["target"],
            "account": primary["account"],
            "gateway": gateway,
            "has_context": bool(status.get("has_context")),
            "test": None,
            "settings": settings.to_dict(),
        }
        if not result["has_context"]:
            result["warning"] = (
                "绑定已生效，但还没有会话：请先在手机微信里给 ClawBot 发一条任意消息，"
                "之后推送才能送达。"
            )
        if send_test:
            try:
                ctx.notifier.send_now("biTrader：微信通知已绑定并开启。")
                result["test"] = {"ok": True, "error": ""}
            except Exception as exc:  # report, do not fail the binding
                result["test"] = {"ok": False, "error": str(exc)}
        return result

    @router.post("/api/install/openclaw/login")
    def channel_login(force: bool = False) -> dict[str, Any]:
        """Start the QR login and return the link the code encodes.

        The page renders that link as an image. The wizard's own terminal art
        is unreadable in a default ``cmd.exe`` window (raster font, half-block
        glyphs), which is why scanning it failed.
        """
        try:
            # ``force`` is the panel's "重新生成": the code on screen is spent
            # or was rejected, so a fresh wizard has to replace it. Without the
            # flag a second click reuses the live code, which is what keeps two
            # wizards from invalidating each other.
            return get_state().ctx.installer.start_login(force=force)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/api/install/openclaw/login")
    def channel_login_status() -> dict[str, Any]:
        """Whether a code is pending, already scanned, or spent."""
        return get_state().ctx.installer.login_status()

    @router.get("/api/install/openclaw/login/qr.svg")
    def channel_login_qr() -> Response:
        """The pending login link as a scannable QR image."""
        try:
            svg = get_state().ctx.installer.login_qr_svg()
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return Response(
            content=svg,
            media_type="image/svg+xml",
            # A code is single-use: never let a browser replay a stale one.
            headers={"Cache-Control": "no-store"},
        )

    @router.post("/api/install/openclaw/login/cancel")
    def channel_login_cancel() -> dict[str, Any]:
        """Discard the pending code before it expires on its own."""
        return get_state().ctx.installer.cancel_login()

    @router.post("/api/install/openclaw/terminal")
    def channel_login_terminal() -> dict[str, Any]:
        """Fallback: open the wizard in a real terminal.

        Only useful when the CLI prints no fallback link; the page keeps it as
        a secondary action rather than the primary path.
        """
        try:
            return get_state().ctx.installer.open_login_terminal()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


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
    def credentials() -> dict[str, Any]:
        ctx = get_state().ctx
        network = _network(None) or ctx.network
        items = []
        for market in MarketType:
            creds = ctx.credentials.load(network, market)
            items.append(
                {
                    "market": market.value,
                    "label": market.label,
                    "configured": creds.is_set,
                    "masked": creds.masked_key,
                    "current": market is ctx.market_type and network is ctx.network,
                }
            )
        return {"network": network.value, "items": items}

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
                "开启微信通知前，请先完成扫码绑定（会自动填入接收目标），"
                "或手动填写接收目标。"
            ),
        )


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
