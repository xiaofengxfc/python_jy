"""平仓 / 保护单 / 条件单的符号口径：两种写法都收，内部统一成交易所认的那个。

这轮全功能实测撞到：``POST /api/positions/close`` 传 ``symbol=ETH/USDT`` 时
``_live_held`` 找不到 ``ETH/USDT:USDT`` 的仓位，回 400「当前没有该交易对的持仓」——
明明有仓。界面传的是持仓行的 ccxt 符号所以按钮没事，接口层两套符号混着用迟早出事。
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
from btrader.web.app import ClosePayload, ConditionalPayload, ProtectionPayload, create_app

from fakes import FakeVenue

BARE = "ETH/USDT"
VENUE = "ETH/USDT:USDT"


@dataclass
class _State:
    ctx: object
    locks: dict
    account_lock: threading.RLock


class _Ctx:
    def __init__(self, venue: FakeVenue, db: Database) -> None:
        settings = AppSettings(network=Network.TESTNET.value)
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

    def rest_for(self, market: MarketType):
        return self.venue

    def quote_rest(self, market: MarketType):
        return self.venue


def _endpoint(app, path: str, method: str):
    for route in app.routes:
        if getattr(route, "path", "") == path and method in getattr(route, "methods", ()):
            return route.endpoint
    raise AssertionError(f"missing route {method} {path}")


@pytest.fixture()
def futures(tmp_path, monkeypatch):
    """合约账户：交易所那一对叫 ETH/USDT:USDT，手上有 1 张多单。"""
    venue = FakeVenue()
    venue._last[VENUE] = 100.0
    venue._last[BARE] = 100.0
    venue.create_order(MarketType.USDM, VENUE, "market", "buy", 1.0, 100.0)
    db = Database(tmp_path / "symbols.db")
    ctx = _Ctx(venue, db)
    state = _State(
        ctx=ctx,
        locks={market: threading.RLock() for market in MarketType},
        account_lock=threading.RLock(),
    )
    monkeypatch.setattr("btrader.web.app._state", state, raising=False)
    try:
        yield SimpleBox(app=create_app(), venue=venue, db=db)
    finally:
        db.close()


@dataclass
class SimpleBox:
    app: object
    venue: FakeVenue
    db: Database


# ------------------------------------------------------------------- 平仓
def test_closing_a_futures_position_accepts_the_bare_symbol(futures):
    """``ETH/USDT`` 指的就是交易所那一对 ``ETH/USDT:USDT``。"""
    close = _endpoint(futures.app, "/api/positions/close", "POST")

    result = close(ClosePayload(symbol=BARE, market="usdm"))

    assert result["size"] == pytest.approx(1.0)
    assert result["side"] == "sell"
    assert futures.venue.position_base(VENUE) == pytest.approx(0.0)
    assert [item["symbol"] for item in futures.venue.fills][-1] == VENUE


def test_the_venue_symbol_keeps_working(futures):
    """带结算币的写法照旧能用（界面走的就是这条）。"""
    close = _endpoint(futures.app, "/api/positions/close", "POST")

    result = close(ClosePayload(symbol=VENUE, market="usdm"))

    assert result["size"] == pytest.approx(1.0)
    assert futures.venue.position_base(VENUE) == pytest.approx(0.0)


# ---------------------------------------------------------------- 保护单
def test_arming_a_protection_accepts_the_bare_symbol(futures):
    protection = _endpoint(futures.app, "/api/positions/protection", "PUT")

    payload = protection(
        ProtectionPayload(symbol=BARE, market="usdm", stop_loss=95.0, take_profit=110.0)
    )

    assert payload["ok"] is True
    # 回的是交易所里那一对：界面按持仓行的符号对上号，手敲的裸写法也一样
    assert payload["protection"]["symbol"] == VENUE
    assert {item[1] for item in futures.venue.stop_orders} == {VENUE}


def test_clearing_a_protection_sends_the_venue_symbol(futures):
    clear = _endpoint(futures.app, "/api/positions/protection", "DELETE")

    clear(symbol=BARE, market="usdm")

    assert futures.venue.protections_cleared == [VENUE]


# ---------------------------------------------------------------- 条件单
def test_a_conditional_order_accepts_the_bare_symbol(futures):
    conditional = _endpoint(futures.app, "/api/orders/conditional", "POST")

    payload = conditional(
        ConditionalPayload(
            market="usdm", symbol=BARE, side="sell", size=1.0, trigger=95.0, reduce_only=True
        )
    )

    assert payload["ok"] is True
    assert payload["order"]["symbol"] == VENUE


# ------------------------------------------------------------------ 现货不动
def test_spot_symbols_are_left_alone(tmp_path, monkeypatch):
    """现货就是 ``BASE/QUOTE``：别给现货也补一个结算币出来。"""
    venue = FakeVenue(
        balances={
            MarketType.SPOT: {"USDT": 10_000.0, "ETH": 1.0},
            MarketType.USDM: {"USDT": 10_000.0},
            MarketType.COINM: {"BTC": 0.2},
        }
    )
    venue._last[BARE] = 100.0
    db = Database(tmp_path / "spot.db")
    ctx = _Ctx(venue, db)
    state = _State(
        ctx=ctx,
        locks={market: threading.RLock() for market in MarketType},
        account_lock=threading.RLock(),
    )
    monkeypatch.setattr("btrader.web.app._state", state, raising=False)
    try:
        app = create_app()
        close = _endpoint(app, "/api/positions/close", "POST")
        result = close(ClosePayload(symbol=BARE, market="spot", size=0.25))
        assert result["size"] == pytest.approx(0.25)
        assert [item["symbol"] for item in venue.fills][-1] == BARE
    finally:
        db.close()
