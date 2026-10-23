"""GET /api/tickers：一次问一批 24 小时涨幅。

三条底线，缺一条界面就会说谎：

1. 读不到的字段给 `None`，不给 0 ——「读不到」和「恰好不涨不跌」不是一回事；
2. 一次最多 50 个：1400 项整市场拉下来会把面板拖死；
3. 符号形状和市场对不上当场 400，别花一次交易所请求才失败。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

import pytest
from fastapi import HTTPException

from btrader.core.types import MarketType, Network
from btrader.exchange.rest import ExchangeError
from btrader.web import app as web_app

SPOT = "BTC/USDT"
USDM = "BTC/USDT:USDT"
LIMIT = 50


def _row(symbol: str, last=81684.01, change=-1.309, volume=1460414449.6) -> dict:
    return {"symbol": symbol, "last": last, "change_pct": change, "quote_volume": volume}


class _Rest:
    """只实现 `ticker_snapshots` 的替身。

    `missing=True` 时连这个名字都没有，用来钉「没有批量接口就 503」。
    """

    def __init__(self, rows=None, boom: BaseException | None = None, missing: bool = False) -> None:
        self.rows = rows
        self.boom = boom
        self.calls: list[list[str]] = []
        self.markets: list[MarketType] = []
        if missing:
            self.ticker_snapshots = None  # type: ignore[assignment]

    def ticker_snapshots(self, market: MarketType, symbols: list[str]) -> list[dict]:
        self.calls.append(list(symbols))
        self.markets.append(market)
        if self.boom is not None:
            raise self.boom
        if self.rows is not None:
            wanted = set(symbols)
            return [row for row in self.rows if row["symbol"] in wanted]
        return [_row(symbol) for symbol in symbols]


@dataclass
class _State:
    ctx: object
    locks: dict
    account_lock: threading.RLock


class _Ctx:
    def __init__(self, rest: _Rest) -> None:
        self.rest = rest
        self.network = Network.MAINNET
        self.market_type = MarketType.SPOT

    def market_rest_for(self, market: MarketType):
        return self.rest

    def rest_for(self, market: MarketType):
        return self.rest


def _endpoint(rest: _Rest, monkeypatch):
    state = _State(
        ctx=_Ctx(rest),
        locks={market: threading.RLock() for market in MarketType},
        account_lock=threading.RLock(),
    )
    monkeypatch.setattr(web_app, "_state", state, raising=False)
    app = web_app.create_app()
    for route in app.routes:
        if getattr(route, "path", "") == "/api/tickers" and "GET" in getattr(route, "methods", ()):
            return route.endpoint
    raise AssertionError("缺少 GET /api/tickers")


# --------------------------------------------------------------- 正常三条数
def test_the_batch_returns_last_change_and_quote_volume(monkeypatch):
    rest = _Rest([_row(SPOT), _row("ETH/USDT", last=3000.5, change=1.23, volume=99.0)])

    out = _endpoint(rest, monkeypatch)(market="spot", symbols=f"{SPOT},ETH/USDT")

    assert [item["symbol"] for item in out["items"]] == [SPOT, "ETH/USDT"]
    assert out["items"][1] == {
        "symbol": "ETH/USDT",
        "last": pytest.approx(3000.5),
        "change_pct": pytest.approx(1.23),
        "quote_volume": pytest.approx(99.0),
    }
    assert rest.calls == [[SPOT, "ETH/USDT"]]


def test_a_futures_batch_goes_to_that_market(monkeypatch):
    rest = _Rest()

    out = _endpoint(rest, monkeypatch)(market="usdm", symbols=USDM)

    assert rest.markets == [MarketType.USDM]
    assert out["items"][0]["symbol"] == USDM


# --------------------------------------------------- 读不到就是 None，不是 0
def test_missing_numbers_come_back_as_none_not_zero(monkeypatch):
    """交易所没给这几个数：界面显示「—」，不能显示 0.00%（那是「不涨不跌」）。"""
    rest = _Rest([_row(SPOT, last=None, change=None, volume=None)])

    item = _endpoint(rest, monkeypatch)(market="spot", symbols=SPOT)["items"][0]

    assert item == {"symbol": SPOT, "last": None, "change_pct": None, "quote_volume": None}
    assert item["change_pct"] is not 0.0


def test_a_real_zero_change_stays_zero(monkeypatch):
    rest = _Rest([_row(SPOT, change=0.0)])

    assert _endpoint(rest, monkeypatch)(market="spot", symbols=SPOT)["items"][0]["change_pct"] == 0.0


# ------------------------------------------------------------------- 上限 50
def test_fifty_is_the_limit(monkeypatch):
    rest = _Rest()
    names = [f"COIN{index}/USDT" for index in range(LIMIT)]

    out = _endpoint(rest, monkeypatch)(market="spot", symbols=",".join(names))

    assert len(out["items"]) == LIMIT
    assert len(rest.calls[0]) == LIMIT


def test_fifty_one_is_a_400(monkeypatch):
    rest = _Rest()
    names = [f"COIN{index}/USDT" for index in range(LIMIT + 1)]

    with pytest.raises(HTTPException) as caught:
        _endpoint(rest, monkeypatch)(market="spot", symbols=",".join(names))

    assert caught.value.status_code == 400
    assert str(LIMIT) in str(caught.value.detail)
    assert rest.calls == [], "超上限不该再打一次交易所"


def test_duplicates_are_asked_once(monkeypatch):
    rest = _Rest()

    out = _endpoint(rest, monkeypatch)(market="spot", symbols=f"{SPOT}, {SPOT} ,ETH/USDT")

    assert rest.calls == [[SPOT, "ETH/USDT"]]
    assert len(out["items"]) == 2


# --------------------------------------------------------------- 写法与失败
def test_an_empty_symbol_list_is_a_400(monkeypatch):
    with pytest.raises(HTTPException) as caught:
        _endpoint(_Rest(), monkeypatch)(market="spot", symbols=" , ,")

    assert caught.value.status_code == 400


def test_a_contract_symbol_on_spot_is_rejected(monkeypatch):
    rest = _Rest()

    with pytest.raises(HTTPException) as caught:
        _endpoint(rest, monkeypatch)(market="spot", symbols=USDM)

    assert caught.value.status_code == 400
    assert SPOT in str(caught.value.detail)
    assert rest.calls == []


def test_a_spot_symbol_on_futures_is_rejected(monkeypatch):
    rest = _Rest()

    with pytest.raises(HTTPException) as caught:
        _endpoint(rest, monkeypatch)(market="usdm", symbols=SPOT)

    assert caught.value.status_code == 400
    assert USDM in str(caught.value.detail)
    assert rest.calls == []


def test_a_client_without_the_batch_api_is_a_503(monkeypatch):
    with pytest.raises(HTTPException) as caught:
        _endpoint(_Rest(missing=True), monkeypatch)(market="spot", symbols=SPOT)

    assert caught.value.status_code == 503
    assert any("\u4e00" <= char <= "\u9fff" for char in str(caught.value.detail))


def test_a_venue_failure_carries_the_reason(monkeypatch):
    rest = _Rest(boom=ExchangeError("fapi request timeout"))

    with pytest.raises(HTTPException) as caught:
        _endpoint(rest, monkeypatch)(market="usdm", symbols=USDM)

    assert caught.value.status_code == 503
    assert "fapi request timeout" in str(caught.value.detail)
