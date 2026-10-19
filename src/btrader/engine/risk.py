"""Deterministic risk checks applied before any order leaves the app."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..core.config import RiskSettings
from ..core.types import MarketType

log = logging.getLogger(__name__)


@dataclass
class RiskDecision:
    allowed: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.allowed


@dataclass
class _DayState:
    day: str = ""
    realized_pnl: float = 0.0
    trades: int = 0
    consecutive_losses: int = 0
    halted_reason: str = ""
    # A manual resume does not erase what was lost today — it moves the line.
    # The daily budget is measured from these baselines instead, so the user
    # can keep trading after acknowledging the drawdown, and the breaker still
    # fires again if the next stretch loses another full allowance.
    pnl_baseline: float = 0.0
    trades_baseline: int = 0

    @property
    def day_pnl(self) -> float:
        return self.realized_pnl - self.pnl_baseline

    @property
    def day_trades(self) -> int:
        return self.trades - self.trades_baseline


class RiskManager:
    """Tracks daily PnL / trade counts and rejects unsafe orders.

    The manager is intentionally simple and deterministic; every rejection is
    returned with a human readable reason so the UI can explain it.
    """

    def __init__(self, settings: RiskSettings) -> None:
        self.settings = settings
        self._lock = threading.RLock()
        self._state = _DayState(day=_today())
        self._loss_events: list[float] = []

    # ------------------------------------------------------------- lifecycle
    @staticmethod
    def _roll(state: _DayState) -> _DayState:
        today = _today()
        if state.day != today:
            return _DayState(day=today)
        return state

    def reset_day(self) -> None:
        with self._lock:
            self._state = _DayState(day=_today())

    @property
    def state(self) -> _DayState:
        with self._lock:
            self._state = self._roll(self._state)
            return self._state

    # --------------------------------------------------------------- checks
    def check_order(
        self,
        quote_notional: float,
        current_position_quote: float,
        added_quote: float,
        leverage: float = 1.0,
        entry: bool = True,
        equity: float = 0.0,
    ) -> RiskDecision:
        s = self.settings
        state = self.state

        if s.kill_switch:
            return RiskDecision(False, "全局急停已开启，请先在风控面板解除。")
        if state.halted_reason:
            return RiskDecision(False, f"风控熔断中：{state.halted_reason}")
        if s.max_daily_trades and state.day_trades >= s.max_daily_trades:
            return RiskDecision(False, f"已达当日最大交易次数（{s.max_daily_trades}）。")
        if s.max_daily_loss and state.day_pnl <= -abs(s.max_daily_loss):
            return RiskDecision(False, f"已达当日最大亏损（{s.max_daily_loss:.2f}）。")
        if entry:
            limit, label = self._order_limit(equity)
            if limit and quote_notional > limit + 1e-9:
                return RiskDecision(
                    False, f"单笔金额 {quote_notional:.2f} 超过上限 {limit:.2f}{label}。"
                )
        if entry and s.max_position_quote and current_position_quote + added_quote > s.max_position_quote + 1e-9:
            return RiskDecision(
                False,
                f"持仓金额将达 {current_position_quote + added_quote:.2f}，超过上限 {s.max_position_quote:.2f}。",
            )
        if leverage > s.max_leverage + 1e-9:
            return RiskDecision(False, f"杠杆 {leverage:g}x 超过上限 {s.max_leverage:g}x。")
        return RiskDecision(True)

    def _order_limit(self, equity: float) -> tuple[float, str]:
        """Effective per-order notional cap, and a short label for the reason.

        ``max_order_quote == 0`` keeps its established meaning of "no cap at
        all". Otherwise the cap scales with equity but never drops below the
        fixed ceiling: without that, a full-size buy from a 10,000 balance was
        refused by a 500 USDT constant while the panel's "全部" button offered
        exactly that amount.
        """
        s = self.settings
        fixed = s.max_order_quote if s.max_order_quote else 0.0
        if not fixed:
            return 0.0, ""
        pct_limit = abs(equity) * s.max_order_pct if s.max_order_pct and equity > 0 else 0.0
        if pct_limit > fixed:
            return pct_limit, f"（账户权益 {equity:.2f} 的 {s.max_order_pct:.0%}）"
        return fixed, "（固定上限）"

    def check_market(self, market: MarketType) -> RiskDecision:
        if market.is_futures and self.settings.max_leverage < 1:
            return RiskDecision(False, "合约杠杆上限配置无效。")
        return RiskDecision(True)

    # ------------------------------------------------------------ accounting
    def record_trade(self, realized_pnl: float) -> None:
        """Record a closed trade outcome (positive = profit).

        Only the realized P&L and the losing streak are tracked here; the
        order counter belongs to :meth:`record_order` so one entry/exit pair
        is not counted three times against ``max_daily_trades``.
        """
        with self._lock:
            state = self._roll(self._state)
            state.realized_pnl += realized_pnl
            if realized_pnl < 0:
                state.consecutive_losses += 1
            else:
                state.consecutive_losses = 0
            if (
                self.settings.max_consecutive_losses
                and state.consecutive_losses >= self.settings.max_consecutive_losses
            ):
                state.halted_reason = f"连续亏损 {state.consecutive_losses} 笔"
                log.warning("风控熔断：%s", state.halted_reason)
            self._state = state

    def record_order(self) -> None:
        with self._lock:
            state = self._roll(self._state)
            state.trades += 1
            self._state = state

    def clear_halt(self) -> None:
        """Manually resume after a breaker fired.

        The day's history is kept: baselines are moved to "now" so the next
        stretch gets a fresh allowance instead of instantly re-tripping on the
        loss that caused the halt.
        """
        with self._lock:
            state = self._roll(self._state)
            state.halted_reason = ""
            state.consecutive_losses = 0
            state.pnl_baseline = state.realized_pnl
            state.trades_baseline = state.trades
            self._state = state

    def snapshot(self) -> dict:
        """Current risk state for the UI.

        The daily-loss and daily-count caps are evaluated here as well: they
        are enforced inside :meth:`check_order` without stamping
        ``halted_reason``, so relying on that field alone reported "normal"
        exactly when orders were being refused.
        """
        with self._lock:
            state = self._roll(self._state)
            s = self.settings
            reason = state.halted_reason
            if not reason and s.max_daily_loss and state.day_pnl <= -abs(s.max_daily_loss):
                reason = f"当日亏损 {state.day_pnl:.2f} 已达上限 {s.max_daily_loss:.2f}"
            if not reason and s.max_daily_trades and state.day_trades >= s.max_daily_trades:
                reason = f"当日交易 {state.day_trades} 笔已达上限 {s.max_daily_trades}"
            if not reason and s.kill_switch:
                reason = "全局急停已开启"
            return {
                "day": state.day,
                "realized_pnl": state.realized_pnl,
                "day_pnl": state.day_pnl,
                "trades": state.trades,
                "day_trades": state.day_trades,
                "consecutive_losses": state.consecutive_losses,
                "halted": bool(reason),
                "halted_reason": reason,
                "kill_switch": bool(s.kill_switch),
                "limits": {
                    "max_daily_loss": s.max_daily_loss,
                    "max_daily_trades": s.max_daily_trades,
                    "max_consecutive_losses": s.max_consecutive_losses,
                    "max_order_quote": s.max_order_quote,
                    "max_order_pct": s.max_order_pct,
                    "max_position_quote": s.max_position_quote,
                },
            }


def _today() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
