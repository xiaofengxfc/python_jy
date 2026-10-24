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
from btrader.exchange.rest import ExchangeError
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
        # 条件单路由要读 risk.max_leverage 与 trade.leverage 来收口，然后推给交易所。
        self.settings = settings
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

# ------------------------------------- 新单全挂不上时不能把仓位留在裸奔状态
class _PickyVenue(FakeVenue):
    """只接受 ``allow_trigger`` 那一个触发价的止损/止盈；None = 一张都不接受。"""

    allow_trigger: float | None = None

    def _gate(self, trigger: float) -> None:
        if self.allow_trigger is None or float(trigger) != float(self.allow_trigger):
            raise ExchangeError("交易所拒绝：触发价不被接受")

    def create_stop_market(self, market, symbol, side, amount, stop_price):
        self._gate(stop_price)
        return super().create_stop_market(market, symbol, side, amount, stop_price)

    def create_take_profit_market(self, market, symbol, side, amount, target_price):
        self._gate(target_price)
        return super().create_take_profit_market(market, symbol, side, amount, target_price)


@pytest.fixture()
def picky_api(tmp_path, monkeypatch):
    venue = _PickyVenue()
    venue._last[SYMBOL] = 100.0
    venue._book[SYMBOL] = 0.5
    db = Database(tmp_path / "picky.db")
    state = _State(
        ctx=_Ctx(venue, db),
        locks={market: threading.RLock() for market in MarketType},
        account_lock=threading.RLock(),
    )
    monkeypatch.setattr(web_app, "_state", state, raising=False)
    app = web_app.create_app()

    def call(path: str, method: str, payload=None, **query):
        endpoint = _endpoint(app, path, method)
        return endpoint(payload) if payload is not None else endpoint(**query)

    try:
        yield call, venue
    finally:
        db.close()


def _old_stop(venue: FakeVenue, trigger: float) -> None:
    """让交易所侧先挂着一张旧止损（重挂之前它会被撤掉）。"""
    venue.open_orders = [
        {
            "id": "old-stop",
            "symbol": SYMBOL,
            "type": "stop_market",
            "side": "sell",
            "amount": 0.5,
            "price": trigger,
            "triggerPrice": trigger,
            "status": "open",
        }
    ]


def test_failed_rearm_restores_the_previous_guard(picky_api):
    """新止损止盈全被拒时，把撤掉的旧止损按原触发价挂回去，并说清楚。"""
    call, venue = picky_api
    _old_stop(venue, 90.0)
    venue.allow_trigger = 90.0  # 只认旧单那个触发价

    with pytest.raises(HTTPException) as caught:
        call(
            "/api/positions/protection",
            "PUT",
            ProtectionPayload(symbol=SYMBOL, stop_loss=95.0, take_profit=105.0),
        )

    detail = _detail(caught.value)
    assert caught.value.status_code == 400
    assert "挂回" in detail
    assert venue.stop_orders == [("stop", SYMBOL, "sell", 0.5, 90.0)]


def test_failed_rearm_reports_a_naked_position(picky_api):
    """连旧单都挂不回去时必须直说「当前没有任何交易所侧保护」。"""
    call, venue = picky_api
    _old_stop(venue, 90.0)
    venue.allow_trigger = None  # 一张都不接受

    with pytest.raises(HTTPException) as caught:
        call(
            "/api/positions/protection",
            "PUT",
            ProtectionPayload(symbol=SYMBOL, stop_loss=95.0, take_profit=105.0),
        )

    detail = _detail(caught.value)
    assert "没有任何交易所侧保护" in detail
    assert "挂回旧单也失败" in detail
    assert venue.stop_orders == []


def test_a_half_failed_rearm_reports_only_the_armed_leg(picky_api):
    """止损挂上了、止盈没挂上：响应里不能把止盈报成已挂。

    以前回的是请求里那两个价位，界面据此把两条腿都画成「已保护」，而止盈那一侧
    在交易所其实什么都没挂。
    """
    call, venue = picky_api
    venue.allow_trigger = 95.0  # 只接受止损那个触发价

    result = call(
        "/api/positions/protection",
        "PUT",
        ProtectionPayload(symbol=SYMBOL, stop_loss=95.0, take_profit=105.0),
    )

    assert result["ok"] is True
    assert result["protection"]["stop_loss"] == 95.0
    assert result["protection"]["take_profit"] == 0.0, "没挂上的止盈不能报成已挂"
    assert any("止盈" in item for item in result["warnings"])
    assert len(result["protection"]["orders"]) == 1

