"""币本位（以及 contractSize != 1 的合约）的量纲：张 -> 基础币只能折一次。

实测撞到的一条：``GET /api/positions/close`` 在币本位持仓上把 ``fetch_positions``
报的「张」直接当成基础币交给 ``OrderRequest.amount``，而 ``TradingService.submit``
还会再按 ``contract_size`` 折一次——持仓 10 张、价格 78000 时算出来是 7800 张，
reduceOnly 单必然被交易所拒，币本位仓位从界面平不掉，止损止盈也挂不上。

这一份钉住三件事：
1. ``_live_held`` 对外一律是**基础币**（币本位用 markPrice 折）；
2. 平仓送到交易所的是张数（10 张），不是把张当币再乘一遍的 5000；
3. 保护单同理，并且返回体里的 ``size`` 也是基础币。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

import pytest

from btrader.core.config import AppSettings, RiskSettings
from btrader.core.storage import Database
from btrader.core.types import MarketType, Network
from btrader.engine.risk import RiskManager
from btrader.engine.trading import TradingService
from btrader.web import app as web_app
from btrader.web.app import ClosePayload, ProtectionPayload, create_app

from fakes import FakeVenue

COINM = "BTC/USD:BTC"
# 币本位永续：一张 100 USD。0.02 BTC @ 50,000 = 1,000 USD = 10 张。
PRICE = 50_000.0
BASE_QTY = 0.02
CONTRACTS = 10.0


@dataclass
class _State:
    ctx: object
    locks: dict
    account_lock: threading.RLock


class _Account:
    """``invalidate()`` 是路由唯一用到账户服务的地方。"""

    def __init__(self) -> None:
        self.invalidated = 0

    def invalidate(self) -> None:
        self.invalidated += 1


class _Ctx:
    def __init__(self, venue: FakeVenue, db: Database) -> None:
        self.settings = AppSettings(
            network=Network.TESTNET.value,
            risk=RiskSettings(
                max_order_quote=0,
                max_position_quote=0,
                max_leverage=100,
                max_daily_trades=0,
                max_daily_loss=0,
                max_consecutive_losses=0,
            ),
        )
        self.network = Network.TESTNET
        self.market_type = MarketType.COINM
        self.venue = venue
        self.trading = TradingService(
            settings=self.settings,
            rest=venue,
            risk=RiskManager(self.settings.risk),
            db=db,
            notifier=None,
            rest_for=self.rest_for,
        )
        self.account = _Account()

    def rest_for(self, market: MarketType) -> FakeVenue:
        return self.venue

    def quote_rest(self, market: MarketType) -> FakeVenue:
        return self.venue


@dataclass
class Box:
    app: object
    venue: FakeVenue
    ctx: _Ctx


def _endpoint(app, path: str, method: str):
    for route in app.routes:
        if getattr(route, "path", "") == path and method in getattr(route, "methods", ()):
            return route.endpoint
    raise AssertionError(f"missing route {method} {path}")


@pytest.fixture()
def coinm(tmp_path, monkeypatch):
    venue = FakeVenue()
    venue._book[COINM] = BASE_QTY
    venue._last[COINM] = PRICE
    venue._entry[COINM] = PRICE
    db = Database(tmp_path / "coinm-web.db")
    ctx = _Ctx(venue, db)
    state = _State(
        ctx=ctx,
        locks={market: threading.RLock() for market in MarketType},
        account_lock=threading.RLock(),
    )
    monkeypatch.setattr(web_app, "_state", state, raising=False)
    try:
        yield Box(app=create_app(), venue=venue, ctx=ctx)
    finally:
        db.close()


def test_live_held_reports_base_coins_for_a_coinm_position(coinm):
    """「持仓」对调用方是基础币：10 张（1,000 USD）就是 0.02 BTC。"""
    held = web_app._live_held(web_app._state, COINM, MarketType.COINM)
    assert held == pytest.approx(BASE_QTY)


def test_closing_a_coinm_position_sends_contracts_not_base(coinm):
    """平仓：交易所收到 10 张；以前是 10 被当币、再折成 5,000 张。"""
    close = _endpoint(coinm.app, "/api/positions/close", "POST")

    result = close(ClosePayload(symbol=COINM))

    assert result["size"] == pytest.approx(BASE_QTY)
    assert result["side"] == "sell"
    assert coinm.venue.position_base(COINM) == pytest.approx(0.0)
    assert coinm.venue.fills[0]["amount"] == pytest.approx(CONTRACTS)


def test_closing_half_a_coinm_position_still_uses_contracts(coinm):
    """带 size 的部分平仓：size 是基础币，送出去的仍是张。"""
    close = _endpoint(coinm.app, "/api/positions/close", "POST")

    result = close(ClosePayload(symbol=COINM, size=BASE_QTY / 2))

    assert result["size"] == pytest.approx(BASE_QTY / 2)
    assert coinm.venue.fills[0]["amount"] == pytest.approx(CONTRACTS / 2)
    assert coinm.venue.position_base(COINM) == pytest.approx(BASE_QTY / 2)


def test_protection_on_a_coinm_position_is_sized_in_contracts(coinm):
    """保护单：止损/止盈单的张数等于持仓张数，返回体的 size 是基础币。"""
    protect = _endpoint(coinm.app, "/api/positions/protection", "PUT")

    result = protect(
        ProtectionPayload(symbol=COINM, stop_loss=48_000.0, take_profit=52_000.0)
    )

    assert result["size"] == pytest.approx(BASE_QTY)
    assert result["ok"] is True
    amounts = [entry[3] for entry in coinm.venue.stop_orders]
    assert amounts, "止损止盈一张都没挂上"
    for amount in amounts:
        assert amount == pytest.approx(CONTRACTS)


def test_a_missing_price_makes_the_conversion_unknown_not_flat(coinm):
    """折不出币时返回 None（不知道），绝不回 0 让调用方以为没有持仓。

    币本位的张要按价折成币，价格缺失时 ``contracts_to_base`` 会回 0.0；那和
    「没有持仓」长得一模一样，所以这一层必须先拦下来。
    """
    converted = web_app._contracts_to_base(
        coinm.venue, MarketType.COINM, COINM, CONTRACTS, 0.0
    )

    assert converted is None
