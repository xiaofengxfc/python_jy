"""下单与预览要校验交易对的写法：符号形状和所报市场对不上，当场 400。

实测的两个残留：

* {market: spot, symbol: BTC/USDT:USDT} 的预览回 allowed: true，一路放行到下单环节；
* 反过来 {market: usdm, symbol: BTC/USDT} 也能过。

ccxt 的 load_markets 一次把现货和三种合约全取回来，符号写错了本地不会撞墙，所以这条
闸只能自己加。带结算币（BASE/QUOTE:SETTLE，交割再多一段 -YYMMDD）的写法只属于合约，
现货是 BASE/QUOTE。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass

import pytest
from fastapi import HTTPException

from btrader.context import AppContext
from btrader.core.credentials import Credentials
from btrader.core.types import MarketType, Network
from btrader.engine.account import AccountService
from btrader.engine.trading import TradingService
from btrader.web import app as web_app
from btrader.web.app import OrderPayload, PreviewPayload

from fakes import FakeVenue

FUTURES = "BTC/USDT:USDT"
DELIVERY = "BTC/USDT:USDT-250328"
SPOT = "BTC/USDT"


@dataclass
class _State:
    ctx: object
    locks: dict
    account_lock: threading.RLock


class _Venue(FakeVenue):
    """替身交易所：记下取价次数，用来证明形状校验没花掉一次请求。"""

    def __init__(self) -> None:
        super().__init__()
        self.credentials = Credentials()
        self.ticker_calls: list[tuple[str, MarketType]] = []

    def reset(self) -> None:
        return None

    def fetch_ticker(self, market: MarketType, symbol: str) -> dict:
        self.ticker_calls.append((symbol, market))
        return super().fetch_ticker(market, symbol)


def _app_context(tmp_path, monkeypatch, venue: _Venue) -> AppContext:
    monkeypatch.setenv("BITRADER_DATA_DIR", str(tmp_path))
    (tmp_path / "config.json").write_text(
        json.dumps({"network": Network.TESTNET.value, "market_type": MarketType.USDM.value}),
        encoding="utf-8",
    )
    ctx = AppContext()
    ctx.rest = venue
    ctx._market_rest = {market: venue for market in MarketType}
    ctx._data_rest = {market: venue for market in MarketType}
    ctx.trading = TradingService(ctx.settings, venue, ctx.risk, ctx.db, notifier=None)
    ctx.account = AccountService(
        rest_for=lambda market: venue,
        chart_rest_for=lambda market: venue,
        db=ctx.db,
        network=ctx.network,
    )
    return ctx


def _serve(monkeypatch, ctx) -> object:
    state = _State(
        ctx=ctx,
        locks={market: threading.RLock() for market in MarketType},
        account_lock=threading.RLock(),
    )
    monkeypatch.setattr(web_app, "_state", state, raising=False)
    return web_app.create_app()


def _endpoint(app, path: str, method: str = "POST"):
    for route in app.routes:
        if getattr(route, "path", "") == path and method in getattr(route, "methods", ()):
            return route.endpoint
    raise AssertionError(f"missing route {method} {path}")


def _venues(tmp_path, monkeypatch):
    venue = _Venue()
    venue._last[SPOT] = 80_000.0
    venue._last[FUTURES] = 80_000.0
    ctx = _app_context(tmp_path, monkeypatch, venue)
    return venue, ctx


def _preview(market: str, symbol: str) -> PreviewPayload:
    return PreviewPayload(
        market=market, symbol=symbol, side="buy", order_type="market", size=0.002
    )


def _order(market: str, symbol: str) -> OrderPayload:
    return OrderPayload(
        market=market, symbol=symbol, side="buy", order_type="market", size=0.002
    )


# ------------------------------------------------------------ 预览：形状不对
def test_preview_refuses_a_contract_symbol_on_spot(tmp_path, monkeypatch):
    """现货市场收到 BTC/USDT:USDT：以前 allowed: true，一路放行到下单。"""
    venue, ctx = _venues(tmp_path, monkeypatch)
    preview = _endpoint(_serve(monkeypatch, ctx), "/api/orders/preview")

    with pytest.raises(HTTPException) as caught:
        preview(_preview(MarketType.SPOT.value, FUTURES))

    assert caught.value.status_code == 400
    detail = str(caught.value.detail)
    assert "BTC/USDT:USDT" in detail and "现货要写成 BTC/USDT" in detail
    assert venue.ticker_calls == [], "形状校验必须在取价之前，不该花一次交易所请求"


def test_preview_refuses_a_dated_delivery_symbol_on_spot(tmp_path, monkeypatch):
    """交割合约多一段 -YYMMDD，同样是合约写法。"""
    _, ctx = _venues(tmp_path, monkeypatch)
    preview = _endpoint(_serve(monkeypatch, ctx), "/api/orders/preview")

    with pytest.raises(HTTPException) as caught:
        preview(_preview(MarketType.SPOT.value, DELIVERY))

    assert caught.value.status_code == 400
    assert "合约符号" in str(caught.value.detail)


def test_preview_refuses_a_bare_symbol_on_futures(tmp_path, monkeypatch):
    """合约市场收到 BTC/USDT：同样 400，并说清该写成 BTC/USDT:USDT。"""
    venue, ctx = _venues(tmp_path, monkeypatch)
    preview = _endpoint(_serve(monkeypatch, ctx), "/api/orders/preview")

    with pytest.raises(HTTPException) as caught:
        preview(_preview(MarketType.USDM.value, SPOT))

    assert caught.value.status_code == 400
    detail = str(caught.value.detail)
    assert "合约符号要带结算币" in detail and "BTC/USDT:USDT" in detail
    assert venue.ticker_calls == []


@pytest.mark.parametrize("market", [MarketType.SPOT.value, MarketType.USDM.value])
@pytest.mark.parametrize("symbol", ["BTCUSDT", "BTC/", "/USDT", "BTC/USDT/XX", ""])
def test_preview_refuses_a_malformed_symbol(tmp_path, monkeypatch, market, symbol):
    _, ctx = _venues(tmp_path, monkeypatch)
    preview = _endpoint(_serve(monkeypatch, ctx), "/api/orders/preview")

    with pytest.raises(HTTPException) as caught:
        preview(_preview(market, symbol))

    assert caught.value.status_code == 400


# ------------------------------------------------------------ 下单：形状不对
def test_placing_refuses_a_contract_symbol_on_spot(tmp_path, monkeypatch):
    venue, ctx = _venues(tmp_path, monkeypatch)
    place = _endpoint(_serve(monkeypatch, ctx), "/api/orders")

    with pytest.raises(HTTPException) as caught:
        place(_order(MarketType.SPOT.value, FUTURES))

    assert caught.value.status_code == 400
    assert "现货要写成 BTC/USDT" in str(caught.value.detail)
    assert venue.fills == [], "形状不对的单子一张都不该发出去"
    assert venue.ticker_calls == [], "下单入口的校验也要排在取价之前"


def test_placing_refuses_a_bare_symbol_on_futures(tmp_path, monkeypatch):
    venue, ctx = _venues(tmp_path, monkeypatch)
    place = _endpoint(_serve(monkeypatch, ctx), "/api/orders")

    with pytest.raises(HTTPException) as caught:
        place(_order(MarketType.USDM.value, SPOT))

    assert caught.value.status_code == 400
    assert "BTC/USDT:USDT" in str(caught.value.detail)
    assert venue.fills == []
    assert venue.ticker_calls == []


# ------------------------------------------------------- 对的写法不许跟着坏
def test_the_matching_spellings_still_pass(tmp_path, monkeypatch):
    """现货 BTC/USDT、合约 BTC/USDT:USDT 照常放行，别把正常下单一起堵了。"""
    venue, ctx = _venues(tmp_path, monkeypatch)
    app = _serve(monkeypatch, ctx)
    preview = _endpoint(app, "/api/orders/preview")

    assert preview(_preview(MarketType.SPOT.value, SPOT))["allowed"] is True
    assert preview(_preview(MarketType.USDM.value, FUTURES))["allowed"] is True


def test_surrounding_blanks_are_stripped(tmp_path, monkeypatch):
    """带空格的写法按去掉空白的那个走，别让空白一路带到交易所。"""
    _, ctx = _venues(tmp_path, monkeypatch)
    app = _serve(monkeypatch, ctx)
    preview = _endpoint(app, "/api/orders/preview")

    result = preview(_preview(MarketType.USDM.value, "  BTC/USDT:USDT  "))
    assert result["allowed"] is True
    assert result["symbol"] == FUTURES
