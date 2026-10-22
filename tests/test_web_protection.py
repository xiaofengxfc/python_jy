"""Protective orders and standalone trigger orders.

Spot has no reduce-only trigger order, so both features are futures-only and
must say so instead of quietly arming nothing. The routes are exercised through
their endpoint functions with an in-memory venue behind them.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

import pytest
from fastapi import HTTPException

from btrader.core.config import AppSettings
from btrader.core.storage import Database
from btrader.core.types import MarketType, Network
from btrader.engine.account import AccountService
from btrader.engine.risk import RiskManager
from btrader.engine.trading import TradingService
from btrader.web import app as web_app
from btrader.web.app import ConditionalPayload, ProtectionPayload

from fakes import FakeVenue

SYMBOL = "BTC/USDT:USDT"


@dataclass
class _State:
    """The slice of the process-wide state these routes touch."""

    ctx: object
    locks: dict
    account_lock: threading.RLock


class _Ctx:
    def __init__(self, venue: FakeVenue, db: Database) -> None:
        settings = AppSettings(network=Network.TESTNET.value)
        self.network = Network.TESTNET
        self.market_type = MarketType.USDM
        self.venue = venue
        self.trading = TradingService(
            settings=settings,
            rest=venue,
            risk=RiskManager(settings.risk),
            db=db,
            notifier=None,
        )
        self.account = AccountService(
            rest_for=lambda market: venue,
            chart_rest_for=lambda market: venue,
            db=db,
            network=Network.TESTNET,
        )

    def rest_for(self, market: MarketType) -> FakeVenue:
        return self.venue

    def quote_rest(self, market: MarketType) -> FakeVenue:
        return self.venue


def _endpoint(app, path: str, method: str):
    for route in app.routes:
        if getattr(route, "path", "") == path and method in getattr(route, "methods", ()):
            return route.endpoint
    raise AssertionError(f"missing route {method} {path}")


@pytest.fixture()
def api(tmp_path, monkeypatch):
    venue = FakeVenue()
    venue._last[SYMBOL] = 100.0
    db = Database(tmp_path / "web.db")
    ctx = _Ctx(venue, db)
    state = _State(
        ctx=ctx,
        locks={market: threading.RLock() for market in MarketType},
        account_lock=threading.RLock(),
    )
    monkeypatch.setattr(web_app, "_state", state, raising=False)
    app = web_app.create_app()

    def call(path: str, method: str, payload=None, **query):
        endpoint = _endpoint(app, path, method)
        return endpoint(payload) if payload is not None else endpoint(**query)

    try:
        yield call, venue, db
    finally:
        db.close()


def _detail(exc: HTTPException) -> str:
    return str(exc.detail)


# ------------------------------------------------------------- trigger orders
def test_spot_has_no_conditional_orders(api):
    call, _venue, _db = api
    with pytest.raises(HTTPException) as caught:
        call(
            "/api/orders/conditional",
            "POST",
            ConditionalPayload(
                market="spot",
                symbol="BTC/USDT",
                side="buy",
                size=1.0,
                trigger=90.0,
            ),
        )
    assert caught.value.status_code == 400
    assert "现货" in _detail(caught.value)


def test_breakout_buy_arms_a_stop(api):
    """Buying above the market is a breakout: Binance calls it a stop."""
    call, venue, _db = api
    result = call(
        "/api/orders/conditional",
        "POST",
        ConditionalPayload(
            market="usdm",
            symbol=SYMBOL,
            side="buy",
            size=0.5,
            trigger=110.0,
            direction="above",
        ),
    )
    assert result["ok"] is True
    placed = venue.conditional_orders[-1]
    assert placed["take_profit"] is False
    assert placed["side"] == "buy"
    assert placed["trigger"] == 110.0
    assert placed["reduce_only"] is False


def test_dip_buy_arms_a_take_profit(api):
    """Buying below the market is a dip entry: the mirror-image order type."""
    call, venue, _db = api
    call(
        "/api/orders/conditional",
        "POST",
        ConditionalPayload(
            market="usdm",
            symbol=SYMBOL,
            side="buy",
            size=0.5,
            trigger=90.0,
            direction="below",
        ),
    )
    assert venue.conditional_orders[-1]["take_profit"] is True


def test_conditional_direction_must_match_the_trigger(api):
    call, _venue, _db = api
    with pytest.raises(HTTPException) as caught:
        call(
            "/api/orders/conditional",
            "POST",
            ConditionalPayload(
                market="usdm",
                symbol=SYMBOL,
                side="buy",
                size=0.5,
                trigger=110.0,
                direction="below",
            ),
        )
    assert caught.value.status_code == 400
    assert "触发方向对不上" in _detail(caught.value)


def test_quote_sized_conditional_sizes_from_the_trigger(api):
    call, venue, _db = api
    result = call(
        "/api/orders/conditional",
        "POST",
        ConditionalPayload(
            market="usdm",
            symbol=SYMBOL,
            side="sell",
            size_mode="quote",
            size=50.0,
            trigger=90.0,
            reduce_only=True,
        ),
    )
    assert result["amount_base"] == pytest.approx(50.0 / 90.0)
    assert venue.conditional_orders[-1]["reduce_only"] is True


# --------------------------------------------------------------- protections
def test_protection_arms_both_legs_on_the_exit_side(api):
    call, venue, _db = api
    venue._book[SYMBOL] = 0.5  # long half a coin
    result = call(
        "/api/positions/protection",
        "PUT",
        ProtectionPayload(symbol=SYMBOL, stop_loss=95.0, take_profit=105.0),
    )
    assert result["ok"] is True
    assert len(venue.stop_orders) == 2
    kinds = [(item[0], item[2], item[3]) for item in venue.stop_orders]
    assert kinds == [("stop", "sell", 0.5), ("target", "sell", 0.5)]
    assert [entry[4] for entry in venue.stop_orders] == [95.0, 105.0]
    assert len(result["protection"]["orders"]) == 2


def test_protection_replaces_the_previous_guard(api):
    call, venue, _db = api
    venue._book[SYMBOL] = -0.5  # short
    call(
        "/api/positions/protection",
        "PUT",
        ProtectionPayload(symbol=SYMBOL, stop_loss=110.0),
    )
    call(
        "/api/positions/protection",
        "PUT",
        ProtectionPayload(symbol=SYMBOL, stop_loss=108.0),
    )
    assert venue.protections_cleared.count(SYMBOL) == 2
    assert venue.stop_orders == [("stop", SYMBOL, "buy", 0.5, 108.0)]


def test_protection_needs_a_position(api):
    call, _venue, _db = api
    with pytest.raises(HTTPException) as caught:
        call(
            "/api/positions/protection",
            "PUT",
            ProtectionPayload(symbol=SYMBOL, stop_loss=95.0),
        )
    assert caught.value.status_code == 400
    assert "没有该交易对的持仓" in _detail(caught.value)


def test_protection_rejects_a_stop_on_the_wrong_side(api):
    call, venue, _db = api
    venue._book[SYMBOL] = 0.5
    with pytest.raises(HTTPException) as caught:
        call(
            "/api/positions/protection",
            "PUT",
            ProtectionPayload(symbol=SYMBOL, stop_loss=110.0),
        )
    assert caught.value.status_code == 400
    assert "止损" in _detail(caught.value)


def test_protection_refuses_spot(api):
    call, _venue, _db = api
    with pytest.raises(HTTPException) as caught:
        call(
            "/api/positions/protection",
            "PUT",
            ProtectionPayload(symbol="BTC/USDT", market="spot", stop_loss=95.0),
        )
    assert caught.value.status_code == 400
    assert "现货" in _detail(caught.value)


def test_protection_can_be_cleared(api):
    call, venue, _db = api
    venue._book[SYMBOL] = 0.5
    call(
        "/api/positions/protection",
        "PUT",
        ProtectionPayload(symbol=SYMBOL, stop_loss=95.0, take_profit=105.0),
    )
    result = call("/api/positions/protection", "DELETE", symbol=SYMBOL)
    assert result == {"ok": True, "cleared": 2}
    assert venue.stop_orders == []
