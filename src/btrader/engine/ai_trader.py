"""AI 自主交易核心（Phase 3-A）。

设计依据：``docs/ai-autonomy-plan.md`` §5.2/§5.4/§5.5/§5.6/§5.9。

这个模块只做三件事：**决策 → 闸门 → 经 TradingService 下单**。它不直接
import 交易所、纸面 broker 或数据库；唯一下单出口是注入的
``TradingService.submit()``（INV-1）。风控的权威仍然是 ``RiskManager``，
本模块的 ``AIGuard`` 只是在其前面再加一层 AI 专属的收紧（INV-2）。

默认构造即停止、纸面模式；实盘必须同时满足
``live_enabled + confirm_live + mainnet + 未急停``（INV-9）。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from .. import ai, paths
from ..core.types import MarketType, OrderSide, OrderType, RunMode
from .trading import OrderRequest, OrderResult

log = logging.getLogger(__name__)

ACTIONS = ("open_long", "open_short", "close_long", "close_short", "hold")
ENTRY_ACTIONS = ("open_long", "open_short")
CLOSE_ACTIONS = ("close_long", "close_short")

STATE_STOPPED = "stopped"
STATE_RUNNING = "running"
STATE_PAUSED = "paused"
STATE_HALTED = "halted"

DECISION_PROMPT = (
    "你是量化交易员。只输出一个 JSON 对象，不要解释。字段：\n"
    '{"action":"open_long|open_short|close_long|close_short|hold",'
    '"symbol":"交易对","quote":下单金额USDT,"price":参考价,"stop_loss":止损价或null,'
    '"take_profit":止盈价或null,"confidence":0到1,"ttl_sec":决策有效期秒,"rationale":"一句话理由"}\n'
    "约束：没有把握就输出 hold；quote 不得超过账户可用资金；不要给出杠杆；"
    "止损必须在方向上合理（多单止损低于参考价，空单高于参考价）。"
)


# --------------------------------------------------------------- data types
@dataclass(frozen=True)
class AIDecision:
    """模型给出的一条决策。schema 里没有 leverage（INV-6）。"""

    decision_id: str
    action: str
    symbol: str
    quote: float = 0.0
    price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    confidence: float = 0.0
    ttl_sec: float = 0.0
    rationale: str = ""
    created_at: float = 0.0

    @property
    def is_entry(self) -> bool:
        return self.action in ENTRY_ACTIONS

    @property
    def is_close(self) -> bool:
        return self.action in CLOSE_ACTIONS


@dataclass
class AITraderConfig:
    """一份自包含的运行配置；实盘开关默认关闭。"""

    enabled: bool = False
    mode: str = RunMode.PAPER.value
    live_enabled: bool = False
    confirm_live: bool = False
    network: str = "mainnet"
    symbol: str = "BTC/USDT"
    market_type: str = MarketType.SPOT.value
    timeframe: str = "15m"
    leverage: float = 1.0
    max_order_quote: float = 50.0
    max_position_quote: float = 200.0
    max_decisions_per_day: int = 96
    max_orders_per_hour: int = 12
    min_confidence: float = 0.6
    decision_ttl_sec: float = 120.0
    max_price_drift_pct: float = 0.005
    cooldown_sec: float = 30.0
    poll_sec: float = 60.0
    require_stop: bool = True


@dataclass
class GuardVerdict:
    ok: bool
    reason: str = ""


class TradeLease:
    """最小互斥租约：auto / ai / quant 同一时刻只有一个能操作资金（INV-14）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._owner = ""

    @property
    def owner(self) -> str:
        with self._lock:
            return self._owner

    def acquire(self, owner: str) -> bool:
        with self._lock:
            if self._owner and self._owner != owner:
                return False
            self._owner = owner
            return True

    def release(self, owner: str) -> None:
        with self._lock:
            if self._owner == owner:
                self._owner = ""


# ------------------------------------------------------------------ parsing
def parse_decision(text: str, *, now: float | None = None) -> tuple[AIDecision | None, str]:
    """把模型文本解析成决策。解析不出来就是失败，调用方必须不下单。"""
    found = ai._find_json_object(text or "")
    if found is None:
        return None, "模型没有返回可解析的 JSON 决策"
    data, _start, _end = found
    try:
        action = str(data.get("action") or "").strip().lower()
        if action not in ACTIONS:
            return None, f"未知动作：{action or '空'}"
        price = data.get("price")
        stop = data.get("stop_loss")
        take = data.get("take_profit")
        decision = AIDecision(
            decision_id=str(data.get("decision_id") or uuid.uuid4().hex[:12]),
            action=action,
            symbol=str(data.get("symbol") or "").strip().upper(),
            quote=float(data.get("quote") or 0.0),
            price=float(price) if price not in (None, "", "null") else None,
            stop_loss=float(stop) if stop not in (None, "", "null") else None,
            take_profit=float(take) if take not in (None, "", "null") else None,
            confidence=float(data.get("confidence") or 0.0),
            ttl_sec=float(data.get("ttl_sec") or 0.0),
            rationale=str(data.get("rationale") or "")[:200],
            created_at=time.time() if now is None else now,
        )
    except (TypeError, ValueError) as exc:
        return None, f"决策字段类型错误：{exc}"
    return decision, ""


def validate_decision(decision: AIDecision, config: AITraderConfig) -> list[str]:
    """结构校验；任何一条不过都不允许下单。"""
    errors: list[str] = []
    if decision.action not in ACTIONS:
        errors.append("动作非法")
    if decision.symbol != config.symbol.upper():
        errors.append(f"交易对不匹配：{decision.symbol or '空'} != {config.symbol}")
    if not (0.0 <= decision.confidence <= 1.0):
        errors.append("置信度必须在 0 到 1 之间")
    if decision.is_entry:
        if decision.quote <= 0:
            errors.append("开仓金额必须大于 0")
        if config.max_order_quote and decision.quote > config.max_order_quote + 1e-9:
            # 超限拒绝而不是截断（INV-3）：截断会让模型以为自己的下单金额生效了。
            errors.append(
                f"单笔金额 {decision.quote:.2f} 超过 AI 上限 {config.max_order_quote:.2f}"
            )
        if config.require_stop and decision.stop_loss is None:
            errors.append("缺少止损价")
        if decision.price is not None and decision.price <= 0:
            errors.append("参考价必须大于 0")
        if decision.stop_loss is not None and decision.price:
            if decision.action == "open_long" and decision.stop_loss >= decision.price:
                errors.append("多单止损价必须低于参考价")
            if decision.action == "open_short" and decision.stop_loss <= decision.price:
                errors.append("空单止损价必须高于参考价")
    if decision.ttl_sec < 0:
        errors.append("有效期不能为负")
    return errors


class AIGuard:
    """AI 专属闸门：在 RiskManager 之前再收紧一层（INV-2/3/4/5/7/12）。"""

    def __init__(self, config: AITraderConfig, risk: Any | None = None) -> None:
        self.config = config
        self.risk = risk

    def check(
        self,
        decision: AIDecision,
        *,
        now: float,
        position_quote: float,
        equity: float,
        decisions_today: int,
        orders_last_hour: int,
        last_order_at: float,
        market_price: float | None,
        kill_switch: bool = False,
        halted_reason: str = "",
    ) -> GuardVerdict:
        c = self.config
        if kill_switch:
            return GuardVerdict(False, "全局急停已开启")
        if halted_reason:
            return GuardVerdict(False, f"风控熔断中：{halted_reason}")
        if decision.action == "hold":
            return GuardVerdict(False, "模型选择观望")
        if not decision.is_entry and not decision.is_close:
            return GuardVerdict(False, "动作不支持")
        if decision.confidence < c.min_confidence:
            return GuardVerdict(False, f"置信度 {decision.confidence:.2f} 低于门槛 {c.min_confidence:.2f}")
        if decision.ttl_sec and now - decision.created_at > decision.ttl_sec:
            return GuardVerdict(False, "决策已过期")
        if market_price and decision.price:
            drift = abs(market_price - decision.price) / max(decision.price, 1e-9)
            if drift > c.max_price_drift_pct:
                return GuardVerdict(False, f"价格已漂移 {drift:.2%}，超过 {c.max_price_drift_pct:.2%}")
        if c.max_decisions_per_day and decisions_today >= c.max_decisions_per_day:
            return GuardVerdict(False, "已达 AI 当日决策次数上限")
        if c.max_orders_per_hour and orders_last_hour >= c.max_orders_per_hour:
            return GuardVerdict(False, "已达 AI 每小时下单次数上限")
        if last_order_at and c.cooldown_sec and now - last_order_at < c.cooldown_sec:
            return GuardVerdict(False, "AI 下单冷却中")
        if decision.is_entry and c.max_position_quote:
            if position_quote + decision.quote > c.max_position_quote + 1e-9:
                return GuardVerdict(False, "持仓金额将超过 AI 上限")
        if decision.is_entry and self.risk is not None:
            verdict = self.risk.check_order(
                decision.quote,
                position_quote,
                decision.quote,
                leverage=c.leverage,
                entry=True,
                equity=equity,
            )
            if not getattr(verdict, "allowed", True):
                return GuardVerdict(False, f"风控拒绝：{getattr(verdict, 'reason', '')}")
        return GuardVerdict(True)


# ------------------------------------------------------------------ trader
class AITrader:
    """AI 自主交易控制器：poller + 决策 + 闸门 + 审计。"""

    def __init__(
        self,
        config: AITraderConfig,
        *,
        trading: Any,
        llm: Any,
        frame_provider: Callable[[], Any] | None = None,
        portfolio_provider: Callable[[], dict[str, Any]] | None = None,
        signal_provider: Callable[[], float] | None = None,
        audit_path: Path | None = None,
        clock: Callable[[], float] = time.time,
        lease: TradeLease | None = None,
        risk: Any | None = None,
    ) -> None:
        self.config = config
        self.trading = trading
        self.llm = llm
        self.frame_provider = frame_provider
        self.portfolio_provider = portfolio_provider
        self.signal_provider = signal_provider
        self._last_signal: float | None = None
        self.audit_path = audit_path or (paths.logs_dir() / "ai-trade-audit.jsonl")
        self.clock = clock
        self.lease = lease or TradeLease()
        self.guard = AIGuard(config, risk)
        # Unique lease identity: two AITrader instances must not both think
        # they hold the single "ai" slot.
        self._owner_name = f"ai-{uuid.uuid4().hex[:8]}"
        self._lock = threading.RLock()
        self._state = STATE_STOPPED
        self._halted_reason = ""
        self._paused = False
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._seq = 0
        self._events: deque[dict[str, Any]] = deque(maxlen=200)
        self._counters_day = ""
        self._decisions_today = 0
        self._order_times: deque[float] = deque(maxlen=200)
        self._last_order_at = 0.0
        self.last_decision: dict[str, Any] | None = None

    # ------------------------------------------------------------- lifecycle
    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def start(self) -> tuple[bool, str]:
        c = self.config
        if not c.enabled:
            return False, "AI 自主交易未启用"
        if c.mode == RunMode.LIVE.value:
            if not c.live_enabled:
                return False, "实盘未显式开启"
            if not c.confirm_live:
                return False, "实盘缺少二次确认"
            if c.network != "mainnet":
                return False, "实盘必须使用主网"
            if c.market_type == MarketType.SPOT.value and c.require_stop:
                return False, "现货实盘无法挂交易所侧止损，请使用合约或关闭止损要求"
        with self._lock:
            if self._state == STATE_HALTED:
                return False, f"已熔断：{self._halted_reason}"
            if self._state == STATE_RUNNING:
                return True, "已在运行"
            if not self.lease.acquire(self._owner_name):
                return False, f"交易权已被 {self.lease.owner or '其他'} 占用"
            self._state = STATE_RUNNING
            self._paused = False
            self._stop_event.clear()
        self._audit({"kind": "lifecycle", "event": "start", "mode": c.mode})
        self._thread = threading.Thread(target=self._loop, name="ai-trader", daemon=True)
        self._thread.start()
        return True, "AI 自主交易已启动"

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3.0)
        self._thread = None
        with self._lock:
            if self._state != STATE_HALTED:
                self._state = STATE_STOPPED
        self.lease.release(self._owner_name)
        self._audit({"kind": "lifecycle", "event": "stop"})

    def pause(self, paused: bool = True) -> None:
        with self._lock:
            self._paused = bool(paused)
            if self._state == STATE_RUNNING:
                self._state = STATE_PAUSED if paused else STATE_RUNNING
        self._audit({"kind": "lifecycle", "event": "pause" if paused else "resume"})

    def halt(self, reason: str = "人工干预") -> None:
        with self._lock:
            self._halted_reason = reason or "人工干预"
            self._state = STATE_HALTED
            self._paused = False
        self._audit({"kind": "lifecycle", "event": "halt", "reason": self._halted_reason})

    def kill(self, reason: str = "急停") -> None:
        """急停：熔断 + 停止 poller + 释放交易权（INV-8）。"""
        self.halt(reason)
        self._stop_event.set()
        self.lease.release(self._owner_name)

    # ------------------------------------------------------------------ loop
    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.step()
            except Exception:  # pragma: no cover - poller must never die silently
                log.warning("AI 自主交易轮询失败", exc_info=True)
            self._stop_event.wait(max(1.0, self.config.poll_sec))

    def _roll_counters(self, now: float) -> None:
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        if day != self._counters_day:
            self._counters_day = day
            self._decisions_today = 0
            self._order_times.clear()
            self._last_order_at = 0.0

    def _orders_last_hour(self, now: float) -> int:
        while self._order_times and now - self._order_times[0] > 3600:
            self._order_times.popleft()
        return len(self._order_times)

    # ------------------------------------------------------------------ step
    def step(self) -> dict[str, Any] | None:
        """poller 的一轮：只有运行中且未暂停才真正决策。"""
        with self._lock:
            if self._state != STATE_RUNNING or self._paused:
                return None
        return self.decide_once()

    def decide_once(self) -> dict[str, Any] | None:
        """同步执行一次「取行情 → 问模型 → 校验 → 闸门 → 下单」。

        与状态机解耦，供测试、``/preview`` 和人工「立即决策」复用。
        """
        now = self.clock()
        self._roll_counters(now)
        portfolio = self._portfolio()
        market_price = portfolio.get("price")
        frame = None
        if self.frame_provider is not None:
            try:
                frame = self.frame_provider()
            except Exception as exc:
                record = self._record(
                    "skip", reason=f"行情不可达：{exc}", decision=None, now=now
                )
                return record
        if frame is None or getattr(frame, "empty", True):
            return self._record("skip", reason="行情数据不可用", decision=None, now=now)

        # Strategy first: the model may only confirm or veto the direction that
        # the selected strategy already produced. It never invents an entry.
        signal_target: float | None = None
        if self.signal_provider is not None:
            try:
                signal_target = float(self.signal_provider())
            except Exception as exc:
                return self._record(
                    "skip", reason=f"策略信号不可用：{exc}", decision=None, now=now
                )
            if abs(signal_target) < 1e-9:
                return self._record(
                    "skip", reason="策略无信号（空仓）", decision=None, now=now
                )
        self._last_signal = signal_target

        # 决策次数是模型调用预算：先判上限，避免为一个注定被拒的请求付钱。
        if self.config.max_decisions_per_day and self._decisions_today >= self.config.max_decisions_per_day:
            return self._record(
                "skip", reason="已达 AI 当日决策次数上限", decision=None, now=now
            )

        prompt = self._build_prompt(frame, portfolio, signal_target)
        answer = self.llm.ask(prompt, system="", cache_key="", kind="ai_trader") or {}
        if not answer.get("ok"):
            return self._record(
                "skip", reason=f"模型调用失败：{answer.get('error') or '未知错误'}",
                decision=None, now=now,
            )
        self._decisions_today += 1
        decision, error = parse_decision(str(answer.get("text") or ""), now=now)
        if decision is None:
            return self._record("skip", reason=error, decision=None, now=now)
        errors = validate_decision(decision, self.config)
        if errors:
            return self._record("reject", reason="；".join(errors), decision=decision, now=now)
        if signal_target is not None:
            if signal_target > 0 and decision.action == "open_short":
                return self._record(
                    "reject",
                    reason="策略信号为多，AI 不得反向开空",
                    decision=decision,
                    now=now,
                )
            if signal_target < 0 and decision.action == "open_long":
                return self._record(
                    "reject",
                    reason="策略信号为空，AI 不得反向开多",
                    decision=decision,
                    now=now,
                )
            if decision.is_entry and signal_target == 0:
                return self._record(
                    "reject",
                    reason="策略无信号，AI 不得开仓",
                    decision=decision,
                    now=now,
                )

        guard = self.guard.check(
            decision,
            now=now,
            position_quote=float(portfolio.get("position_quote") or 0.0),
            equity=float(portfolio.get("equity") or 0.0),
            decisions_today=max(0, self._decisions_today - 1),
            orders_last_hour=self._orders_last_hour(now),
            last_order_at=self._last_order_at,
            market_price=market_price,
            kill_switch=bool(portfolio.get("kill_switch")),
            halted_reason=self._halted_reason or str(portfolio.get("halted_reason") or ""),
        )
        if not guard.ok:
            return self._record("reject", reason=guard.reason, decision=decision, now=now)

        if decision.action == "hold":
            return self._record("skip", reason="观望", decision=decision, now=now)

        try:
            order = self._submit(decision, market_price)
        except Exception as exc:
            return self._record(
                "error", reason=f"下单失败：{exc}", decision=decision, now=now
            )
        self._order_times.append(now)
        self._last_order_at = now
        return self._record("order", reason="", decision=decision, now=now, order=order)

    def _submit(self, decision: AIDecision, market_price: float | None) -> OrderResult:
        c = self.config
        price = decision.price or market_price or 0.0
        if price <= 0:
            raise ValueError("没有可用的参考价")
        quote = decision.quote
        amount = quote / price
        side = OrderSide.BUY if decision.action == "open_long" else OrderSide.SELL
        reduce_only = decision.is_close
        req = OrderRequest(
            symbol=c.symbol,
            side=side.value,
            order_type=OrderType.MARKET.value,
            amount=amount,
            price=price,
            quote_amount=quote,
            reduce_only=reduce_only,
            leverage=c.leverage,
            market_type=MarketType(c.market_type),
            mode=RunMode(c.mode),
            stop_loss=decision.stop_loss,
            take_profit=decision.take_profit,
        )
        # 唯一下单出口（INV-1）：任何 broker/交易所调用都不得出现在本模块。
        return self.trading.submit(req)

    # ------------------------------------------------------------- reporting
    def _portfolio(self) -> dict[str, Any]:
        if self.portfolio_provider is None:
            return {}
        try:
            data = self.portfolio_provider() or {}
            return data if isinstance(data, dict) else {}
        except Exception:  # pragma: no cover - a broken view must not place orders
            log.warning("读取 AI 组合视图失败", exc_info=True)
            return {}

    def _build_prompt(
        self,
        frame: Any,
        portfolio: dict[str, Any],
        signal_target: float | None = None,
    ) -> str:
        block = ""
        try:
            block = ai.digest(frame, timeframe=self.config.timeframe, label=self.config.symbol)
        except Exception:  # pragma: no cover - digest is best effort
            block = ""
        position = float(portfolio.get("position_quote") or 0.0)
        equity = float(portfolio.get("equity") or 0.0)
        price = portfolio.get("price")
        signal_line = ""
        if signal_target is not None:
            direction = "做多" if signal_target > 0 else "做空" if signal_target < 0 else "空仓"
            signal_line = (
                f"【策略信号】目标仓位 {signal_target:+.0%}（{direction}）。"
                "你只做复核：同意该方向才可开对应方向；不同意就 hold。"
                "不得反向开仓，不得在空仓信号下开新仓。\n"
            )
        return (
            f"{DECISION_PROMPT}\n\n{signal_line}"
            f"【账户】权益 {equity:.2f} USDT，"
            f"当前持仓 {position:.2f} USDT，最新价 {price}。\n"
            f"【上限】单笔 {self.config.max_order_quote:.2f} USDT，"
            f"最大持仓 {self.config.max_position_quote:.2f} USDT，"
            f"最小置信度 {self.config.min_confidence:.2f}。\n\n{block}"
        )

    def _record(
        self,
        outcome: str,
        *,
        reason: str,
        decision: AIDecision | None,
        now: float,
        order: OrderResult | None = None,
    ) -> dict[str, Any]:
        self._seq += 1
        order_row = None
        if order is not None:
            try:
                order_row = order.order
            except Exception:  # pragma: no cover
                order_row = None
        record = {
            "at": now,
            "seq": self._seq,
            "kind": "decision",
            "outcome": outcome,
            "reason": reason,
            "decision_id": decision.decision_id if decision else "",
            "action": decision.action if decision else "",
            "symbol": decision.symbol if decision else self.config.symbol,
            "quote": decision.quote if decision else 0.0,
            "price": decision.price if decision else None,
            "confidence": decision.confidence if decision else 0.0,
            "signal_target": self._last_signal,
            "stop_loss": decision.stop_loss if decision else None,
            "rationale": decision.rationale if decision else "",
            "order_id": (order_row or {}).get("id") if isinstance(order_row, dict) else None,
            "mode": self.config.mode,
            "state": self.state,
        }
        self.last_decision = record
        with self._lock:
            self._events.append(record)
        self._audit(record)
        return record

    def _audit(self, record: dict[str, Any]) -> None:
        """每笔决策/状态变更追加一行；写失败绝不能打断交易（INV-15）。"""
        try:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:  # pragma: no cover - disk full etc.
            log.warning("写入 AI 交易审计失败", exc_info=True)

    def status(self, since: int = 0) -> dict[str, Any]:
        now = self.clock()
        self._roll_counters(now)
        events = [item for item in list(self._events) if int(item.get("seq") or 0) > since]
        return {
            "state": self.state,
            "paused": self._paused,
            "halted_reason": self._halted_reason,
            "config": {
                "mode": self.config.mode,
                "symbol": self.config.symbol,
                "market_type": self.config.market_type,
                "timeframe": self.config.timeframe,
                "live_enabled": self.config.live_enabled,
                "max_order_quote": self.config.max_order_quote,
                "max_position_quote": self.config.max_position_quote,
            },
            "counters": {
                "decisions_today": self._decisions_today,
                "orders_last_hour": self._orders_last_hour(now),
                "max_decisions_per_day": self.config.max_decisions_per_day,
                "max_orders_per_hour": self.config.max_orders_per_hour,
            },
            "last_decision": self.last_decision,
            "events": events,
            "seq": self._seq,
        }
