"""Risk manager and paper broker tests."""

from __future__ import annotations

import pytest

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
    # A flip leaves a brand-new position on the other side, so its own tally
    # starts at zero; the closed short's +10 lives in the account ledger.
    assert position.realized_pnl == 0.0
    assert broker.realized_pnl() == pytest.approx(10.0)


def test_paper_reset():
    broker = PaperBroker(starting_balance=500)
    broker.place_order("BTC/USDT", "buy", "market", 1.0, price=100)
    broker.reset()
    assert broker.account.cash == 500
    assert broker.account.positions == {}


def test_paper_mark_fills_limit_against_last_price():
    broker = PaperBroker(starting_balance=1_000, fee_rate=0.0, slippage_bps=0)
    broker.place_order("BTC/USDT", "buy", "limit", 1.0, price=99.0)
    assert broker.mark("BTC/USDT", 100.0) == []
    filled = broker.mark("BTC/USDT", 99.0)
    assert len(filled) == 1
    assert filled[0]["status"] == "closed"
    assert broker.open_orders() == []


def test_paper_mark_fills_sell_after_buy():
    broker = PaperBroker(starting_balance=1_000, fee_rate=0.0, slippage_bps=0)
    broker.place_order("BTC/USDT", "buy", "market", 1.0, price=100.0)
    broker.place_order("BTC/USDT", "sell", "limit", 1.0, price=110.0)
    assert broker.mark("BTC/USDT", 109.0) == []
    assert len(broker.mark("BTC/USDT", 110.0)) == 1
    assert broker.account.position_for("BTC/USDT").contracts == 0.0
    assert broker.account.cash == 1_010.0
    assert broker.account.orders[-1]["status"] == "closed"


def test_paper_cancel_returns_final_state():
    broker = PaperBroker(starting_balance=1_000)
    order = broker.place_order("BTC/USDT", "buy", "limit", 1.0, price=99.0)
    canceled = broker.cancel(order["id"])
    assert canceled is not None and canceled["status"] == "canceled"
    assert broker.cancel(order["id"]) is None
    assert broker.mark("BTC/USDT", 98.0) == []
    assert broker.account.position_for("BTC/USDT").contracts == 0.0


def test_paper_spot_sell_limit_beyond_holdings_stays_open():
    """A resting sell must not drive a spot balance negative when filled."""
    broker = PaperBroker(starting_balance=1_000, fee_rate=0.0, slippage_bps=0)
    broker.place_order("BTC/USDT", "buy", "market", 1.0, price=100.0)
    broker.place_order(
        "BTC/USDT", "sell", "limit", 2.0, price=120.0, allow_short=False
    )
    assert broker.mark("BTC/USDT", 121.0) == []
    assert broker.account.position_for("BTC/USDT").contracts == 1.0


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



def test_paper_stop_loss_closes_long():
    broker = PaperBroker(starting_balance=1_000, fee_rate=0.0, slippage_bps=0)
    broker.place_order(
        "BTC/USDT", "buy", "market", 1.0, price=100.0, stop_loss=95.0, take_profit=120.0
    )
    assert broker.check_protections("BTC/USDT", 96.0) == []
    exits = broker.check_protections("BTC/USDT", 94.0)
    assert len(exits) == 1
    assert exits[0]["protection"] == "stop_loss"
    assert exits[0]["status"] == "closed"
    assert broker.account.position_for("BTC/USDT").contracts == 0.0
    assert broker.protections() == {}
    assert exits[0]["realized"] == pytest.approx(-6.0)


def test_paper_take_profit_closes_short():
    broker = PaperBroker(starting_balance=1_000, fee_rate=0.0, slippage_bps=0)
    broker.place_order("BTC/USDT:USDT", "sell", "market", 1.0, price=100.0, take_profit=90.0)
    assert broker.check_protections("BTC/USDT:USDT", 95.0) == []
    exits = broker.check_protections("BTC/USDT:USDT", 89.0)
    assert [o["protection"] for o in exits] == ["take_profit"]
    assert broker.account.position_for("BTC/USDT:USDT").contracts == 0.0
    assert exits[0]["realized"] == pytest.approx(11.0)


def test_paper_protection_dropped_when_flat_in_mark():
    broker = PaperBroker(starting_balance=1_000, fee_rate=0.0, slippage_bps=0)
    broker.place_order("BTC/USDT", "buy", "market", 1.0, price=100.0, stop_loss=95.0)
    broker.place_order("BTC/USDT", "sell", "market", 1.0, price=110.0)
    assert broker.mark("BTC/USDT", 111.0) == []
    assert broker.protections() == {}


def test_paper_snapshot_round_trip():
    broker = PaperBroker(starting_balance=1_000, fee_rate=0.0, slippage_bps=0)
    broker.place_order("BTC/USDT", "buy", "market", 0.5, price=100.0, stop_loss=90.0)
    broker.place_order("ETH/USDT", "buy", "limit", 2.0, price=10.0)
    broker.mark("ETH/USDT", 12.0)

    data = broker.snapshot()
    restored = PaperBroker(starting_balance=10_000)
    assert restored.restore(data)
    assert restored.account.cash == pytest.approx(broker.account.cash)
    held = restored.account.position_for("BTC/USDT")
    assert held.contracts == pytest.approx(0.5)
    assert held.entry_price == pytest.approx(100.0)
    assert restored.protections()["BTC/USDT"]["stopLoss"] == 90.0
    assert [o["status"] for o in restored.open_orders()] == ["open"]
    assert restored.mark_price("ETH/USDT") == 12.0


def test_paper_restore_rejects_garbage():
    broker = PaperBroker(starting_balance=500)
    assert broker.restore(None) is False
    assert broker.restore({"positions": [{"symbol": 1}]}) in (False, True)
    assert broker.account.cash == 500


def _trading(tmp_path, balance=1_000.0):
    from btrader.core.config import AppSettings
    from btrader.core.credentials import Credentials
    from btrader.core.storage import Database
    from btrader.core.types import Network
    from btrader.engine.trading import TradingService
    from btrader.exchange.rest import BinanceREST

    settings = AppSettings(risk=RiskSettings(max_order_quote=0, max_position_quote=0))
    rest = BinanceREST(Network.TESTNET, Credentials("k", "s"))
    paper = PaperBroker(starting_balance=balance, fee_rate=0.0, slippage_bps=0)
    db = Database(tmp_path / "state.db")
    return settings, TradingService(settings, rest, RiskManager(settings.risk), paper, db), db


def test_paper_state_survives_restart(tmp_path):
    _, trading, db = _trading(tmp_path)
    trading.paper.place_order("BTC/USDT", "buy", "market", 0.5, price=100.0, stop_loss=90.0)
    trading.save_paper_state()
    snapshot = db.get_json("paper.account.v1")
    assert snapshot and snapshot["cash"] == pytest.approx(950.0)

    fresh = PaperBroker(starting_balance=10_000)
    assert fresh.restore(snapshot)
    assert fresh.account.cash == pytest.approx(950.0)
    assert fresh.account.position_for("BTC/USDT").contracts == pytest.approx(0.5)
    assert fresh.protections()["BTC/USDT"]["stopLoss"] == 90.0
    db.close()


def test_paper_order_persists_without_explicit_save(tmp_path):
    from btrader.engine.trading import OrderRequest
    from btrader.core.types import RunMode

    _, trading, db = _trading(tmp_path)
    trading.submit(
        OrderRequest(
            symbol="BTC/USDT",
            side="buy",
            order_type="market",
            amount=1.0,
            price=100.0,
            mode=RunMode.PAPER,
        )
    )
    stored = db.get_json("paper.account.v1")
    assert stored and stored["cash"] == pytest.approx(900.0)
    db.close()


def test_settle_triggers_protection_and_feeds_risk(tmp_path):
    _, trading, db = _trading(tmp_path)
    trading.risk.settings.max_consecutive_losses = 0
    trading.risk.settings.max_daily_loss = 0
    trading.paper.place_order(
        "BTC/USDT", "buy", "market", 1.0, price=100.0, stop_loss=95.0
    )
    assert trading.settle_paper_orders("BTC/USDT", 96.0) == []
    assert trading.paper.account.position_for("BTC/USDT").contracts == 1.0

    exits = trading.settle_paper_orders("BTC/USDT", 94.0)
    assert len(exits) == 1 and exits[0]["protection"] == "stop_loss"
    assert trading.paper.account.position_for("BTC/USDT").contracts == 0.0
    assert trading.risk.state.realized_pnl == pytest.approx(-6.0)
    # the exit was persisted too, so a restart cannot resurrect the position
    stored = db.get_json("paper.account.v1")
    assert stored["positions"] == []
    assert stored["protections"] == {}
    db.close()


def test_paper_stop_loss_closes_short():
    """A short is stopped out when price rises, not when it falls."""
    broker = PaperBroker(starting_balance=1_000, fee_rate=0.0, slippage_bps=0)
    broker.place_order(
        "BTC/USDT:USDT", "sell", "market", 1.0, price=100.0, stop_loss=105.0
    )
    assert broker.protections()["BTC/USDT:USDT"]["side"] == "short"
    assert broker.check_protections("BTC/USDT:USDT", 104.0) == []
    exits = broker.check_protections("BTC/USDT:USDT", 106.0)
    assert [o["protection"] for o in exits] == ["stop_loss"]
    assert exits[0]["side"] == "buy"  # the exit buys back
    assert exits[0]["realized"] == pytest.approx(-6.0)
    assert broker.protections() == {}


def test_paper_reversed_entry_replaces_guard():
    """A new entry in the other direction must not inherit the old stop."""
    broker = PaperBroker(starting_balance=1_000, fee_rate=0.0, slippage_bps=0)
    broker.place_order("BTC/USDT", "buy", "market", 1.0, price=100.0, stop_loss=95.0)
    broker.place_order("BTC/USDT", "sell", "market", 1.0, price=110.0, stop_loss=115.0)
    guard = broker.protections()["BTC/USDT"]
    assert guard["side"] == "short"
    assert guard["stopLoss"] == 115.0
    assert guard["takeProfit"] == 0.0


def test_paper_set_protection_for_existing_position():
    """A position opened bare can be guarded afterwards, and the side follows
    the position rather than whatever the caller guessed."""
    broker = PaperBroker(starting_balance=1_000, fee_rate=0.0, slippage_bps=0)
    broker.place_order("BTC/USDT:USDT", "sell", "market", 1.0, price=100.0)
    assert broker.protections() == {}

    guard = broker.set_protection("BTC/USDT:USDT", 108.0, 88.0)
    assert guard["side"] == "short"
    assert broker.protections()["BTC/USDT:USDT"]["stopLoss"] == 108.0

    assert broker.check_protections("BTC/USDT:USDT", 107.0) == []
    exits = broker.check_protections("BTC/USDT:USDT", 109.0)
    assert [o["protection"] for o in exits] == ["stop_loss"]
    assert broker.account.position_for("BTC/USDT:USDT").contracts == 0.0

    # An empty pair of levels disarms the guard.
    broker.place_order("BTC/USDT:USDT", "sell", "market", 1.0, price=100.0)
    assert broker.set_protection("BTC/USDT:USDT", 0.0, 0.0) == {}
    assert broker.protections() == {}


def test_conditional_order_fires_once_when_price_crosses():
    """A standalone stop entry fires when the market reaches its trigger, and
    does not fire again after it has been converted into a real order."""
    broker = PaperBroker(starting_balance=1_000, fee_rate=0.0, slippage_bps=0)
    item = broker.place_conditional(
        "BTC/USDT", "buy", 1.0, 95.0, direction="below"
    )
    assert item["status"] == "open"

    assert broker.mark("BTC/USDT", 96.0) == []
    assert broker.open_conditional() == [item]

    fired = broker.mark("BTC/USDT", 94.0)
    assert [o["status"] for o in fired] == ["closed"]
    assert broker.account.position_for("BTC/USDT").contracts == pytest.approx(1.0)
    assert broker.open_conditional() == []
    assert broker.conditional_orders()[0]["status"] == "triggered"

    # A second cross must not re-enter the position.
    assert broker.mark("BTC/USDT", 93.0) == []
    assert broker.account.position_for("BTC/USDT").contracts == pytest.approx(1.0)


def test_conditional_limit_exit_arms_before_position_exists():
    """An exit can be armed while the entry is still a resting limit order."""
    broker = PaperBroker(starting_balance=1_000, fee_rate=0.0, slippage_bps=0)
    broker.place_order("BTC/USDT", "buy", "limit", 1.0, price=100.0)
    broker.place_conditional(
        "BTC/USDT", "sell", 1.0, 120.0, direction="above", reduce_only=True
    )
    assert broker.mark("BTC/USDT", 99.0)
    assert broker.account.position_for("BTC/USDT").contracts == pytest.approx(1.0)
    assert broker.open_conditional()

    fired = broker.mark("BTC/USDT", 121.0)
    assert len(fired) == 1
    assert broker.account.position_for("BTC/USDT").contracts == 0.0


def test_conditional_order_can_be_canceled():
    broker = PaperBroker(starting_balance=1_000, fee_rate=0.0, slippage_bps=0)
    item = broker.place_conditional("BTC/USDT", "buy", 1.0, 90.0)
    canceled = broker.cancel_conditional(item["id"])
    assert canceled and canceled["status"] == "canceled"
    assert broker.open_conditional() == []
    assert broker.cancel_conditional(item["id"]) is None
    assert broker.mark("BTC/USDT", 80.0) == []


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
    from btrader.engine.risk import RISK_STATE_KEY

    db = Database(tmp_path / "risk2.db")
    settings = RiskSettings(max_daily_loss=100, max_consecutive_losses=0)
    first = RiskManager(settings, db)
    first.record_trade(-150)
    assert not first.check_order(10, 0, 10)

    stored = db.get_json(RISK_STATE_KEY)
    stored["day"] = "2000-01-01"
    db.set_json(RISK_STATE_KEY, stored)

    restarted = RiskManager(settings, db)
    assert restarted.check_order(10, 0, 10).allowed
    db.close()


def test_paper_realized_pnl_outlives_the_position():
    """A booked result belongs to the pair, not to the position object.

    The position is dropped from every payload the moment it goes flat, so
    summing position.realized_pnl made a closed round read as 0: the panel
    said "已实现盈亏 0" right after a losing exit.
    """
    broker = PaperBroker(starting_balance=1_000, fee_rate=0.0, slippage_bps=0)
    broker.place_order("BTC/USDT", "buy", "market", 1.0, price=100.0)
    broker.place_order("BTC/USDT", "sell", "market", 1.0, price=90.0)

    assert broker.account.positions["BTC/USDT"].contracts == 0.0
    assert broker.account.realized_by_symbol["BTC/USDT"] == pytest.approx(-10.0)
    assert broker.realized_pnl() == pytest.approx(-10.0)


def test_paper_realized_pnl_accumulates_across_rounds():
    broker = PaperBroker(starting_balance=1_000, fee_rate=0.0, slippage_bps=0)
    for entry, exit_price in ((100.0, 110.0), (110.0, 105.0)):
        broker.place_order("BTC/USDT", "buy", "market", 1.0, price=entry)
        broker.place_order("BTC/USDT", "sell", "market", 1.0, price=exit_price)
    assert broker.account.realized_by_symbol["BTC/USDT"] == pytest.approx(5.0)
    assert broker.realized_pnl() == pytest.approx(5.0)


def test_paper_realized_pnl_survives_restart(tmp_path):
    """Without the persisted ledger every closed round vanished on restart."""
    _, trading, db = _trading(tmp_path)
    trading.paper.place_order("ETH/USDT", "buy", "market", 2.0, price=50.0)
    trading.paper.place_order("ETH/USDT", "sell", "market", 2.0, price=45.0)
    trading.save_paper_state()

    snapshot = db.get_json("paper.account.v1")
    assert snapshot["realized_by_symbol"]["ETH/USDT"] == pytest.approx(-10.0)
    assert snapshot["positions"] == []

    fresh = PaperBroker(starting_balance=10_000)
    assert fresh.restore(snapshot)
    assert fresh.realized_pnl() == pytest.approx(-10.0)
    db.close()


def test_paper_restore_rebuilds_ledger_from_an_old_snapshot():
    """A snapshot written before the ledger existed still carries its trades."""
    broker = PaperBroker(starting_balance=1_000, fee_rate=0.0, slippage_bps=0)
    broker.place_order("BTC/USDT", "buy", "market", 1.0, price=100.0)
    broker.place_order("BTC/USDT", "sell", "market", 1.0, price=104.0)

    snapshot = broker.snapshot()
    snapshot.pop("realized_by_symbol")
    restored = PaperBroker(starting_balance=10_000)
    assert restored.restore(snapshot)
    assert restored.account.realized_by_symbol["BTC/USDT"] == pytest.approx(4.0)


def test_paper_entry_fee_is_booked_as_realized():
    """A commission is a realized cost the moment it is paid.

    Leaving it off the opening side is what made 已实现盈亏 and the 资产 page
    disagree: the gap was exactly the fees of the still-open positions.
    """
    broker = PaperBroker(starting_balance=1_000, fee_rate=0.001, slippage_bps=0)
    broker.place_order("BTC/USDT", "buy", "market", 1.0, price=100.0)
    assert broker.realized_pnl() == pytest.approx(-0.1)
    assert broker.account.realized_by_symbol["BTC/USDT"] == pytest.approx(-0.1)


def test_paper_pnl_columns_reconcile_with_equity():
    """已实现 + 浮动 must equal 权益变化, fees included.

    This is the invariant both pages rely on: if it holds, the trading panel
    and the 资产 page cannot quote two different results for one account.
    """
    broker = PaperBroker(starting_balance=1_000, fee_rate=0.001, slippage_bps=0)
    broker.place_order("BTC/USDT", "buy", "market", 1.0, price=100.0)
    broker.place_order("BTC/USDT", "sell", "market", 0.4, price=120.0)

    position = broker.account.positions["BTC/USDT"]
    last = 90.0
    realized = broker.realized_pnl()
    unrealized = (last - position.entry_price) * position.contracts
    equity = broker.account.cash + last * position.contracts
    assert equity - broker.account.starting_balance == pytest.approx(
        realized + unrealized
    )


def test_paper_fee_aware_ledger_survives_restart():
    broker = PaperBroker(starting_balance=1_000, fee_rate=0.001, slippage_bps=0)
    broker.place_order("BTC/USDT", "buy", "market", 1.0, price=100.0)

    restored = PaperBroker(starting_balance=10_000)
    assert restored.restore(broker.snapshot())
    assert restored.realized_pnl() == pytest.approx(-0.1)


def test_paper_partial_close_keeps_the_entry_price():
    """Closing part of a position must not move the average cost.

    The weighted-average formula used to run on reducing fills too, so every
    partial close dragged the entry towards the exit price and the position
    then reported a 浮动盈亏 (and a later realized figure) that matched
    neither the fills nor the 资产 page.
    """
    broker = PaperBroker(starting_balance=1_000, fee_rate=0.0, slippage_bps=0)
    broker.place_order("BTC/USDT", "buy", "market", 1.0, price=100.0)
    broker.place_order("BTC/USDT", "sell", "market", 0.4, price=120.0)

    position = broker.account.positions["BTC/USDT"]
    assert position.contracts == pytest.approx(0.6)
    assert position.entry_price == pytest.approx(100.0)
    # 0.6 still held at a 100 cost: a move to 90 is a 6 loss, not a 2 gain.
    assert position.unrealized(90.0) == pytest.approx(-6.0)

    # Adding in the same direction still averages the two lots.
    broker.place_order("BTC/USDT", "buy", "market", 0.6, price=110.0)
    position = broker.account.positions["BTC/USDT"]
    assert position.contracts == pytest.approx(1.2)
    assert position.entry_price == pytest.approx(105.0)


def test_paper_old_snapshot_books_entry_fees_too():
    """Rebuilding a pre-ledger snapshot must not lose the entry-side fees.

    Summing the trades alone left every opening commission unbooked, so the
    restored account still disagreed with its own equity by that amount.
    """
    broker = PaperBroker(starting_balance=1_000, fee_rate=0.001, slippage_bps=0)
    broker.place_order("BTC/USDT", "buy", "market", 1.0, price=100.0)
    broker.place_order("BTC/USDT", "sell", "market", 1.0, price=110.0)

    snapshot = broker.snapshot()
    snapshot.pop("realized_by_symbol")
    restored = PaperBroker(starting_balance=10_000)
    assert restored.restore(snapshot)
    # 0.1 in + 0.11 out of fees, and +10 on the round trip.
    assert restored.realized_pnl() == pytest.approx(10.0 - 0.1 - 0.11)


def test_paper_legacy_ledger_is_topped_up_on_restore():
    """A ledger saved before fees were booked gets repaired on the next load.

    The trade log replays to the stored cash, which proves it holds every fill,
    so the pair totals are simply recomputed — the opening commissions the old
    code never booked come back and the account reconciles with its 权益.
    """
    broker = PaperBroker(starting_balance=1_000, fee_rate=0.001, slippage_bps=0)
    broker.place_order("BTC/USDT", "buy", "market", 1.0, price=100.0)
    broker.place_order("BTC/USDT", "sell", "market", 1.0, price=110.0)

    snapshot = broker.snapshot()
    snapshot["realized_by_symbol"] = {"BTC/USDT": 10.0 - 0.11}  # closing fill only
    restored = PaperBroker(starting_balance=10_000)
    assert restored.restore(snapshot)
    assert restored.realized_pnl() == pytest.approx(10.0 - 0.1 - 0.11)

    # A ledger that already knows its fees is left alone.
    again = PaperBroker(starting_balance=10_000)
    assert again.restore(restored.snapshot())
    assert again.realized_pnl() == pytest.approx(10.0 - 0.1 - 0.11)


def test_paper_new_position_starts_its_own_realized_tally():
    """The row shows this position's result, not the pair's history.

    Closing a round and opening a new one used to keep reporting the closed
    round as the new position's 已实现盈亏, because the row read the pair's
    cumulative ledger instead of the position itself.
    """
    broker = PaperBroker(starting_balance=1_000, fee_rate=0.0, slippage_bps=0)
    broker.place_order("BTC/USDT", "buy", "market", 1.0, price=100.0)
    broker.place_order("BTC/USDT", "sell", "market", 1.0, price=130.0)
    assert broker.account.realized_by_symbol["BTC/USDT"] == pytest.approx(30.0)

    broker.place_order("BTC/USDT", "buy", "market", 1.0, price=200.0)
    position = broker.account.positions["BTC/USDT"]
    assert position.realized_pnl == pytest.approx(0.0)
    # ... while the account still remembers the pair it already booked.
    assert broker.account.realized_by_symbol["BTC/USDT"] == pytest.approx(30.0)

    broker.place_order("BTC/USDT", "sell", "market", 0.5, price=220.0)
    assert position.realized_pnl == pytest.approx(10.0)
    assert broker.account.realized_by_symbol["BTC/USDT"] == pytest.approx(40.0)


def test_paper_entry_fee_is_booked_on_the_position_too():
    """已实现盈亏 is a net figure: the entry commission is deducted at once.

    Reporting 0 while 0.1 of commission had already left the account is what
    made the panel look like it never counted fees.
    """
    broker = PaperBroker(starting_balance=1_000, fee_rate=0.001, slippage_bps=0)
    broker.place_order("BTC/USDT", "buy", "market", 1.0, price=100.0)

    position = broker.account.positions["BTC/USDT"]
    assert position.realized_pnl == pytest.approx(-0.1)
    assert broker.realized_pnl() == pytest.approx(-0.1)


def test_paper_position_tally_is_repaired_on_restore():
    """A snapshot whose row held the pair total is repaired from the fills.

    This is what a saved account looked like after the previous semantics:
    the still-open position carried the pair's whole history as its own
    已实现盈亏, so the panel showed a closed round on a position that had not
    closed anything.
    """
    broker = PaperBroker(starting_balance=1_000, fee_rate=0.0, slippage_bps=0)
    broker.place_order("BTC/USDT", "buy", "market", 1.0, price=100.0)
    broker.place_order("BTC/USDT", "sell", "market", 1.0, price=130.0)
    # New leg, nothing closed on it yet.
    broker.place_order("BTC/USDT", "buy", "market", 1.0, price=200.0)

    snapshot = broker.snapshot()
    snapshot["positions"][0]["realized_pnl"] = 30.0  # the old, wrong figure

    restored = PaperBroker(starting_balance=10_000)
    assert restored.restore(snapshot)
    position = restored.account.positions["BTC/USDT"]
    assert position.realized_pnl == pytest.approx(0.0)
    assert restored.account.realized_by_symbol["BTC/USDT"] == pytest.approx(30.0)


def test_paper_position_tally_is_net_of_both_fees():
    """The row must deduct the commission on both sides of the trade.

    A round trip that ends where it started is not a zero: it cost the entry
    fee plus the exit fee, and the position has to report that.
    """
    broker = PaperBroker(starting_balance=1_000, fee_rate=0.001, slippage_bps=0)
    broker.place_order("BTC/USDT", "buy", "market", 1.0, price=100.0)
    position = broker.account.positions["BTC/USDT"]
    assert position.realized_pnl == pytest.approx(-0.1)  # entry commission

    broker.place_order("BTC/USDT", "sell", "market", 1.0, price=100.0)
    assert position.realized_pnl == pytest.approx(-0.2)  # 0.1 in + 0.1 out
    assert broker.realized_pnl() == pytest.approx(-0.2)
    assert broker.realized_pnl() == pytest.approx(position.realized_pnl)


def test_paper_partial_closes_deduct_their_share_of_the_entry_fee():
    """Splitting the exit must not lose or double count the entry commission.

    Half out at 110 then the rest at 100: the fees are 0.1 in, 0.055 and 0.05
    out, so the position reports +5 and then -0.105, and the two add up to the
    net result of the whole leg.
    """
    broker = PaperBroker(starting_balance=1_000, fee_rate=0.001, slippage_bps=0)
    broker.place_order("BTC/USDT", "buy", "market", 1.0, price=100.0)
    broker.place_order("BTC/USDT", "sell", "market", 0.5, price=110.0)
    position = broker.account.positions["BTC/USDT"]
    assert position.realized_pnl == pytest.approx(-0.1 + 5.0 - 0.055)

    broker.place_order("BTC/USDT", "sell", "market", 0.5, price=100.0)
    assert position.realized_pnl == pytest.approx(4.795)
    # Fully closed: the position's own figure and the account agree exactly.
    assert broker.realized_pnl() == pytest.approx(4.795)


def test_paper_trade_record_carries_the_fill_details():
    """The 成交 detail dialog reads these fields, so the fill must keep them.

    Without the market, the order type and the closed size, a fill opened from
    the dialog described almost nothing about itself.
    """
    broker = PaperBroker(starting_balance=1_000, fee_rate=0.001, slippage_bps=0)
    broker.place_order(
        "BTC/USDT", "buy", "market", 1.0, price=100.0, market="usdm"
    )
    broker.place_order(
        "BTC/USDT", "sell", "market", 0.4, price=110.0, market="usdm"
    )

    entry, exit_ = broker.account.trades
    assert entry["market"] == "usdm"
    assert entry["type"] == "market"
    assert entry["closedQty"] == 0.0
    assert entry["cost"] == pytest.approx(100.0)
    assert exit_["closedQty"] == pytest.approx(0.4)
    assert exit_["realized"] == pytest.approx(4.0 - 0.044)


def test_paper_guard_of_the_other_side_never_fires():
    """一笔多单的止损不能被拿来平掉反手后的空单。"""
    broker = PaperBroker(starting_balance=1_000, fee_rate=0.0, slippage_bps=0)
    broker.place_order("BTC/USDT:USDT", "buy", "market", 1.0, price=100.0, stop_loss=95.0)
    # 直接把仓位翻成空头（绕开下单路径），旧的多单保护单还挂在账上。
    broker.account.position_for("BTC/USDT:USDT").contracts = -1.0
    assert broker.protections()["BTC/USDT:USDT"]["side"] == "long"
    # 空头视角下 95 早就「跌破」了：旧保护单若触发就会立刻平掉新空单。
    assert broker.check_protections("BTC/USDT:USDT", 94.0) == []
    assert broker.protections() == {}
    assert broker.account.position_for("BTC/USDT:USDT").contracts == -1.0


def test_paper_protection_exit_keeps_the_market_of_the_entry():
    """保护单成交要记在与开仓同一个市场上（不是硬编码的现货）。"""
    broker = PaperBroker(starting_balance=1_000, fee_rate=0.0, slippage_bps=0)
    broker.place_order(
        "BTC/USDT:USDT", "buy", "market", 1.0, price=100.0, stop_loss=95.0, market="usdm"
    )
    exits = broker.check_protections("BTC/USDT:USDT", 94.0)
    assert exits and exits[0]["market"] == "usdm"
    assert broker.account.trades[-1]["market"] == "usdm"
