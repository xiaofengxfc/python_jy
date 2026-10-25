"""Risk manager rules: the limits, the breakers and their persistence.

The simulated account these used to run against is gone — orders now only exist
on the venue — so everything here tests the decisions themselves: what is
refused, what is still allowed while halted, and what survives a restart.
"""

from __future__ import annotations

import pytest

from btrader.core.config import RiskSettings
from btrader.core.storage import Database
from btrader.engine.risk import RiskManager

def _risk(**kwargs) -> RiskManager:
    return RiskManager(RiskSettings(**kwargs))


def test_order_above_limit_rejected():
    risk = _risk(max_order_quote=100)
    assert not risk.check_order(150, 0, 150)
    assert risk.check_order(90, 0, 90)


def test_position_limit_rejected():
    risk = _risk(max_position_quote=1000, max_order_quote=10_000)
    decision = risk.check_order(400, 800, 400)
    assert not decision
    assert "持仓" in decision.reason


def test_daily_loss_halts():
    risk = _risk(max_daily_loss=100)
    risk.record_trade(-60)
    assert risk.check_order(10, 0, 10).allowed
    risk.record_trade(-50)
    assert not risk.check_order(10, 0, 10).allowed


def test_consecutive_losses_halt():
    risk = _risk(max_consecutive_losses=3, max_daily_loss=0)
    for _ in range(3):
        risk.record_trade(-5)
    decision = risk.check_order(10, 0, 10)
    assert not decision
    assert "连续亏损" in decision.reason
    risk.clear_halt()
    assert risk.check_order(10, 0, 10).allowed


def test_clear_halt_keeps_daily_cap_enforceable():
    """Resuming must not erase the day's loss: the breaker can fire again."""
    risk = _risk(max_daily_loss=100, max_consecutive_losses=3)
    for _ in range(3):
        risk.record_trade(-40)
    assert not risk.check_order(1, 0, 1).allowed

    risk.clear_halt()
    state = risk.snapshot()
    assert state["halted"] is False
    assert state["day_pnl"] == pytest.approx(0.0)
    assert state["realized_pnl"] == pytest.approx(-120.0)
    assert risk.check_order(1, 0, 1).allowed

    # A further full allowance of losses trips the daily cap again.
    risk.record_trade(-101)
    assert not risk.check_order(1, 0, 1).allowed


def test_risk_snapshot_reports_halt_and_limits():
    risk = _risk(max_daily_loss=100, max_consecutive_losses=2, max_daily_trades=50)
    risk.record_trade(-150)
    state = risk.snapshot()
    assert state["halted"] is True
    assert state["day_pnl"] == pytest.approx(-150.0)
    assert state["limits"]["max_daily_trades"] == 50
    assert state["limits"]["max_order_pct"] == 0.25


def test_kill_switch_blocks_everything():
    risk = _risk(kill_switch=True)
    decision = risk.check_order(1, 0, 1)
    assert not decision
    assert "急停" in decision.reason


def test_kill_switch_still_allows_an_exit():
    """Emergency stop is a gate on new risk, not a lock on the position.

    Refusing the reduce-only order would trap the user inside the exposure the
    switch was meant to stop them adding to.
    """
    risk = _risk(kill_switch=True)
    assert risk.check_order(1, 0, 1, entry=False).allowed


def test_chain_loss_halt_still_allows_an_exit():
    risk = _risk(max_consecutive_losses=1, max_order_quote=0, max_position_quote=0)
    risk.record_trade(-10)
    assert not risk.check_order(1, 0, 1, entry=True).allowed
    assert risk.check_order(1, 0, 1, entry=False).allowed


def test_daily_loss_halt_still_allows_an_exit():
    risk = _risk(max_daily_loss=100, max_order_quote=0, max_position_quote=0)
    risk.record_trade(-150)
    assert not risk.check_order(1, 0, 1, entry=True).allowed
    assert risk.check_order(1, 0, 1, entry=False).allowed


def test_daily_trade_cap_still_allows_an_exit():
    risk = _risk(max_daily_trades=1, max_order_quote=0, max_position_quote=0)
    risk.record_order()
    assert not risk.check_order(1, 0, 1, entry=True).allowed
    assert risk.check_order(1, 0, 1, entry=False).allowed


def test_leverage_limit():
    risk = _risk(max_leverage=5, max_order_quote=0, max_position_quote=0)
    assert not risk.check_order(10, 0, 10, leverage=10)
    assert risk.check_order(10, 0, 10, leverage=5)


def test_order_cap_scales_with_equity():
    """A 10,000 balance must be able to buy more than the fixed 500 ceiling."""
    risk = _risk(max_order_quote=500, max_order_pct=0.25)
    # equity 10,000 -> 25% = 2,500 wins over the fixed 500
    assert risk.check_order(2_400, 0, 2_400, equity=10_000).allowed
    decision = risk.check_order(2_600, 0, 2_600, equity=10_000)
    assert not decision and "上限" in decision.reason
    # a small balance falls back to the fixed ceiling, which is the tighter one
    assert risk.check_order(400, 0, 400, equity=1_000).allowed
    assert not risk.check_order(600, 0, 600, equity=1_000)


def test_order_cap_zero_means_disabled():
    """max_order_quote=0 has always meant "no cap"; the percentage must obey."""
    risk = _risk(max_order_quote=0, max_order_pct=0.25, max_position_quote=0)
    assert risk.check_order(9_999, 0, 9_999, equity=10_000).allowed


def test_order_cap_counts_margin_not_notional():
    """合约的上限量的是**占用保证金**，不是名义额。

    10 倍下点「25%」是「权益 250% 的名义额」。按名义额卡的话，杠杆一调高就
    一单都下不出去（用户看到的是「杠杆设了不生效」）。先折回保证金再比：
    名义额 2500 / 10x = 保证金 250，在 500 的上限之内。
    """
    risk = _risk(max_order_quote=500, max_order_pct=0.25, max_position_quote=0)
    assert risk.check_order(2_500, 0, 2_500, leverage=10).allowed
    decision = risk.check_order(6_000, 0, 6_000, leverage=10)
    assert not decision
    assert "保证金" in decision.reason
    assert "10x" in decision.reason and "600.00" in decision.reason
    # 权益 10,000 时上限升到 25%（2,500）：10 倍下名义额 25,000 才到线
    assert risk.check_order(25_000, 0, 25_000, leverage=10, equity=10_000).allowed
    assert not risk.check_order(25_100, 0, 25_100, leverage=10, equity=10_000).allowed


def test_leverage_one_reads_as_a_plain_notional_cap():
    """现货（杠杆恒 1）的判定与文案逐位不变：老的单据与提示不该跟着换说法。"""
    risk = _risk(max_order_quote=100)
    decision = risk.check_order(150, 0, 150, leverage=1)
    assert not decision
    assert decision.reason == "单笔金额 150.00 超过上限 100.00（固定上限）。"


def test_realized_pnl_feeds_daily_loss_breaker():
    """record_trade must actually move the daily-loss counter."""
    risk = _risk(max_daily_loss=100, max_order_quote=0, max_position_quote=0)
    risk.record_trade(-101)
    decision = risk.check_order(10, 0, 10)
    assert not decision and "当日最大亏损" in decision.reason


def test_order_count_not_double_counted_by_record_trade():
    risk = _risk(max_daily_trades=2, max_order_quote=0, max_position_quote=0)
    risk.record_order()
    risk.record_order()
    # Exits must not consume the daily order budget a second time.
    risk.record_trade(5.0)
    decision = risk.check_order(10, 0, 10)
    assert not decision and "最大交易次数" in decision.reason


def test_risk_halt_survives_restart(tmp_path):
    """Closing the app used to clear a halt, making the breaker advisory."""
    from btrader.core.storage import Database

    db = Database(tmp_path / "risk.db")
    settings = RiskSettings(max_consecutive_losses=2, max_daily_loss=0)
    first = RiskManager(settings, db)
    first.record_trade(-10)
    first.record_trade(-10)
    assert not first.check_order(10, 0, 10)

    restarted = RiskManager(settings, db)
    decision = restarted.check_order(10, 0, 10)
    assert not decision, "a restart must not clear the halt"
    assert "连续亏损" in decision.reason
    db.close()


def test_risk_state_from_an_older_day_is_discarded(tmp_path):
    """The caps are per-day; yesterday's loss must not block this morning."""
    from btrader.core.storage import Database
    from btrader.core.types import Network
    from btrader.engine.risk import risk_state_key

    db = Database(tmp_path / "risk2.db")
    settings = RiskSettings(max_daily_loss=100, max_consecutive_losses=0)
    first = RiskManager(settings, db)
    first.record_trade(-150)
    assert not first.check_order(10, 0, 10)

    key = risk_state_key(Network.MAINNET.value)
    stored = db.get_json(key)
    stored["day"] = "2000-01-01"
    db.set_json(key, stored)

    restarted = RiskManager(settings, db)
    assert restarted.check_order(10, 0, 10).allowed
    db.close()
