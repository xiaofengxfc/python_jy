"""Deterministic risk checks applied before any order leaves the app."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from ..core.clock import local_day
from ..core.config import RiskSettings
from ..core.storage import Database
from ..core.types import MarketType, Network

log = logging.getLogger(__name__)

# settings_kv key holding the day's risk counters. Without it a restart reset
# the daily-loss and losing-streak breakers, which made them advisory in live
# trading: closing the app cleared the halt.
RISK_STATE_KEY = "risk.state.v1"


def risk_state_key(scope: str) -> str:
    """当日计数与熔断状态的 key，按作用域（现在是网络）分开。

    主网与测试网共用一份时，测试网上的一次亏损会把主网的当日额度用掉，主网熔断
    也会让测试网起不来——限额是账户级的，不是程序级的，两个账户就该各数各的。
    """
    return f"{RISK_STATE_KEY}:{scope}"


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
    returned with a human readable reason so the UI can explain it. State is
    mirrored into ``settings_kv`` after every write so a restart cannot quietly
    drop a halt or reset the day's loss budget.
    """

    def __init__(
        self,
        settings: RiskSettings,
        db: "Database | None" = None,
        scope: str = Network.MAINNET.value,
    ) -> None:
        self.settings = settings
        self._scope = scope
        self._lock = threading.RLock()
        self._state = _DayState(day=_today())
        self._loss_events: list[float] = []
        self.db = db
        if db is not None:
            self.restore()

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
            self._persist()

    def set_scope(self, scope: str) -> None:
        """换作用域（切网络）时读回那一侧的当日状态。

        另一侧的数据留在库里不动：切回主网时熔断与当日盈亏还在，不会被测试网的一次
        运行冲掉。day 不是今天的行本来就会被 restore 丢弃，所以这里只管换 key。
        """
        with self._lock:
            if scope == self._scope:
                return
            self._scope = scope
            self._state = _DayState(day=_today())
        self.restore()

    def restore(self) -> bool:
        """Load the persisted counters, if they belong to today.

        A row from an earlier day is discarded: the caps are per-day, so
        yesterday's losses must not block today's first order.
        """
        if self.db is None:
            return False
        key = risk_state_key(self._scope)
        try:
            data = self.db.get_json(key)
            if data is None:
                # 0.6.3 只有一份 risk.state.v1（没有网络后缀）。不搬的话，升级上来的
                # 用户当天已经用掉的额度与熔断状态会凭空复位，限额头一天等于不存在。
                if self.db.adopt_legacy_key(RISK_STATE_KEY, key):
                    data = self.db.get_json(key)
        except Exception:  # pragma: no cover - a corrupt row must not block startup
            log.warning("读取风控状态失败", exc_info=True)
            return False
        if not isinstance(data, dict) or data.get("day") != _today():
            return False
        with self._lock:
            self._state = _DayState(
                day=data.get("day") or _today(),
                realized_pnl=float(data.get("realized_pnl") or 0.0),
                trades=int(data.get("trades") or 0),
                consecutive_losses=int(data.get("consecutive_losses") or 0),
                halted_reason=str(data.get("halted_reason") or ""),
                pnl_baseline=float(data.get("pnl_baseline") or 0.0),
                trades_baseline=int(data.get("trades_baseline") or 0),
            )
        if self._state.halted_reason:
            log.warning("恢复风控熔断状态：%s", self._state.halted_reason)
        return True

    def _persist(self) -> None:
        """Mirror the current day state. Best effort: history must not throw."""
        if self.db is None:
            return
        state = self._state
        try:
            self.db.set_json(
                risk_state_key(self._scope),
                {
                    "day": state.day,
                    "realized_pnl": state.realized_pnl,
                    "trades": state.trades,
                    "consecutive_losses": state.consecutive_losses,
                    "halted_reason": state.halted_reason,
                    "pnl_baseline": state.pnl_baseline,
                    "trades_baseline": state.trades_baseline,
                },
            )
        except Exception:  # pragma: no cover - never fail an order over history
            log.warning("保存风控状态失败", exc_info=True)

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

        # Every breaker below guards *new exposure*. Opening more risk while
        # halted is what loses money; blocking a reduce-only exit would keep
        # the user locked inside the position the breaker just complained
        # about — strictly worse than the loss it was trying to prevent.
        if entry:
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
            if limit:
                # 上限量的是这一单吃掉多少**保证金**。合约里名义额天生是它的
                # leverage 倍：拿名义额直接比上限，杠杆越高越容易被拒——10 倍下
                # 点「25%」（名义额 = 权益的 250%）必撞「单笔金额超过上限」，
                # 用户看到的就是「杠杆一调高，一单都下不出去」。先折回保证金再比。
                # leverage <= 1（现货恒 1）时不除，判定与文案都和以前逐位相同。
                margin = quote_notional / leverage if leverage > 1 else quote_notional
                if margin > limit + 1e-9:
                    if leverage > 1:
                        reason = (
                            f"单笔保证金 {margin:.2f}（名义额 {quote_notional:.2f} ÷ "
                            f"{leverage:g}x）超过上限 {limit:.2f}{label}。"
                        )
                    else:
                        reason = f"单笔金额 {quote_notional:.2f} 超过上限 {limit:.2f}{label}。"
                    return RiskDecision(False, reason)
        if entry and s.max_position_quote and current_position_quote + added_quote > s.max_position_quote + 1e-9:
            return RiskDecision(
                False,
                f"持仓金额将达 {current_position_quote + added_quote:.2f}，超过上限 {s.max_position_quote:.2f}。",
            )
        if entry and leverage > s.max_leverage + 1e-9:
            return RiskDecision(False, f"杠杆 {leverage:g}x 超过上限 {s.max_leverage:g}x。")
        return RiskDecision(True)

    def _order_limit(self, equity: float) -> tuple[float, str]:
        """Effective per-order **margin** cap, and a short label for the reason.

        ``max_order_quote == 0`` keeps its established meaning of "no cap at
        all". Otherwise the cap scales with equity but never drops below the
        fixed ceiling: without that, a full-size buy from a 10,000 balance was
        refused by a 500 USDT constant while the panel's "全部" button offered
        exactly that amount.

        比较的是占用的保证金（``check_order`` 把名义额按杠杆折回来），不是名义额：
        期货 10 倍时名义额天然是权益的数倍，按名义额卡等于「杠杆越高越下不了单」。
        持仓上限（``max_position_quote``）仍然量名义额，两者不是一回事。
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
            self._persist()

    def record_order(self) -> None:
        with self._lock:
            state = self._roll(self._state)
            state.trades += 1
            self._state = state
            self._persist()

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
            self._persist()

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
    """本机时区的今天（见 core.clock）：和 AI 决策次数、模型调用次数同一口径。

    以前这里按 UTC 取日期，另外两处按本机时区，UTC+8 下熔断的「当日亏损」
    早上 8 点就清零，而决策次数要到午夜——同一份审计里两个「今日」不是一天。
    """
    return local_day()
