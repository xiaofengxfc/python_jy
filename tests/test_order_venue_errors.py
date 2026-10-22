"""交易所拒单与参数边界：一律 400 加中文，不许 500、不许静默改成别的东西。

这一轮实测（t33）在测试网上撞到的三件事：

* 杠杆按浮点发出去，币安回 -1102，于是每张合约单都是「设置杠杆 1x 失败」；
* 账户开双向持仓时币安回 -4061，原文是英文，被打成 500；
* 限价单不填价格会按现价挂出去，数量为 0/负数先被 pydantic 拦成 422 英文。
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from btrader.core.config import AppSettings, RiskSettings
from btrader.core.storage import Database
from btrader.core.types import MarketType, OrderSide, OrderType
from btrader.engine.risk import RiskManager
from btrader.engine.trading import OrderRejected, OrderRequest, TradingService
from btrader.exchange.rest import ExchangeError

from fakes import FakeVenue

import btrader.web.app as web_app
from btrader.web.app import OrderPayload

FUT = "BTC/USDT:USDT"

LEVERAGE_1102 = (
    'binanceusdm {"code":-1102,"msg":"Mandatory parameter leverage was not '
    'sent, was empty/null, or malformed."}'
)
HEDGE_4061 = (
    'binanceusdm {"code":-4061,"msg":"Order position side does not match user '
    'setting."}'
)
MARGIN_2019 = 'binanceusdm {"code":-2019,"msg":"Margin is insufficient."}'


class StrictVenue(FakeVenue):
    """币安要求杠杆是整数：ccxt 把 1.0 发成 "1.0"，交易所回 -1102。"""

    def set_leverage(self, symbol, leverage, market):
        if not isinstance(leverage, int):
            raise ExchangeError(LEVERAGE_1102)
        return super().set_leverage(symbol, leverage, market)


class RejectingVenue(FakeVenue):
    """下单必被交易所拒掉，用来验证拒单文案。"""

    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

    def create_order(self, market, symbol, order_type, side, amount, price=None, params=None):
        raise ExchangeError(self.message)


def _risk() -> RiskSettings:
    return RiskSettings(
        max_order_quote=0,
        max_order_pct=0,
        max_position_quote=0,
        max_leverage=20,
        max_daily_trades=0,
        max_daily_loss=0,
        max_consecutive_losses=0,
        require_confirmation=False,
    )


def _trading(tmp_path, venue, name: str = "v.db"):
    settings = AppSettings(risk=_risk())
    db = Database(tmp_path / name)
    return TradingService(settings, venue, RiskManager(settings.risk), db), db


def _entry(leverage: float = 1.0) -> OrderRequest:
    return OrderRequest(
        symbol=FUT,
        side=OrderSide.BUY.value,
        order_type=OrderType.MARKET.value,
        amount=0.002,
        price=80_000.0,
        leverage=leverage,
        market_type=MarketType.USDM,
    )


# --------------------------------------------------------------------- 杠杆
def test_the_venue_leverage_is_sent_as_an_integer(tmp_path):
    """1.0 要按整数 1 发给交易所，否则币安 -1102，每张合约单都下不出去。"""
    venue = StrictVenue()
    venue._last[FUT] = 80_000.0
    trading, db = _trading(tmp_path, venue)
    try:
        trading.submit(_entry(1.0))
    finally:
        db.close()

    assert venue.leverage_calls == [(FUT, 1, MarketType.USDM)]
    assert isinstance(venue.leverage_calls[0][1], int)
    assert venue.fills, "杠杆设成功之后单子应该真的发出去"


def test_a_fractional_leverage_is_refused_in_chinese(tmp_path):
    venue = StrictVenue()
    venue._last[FUT] = 80_000.0
    trading, db = _trading(tmp_path, venue)
    try:
        with pytest.raises(OrderRejected) as err:
            trading.submit(_entry(2.5))
    finally:
        db.close()

    assert "整数" in str(err.value)
    assert venue.fills == []


# ------------------------------------------------------------- 交易所拒单文案
def test_hedge_mode_rejection_becomes_actionable_chinese(tmp_path):
    """-4061（双向持仓）以前原样 500 抛出去，用户只能看见一段英文错误码。"""
    venue = RejectingVenue(HEDGE_4061)
    venue._last[FUT] = 80_000.0
    trading, db = _trading(tmp_path, venue)
    try:
        with pytest.raises(OrderRejected) as err:
            trading.submit(_entry(1.0))
    finally:
        db.close()

    message = str(err.value)
    assert "双向持仓" in message and "单向持仓" in message
    assert "-4061" not in message


def test_other_venue_rejections_are_prefixed_in_chinese(tmp_path):
    venue = RejectingVenue(MARGIN_2019)
    venue._last[FUT] = 80_000.0
    trading, db = _trading(tmp_path, venue)
    try:
        with pytest.raises(OrderRejected) as err:
            trading.submit(_entry(1.0))
    finally:
        db.close()

    assert str(err.value).startswith("交易所拒绝下单：")


# --------------------------------------------------------------- 参数边界
def _endpoint(path: str, method: str = "POST"):
    app = web_app.create_app()
    for route in app.routes:
        if getattr(route, "path", "") == path and method in getattr(route, "methods", ()):
            return route.endpoint
    raise AssertionError(f"missing route {method} {path}")


def test_a_limit_order_without_a_price_is_refused():
    """不填价格以前会按现价挂出去——用户没同意过那个价，可能立刻成交。"""
    place = _endpoint("/api/orders")
    with pytest.raises(HTTPException) as err:
        place(
            OrderPayload(
                market="usdm",
                symbol=FUT,
                side="buy",
                order_type="limit",
                size=0.002,
                price=None,
            )
        )
    assert err.value.status_code == 400
    assert "限价单必须填写价格" in str(err.value.detail)


@pytest.mark.parametrize("size", [0.0, -1.0])
def test_a_non_positive_size_is_a_400_in_chinese(size):
    """数量 0 / 负数以前是 pydantic 的 422 英文，界面没有中文可显示。"""
    place = _endpoint("/api/orders")
    with pytest.raises(HTTPException) as err:
        place(
            OrderPayload(
                market="usdm",
                symbol=FUT,
                side="buy",
                order_type="market",
                size=size,
            )
        )
    assert err.value.status_code == 400
    assert "下单数量必须大于 0" in str(err.value.detail)
