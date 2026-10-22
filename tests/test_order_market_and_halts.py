"""下单不该改全局设置；持仓读不到时，减仓仍然要能走。

两条都是审查里记下来的遗留问题：

* W9：``POST /api/orders`` 下单前调 ``ctx.set_market_type(mt)``，默认 ``persist=True``，
  于是一次现货下单把全局市场和 ``config.json`` 一起改掉，侧栏、图表、设置页跟着切。
* R12：``_live_held`` 读持仓抛异常时返回 0.0，``_is_reducing`` 因此认定这笔卖出不是
  减仓，熔断/急停期间的平仓被按开仓判据拒掉。
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
from btrader.web.app import OrderPayload

from fakes import FakeVenue

FUTURES = "BTC/USDT:USDT"
SPOT = "BTC/USDT"


@dataclass
class _State:
    """这些路由用到的进程级状态。"""

    ctx: object
    locks: dict
    account_lock: threading.RLock


class _Venue(FakeVenue):
    """替身交易所：补上 AppContext 切市场时要碰的两个属性，
    并能按开关让「读持仓」失败。"""

    def __init__(self) -> None:
        super().__init__()
        self.credentials = Credentials()
        self.fail_positions = False

    def reset(self) -> None:  # ccxt 客户端重建；替身没有可重建的东西
        return None

    def fetch_positions(self, market: MarketType, symbols=None):
        if self.fail_positions:
            raise RuntimeError("交易所读取持仓超时")
        return super().fetch_positions(market, symbols)


def _app_context(tmp_path, monkeypatch, venue: _Venue) -> AppContext:
    """一个真的 AppContext：config.json 落在 tmp_path，交易所全走替身。

    起手是「测试网 + 合约」，这样既不用二次确认，也不用额外切换市场。
    """
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


def _endpoint(app, path: str, method: str):
    for route in app.routes:
        if getattr(route, "path", "") == path and method in getattr(route, "methods", ()):
            return route.endpoint
    raise AssertionError(f"missing route {method} {path}")


def _saved_market(path) -> str:
    return json.loads(path.read_text(encoding="utf-8")).get("market_type", "")


def _detail(exc: HTTPException) -> str:
    return str(exc.detail)


# ------------------------------------------------- W9 下单不许改全局市场
def test_a_spot_order_leaves_the_global_market_alone(tmp_path, monkeypatch):
    """在合约页面下一张现货单：全局市场、设置、config.json 都不该跟着变。"""
    venue = _Venue()
    venue._last[SPOT] = 100.0
    ctx = _app_context(tmp_path, monkeypatch, venue)
    config_file = tmp_path / "config.json"
    assert _saved_market(config_file) == MarketType.USDM.value
    place = _endpoint(_serve(monkeypatch, ctx), "/api/orders", "POST")

    place(
        OrderPayload(
            market=MarketType.SPOT.value,
            symbol=SPOT,
            side="buy",
            order_type="market",
            size=1.0,
        )
    )

    assert ctx.market_type is MarketType.USDM
    assert ctx.settings.market_type == MarketType.USDM.value
    assert _saved_market(config_file) == MarketType.USDM.value
    # 订单本身还得落在请求的市场里，否则就是把功能一起改坏了
    assert venue.fills[-1]["market"] == MarketType.SPOT.value


def test_a_refused_order_does_not_leave_the_market_switched(tmp_path, monkeypatch):
    """被拒的单子也必须把借来的市场还回去，否则错的那一次就留在界面上了。"""
    venue = _Venue()
    venue._last[SPOT] = 100.0
    ctx = _app_context(tmp_path, monkeypatch, venue)
    ctx.settings.risk.kill_switch = True
    place = _endpoint(_serve(monkeypatch, ctx), "/api/orders", "POST")

    with pytest.raises(HTTPException) as caught:
        place(
            OrderPayload(
                market=MarketType.SPOT.value,
                symbol=SPOT,
                side="buy",
                order_type="market",
                size=1.0,
            )
        )

    assert caught.value.status_code == 400
    assert ctx.market_type is MarketType.USDM
    assert ctx.settings.market_type == MarketType.USDM.value
    assert _saved_market(tmp_path / "config.json") == MarketType.USDM.value


def _held_position(venue: _Venue, ctx: AppContext) -> None:
    """让实盘成交账本记着一条多头 0.5 的持仓（sync_live_fills 是它的入口）。"""
    venue._book[FUTURES] = 0.5
    venue._entry[FUTURES] = 100.0
    venue.fills.append(
        {
            "id": "fill-1",
            "symbol": FUTURES,
            "side": "buy",
            "amount": 0.5,
            "price": 100.0,
            "timestamp": 1_700_000_000_000,
            "fee": {"cost": 0.0, "currency": "USDT"},
        }
    )
    assert ctx.trading.sync_live_fills([MarketType.USDM]) == []
    assert ctx.trading._live_ledger()["books"][FUTURES]["contracts"] == pytest.approx(0.5)


# ------------------------------- R12 熔断期：减仓放行，开仓照旧拦住
def test_a_close_still_goes_through_a_halt_when_the_position_read_fails(tmp_path, monkeypatch):
    """熔断 + 读不到持仓：平仓要能走，否则人被困在仓位里出不来。"""
    venue = _Venue()
    venue._last[FUTURES] = 100.0
    ctx = _app_context(tmp_path, monkeypatch, venue)
    _held_position(venue, ctx)
    ctx.settings.risk.kill_switch = True  # 急停/熔断
    venue.fail_positions = True
    place = _endpoint(_serve(monkeypatch, ctx), "/api/orders", "POST")

    result = place(
        OrderPayload(
            market=MarketType.USDM.value,
            symbol=FUTURES,
            side="sell",
            order_type="market",
            size=0.5,
        )
    )

    assert result["ok"] is True
    # 走的是交易所侧 reduceOnly：不确定持仓时，只允许「确实减仓」的单子
    assert venue.fills[-1]["info"].get("reduceOnly") is True
    assert venue.fills[-1]["side"] == "sell"


def test_an_entry_is_still_refused_during_a_halt(tmp_path, monkeypatch):
    """急停期间开仓照旧被拒：读得到持仓时由风控拦，读不到时也不许开。"""
    venue = _Venue()
    venue._last[FUTURES] = 100.0
    ctx = _app_context(tmp_path, monkeypatch, venue)
    _held_position(venue, ctx)
    ctx.settings.risk.kill_switch = True
    place = _endpoint(_serve(monkeypatch, ctx), "/api/orders", "POST")
    payload = OrderPayload(
        market=MarketType.USDM.value,
        symbol=FUTURES,
        side="buy",
        order_type="market",
        size=0.5,
    )

    with pytest.raises(HTTPException) as caught:
        place(payload)
    assert caught.value.status_code == 400
    assert "急停" in _detail(caught.value)

    # 连持仓都读不到的时候，开仓同样不能过去
    venue.fail_positions = True
    with pytest.raises(HTTPException) as caught:
        place(payload)
    assert caught.value.status_code == 400
    assert venue.fills[-1]["side"] == "buy"  # 还是那条种子成交，没有新单

