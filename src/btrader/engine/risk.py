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
    ) -> RiskDecision:
        s = self.settings
        state = self.state

        if s.kill_switch:
            return RiskDecision(False, "全局急停已开启，请先在风控面板解除。")
        if state.halted_reason:
            return RiskDecision(False, f"风控熔断中：{state.halted_reason}")
        if s.max_daily_trades and state.trades >= s.max_daily_trades:
            return RiskDecision(False, f"已达当日最大交易次数（{s.max_daily_trades}）。")
        if s.max_daily_loss and state.realized_pnl <= -abs(s.max_daily_loss):
            return RiskDecision(False, f"已达当日最大亏损（{s.max_daily_loss:.2f}）。")
        if entry and s.max_order_quote and quote_notional > s.max_order_quote + 1e-9:
            return RiskDecision(
                False,
                f"单笔金额 {quote_notional:.2f} 超过上限 {s.max_order_quote:.2f}。",
            )
        if entry and s.max_position_quote and current_position_quote + added_quote > s.max_position_quote + 1e-9:
            return RiskDecision(
                False,
                f"持仓金额将达 {current_position_quote + added_quote:.2f}，超过上限 {s.max_position_quote:.2f}。",
            )
        if leverage > s.max_leverage + 1e-9:
            return RiskDecision(False, f"杠杆 {leverage:g}x 超过上限 {s.max_leverage:g}x。")
        return RiskDecision(True)

    def check_market(self, market: MarketType) -> RiskDecision:
        if market.is_futures and self.settings.max_leverage < 1:
            return RiskDecision(False, "合约杠杆上限配置无效。")
        return RiskDecision(True)

    # ------------------------------------------------------------ accounting
    def record_trade(self, realized_pnl: float) -> None:
        """Record a closed trade outcome (positive = profit)."""
        with self._lock:
            state = self._roll(self._state)
            state.trades += 1
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
        with self._lock:
            state = self._roll(self._state)
            state.halted_reason = ""
            state.consecutive_losses = 0
            self._state = state


def _today() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
