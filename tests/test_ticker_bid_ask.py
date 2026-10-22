"""Ticker 的买一卖一：有盘口就给真价格，拿不到才留 0。

币安的合约 24 小时 ticker 里没有买一卖一（ccxt 的 bid/ask 是 None），
以前两个入口都把它压成 0.0：/api/ticker 直接 `or 0.0`，WebSocket 只在
盘口帧之后才补得上。前端 `bid && ask` 判空，于是用户看到「有最新价、
买卖价却是 —」。这里钉住三件事：补数的来源、补不上时留 0、帧形状不变。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

import pytest
from fastapi import HTTPException

from btrader.core.types import MarketType, Network
from btrader.exchange.rest import (
    BinanceREST,
    ExchangeError,
    ticker_bid_ask,
)
from btrader.web import app as web_app

SYMBOL = "BTC/USDT:USDT"
# 币安合约 24 小时 ticker 的真实形状：没有 bid/ask 这两个字段。
FUTURES_TICKER = {
    "symbol": SYMBOL,
    "last": 81_613.5,
    "bid": None,
    "ask": None,
    "high": 84_875.2,
    "low": 81_264.1,
    "baseVolume": 1_000.0,
    "quoteVolume": 81_000_000.0,
    "percentage": 1.25,
    "timestamp": 1_792_685_211_080,
    "info": {"lastPrice": "81613.5", "symbol": "BTCUSDT"},
}


# ------------------------------------------------------------------ helper
def test_a_futures_ticker_borrows_the_book_top() -> None:
    """合约 ticker 里没有买卖价：用盘口的买一卖一补上。"""
    bid, ask = ticker_bid_ask(FUTURES_TICKER, 81_613.5, 81_613.6)

    assert bid == pytest.approx(81_613.5)
    assert ask == pytest.approx(81_613.6)


def test_the_ticker_own_spread_wins_over_the_book() -> None:
    """现货 ticker 自带买卖价：不要拿另一个时刻的盘口盖掉它。"""
    spot = {"last": 1.0, "bid": 0.99, "ask": 1.01, "info": {}}

    bid, ask = ticker_bid_ask(spot, 0.5, 1.5)

    assert (bid, ask) == (pytest.approx(0.99), pytest.approx(1.01))


def test_the_raw_payload_is_read_before_spending_a_request() -> None:
    """ccxt 的 unified 字段是 None、原始 payload 里却有买卖价时，别多打一次接口。"""
    raw = {"last": 1.0, "bid": None, "ask": None, "info": {"bidPrice": "0.9", "askPrice": "1.1"}}

    assert ticker_bid_ask(raw) == (pytest.approx(0.9), pytest.approx(1.1))


def test_nothing_available_stays_zero_so_the_ui_shows_a_dash() -> None:
    """什么都没拿到就留 0，前端 `bid && ask` 判空显示「—」。"""
    assert ticker_bid_ask(FUTURES_TICKER) == (0.0, 0.0)


# ------------------------------------------------------------- bookTicker
class FakeBookTickerClient:
    """只实现 bookTicker 原始端点的 ccxt 替身。"""

    def __init__(self, payload) -> None:
        self.markets = {SYMBOL: {"id": "BTCUSDT", "symbol": SYMBOL, "contract": True}}
        self.payload = payload
        self.params: list[dict] = []
        self.forget_markets = False

    def load_markets(self) -> dict:
        return self.markets

    def market(self, symbol: str) -> dict:
        return self.markets[symbol]

    def fapiPublicGetTickerBookTicker(self, params=None) -> dict:
        self.params.append(dict(params or {}))
        return self.payload

    def dapiPublicGetTickerBookTicker(self, params=None):
        self.params.append(dict(params or {}))
        return self.payload


def _rest(client) -> BinanceREST:
    rest = BinanceREST(Network.MAINNET)
    rest._clients[MarketType.USDM] = client
    rest._clients[MarketType.COINM] = client
    return rest


def test_book_ticker_is_asked_with_the_venue_symbol() -> None:
    """币安要 BTCUSDT，不是 BTC/USDT:USDT。"""
    client = FakeBookTickerClient(
        {"symbol": "BTCUSDT", "bidPrice": "81613.50", "askPrice": "81613.60"}
    )

    bid, ask = _rest(client).fetch_bid_ask(MarketType.USDM, SYMBOL)

    assert client.params == [{"symbol": "BTCUSDT"}]
    assert (bid, ask) == (pytest.approx(81_613.5), pytest.approx(81_613.6))


def test_coinm_answers_with_a_list_and_still_works() -> None:
    """币本位带 symbol 也回数组（实测），不能当成字典读。"""
    client = FakeBookTickerClient(
        [
            {"symbol": "BTCUSD_PERP", "bidPrice": "81544.1", "askPrice": "81544.2"},
        ]
    )
    rest = _rest(client)
    rest._clients[MarketType.COINM] = client

    bid, ask = rest.fetch_bid_ask(MarketType.COINM, "BTC/USD:BTC")

    assert (bid, ask) == (pytest.approx(81544.1), pytest.approx(81544.2))


def test_a_venue_that_refuses_the_book_raises_for_the_caller_to_swallow() -> None:
    class Refusing(FakeBookTickerClient):
        def fapiPublicGetTickerBookTicker(self, params=None):
            raise RuntimeError("binance {'code':-1121,'msg':'Invalid symbol.'}")

    with pytest.raises(ExchangeError):
        _rest(Refusing({})).fetch_bid_ask(MarketType.USDM, SYMBOL)


# -------------------------------------------------------------- 路由一层
@dataclass
class _State:
    ctx: object
    locks: dict


class _TickerRest:
    def __init__(self, ticker: dict, spread=(0.0, 0.0), fail_book: bool = False) -> None:
        self.ticker = ticker
        self.spread = spread
        self.fail_book = fail_book
        self.book_calls = 0

    def fetch_ticker(self, market: MarketType, symbol: str) -> dict:
        return dict(self.ticker)

    def fetch_bid_ask(self, market: MarketType, symbol: str) -> tuple[float, float]:
        self.book_calls += 1
        if self.fail_book:
            raise RuntimeError("bookTicker 超时")
        return self.spread


class _Ctx:
    def __init__(self, rest: _TickerRest) -> None:
        self.rest = rest

    def market_rest_for(self, market: MarketType) -> _TickerRest:
        return self.rest


def _ticker_endpoint(monkeypatch, rest: _TickerRest):
    state = _State(
        ctx=_Ctx(rest),
        locks={market: threading.RLock() for market in MarketType},
    )
    monkeypatch.setattr(web_app, "_state", state, raising=False)
    app = web_app.create_app()
    for route in app.routes:
        if getattr(route, "path", "") == "/api/ticker":
            return route.endpoint
    raise AssertionError("missing /api/ticker")


def test_the_ticker_route_reports_the_real_spread(monkeypatch) -> None:
    """现象：/api/ticker 的 bid/ask 是 0.0，盘口却有价。"""
    rest = _TickerRest(FUTURES_TICKER, spread=(81_613.5, 81_613.6))
    payload = _ticker_endpoint(monkeypatch, rest)(market="usdm", symbol=SYMBOL)

    assert rest.book_calls == 1
    assert payload["bid"] == pytest.approx(81_613.5)
    assert payload["ask"] == pytest.approx(81_613.6)
    assert payload["last"] == pytest.approx(81_613.5)


def test_the_ticker_route_keeps_the_frame_when_the_book_is_down(monkeypatch) -> None:
    """买卖一取不到只留 0，最新价照样要出：不能因为补数失败整页报错。"""
    rest = _TickerRest(FUTURES_TICKER, fail_book=True)
    payload = _ticker_endpoint(monkeypatch, rest)(market="usdm", symbol=SYMBOL)

    assert payload["bid"] == 0.0
    assert payload["ask"] == 0.0
    assert payload["last"] == pytest.approx(81_613.5)
    assert set(payload) == {
        "symbol",
        "last",
        "bid",
        "ask",
        "high",
        "low",
        "base_volume",
        "quote_volume",
        "change_pct",
        "timestamp",
    }


def test_a_spot_ticker_with_its_own_spread_costs_no_extra_request(monkeypatch) -> None:
    """现货 ticker 自带买卖价，不该为了补数多打一次请求。"""
    spot = {"last": 81_654.0, "bid": 81_654.0, "ask": 81_654.01, "info": {}}
    rest = _TickerRest(spot, spread=(1.0, 2.0))
    payload = _ticker_endpoint(monkeypatch, rest)(market="spot", symbol="BTC/USDT")

    assert rest.book_calls == 0
    assert payload["ask"] == pytest.approx(81_654.01)