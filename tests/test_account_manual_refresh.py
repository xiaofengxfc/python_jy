"""面板要能要一份「此刻」的账户：/api/account 的 refresh 参数。

现货实测：成交之后大约 10 秒里，持仓面板还把刚买来的那一行显示成「钱包余额」
（按钮是「卖出」、预填整笔余额）。根因不是账本，是快照的缓存——面板每 2 秒轮询一次，
吃的是 3/10 秒的缓存，而且前端没有办法说「给我最新的」。

改法：`GET /api/account?refresh=1` 走 `AccountService.live_snapshot`（现读），
默认仍吃缓存。现读只按交易对读：不带 symbol 的整钱包现读是 `/api/portfolio` 的活，
面板按需触发一次就把整个钱包的币都定价一遍，代价太大。也不进 `account_lock`：
读余额不需要排队等下单。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from btrader.core.config import AppSettings
from btrader.core.contracts import ContractSpec
from btrader.core.storage import Database
from btrader.core.types import MarketType, Network
from btrader.engine.account import AccountService
from btrader.engine.risk import RiskManager
from btrader.engine.trading import TradingService
from btrader.web import app as web_app
from btrader.web.app import PreviewPayload

from fakes import FakeVenue

SPOT = "BTC/USDT"
PRICE = 100.0


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

    def market_rest_for(self, market: MarketType):
        return self.venue

    def quote_rest(self, market: MarketType):
        return self.venue


@dataclass
class _State:
    ctx: object
    locks: dict
    account_lock: threading.RLock


@dataclass
class Box:
    app: object
    venue: FakeVenue
    db: Database
    state: _State


def _endpoint(app, path: str, method: str):
    for route in app.routes:
        if getattr(route, "path", "") == path and method in getattr(route, "methods", ()):
            return route.endpoint
    raise AssertionError(f"missing route {method} {path}")


@pytest.fixture()
def box(tmp_path, monkeypatch) -> Box:
    venue = FakeVenue(balances={MarketType.SPOT: {"USDT": 1_000.0, "BTC": 1.0}})
    venue._last[SPOT] = PRICE
    db = Database(tmp_path / "refresh.db")
    ctx = _Ctx(venue, db)
    state = _State(
        ctx=ctx,
        locks={market: threading.RLock() for market in MarketType},
        account_lock=threading.RLock(),
    )
    monkeypatch.setattr(web_app, "_state", state, raising=False)
    try:
        yield Box(app=web_app.create_app(), venue=venue, db=db, state=state)
    finally:
        db.close()


def _account(box: Box, **params):
    endpoint = _endpoint(box.app, "/api/account", "GET")
    return endpoint(**params)


def _btc(box: Box) -> float:
    return float(box.venue.balances[MarketType.SPOT]["BTC"])


# --------------------------------------------------------------- refresh=1
def test_refresh_reads_the_venue_again(box: Box):
    """刷新一次要看到交易所此刻的数量，而不是缓存里那一份。"""
    first = _account(box, market="spot", symbol=SPOT)
    assert first["positions"][0]["contracts"] == pytest.approx(1.0)

    box.venue.balances[MarketType.SPOT]["BTC"] = 1.5

    fresh = _account(box, market="spot", symbol=SPOT, refresh="1")
    assert fresh["positions"][0]["contracts"] == pytest.approx(1.5)


def test_refresh_sees_a_new_row_that_the_cache_does_not_have(box: Box):
    """刚买来的那一对：账本里有了，缓存里还是「钱包余额」那一行。"""
    _account(box, market="spot", symbol=SPOT)  # 冷读，把缓存填上

    # 程序买了一笔，账本记上，钱包也跟着变
    box.db.save_trade(
        "binance:testnet",
        {
            "id": "fill-1",
            "symbol": SPOT,
            "side": "buy",
            "price": PRICE,
            "amount": 0.2,
            "cost": 0.2 * PRICE,
            "fee": {"cost": 0.0, "currency": "USDT"},
            "timestamp": 1_700_000_000_000,
        },
        "live",
    )
    box.venue.balances[MarketType.SPOT]["BTC"] = 1.2

    cached = _account(box, market="spot", symbol=SPOT)
    assert cached["positions"][0]["origin"] == "wallet"

    fresh = _account(box, market="spot", symbol=SPOT, refresh="1")
    row = fresh["positions"][0]
    assert row["origin"] == "ledger"
    assert row["contracts"] == pytest.approx(1.2)


def test_the_plain_read_still_uses_the_cache(box: Box):
    """不带 refresh 的那条路是每 2 秒一次的面板轮询：必须继续吃缓存。"""
    _account(box, market="spot", symbol=SPOT)
    box.venue.balances[MarketType.SPOT]["BTC"] = 1.5

    again = _account(box, market="spot", symbol=SPOT)
    assert again["positions"][0]["contracts"] == pytest.approx(1.0)


def test_a_refresh_without_a_symbol_does_not_reprice_the_whole_wallet(box: Box):
    """整钱包定价是 /api/portfolio 的活：面板按需刷新不能顺手把它做一遍。"""
    calls = {"balance": 0, "tickers": 0}
    balance = box.venue.fetch_balance
    tickers = box.venue.fetch_tickers

    def counting_balance(market):
        calls["balance"] += 1
        return balance(market)

    def counting_tickers(market, symbols=None):
        calls["tickers"] += 1
        return tickers(market, symbols)

    box.venue.fetch_balance = counting_balance  # type: ignore[assignment]
    box.venue.fetch_tickers = counting_tickers  # type: ignore[assignment]

    first = _account(box, market="spot")
    before = dict(calls)
    second = _account(box, market="spot", refresh="1")

    assert calls == before, "不带 symbol 的 refresh 不该再整钱包读一遍"
    assert set(first) == set(second)


def test_the_payload_shape_does_not_depend_on_refresh(box: Box):
    plain = _account(box, market="spot", symbol=SPOT)
    fresh = _account(box, market="spot", symbol=SPOT, refresh="1")
    assert set(plain) == set(fresh)
    assert set(plain["positions"][0]) == set(fresh["positions"][0])


def test_refresh_does_not_wait_for_the_account_lock(box: Box):
    """读账户不该排在订单后面：现读不进 account_lock。"""
    holding = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with box.state.account_lock:
            holding.set()
            release.wait(5)

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    try:
        assert holding.wait(5)
        started = time.monotonic()
        payload = _account(box, market="spot", symbol=SPOT, refresh="1")
        elapsed = time.monotonic() - started
        assert payload["positions"], payload
        assert elapsed < 2.0, f"现读等了 {elapsed:.2f} 秒的 account_lock"
    finally:
        release.set()
        thread.join(5)
