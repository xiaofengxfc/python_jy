"""现货平仓的口径：只平本地成交账本记着的数量，绝不把钱包清空。

现货的「持仓」就是币余额，里面混着用户自己充提的钱。``POST /api/positions/close``
不带 size 时原本把整个余额当成持仓卖掉：t33 实测用一次不带 size 的平仓，把钱包里
本来就有的 1 BTC（不是本程序买的）全卖了。合约没有这个问题，交易所那份持仓本来就是
本程序开的（或至少是账户在这个市场上的敞口）。

改后的口径：

- 不带 size → 只平 ``trading._live_ledger()["books"]`` 里这一对的持仓；
- 账本里没有这一对 → 400，说清要显式给 size，钱包一个币都不动；
- 带 size → 卖这么多，仍按钱包可用余额封顶（现货不能卖自己没有的币）。
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
from btrader.engine.trading import TradingService, ledger_key
from btrader.web import app as web_app
from btrader.web.app import ClosePayload

from fakes import FakeVenue

SPOT_SYMBOL = "BTC/USDT"
FUTURES_SYMBOL = "BTC/USDT:USDT"


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
        self.market_type = MarketType.SPOT
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
def spot_app(tmp_path, monkeypatch):
    """现货账户：钱包里 1 BTC，是用户自己充的，跟本程序没关系。"""
    venue = FakeVenue(
        balances={
            MarketType.SPOT: {"USDT": 10_000.0, "BTC": 1.0},
            MarketType.USDM: {"USDT": 10_000.0},
            MarketType.COINM: {"BTC": 0.2},
        }
    )
    venue._last[SPOT_SYMBOL] = 100.0
    venue._last[FUTURES_SYMBOL] = 100.0
    db = Database(tmp_path / "close.db")
    ctx = _Ctx(venue, db)
    state = _State(
        ctx=ctx,
        locks={market: threading.RLock() for market in MarketType},
        account_lock=threading.RLock(),
    )
    monkeypatch.setattr(web_app, "_state", state, raising=False)
    try:
        yield SimpleBox(app=web_app.create_app(), venue=venue, db=db, ctx=ctx)
    finally:
        db.close()


@dataclass
class SimpleBox:
    app: object
    venue: FakeVenue
    db: Database
    ctx: object


def _book(db: Database, symbol: str, contracts: float, entry: float = 100.0) -> None:
    db.set_json(
        ledger_key(Network.TESTNET),
        {"seen": [], "books": {symbol: {"contracts": contracts, "entry": entry}}},
    )


def _btc(spot_app: SimpleBox) -> float:
    return float(spot_app.venue.balances[MarketType.SPOT]["BTC"])


def _close(spot_app: SimpleBox, **payload):
    endpoint = _endpoint(spot_app.app, "/api/positions/close", "POST")
    return endpoint(ClosePayload(**payload))


# ------------------------------------------------------------ 复现：卖光钱包
def test_a_spot_close_without_a_size_leaves_the_wallet_alone(spot_app):
    """钱包里有 1 BTC、账本里没有这一对：不带 size 不许卖，一个币都不能动。"""
    assert _btc(spot_app) == pytest.approx(1.0)

    with pytest.raises(HTTPException) as caught:
        _close(spot_app, symbol=SPOT_SYMBOL, market="spot")

    assert caught.value.status_code == 400
    detail = str(caught.value.detail)
    assert "账本" in detail and "size" in detail, detail
    # 改之前这里会把 1 BTC 全卖掉
    assert _btc(spot_app) == pytest.approx(1.0)
    assert spot_app.venue.fills == []


def test_a_spot_close_with_an_explicit_size_sells_exactly_that(spot_app):
    """账本里没有记录时，显式给 size 才卖，卖的就是这么多。"""
    result = _close(spot_app, symbol=SPOT_SYMBOL, market="spot", size=0.001)

    assert result["size"] == pytest.approx(0.001)
    assert result["side"] == "sell"
    assert _btc(spot_app) == pytest.approx(0.999)


def test_a_spot_close_without_a_size_follows_the_book(spot_app):
    """账本记着 0.5：不带 size 平 0.5，钱包剩下的 0.5 留着。"""
    _book(spot_app.db, SPOT_SYMBOL, 0.5)

    result = _close(spot_app, symbol=SPOT_SYMBOL, market="spot")

    assert result["size"] == pytest.approx(0.5)
    assert _btc(spot_app) == pytest.approx(0.5)


def test_an_explicit_size_is_still_capped_by_the_wallet(spot_app):
    """显式 size 也不能超过钱包可用余额：现货卖不出自己没有的币。"""
    _book(spot_app.db, SPOT_SYMBOL, 3.0)

    result = _close(spot_app, symbol=SPOT_SYMBOL, market="spot", size=5.0)

    assert result["size"] == pytest.approx(1.0)
    assert _btc(spot_app) == pytest.approx(0.0)


def test_a_booked_size_beyond_the_wallet_is_capped(spot_app):
    """账本记的数量比钱包多（手工提走了一部分）：按钱包封顶，不许卖空气。"""
    _book(spot_app.db, SPOT_SYMBOL, 1.5)

    result = _close(spot_app, symbol=SPOT_SYMBOL, market="spot")

    assert result["size"] == pytest.approx(1.0)
    assert _btc(spot_app) == pytest.approx(0.0)


# --------------------------------------------------------------- 合约不变
def test_a_futures_close_without_a_size_still_closes_the_whole_position(spot_app):
    """合约侧照旧：不带 size 就是全平，那条口径本来就对。"""
    spot_app.venue.create_order(
        MarketType.USDM, FUTURES_SYMBOL, "market", "buy", 2.0, 100.0
    )

    result = _close(spot_app, symbol=FUTURES_SYMBOL, market="usdm")

    assert result["size"] == pytest.approx(2.0)
    assert result["side"] == "sell"
