"""现货成交必须真的进本地账本，否则「平仓」和风控都看不见现货。

t48 在测试网上把现货功能面走了一遍，撞到三件事：

* 成交同步拿 U 本位的客户端去问现货成交，币安回 -2015（那把 Key 没有现货权限），
  异常被 catch 成一行 debug：现货成交从来没进过账本，风控也收不到现货平仓的盈亏。
* 现货交易对只在「有挂单」或「设置指向现货」时才被轮询。设置指向合约时，刚买进来
  的一对没人问，账本永远是空的，不带 size 的平仓只能回「账本里没有这一对」。
* 账本里的数量是浮点累加出来的（0.00012 + 0.0003 = 0.00041999999999999996），
  直接交给交易所截位会掉一个最小步长，全平之后账本上剩下卖不掉的尘埃。
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass

import pytest
from fastapi import HTTPException

from btrader.core.config import AppSettings
from btrader.core.storage import Database
from btrader.core.types import MarketType, Network
from btrader.engine.account import AccountService
from btrader.engine.risk import RiskManager
from btrader.engine.trading import LIVE_LEDGER_KEY, OrderRequest, TradingService
from btrader.exchange.rest import ExchangeError
from btrader.web import app as web_app
from btrader.web.app import ClosePayload

from fakes import FakeVenue

SPOT_SYMBOL = "BTC/USDT"
FUTURES_SYMBOL = "BTC/USDT:USDT"

NO_SPOT_PERMISSION = (
    'binance {"code":-2015,"msg":"Invalid API-key, IP, or permissions for action."}'
)


def _fill(trade_id: str, side: str, amount: float, price: float = 100.0) -> dict:
    return {
        "id": trade_id,
        "symbol": SPOT_SYMBOL,
        "side": side,
        "amount": amount,
        "price": price,
        "timestamp": 1_700_000_000_000,
        "fee": {"cost": 0.0, "currency": "USDT"},
    }


class _UsdmVenue(FakeVenue):
    """U 本位客户端：拿它去问现货成交，币安回 -2015。"""

    def fetch_my_trades(self, market: MarketType, symbol: str, limit: int = 50):
        if not market.is_futures:
            raise ExchangeError(NO_SPOT_PERMISSION)
        return super().fetch_my_trades(market, symbol, limit)


class _TruncatingVenue(FakeVenue):
    """ccxt 的 amount_to_precision 是截位（Decimal），这里照抄那个行为。"""

    def amount_to_precision(self, market: MarketType, symbol: str, amount: float) -> float:
        return math.floor(float(amount) * 10**5) / 10**5


def _settings() -> AppSettings:
    """默认设置 + 全局市场指向合约：这正是线上出问题的那套组合。"""
    settings = AppSettings(network=Network.TESTNET.value)
    settings.market_type = MarketType.USDM.value
    settings.last_symbol = FUTURES_SYMBOL
    return settings


@dataclass
class _State:
    ctx: object
    locks: dict
    account_lock: threading.RLock


def _endpoint(app, path: str, method: str = "POST"):
    for route in app.routes:
        if getattr(route, "path", "") == path and method in getattr(route, "methods", ()):
            return route.endpoint
    raise AssertionError(f"missing route {method} {path}")


# ------------------------------------------- 每个市场用自己的客户端去问成交
def test_each_market_is_asked_with_its_own_client(tmp_path):
    """U 本位的 Key 问不到现货成交（-2015），现货那一路必须用它自己的客户端。"""
    settings = _settings()
    db = Database(tmp_path / "sync.db")
    spot = FakeVenue()
    spot._last[SPOT_SYMBOL] = 100.0
    usdm = _UsdmVenue()
    usdm._last[FUTURES_SYMBOL] = 100.0
    trading = TradingService(
        settings,
        usdm,
        RiskManager(settings.risk),
        db,
        notifier=None,
        rest_for=lambda market: usdm if market.is_futures else spot,
    )
    try:
        # 现货下一笔市价买单：替身交易所会把它记进 fills，但账本还没被轮询过
        trading.submit(
            OrderRequest(
                symbol=SPOT_SYMBOL,
                side="buy",
                order_type="market",
                amount=0.5,
                price=100.0,
                market_type=MarketType.SPOT,
            )
        )
        assert SPOT_SYMBOL in trading._live_symbols(MarketType.SPOT)

        trading.sync_live_fills([MarketType.SPOT])

        books = trading._live_ledger()["books"]
        assert books[SPOT_SYMBOL]["contracts"] == pytest.approx(0.5)
    finally:
        db.close()


# ------------------------------- 现货交易对要被轮询到（设置指向合约也一样）
def test_a_booked_spot_pair_is_still_polled_after_a_restart(tmp_path):
    """账本里记着的现货对要接着轮询：重启后 _traded 是空的，只能靠账本本身。"""
    settings = _settings()
    db = Database(tmp_path / "restart.db")
    db.set_json(
        LIVE_LEDGER_KEY,
        {
            "seen": [],
            "books": {
                SPOT_SYMBOL: {"contracts": 0.5, "entry": 100.0},
                FUTURES_SYMBOL: {"contracts": 0.002, "entry": 100.0},
            },
        },
    )
    venue = FakeVenue()
    trading = TradingService(settings, venue, RiskManager(settings.risk), db, notifier=None)
    try:
        assert trading._live_symbols(MarketType.SPOT) == [SPOT_SYMBOL]
        # 两个市场各问各的，别把合约对混进现货的轮询里
        assert trading._live_symbols(MarketType.USDM) == [FUTURES_SYMBOL]
    finally:
        db.close()


# ------------------------------- 刚买完就平仓：先同步这一对，再按账本算数量
def test_a_spot_close_syncs_the_pair_before_sizing(tmp_path, monkeypatch):
    """账本还空着（轮询还没跑到）也要平刚买进来的那 0.5，而不是钱包里的 3 BTC。"""
    venue = FakeVenue(
        balances={
            MarketType.SPOT: {"USDT": 10_000.0, "BTC": 3.0},
            MarketType.USDM: {"USDT": 10_000.0},
            MarketType.COINM: {"BTC": 0.2},
        }
    )
    venue._last[SPOT_SYMBOL] = 100.0
    venue.fills.append(_fill("t-1", "buy", 0.5))
    settings = _settings()
    db = Database(tmp_path / "close.db")

    class _Ctx:
        def __init__(self) -> None:
            self.settings = settings
            self.network = Network.TESTNET
            self.market_type = MarketType.SPOT
            self.trading = TradingService(
                settings, venue, RiskManager(settings.risk), db, notifier=None
            )
            self.account = AccountService(
                rest_for=lambda market: venue,
                chart_rest_for=lambda market: venue,
                db=db,
                network=Network.TESTNET,
            )

        def rest_for(self, market):
            return venue

        def quote_rest(self, market):
            return venue

    state = _State(
        ctx=_Ctx(),
        locks={market: threading.RLock() for market in MarketType},
        account_lock=threading.RLock(),
    )
    monkeypatch.setattr(web_app, "_state", state, raising=False)
    close = _endpoint(web_app.create_app(), "/api/positions/close")
    try:
        result = close(ClosePayload(market=MarketType.SPOT.value, symbol=SPOT_SYMBOL))
        assert result["size"] == pytest.approx(0.5)
        assert result["side"] == "sell"
        assert venue.fills[-1]["amount"] == pytest.approx(0.5)
    finally:
        db.close()


# --------------------------------- 浮点噪声不许吃掉一个最小步长（平完剩尘埃）
def test_float_noise_in_the_ledger_does_not_cost_a_step(tmp_path):
    """账本数量 0.00041999999999999996 要按 0.00042 发，截位成 0.00041 就剩尘埃。"""
    settings = _settings()
    db = Database(tmp_path / "dust.db")
    venue = _TruncatingVenue()
    trading = TradingService(settings, venue, RiskManager(settings.risk), db, notifier=None)
    try:
        amount, _base = trading.exchange_amount(
            MarketType.SPOT, SPOT_SYMBOL, 0.00012 + 0.0003, 80_000.0
        )
        assert amount == pytest.approx(0.00042)
    finally:
        db.close()
