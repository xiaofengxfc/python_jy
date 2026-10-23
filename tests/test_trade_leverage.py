"""手动下单的杠杆：设置段 ``trade.leverage`` 与下单/预览的贯通。

用户原话「不能设置杠杆倍速」。后端本来就有 ``rest.set_leverage()``、
``OrderRequest.leverage`` 和 ``trading._ensure_leverage``，缺的是「记住上次用的
值」这一层——以及 ``POST /api/orders`` 那句没传 leverage 的 payload：面板填 10x，
订单请求体里根本没有这个字段，交易所始终按账户默认杠杆开仓。

这一组钉五件事：

1. ``trade.leverage`` 按市场存，只存合约市场（现货恒 1、不入库），默认全是 1；
2. 只发一个市场时另一个市场不动，写坏的数字 400 并点名字段；
3. ``POST /api/settings/reset`` 认 ``trade`` 段，重置回 1；
4. 请求体不带 leverage 时用设置里记住的那个值；
5. 超过 ``risk.max_leverage`` 按上限下单，并且**说明一声**（静默改数比报错更糟），
   现货即使传 10 也按 1 下。
"""

from __future__ import annotations

import json
import threading

import pytest
from fastapi import HTTPException

from btrader.context import AppContext
from btrader.core.config import AppSettings, ConfigStore, RiskSettings
from btrader.core.storage import Database
from btrader.core.types import MarketType, Network
from btrader.engine.account import AccountService
from btrader.engine.trading import TradingService
from btrader.web.app import OrderPayload, PreviewPayload
from btrader.web.routes import (
    SettingsPayload,
    apply_settings,
    reset_settings,
    resolve_order_leverage,
)

import btrader.web.app as web_app

from fakes import FakeVenue

SPOT = "BTC/USDT"
USDM = "BTC/USDT:USDT"
COINM = "BTC/USD:BTC"


# ------------------------------------------------------------------ settings
class _Ctx:
    """真的落盘的上下文：save_settings 走 ConfigStore，重读一遍就是「重启」。"""

    def __init__(self, store: ConfigStore) -> None:
        self.store = store
        self.settings = store.settings
        self.platform = "binance"
        self.network = Network(store.settings.network)
        self.market_type = MarketType(store.settings.market_type)
        self.saved = 0

    def save_settings(self) -> None:
        self.saved += 1
        self.store.save()

    def apply_proxy(self, proxy: str) -> None:
        self.settings.proxy = proxy.strip()

    def set_network(self, network: Network, *, persist: bool = True) -> None:
        self.network = network
        self.settings.network = network.value

    def set_market_type(self, market: MarketType, *, persist: bool = True) -> None:
        self.market_type = market
        self.settings.market_type = market.value

    def set_platform(self, platform: str, *, persist: bool = True) -> None:
        self.platform = platform
        self.settings.platform = platform


def _ctx(tmp_path) -> tuple[_Ctx, "object"]:
    path = tmp_path / "config.json"
    return _Ctx(ConfigStore(path)), path


def _saved_trade(path) -> dict:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return ((raw.get("trade") or {}).get("leverage")) or {}


def test_every_futures_market_starts_at_one(tmp_path):
    """默认各市场 1 倍：和「根本没有这一层」时的行为逐位一样。"""
    ctx, _ = _ctx(tmp_path)
    assert ctx.settings.trade.leverage == {"usdm": 1, "coinm": 1}


def test_writing_one_market_leaves_the_other_alone(tmp_path):
    """只发 usdm 时 coinm 不动；反过来也一样。按市场合并，不是整段覆盖。"""
    ctx, path = _ctx(tmp_path)

    apply_settings(ctx, SettingsPayload(trade={"leverage": {"usdm": 10}}))

    assert ctx.settings.trade.leverage == {"usdm": 10, "coinm": 1}
    assert _saved_trade(path) == {"usdm": 10, "coinm": 1}

    apply_settings(ctx, SettingsPayload(trade={"leverage": {"coinm": 5}}))

    assert ConfigStore(path).settings.trade.leverage == {"usdm": 10, "coinm": 5}


@pytest.mark.parametrize(
    "value, hint",
    [
        (0, "1–125"),
        (126, "1–125"),
        (1.5, "整数"),
        ("nan", "有限数字"),
    ],
)
def test_a_bad_leverage_is_refused_and_names_the_market(tmp_path, value, hint):
    """0 / 126 / 1.5 / "nan" 各自 400，文案要能看出是哪个市场的哪一项。"""
    ctx, path = _ctx(tmp_path)

    with pytest.raises(HTTPException) as caught:
        apply_settings(ctx, SettingsPayload(trade={"leverage": {"usdm": value}}))

    assert caught.value.status_code == 400
    assert "trade.leverage.usdm" in caught.value.detail, caught.value.detail
    assert hint in caught.value.detail, caught.value.detail
    # 一个都没写进去，盘上也没有 trade 段。
    assert ctx.settings.trade.leverage == {"usdm": 1, "coinm": 1}
    assert _saved_trade(path) == {}


def test_spot_has_no_leverage_to_set(tmp_path):
    """现货恒 1 倍。收一个 "spot" 键等于骗人：存下来界面上就会显示一个假的杠杆。"""
    ctx, _ = _ctx(tmp_path)

    with pytest.raises(HTTPException) as caught:
        apply_settings(ctx, SettingsPayload(trade={"leverage": {"spot": 10}}))

    assert caught.value.status_code == 400
    assert "spot" in caught.value.detail and "现货" in caught.value.detail


def test_reset_puts_every_market_back_to_one(tmp_path):
    """恢复默认认 trade 段，而且只动这一段。"""
    ctx, path = _ctx(tmp_path)
    ctx.settings.risk.max_leverage = 7.0
    apply_settings(ctx, SettingsPayload(trade={"leverage": {"usdm": 20, "coinm": 3}}))
    assert ctx.settings.trade.leverage == {"usdm": 20, "coinm": 3}

    reset_settings(ctx, "trade")

    assert ctx.settings.trade.leverage == {"usdm": 1, "coinm": 1}
    assert ConfigStore(path).settings.trade.leverage == {"usdm": 1, "coinm": 1}
    # 风控上限不是「杠杆设置」，重置 trade 不该碰它。
    assert ConfigStore(path).settings.risk.max_leverage == pytest.approx(7.0)


def test_a_hand_written_leverage_is_cleaned_on_load(tmp_path):
    """手改 config.json 写进 0 / 999 / spot：读盘退回 1，整份配置照样打得开。"""
    path = tmp_path / "config.json"
    path.write_text(
        '{"trade": {"leverage": {"usdm": 0, "coinm": 999, "spot": 3, "okx": 4}},'
        ' "theme": "dark"}',
        encoding="utf-8",
    )

    settings = ConfigStore(path).settings

    assert settings.trade.leverage == {"usdm": 1, "coinm": 1}
    assert settings.theme == "dark"
    # 读出来还能原样存回去（存的是干净的那一份）。
    store = ConfigStore(path)
    store.save()
    assert json.loads(path.read_text(encoding="utf-8"))["trade"]["leverage"] == {
        "usdm": 1,
        "coinm": 1,
    }


# ------------------------------------------------------------- 下单与预览的贯通
class _State:
    """下单路由用到的进程级状态，和 app.py 一样共用交易服务那把账户锁。"""

    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.locks = {market: threading.RLock() for market in MarketType}
        self.loading = {market: threading.Lock() for market in MarketType}
        self.symbols: dict = {}
        self.account_lock = ctx.trading.account_lock
        self.equity_history: list = []
        self.equity_stamp = 0.0
        self.stream = None
        self.stop_reconcile = threading.Event()


def _open_caps(risk: RiskSettings) -> None:
    """这些用例关心的是杠杆，不是仓位限额：把金额类上限关掉。"""
    risk.max_order_quote = 0.0
    risk.max_order_pct = 0.0
    risk.max_position_quote = 0.0
    risk.max_daily_loss = 0.0
    risk.max_daily_trades = 0
    risk.max_consecutive_losses = 0
    risk.require_confirmation = False


def _context(tmp_path, monkeypatch, venue: FakeVenue) -> AppContext:
    monkeypatch.setenv("BITRADER_DATA_DIR", str(tmp_path))
    (tmp_path / "config.json").write_text(
        json.dumps({"network": "testnet", "market_type": MarketType.USDM.value}),
        encoding="utf-8",
    )
    ctx = AppContext()
    ctx.rest = venue
    ctx._market_rest = {market: venue for market in MarketType}
    ctx._data_rest = {market: venue for market in MarketType}
    ctx.trading = TradingService(
        ctx.settings, venue, ctx.risk, ctx.db, notifier=None, rest_for=ctx.rest_for
    )
    ctx.account = AccountService(
        rest_for=lambda market: venue,
        chart_rest_for=lambda market: venue,
        db=ctx.db,
        network=ctx.network,
    )
    _open_caps(ctx.settings.risk)
    return ctx


def _endpoint(container, path: str, method: str):
    for route in container.routes:
        if getattr(route, "path", "") == path and method in getattr(route, "methods", ()):
            return route.endpoint
    raise AssertionError(f"missing route {method} {path}")


def _served(tmp_path, monkeypatch, *, max_leverage: float = 20.0, saved: dict | None = None):
    """一只替身交易所 + 一个可以直调的下单接口。"""
    venue = FakeVenue()
    venue._last[USDM] = 80_000.0
    venue._last[COINM] = 80_000.0
    venue._last[SPOT] = 80_000.0
    ctx = _context(tmp_path, monkeypatch, venue)
    ctx.settings.risk.max_leverage = max_leverage
    ctx.settings.trade.leverage.update(saved or {})
    monkeypatch.setattr(web_app, "_state", _State(ctx), raising=False)
    app = web_app.create_app()
    return venue, app


def test_an_order_without_leverage_uses_the_saved_value(tmp_path, monkeypatch):
    """面板上记住 7 倍，请求体不带 leverage：必须按 7 倍发给交易所。"""
    venue, app = _served(tmp_path, monkeypatch, saved={"usdm": 7})
    place = _endpoint(app, "/api/orders", "POST")

    result = place(
        OrderPayload(
            market="usdm", symbol=USDM, side="buy", order_type="market", size=0.002
        )
    )

    assert result["leverage"] == 7
    assert result["leverage_note"] == ""
    assert venue.leverage_calls == [(USDM, 7, MarketType.USDM)]
    assert isinstance(venue.leverage_calls[0][1], int), "交易所只认整数杠杆"


def test_a_leverage_above_the_cap_is_capped_and_explained(tmp_path, monkeypatch):
    """超过 risk.max_leverage 按上限下单，但要在响应里说清收口了。"""
    venue, app = _served(tmp_path, monkeypatch, max_leverage=5.0)
    place = _endpoint(app, "/api/orders", "POST")

    result = place(
        OrderPayload(
            market="usdm", symbol=USDM, side="buy", order_type="market",
            size=0.002, leverage=20.0,
        )
    )

    assert result["leverage"] == 5
    assert venue.leverage_calls == [(USDM, 5, MarketType.USDM)]
    assert "上限" in result["leverage_note"], result["leverage_note"]
    assert "20" in result["leverage_note"] and "5" in result["leverage_note"]


def test_a_spot_order_takes_one_whatever_is_sent(tmp_path, monkeypatch):
    """现货没有杠杆：传 10 也按 1 下，而且不去调交易所的杠杆接口。"""
    venue, app = _served(tmp_path, monkeypatch)
    place = _endpoint(app, "/api/orders", "POST")

    result = place(
        OrderPayload(
            market="spot", symbol=SPOT, side="buy", order_type="market",
            size_mode="quote", size=100.0, leverage=10.0,
        )
    )

    assert result["leverage"] == 1
    assert venue.leverage_calls == []
    assert "现货" in result["leverage_note"], result["leverage_note"]
    assert venue.fills, "单子本身要照常发出去"


def test_a_preview_uses_the_saved_value_when_the_body_has_none(tmp_path, monkeypatch):
    """预览也要按同一套口径报倍数，界面上那行提示才不是假的。"""
    venue, app = _served(tmp_path, monkeypatch, saved={"usdm": 7})
    preview = _endpoint(app, "/api/orders/preview", "POST")

    result = preview(
        PreviewPayload(
            market="usdm", symbol=USDM, side="buy", order_type="limit",
            size=0.002, price=80_000.0,
        )
    )

    assert result["leverage"] == 7
    assert result["leverage_note"] == ""
    assert venue.fills == [], "预览不发单"


def test_a_preview_reports_the_cap_instead_of_the_request(tmp_path, monkeypatch):
    venue, app = _served(tmp_path, monkeypatch, max_leverage=5.0)
    preview = _endpoint(app, "/api/orders/preview", "POST")

    result = preview(
        PreviewPayload(
            market="usdm", symbol=USDM, side="buy", order_type="limit",
            size=0.002, price=80_000.0, leverage=20.0,
        )
    )

    assert result["leverage"] == 5
    assert "上限" in result["leverage_note"]


def test_a_spot_preview_says_one(tmp_path, monkeypatch):
    venue, app = _served(tmp_path, monkeypatch)
    preview = _endpoint(app, "/api/orders/preview", "POST")

    result = preview(
        PreviewPayload(
            market="spot", symbol=SPOT, side="buy", order_type="limit",
            size_mode="quote", size=100.0, price=80_000.0, leverage=10.0,
        )
    )

    assert result["leverage"] == 1
    assert "现货" in result["leverage_note"]
    assert venue.leverage_calls == []


def test_the_leveraged_cap_is_the_smaller_of_the_two(tmp_path):
    """``resolve_order_leverage`` 的口径：min(请求, 上限)，显式的请求压过记下的值。"""
    settings = AppSettings()
    settings.trade.leverage["usdm"] = 8
    settings.risk.max_leverage = 12.0

    assert resolve_order_leverage(settings, MarketType.USDM, None)[0] == 8
    assert resolve_order_leverage(settings, MarketType.USDM, 10.0)[0] == 10
    effective, note = resolve_order_leverage(settings, MarketType.USDM, 30.0)
    assert effective == 12
    assert "上限" in note
    # 现货恒 1，无论谁说什么。
    assert resolve_order_leverage(settings, MarketType.SPOT, 10.0)[0] == 1


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_a_non_finite_leverage_is_refused(tmp_path, value):
    """pydantic 是把 NaN 当浮点收下来的：不拦就会写进 OrderRequest。"""
    settings = AppSettings()

    with pytest.raises(HTTPException) as caught:
        resolve_order_leverage(settings, MarketType.USDM, value)

    assert caught.value.status_code == 400
    assert "leverage" in caught.value.detail


def test_a_leverage_below_one_is_refused(tmp_path):
    settings = AppSettings()

    with pytest.raises(HTTPException) as caught:
        resolve_order_leverage(settings, MarketType.USDM, 0.0)

    assert caught.value.status_code == 400
    assert "1" in caught.value.detail
