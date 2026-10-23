"""交易对写法决定市场类型：一份判定，交割合约归它结算币那一侧。

实测报的一条：``core.types.market_for_symbol("BTC/USDT:USDT-250328")`` 回 coinm ——
它只看冒号后面是不是 USDT/USDC，没把 ``-YYMMDD`` 切掉，``USDT-250328`` 自然不等于
``USDT``。后果不在显示上：不带 market 参数的平仓、撤单、保护单先按符号推市场，
推成币本位就去币本位的客户端签名，那张单发不到用户持仓的那个市场。

同一件事曾经有三份写法：core.types（错）、web.app 的 _check_symbol_shape（自己一份
正则）、engine.trading 的 _symbol_market（对，注释里写着 core 那份漏了这一步）。
现在只有 core.types.split_symbol / market_for_symbol，其余全部改成调用它。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

import pytest

from btrader.core.config import AppSettings
from btrader.core.storage import Database
from btrader.core.types import (
    MarketType,
    Network,
    market_for_symbol,
    split_symbol,
    symbol_is_contract,
    symbol_shape_error,
)
from btrader.engine.account import AccountService
from btrader.engine.risk import RiskManager
from btrader.engine.trading import TradingService, _symbol_market
from btrader.web import app as web_app
from btrader.web.app import ClosePayload, ProtectionPayload, create_app

from fakes import FakeVenue

SPOT = "BTC/USDT"
USDM_PERP = "BTC/USDT:USDT"
COINM_PERP = "BTC/USD:BTC"
USDM_DELIVERY = "BTC/USDT:USDT-250328"
COINM_DELIVERY = "BTC/USD:BTC-250328"

# 真实符号各判一次：交割合约属于它结算币那一侧的市场。
CASES = [
    (SPOT, MarketType.SPOT),
    (USDM_PERP, MarketType.USDM),
    (COINM_PERP, MarketType.COINM),
    (USDM_DELIVERY, MarketType.USDM),
    (COINM_DELIVERY, MarketType.COINM),
]


# ------------------------------------------------------------ 一份判定
@pytest.mark.parametrize("symbol,expected", CASES)
def test_a_symbol_lands_in_the_market_it_is_traded_on(symbol, expected):
    """交割合约（带到期日）别再被判成币本位。"""
    assert market_for_symbol(symbol) is expected


@pytest.mark.parametrize("symbol,expected", CASES)
def test_the_web_and_engine_helpers_agree_with_the_core_one(symbol, expected):
    """三处各写一份的时候只有交易账本那份是对的，现在必须一致。"""
    assert web_app._market_for_symbol(symbol) is expected
    assert _symbol_market(symbol) is expected


def test_the_expiry_date_is_split_off_the_settlement() -> None:
    """split_symbol 是唯一那处切分：到期日不属于结算币。"""
    assert split_symbol(USDM_DELIVERY) == ("BTC", "USDT", "USDT")
    assert split_symbol(COINM_DELIVERY) == ("BTC", "USD", "BTC")
    assert split_symbol(SPOT) == ("BTC", "USDT", "")
    assert split_symbol("  BTC/USDT:USDC  ") == ("BTC", "USDT", "USDC")


def test_a_spot_pair_is_not_a_contract_but_a_dated_one_is() -> None:
    """写法判定：现货一个冒号都没有，交割多一段到期日。"""
    assert symbol_is_contract(SPOT) is False
    assert symbol_is_contract(USDM_PERP) is True
    assert symbol_is_contract(USDM_DELIVERY) is True


def test_an_empty_symbol_does_not_pass_for_a_contract() -> None:
    """空符号不许被判成合约（会不会被当成现货由调用方自己拦）。"""
    assert split_symbol("") == ("", "", "")
    assert market_for_symbol("") is MarketType.SPOT


# ------------------------------------------- 写法校验：冒号只能有一个
# 量测复核抓的一条：BTC/USDT:USDT:USDT 在 symbol_shape_error 里返回 ""（写得像），
# 于是它进得了收藏、进得了 /api/tickers，到交易所那一层才炸。结算币段里再出现冒号
# 不是「另一种写法」，是写错了——real 符号整串最多一个冒号。


@pytest.mark.parametrize(
    "symbol",
    [
        "BTC/USDT:USDT:USDT",
        "BTC/USD:BTC:USDT",
        "BTC/USDT:USDT:USDT-250926",
        "BTC/USDT::USDT",
    ],
)
def test_a_second_colon_is_a_shape_error(symbol: str) -> None:
    """两个冒号判写法错，并说清该写成什么。"""
    problem = symbol_shape_error(symbol)
    assert problem, f"{symbol} 不该通过写法校验"
    assert "冒号" in problem
    # 文案要能照着改：四种合法写法至少点出合约与币本位这两种
    assert "BTC/USDT:USDT" in problem
    assert "BTC/USD:BTC" in problem


@pytest.mark.parametrize(
    "symbol",
    ["BTC//USDT", "BTC/USDT:", ":USDT", ":", "BTC/", "/USDT", "BTC/USDT:-250926"],
)
def test_empty_segments_are_shape_errors(symbol: str) -> None:
    """空段与只有冒号：都不是交易对。"""
    assert symbol_shape_error(symbol), f"{symbol} 不该通过写法校验"


@pytest.mark.parametrize(
    "symbol",
    [
        "BTC/USDT",              # 现货
        "BTC/USDT:USDT",         # U 本位永续
        "BTC/USD:BTC",           # 币本位永续
        "BTC/USD:BTC-250926",    # 币本位交割：到期日要能过
        "BTC/USDT:USDT-250926",  # U 本位交割同样
        "  btc/usdt:usdt  ",     # 前后空格要去掉；大小写混写不拦（交易所符号本身不区分大小写）
    ],
)
def test_the_legal_spellings_still_pass(symbol: str) -> None:
    """补齐合法写法：改坏一处校验最容易顺手把交割合约一起拒了。"""
    assert symbol_shape_error(symbol) == ""


# ------------------------------------------- 不带 market 的入口走对客户端
@dataclass
class _State:
    ctx: object
    locks: dict
    account_lock: threading.RLock


class _Ctx:
    """按市场给客户端的上下文，并记下每个市场被问了几次。"""

    def __init__(self, venues: dict[MarketType, FakeVenue], db: Database) -> None:
        self.settings = AppSettings(network=Network.TESTNET.value)
        self.network = Network.TESTNET
        self.market_type = MarketType.USDM
        self.venues = venues
        self.asked: list[MarketType] = []
        self.trading = TradingService(
            settings=self.settings,
            rest=venues[MarketType.USDM],
            risk=RiskManager(self.settings.risk),
            db=db,
            notifier=None,
            rest_for=self.rest_for,
        )
        self.account = AccountService(
            rest_for=self.rest_for,
            chart_rest_for=self.rest_for,
            db=db,
            network=Network.TESTNET,
        )

    def _venue(self, market: MarketType) -> FakeVenue:
        self.asked.append(market)
        return self.venues[market]

    def rest_for(self, market: MarketType) -> FakeVenue:
        return self._venue(market)

    def quote_rest(self, market: MarketType) -> FakeVenue:
        return self._venue(market)

    def market_rest_for(self, market: MarketType) -> FakeVenue:
        return self._venue(market)


@dataclass
class Box:
    app: object
    ctx: _Ctx

    def venue(self, market: MarketType) -> FakeVenue:
        return self.ctx.venues[market]


@pytest.fixture()
def venues(tmp_path, monkeypatch):
    """三个市场各一个替身；U 本位那边挂着一张交割合约的多单。"""
    book = {market: FakeVenue() for market in MarketType}
    for venue in book.values():
        venue._last[USDM_DELIVERY] = 80_000.0
        venue._last[COINM_DELIVERY] = 80_000.0
    book[MarketType.USDM].create_order(
        MarketType.USDM, USDM_DELIVERY, "market", "buy", 1.0, 80_000.0
    )
    db = Database(tmp_path / "market.db")
    ctx = _Ctx(book, db)
    state = _State(
        ctx=ctx,
        locks={market: threading.RLock() for market in MarketType},
        account_lock=threading.RLock(),
    )
    monkeypatch.setattr(web_app, "_state", state, raising=False)
    try:
        yield Box(app=create_app(), ctx=ctx)
    finally:
        db.close()


def _endpoint(app, path: str, method: str):
    for route in app.routes:
        if getattr(route, "path", "") == path and method in getattr(route, "methods", ()):
            return route.endpoint
    raise AssertionError(f"missing route {method} {path}")


def test_closing_a_delivery_contract_asks_the_usdm_venue(venues):
    """不带 market 平一张交割合约：以前按符号推成币本位，单发错了市场。"""
    close = _endpoint(venues.app, "/api/positions/close", "POST")

    result = close(ClosePayload(symbol=USDM_DELIVERY))

    assert result["size"] == pytest.approx(1.0)
    assert result["side"] == "sell"
    assert venues.venue(MarketType.USDM).position_base(USDM_DELIVERY) == pytest.approx(0.0)
    assert MarketType.COINM not in venues.ctx.asked
    assert venues.venue(MarketType.COINM).fills == []


def test_cancelling_an_order_on_a_delivery_contract_asks_the_usdm_venue(venues):
    """撤单也只收到符号时，同样得落到 U 本位那边。"""
    cancel = _endpoint(venues.app, "/api/orders/{order_id}", "DELETE")

    cancel(order_id="123456", symbol=USDM_DELIVERY, market=None)

    assert MarketType.USDM in venues.ctx.asked
    assert MarketType.COINM not in venues.ctx.asked


def test_arming_a_protection_on_a_delivery_contract_asks_the_usdm_venue(venues):
    """保护单同理：走错市场等于给一个不存在的仓位挂止损。"""
    arm = _endpoint(venues.app, "/api/positions/protection", "PUT")

    payload = arm(ProtectionPayload(symbol=USDM_DELIVERY, stop_loss=79_000.0))

    assert payload["ok"] is True
    assert [item[1] for item in venues.venue(MarketType.USDM).stop_orders] == [USDM_DELIVERY]
    assert venues.venue(MarketType.COINM).stop_orders == []


def test_clearing_a_protection_on_a_coin_margined_delivery_asks_the_coinm_venue(venues):
    """币本位交割本来就是币本位，改完照旧（别修好一边弄错另一边）。"""
    clear = _endpoint(venues.app, "/api/positions/protection", "DELETE")

    clear(symbol=COINM_DELIVERY, market=None)

    assert venues.venue(MarketType.COINM).protections_cleared == [COINM_DELIVERY]
    assert venues.venue(MarketType.USDM).protections_cleared == []