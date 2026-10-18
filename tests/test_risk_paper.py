"""Risk manager and paper broker tests."""

from __future__ import annotations

from btrader.core.config import RiskSettings
from btrader.engine.paper import PaperBroker
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


def test_kill_switch_blocks_everything():
    risk = _risk(kill_switch=True)
    decision = risk.check_order(1, 0, 1)
    assert not decision
    assert "急停" in decision.reason


def test_leverage_limit():
    risk = _risk(max_leverage=5, max_order_quote=0, max_position_quote=0)
    assert not risk.check_order(10, 0, 10, leverage=10)
    assert risk.check_order(10, 0, 10, leverage=5)


def test_paper_market_buy_updates_cash_and_position():
    broker = PaperBroker(starting_balance=1_000, fee_rate=0.001, slippage_bps=0)
    order = broker.place_order("BTC/USDT", "buy", "market", 0.5, price=100.0)
    assert order["status"] == "closed"
    assert abs(broker.account.cash - (1000 - 50 - 0.05)) < 1e-9
    position = broker.account.position_for("BTC/USDT")
    assert position.contracts == 0.5
    assert position.entry_price == 100.0


def test_paper_round_trip_pnl():
    broker = PaperBroker(starting_balance=1_000, fee_rate=0.0, slippage_bps=0)
    broker.place_order("ETH/USDT", "buy", "market", 2.0, price=100.0)
    broker.place_order("ETH/USDT", "sell", "market", 2.0, price=110.0)
    position = broker.account.position_for("ETH/USDT")
    assert position.contracts == 0.0
    assert position.realized_pnl == 20.0
    assert broker.account.cash == 1_020.0


def test_paper_limit_order_rests_then_fills():
    broker = PaperBroker(starting_balance=1_000, fee_rate=0.0, slippage_bps=0)
    order = broker.place_order("BTC/USDT", "buy", "limit", 1.0, price=99.0)
    assert order["status"] == "open"
    assert broker.try_fill("BTC/USDT", high=101, low=100) == []
    filled = broker.try_fill("BTC/USDT", high=101, low=98)
    assert len(filled) == 1
    assert broker.account.position_for("BTC/USDT").contracts == 1.0


def test_paper_short_and_flip():
    broker = PaperBroker(starting_balance=1_000, fee_rate=0.0, slippage_bps=0)
    broker.place_order("BTC/USDT", "sell", "market", 1.0, price=100.0)
    assert broker.account.position_for("BTC/USDT").contracts == -1.0
    broker.place_order("BTC/USDT", "buy", "market", 2.0, price=90.0)
    position = broker.account.position_for("BTC/USDT")
    assert position.contracts == 1.0
    assert position.entry_price == 90.0
    assert position.realized_pnl == 10.0


def test_paper_reset():
    broker = PaperBroker(starting_balance=500)
    broker.place_order("BTC/USDT", "buy", "market", 1.0, price=100)
    broker.reset()
    assert broker.account.cash == 500
    assert broker.account.positions == {}
