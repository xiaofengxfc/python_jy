"""Account snapshots: the one place account numbers come from.

The app no longer keeps a simulated account, so these tests pin down what the
snapshot service owes the UI: the venue's positions, the guards it found there,
a realized PnL replayed from the fill log, and honest degradation when the key
or the network is missing.
"""

from __future__ import annotations

import pytest

from btrader.core.storage import Database
from btrader.core.types import MarketType, Network
from btrader.engine.account import AccountService, replay_ledger

from fakes import FakeVenue


def _service(tmp_path, venue: FakeVenue, *, ttl: float = 3.0) -> tuple[AccountService, Database]:
    db = Database(tmp_path / "account.db")
    service = AccountService(
        rest_for=lambda market: venue,
        chart_rest_for=lambda market: venue,
        db=db,
        network=Network.TESTNET,
        ttl=ttl,
    )
    return service, db


def _fill(symbol, side, amount, price, fee=0.0, ts=0):
    return {
        "symbol": symbol,
        "side": side,
        "amount": amount,
        "price": price,
        "fee": fee,
        "timestamp": ts,
    }


# ------------------------------------------------------------------- ledger
def test_replay_ledger_averages_the_open_leg():
    book = replay_ledger(
        [_fill("BTC/USDT", "buy", 1.0, 100.0, ts=1), _fill("BTC/USDT", "buy", 1.0, 120.0, ts=2)]
    )
    assert book["BTC/USDT"]["size"] == pytest.approx(2.0)
    assert book["BTC/USDT"]["entry"] == pytest.approx(110.0)


def test_replay_ledger_books_a_closed_round_net_of_fees():
    book = replay_ledger(
        [
            _fill("BTC/USDT", "buy", 2.0, 100.0, fee=0.2, ts=1),
            _fill("BTC/USDT", "sell", 1.0, 130.0, fee=0.13, ts=2),
        ]
    )
    # 1.0 closed at +30, minus both commissions (0.2 + 0.13)
    assert book["BTC/USDT"]["realized"] == pytest.approx(29.67)
    assert book["BTC/USDT"]["size"] == pytest.approx(1.0)
    assert book["BTC/USDT"]["entry"] == pytest.approx(100.0)


def test_replay_ledger_restarts_the_entry_after_a_flip():
    book = replay_ledger(
        [
            _fill("BTC/USDT", "buy", 1.0, 100.0, ts=1),
            _fill("BTC/USDT", "sell", 2.0, 110.0, ts=2),
        ]
    )
    # Long closed at +10, the remaining short opened at 110, not at 100.
    assert book["BTC/USDT"]["realized"] == pytest.approx(10.0)
    assert book["BTC/USDT"]["size"] == pytest.approx(-1.0)
    assert book["BTC/USDT"]["entry"] == pytest.approx(110.0)


def test_replay_ledger_ignores_unknown_symbols():
    assert replay_ledger([_fill("", "buy", 1.0, 100.0)]) == {}


# ----------------------------------------------------------------- snapshot
def test_snapshot_without_credentials_explains_itself(tmp_path):
    """No key is a message, not an exception: the page still has to render."""
    venue = FakeVenue(has_credentials=False)
    service, db = _service(tmp_path, venue)
    try:
        snap = service.snapshot(MarketType.SPOT, "BTC/USDT")
        assert snap.has_credentials is False
        assert "API Key" in snap.error
        payload = snap.as_payload()
        assert payload["positions"] == [] and "conditional" not in payload
    finally:
        db.close()


def test_snapshot_reads_positions_and_guards_from_the_venue(tmp_path):
    venue = FakeVenue()
    venue.create_order(MarketType.USDM, "BTC/USDT:USDT", "market", "buy", 1.0, 100.0)
    venue.open_orders = [
        {"id": "sl", "symbol": "BTC/USDT:USDT", "side": "sell", "type": "stop_market", "amount": 1.0, "stopPrice": 95.0},
        {"id": "tp", "symbol": "BTC/USDT:USDT", "side": "sell", "type": "take_profit_market", "amount": 1.0, "triggerPrice": 110.0},
        {"id": "rest", "symbol": "BTC/USDT:USDT", "side": "sell", "type": "limit", "amount": 1.0, "price": 120.0},
    ]
    service, db = _service(tmp_path, venue)
    try:
        snap = service.snapshot(MarketType.USDM, "BTC/USDT:USDT")
        assert len(snap.positions) == 1
        assert snap.positions[0]["contracts"] == pytest.approx(1.0)
        assert len(snap.orders) == 3
        guards = {item["symbol"]: item for item in snap.protections}
        assert guards["BTC/USDT:USDT"]["stop_loss"] == pytest.approx(95.0)
        assert guards["BTC/USDT:USDT"]["take_profit"] == pytest.approx(110.0)
    finally:
        db.close()


def test_snapshot_is_cached_between_polls(tmp_path):
    """Signing three requests per poll would burn the weight budget."""
    calls = {"n": 0}

    class Counting(FakeVenue):
        def fetch_balance(self, market_type):
            calls["n"] += 1
            return super().fetch_balance(market_type)

    service, db = _service(tmp_path, Counting())
    try:
        service.snapshot(MarketType.SPOT)
        service.snapshot(MarketType.SPOT)
        assert calls["n"] == 1

        service.snapshot(MarketType.SPOT, refresh=True)
        assert calls["n"] == 2
    finally:
        db.close()


def test_failed_read_keeps_the_last_good_numbers(tmp_path):
    """A flash of zeros would read as "your money is gone"."""
    venue = FakeVenue()
    venue.create_order(MarketType.USDM, "BTC/USDT:USDT", "market", "buy", 1.0, 100.0)
    service, db = _service(tmp_path, venue)
    try:
        first = service.snapshot(MarketType.USDM, "BTC/USDT:USDT")
        assert first.positions

        venue.has_credentials = True

        def boom(market_type):
            raise RuntimeError("venue unreachable")

        venue.fetch_balance = boom  # type: ignore[assignment]
        second = service.snapshot(MarketType.USDM, "BTC/USDT:USDT", refresh=True)
        assert second.positions == first.positions
        assert "读取余额失败" in second.error
    finally:
        db.close()


def test_baseline_is_recorded_once_and_stored(tmp_path):
    venue = FakeVenue()
    service, db = _service(tmp_path, venue)
    try:
        first = service.snapshot(MarketType.USDM)
        assert first.baseline == pytest.approx(first.equity)

        # A later, larger wallet must not move the reference point.
        venue.balances[MarketType.USDM] = {"USDT": 25_000.0}
        service.invalidate()
        second = service.snapshot(MarketType.USDM)
        assert second.baseline == pytest.approx(first.baseline)
        assert second.equity == pytest.approx(25_000.0)
    finally:
        db.close()


def test_pair_read_borrows_the_whole_wallet_equity(tmp_path):
    """The sidebar quotes equity for the account, not for one coin."""
    venue = FakeVenue()
    venue.balances[MarketType.SPOT] = {"USDT": 1_000.0, "BTC": 0.5, "ETH": 2.0}
    venue._last["BTC/USDT"] = 100.0
    venue._last["ETH/USDT"] = 50.0
    service, db = _service(tmp_path, venue)
    try:
        pair = service.snapshot(MarketType.SPOT, "BTC/USDT")
        assert len(pair.positions) == 1
        assert pair.equity == pytest.approx(1_150.0)
        assert service.snapshot(MarketType.SPOT).equity == pytest.approx(1_150.0)
    finally:
        db.close()


def test_pair_load_never_freezes_a_partial_baseline(tmp_path):
    """A pair load prices one coin; that must not become "where I started"."""
    venue = FakeVenue()
    venue.balances[MarketType.SPOT] = {"USDT": 1_000.0, "BTC": 0.5, "ETH": 2.0}
    venue._last["BTC/USDT"] = 100.0
    venue._last["ETH/USDT"] = 50.0
    service, db = _service(tmp_path, venue)
    try:
        pair = service._load(MarketType.SPOT, "BTC/USDT")
        assert pair.equity == pytest.approx(1_050.0)  # cash + one coin
        assert pair.baseline == 0.0
        assert service._stored_baseline(MarketType.SPOT) == 0.0

        full = service.snapshot(MarketType.SPOT)
        assert full.baseline == pytest.approx(1_150.0)
        assert service._stored_baseline(MarketType.SPOT) == pytest.approx(1_150.0)
    finally:
        db.close()


def test_spot_holdings_are_priced_and_counted_in_equity(tmp_path):
    venue = FakeVenue()
    venue.balances[MarketType.SPOT] = {"USDT": 1_000.0, "BTC": 0.5}
    venue._last["BTC/USDT"] = 100.0
    service, db = _service(tmp_path, venue)
    try:
        snap = service.snapshot(MarketType.SPOT)
        assert snap.cash == pytest.approx(1_000.0)
        assert snap.positions[0]["quote_value"] == pytest.approx(50.0)
        assert snap.equity == pytest.approx(1_050.0)
    finally:
        db.close()
