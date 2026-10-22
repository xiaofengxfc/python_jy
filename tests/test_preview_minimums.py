"""下单预览要把交易所的最小下单额一起报出来。

现货实测：「卖出」不预检最小名义额，太小的单直接打到交易所才被 NOTIONAL 拒掉
（4.77 USDT 那次）。预览本来就在算交易所数量，顺手把这一对的最小名义额 / 最小数量
也读出来返回——拿不到就是 null，不编数字。

这里只**报数**，不新增闸门：真正的门仍然是交易所那一道和 `RiskManager`。
"""

from __future__ import annotations

import threading
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


@dataclass
class Box:
    app: object
    venue: FakeVenue
    db: Database


def _endpoint(app, path: str, method: str):
    for route in app.routes:
        if getattr(route, "path", "") == path and method in getattr(route, "methods", ()):
            return route.endpoint
    raise AssertionError(f"missing route {method} {path}")


def _make(tmp_path, monkeypatch, spec: ContractSpec | None) -> Box:
    venue = FakeVenue(balances={MarketType.SPOT: {"USDT": 1_000.0, "BTC": 1.0}})
    venue._last[SPOT] = PRICE
    if spec is not None:
        venue.spec_overrides = {MarketType.SPOT: spec}
    db = Database(tmp_path / "preview.db")
    settings = AppSettings(network=Network.TESTNET.value)
    ctx = SimpleNamespace(
        settings=settings,
        network=Network.TESTNET,
        market_type=MarketType.SPOT,
        venue=venue,
        trading=TradingService(
            settings=settings,
            rest=venue,
            risk=RiskManager(settings.risk),
            db=db,
            notifier=None,
        ),
        account=AccountService(
            rest_for=lambda market: venue,
            chart_rest_for=lambda market: venue,
            db=db,
            network=Network.TESTNET,
        ),
        rest_for=lambda market: venue,
        market_rest_for=lambda market: venue,
        quote_rest=lambda market: venue,
    )
    state = SimpleNamespace(
        ctx=ctx,
        locks={market: threading.RLock() for market in MarketType},
        account_lock=threading.RLock(),
    )
    monkeypatch.setattr(web_app, "_state", state, raising=False)
    return Box(app=web_app.create_app(), venue=venue, db=db)


@pytest.fixture()
def sized(tmp_path, monkeypatch) -> Box:
    """交易所公布：最少 0.0001 BTC，最少 5 USDT 名义额。"""
    spec = ContractSpec(
        contract_size=1.0,
        base="BTC",
        quote="USDT",
        settle="USDT",
        min_amount=0.0001,
        min_cost=5.0,
    )
    box = _make(tmp_path, monkeypatch, spec)
    try:
        yield box
    finally:
        box.db.close()


@pytest.fixture()
def unknown(tmp_path, monkeypatch) -> Box:
    """交易所什么都没公布：两个字段都必须 null，不许编。"""
    box = _make(tmp_path, monkeypatch, ContractSpec())
    try:
        yield box
    finally:
        box.db.close()


def _preview(box: Box, **overrides):
    endpoint = _endpoint(box.app, "/api/orders/preview", "POST")
    payload = {
        "market": "spot",
        "symbol": SPOT,
        "side": "buy",
        "order_type": "market",
        "size_mode": "quote",
        "size": 10.0,
    }
    payload.update(overrides)
    return endpoint(PreviewPayload(**payload))


def test_preview_reports_the_venue_minimums(sized: Box):
    result = _preview(sized)
    assert result["min_amount"] == pytest.approx(0.0001)
    assert result["min_notional"] == pytest.approx(5.0)
    # 报数不等于加闸门：10 USDT 这笔本身仍然照旧
    assert result["allowed"] is True


def test_preview_reports_nulls_when_the_venue_publishes_nothing(unknown: Box):
    result = _preview(unknown)
    assert result["min_amount"] is None
    assert result["min_notional"] is None
    assert result["allowed"] is True


def test_the_minimums_are_reported_even_when_the_risk_check_says_no(sized: Box):
    """被风控拒的那次预览也得带上最小额：界面正是在这时候要提示「太小了」。"""
    result = _preview(sized, size_mode="quote", size=10_000.0)
    assert result["allowed"] is False
    assert result["min_notional"] == pytest.approx(5.0)


def test_the_minimums_come_from_the_venue_spec_not_from_a_guess(sized: Box):
    """最小数量按基础币报（现货 1 张 = 1 币），不是照抄合约里的张数。"""
    result = _preview(sized)
    assert result["exchange_amount"] > 0
    assert result["min_amount"] == pytest.approx(0.0001)
