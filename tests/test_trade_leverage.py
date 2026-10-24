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
from btrader.core.credentials import Credentials
from btrader.engine.account import AccountService
from btrader.engine.trading import TradingService
from btrader.exchange.rest import BinanceREST, ExchangeError
from btrader.web.app import ConditionalPayload, OrderPayload, PreviewPayload
from btrader.web.routes import (
    AITraderController,
    AITraderStartPayload,
    SettingsPayload,
    apply_settings,
    reset_settings,
    resolve_engine_leverage,
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


def test_a_boolean_leverage_is_refused_instead_of_becoming_one(tmp_path, monkeypatch):
    """leverage: true 以前被 pydantic 变成 1.0 静默放行：true / false 都不是倍数。"""
    venue, app = _served(tmp_path, monkeypatch, saved={"usdm": 3})
    place = _endpoint(app, "/api/orders", "POST")

    for value in (True, False):
        with pytest.raises(HTTPException) as caught:
            place(
                OrderPayload(
                    market="usdm", symbol=USDM, side="buy", order_type="market",
                    size=0.002, leverage=value,
                )
            )
        assert caught.value.status_code == 400
        assert "倍数" in str(caught.value.detail), caught.value.detail
    assert venue.fills == [], "被拒的请求不该发到交易所"
    assert venue.leverage_calls == []


def test_a_boolean_leverage_is_refused_on_the_preview_too(tmp_path, monkeypatch):
    venue, app = _served(tmp_path, monkeypatch)
    preview = _endpoint(app, "/api/orders/preview", "POST")

    with pytest.raises(HTTPException) as caught:
        preview(
            PreviewPayload(
                market="usdm", symbol=USDM, side="buy", order_type="limit",
                size=0.002, price=80_000.0, leverage=True,
            )
        )
    assert caught.value.status_code == 400
    assert "倍数" in str(caught.value.detail)


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


# ------------------------------------------- 引擎（执行台 / AI 交易）与条件单
def test_an_engine_leverage_is_capped_instead_of_rejected():
    """执行台 / AI 交易的超限杠杆按上限跑，不是每根 K 线被风控拒一次。

    以前两条引擎路径把配置里的杠杆原样塞进 AutoTraderConfig / AITraderConfig，
    risk.check_order 每次开仓都拒一次「杠杆 20x 超过上限 10x」：界面显示在跑，
    一笔都不成交，而同一屏的下单框填 20 会按 10 下单。
    """
    settings = AppSettings()
    settings.risk.max_leverage = 10.0

    effective, note = resolve_engine_leverage(settings, MarketType.USDM, 20.0)
    assert effective == 10.0
    assert float(effective).is_integer(), "交易所只认整数杠杆"
    assert "上限" in note and "20" in note and "10" in note

    effective, note = resolve_engine_leverage(settings, MarketType.USDM, 5.0)
    assert effective == 5.0
    assert note == ""


def test_an_engine_leverage_on_spot_is_one():
    """现货恒 1：配置里写 10 也按 1 跑，而且不用解释（用户没得选）。"""
    settings = AppSettings()

    assert resolve_engine_leverage(settings, MarketType.SPOT, 10.0) == (1.0, "")


@pytest.mark.parametrize("value", [0.0, 2.5, True])
def test_a_broken_engine_leverage_is_refused(value):
    """0 / 1.5 / true 都不是倍数：引擎启动时当场 400，不带着坏值跑。"""
    settings = AppSettings()

    with pytest.raises(HTTPException) as caught:
        resolve_engine_leverage(settings, MarketType.USDM, value)

    assert caught.value.status_code == 400


def test_ai_auto_leverage_must_be_a_whole_multiple(tmp_path):
    """ai_auto.leverage 以前只挡「不能为负」：0 存下来等于静默不推杠杆。"""
    ctx, _ = _ctx(tmp_path)

    for bad in (0, 1.5, 126):
        with pytest.raises(HTTPException) as caught:
            apply_settings(ctx, SettingsPayload(ai_auto={"leverage": bad}))
        assert caught.value.status_code == 400
        assert "leverage" in caught.value.detail, caught.value.detail
    assert ctx.settings.ai_auto.leverage == 1.0

    apply_settings(ctx, SettingsPayload(ai_auto={"leverage": 10}))
    assert ctx.settings.ai_auto.leverage == pytest.approx(10.0)


def test_exec_leverage_must_be_a_whole_multiple(tmp_path):
    """执行台那一项同理：2.5 只会在下单那一刻才报「只能是整数」。"""
    ctx, _ = _ctx(tmp_path)

    for bad in (2.5, 130):
        with pytest.raises(HTTPException) as caught:
            apply_settings(ctx, SettingsPayload(exec={"leverage": bad}))
        assert caught.value.status_code == 400
        assert "exec.leverage" in caught.value.detail, caught.value.detail
    assert ctx.settings.exec.leverage == 1.0


def test_ai_start_carries_the_leverage_into_the_config(tmp_path, monkeypatch):
    """AI 交易的杠杆：请求体的值收口后进 AITraderConfig，并在 status 里回显。

    以前 AITraderStartPayload 根本没有 leverage 字段，_build 只读 ai_auto.leverage，
    而设置页没有任何 ai_auto 的控件——AI 交易的倍数除了手改 config.json 改不了。
    """
    venue, app = _served(tmp_path, monkeypatch, max_leverage=10.0)
    state = web_app._state
    state.ctx.settings.ai.enabled = True
    controller = AITraderController()

    trader = controller._build(
        state,
        AITraderStartPayload(
            market="usdm",
            symbol=USDM,
            timeframe="15m",
            strategy=state.ctx.strategies.keys[0],
            leverage=20,
        ),
    )

    assert trader.config.leverage == 10.0
    assert "上限" in trader.config.leverage_note
    reported = trader.status()["config"]
    assert reported["leverage"] == 10.0
    assert reported["leverage_note"] == trader.config.leverage_note


def test_ai_start_falls_back_to_the_saved_leverage(tmp_path, monkeypatch):
    """请求体不带 leverage 就用 ai_auto 里那个值（与其它字段同一套优先级）。"""
    venue, app = _served(tmp_path, monkeypatch, max_leverage=10.0)
    state = web_app._state
    state.ctx.settings.ai.enabled = True
    state.ctx.settings.ai_auto.leverage = 4.0
    controller = AITraderController()

    trader = controller._build(
        state,
        AITraderStartPayload(
            market="usdm",
            symbol=USDM,
            timeframe="15m",
            strategy=state.ctx.strategies.keys[0],
        ),
    )

    assert trader.config.leverage == 4.0


def test_a_conditional_order_pushes_the_leverage_it_shows(tmp_path, monkeypatch):
    """条件单也是真委托：面板显示 7 倍，交易所侧必须先对齐到 7 倍再挂触发器。

    以前 ConditionalPayload 没有 leverage 字段，pydantic 把界面发来的值静默丢掉，
    路由也不调 set_leverage——挂上去的仓位按账户当时的杠杆开，面板还写着「同样按
    这里的倍数开」。
    """
    venue, app = _served(tmp_path, monkeypatch, saved={"usdm": 7})
    place = _endpoint(app, "/api/orders/conditional", "POST")

    result = place(
        ConditionalPayload(
            market="usdm",
            symbol=USDM,
            side="buy",
            size_mode="amount",
            size=0.002,
            trigger=79_000.0,
        )
    )

    assert venue.leverage_calls == [(USDM, 7, MarketType.USDM)]
    assert venue.conditional_orders, "触发器要真的挂出去"
    assert result["leverage"] == 7
    assert result["leverage_note"] == ""


def test_a_conditional_order_reports_the_cap(tmp_path, monkeypatch):
    """条件单超上限同样收口并说明，和 /api/orders 一个口径。"""
    venue, app = _served(tmp_path, monkeypatch, max_leverage=5.0)
    place = _endpoint(app, "/api/orders/conditional", "POST")

    result = place(
        ConditionalPayload(
            market="usdm",
            symbol=USDM,
            side="buy",
            size_mode="amount",
            size=0.002,
            trigger=79_000.0,
            leverage=20.0,
        )
    )

    assert result["leverage"] == 5
    assert venue.leverage_calls == [(USDM, 5, MarketType.USDM)]
    assert "上限" in result["leverage_note"]


def test_a_reduce_only_conditional_order_leaves_the_leverage_alone(tmp_path, monkeypatch):
    """平仓用的条件单不动杠杆（和 submit 一样）：改杠杆会改掉已有仓位的保证金。"""
    venue, app = _served(tmp_path, monkeypatch, saved={"usdm": 7})
    place = _endpoint(app, "/api/orders/conditional", "POST")

    place(
        ConditionalPayload(
            market="usdm",
            symbol=USDM,
            side="sell",
            size_mode="amount",
            size=0.002,
            trigger=79_000.0,
            reduce_only=True,
        )
    )

    assert venue.leverage_calls == []

# ------------------------------------------------------- 交易所侧的杠杆调用
class _LeverageClient:
    """只实现 set_leverage 的假 ccxt 客户端：用来钉重试与「不支持就说出来」。"""

    def __init__(self, failures: int = 0, supports: bool = True) -> None:
        self.has = {"setLeverage": True} if supports else {}
        self.failures = failures
        self.calls = 0

    def set_leverage(self, leverage, symbol, params=None):
        self.calls += 1
        if self.calls <= self.failures:
            import ccxt

            raise ccxt.NetworkError("Connection reset by peer")
        return {"symbol": symbol, "leverage": leverage}


def _rest_with(client) -> BinanceREST:
    rest = BinanceREST(Network.TESTNET, Credentials(api_key="k", api_secret="s"))
    rest.client = lambda market: client  # type: ignore[assignment]
    rest.LEVERAGE_RETRY_DELAY = 0
    return rest


def test_set_leverage_retries_once_on_a_network_blip():
    """网络抖动重试一次：一次抖动就让整张合约单 400，等于把用户挡在门外。"""
    client = _LeverageClient(failures=1)
    rest = _rest_with(client)

    assert rest.set_leverage(USDM, 10, MarketType.USDM)["leverage"] == 10
    assert client.calls == 2


def test_set_leverage_gives_up_after_one_retry():
    """只重试一次：一直失败就老实报错，不无限重试把下单卡住。"""
    client = _LeverageClient(failures=5)
    rest = _rest_with(client)

    with pytest.raises(ExchangeError) as caught:
        rest.set_leverage(USDM, 10, MarketType.USDM)

    assert client.calls == 2
    assert "网络错误" in str(caught.value), str(caught.value)


def test_set_leverage_does_not_pretend_when_unsupported():
    """客户端没有 setLeverage 时以前安静返回 {}：面板写着 10 倍，交易所没收到。"""
    rest = _rest_with(_LeverageClient(supports=False))

    with pytest.raises(ExchangeError) as caught:
        rest.set_leverage(USDM, 10, MarketType.USDM)

    assert "不支持设置杠杆" in str(caught.value), str(caught.value)


# ------------------------------------------------------------ 风控里的杠杆上限
@pytest.mark.parametrize("value", [0, 0.5, 126, -3])
def test_the_leverage_cap_must_be_a_sane_multiple(tmp_path, value):
    """risk.max_leverage 存成 0 会让每张合约单被拒，存成 126 是交易所不收。"""
    ctx, _ = _ctx(tmp_path)

    with pytest.raises(HTTPException) as caught:
        apply_settings(ctx, SettingsPayload(risk={"max_leverage": value}))

    assert caught.value.status_code == 400
    assert "max_leverage" in caught.value.detail, caught.value.detail
    assert ctx.settings.risk.max_leverage == pytest.approx(10.0)


def test_the_leverage_cap_can_be_raised_within_range(tmp_path):
    ctx, _ = _ctx(tmp_path)

    apply_settings(ctx, SettingsPayload(risk={"max_leverage": 25}))
    assert ctx.settings.risk.max_leverage == pytest.approx(25.0)


def test_other_risk_fields_still_save_with_a_zero_cap_on_disk(tmp_path):
    """老配置里 max_leverage 是 0 时，改别的风控项不该被这条校验连坐。"""
    ctx, _ = _ctx(tmp_path)
    ctx.settings.risk.max_leverage = 0.0

    apply_settings(ctx, SettingsPayload(risk={"max_daily_trades": 12}))

    assert ctx.settings.risk.max_daily_trades == 12


