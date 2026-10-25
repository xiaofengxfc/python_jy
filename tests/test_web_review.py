"""Web 后端审查中确认的几处问题的回归测试。

每一条都对应 :mod:`btrader.web.app` / :mod:`btrader.web.routes` /
:mod:`btrader.core.config` / :mod:`btrader.web.stream` 里一个具体的坑：
交易所报错被 NameError 吃掉、配置块类型不对就起不来、保存被拒却留下半改的内存、
null 被写成字符串 "None"、死掉的行情 watcher 挡住后续订阅。
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from dataclasses import dataclass

import pytest
from fastapi import HTTPException

from btrader.core.config import AppSettings, ConfigError, ConfigStore
from btrader.core.storage import Database
from btrader.core.types import MarketType, Network
from btrader.engine.account import AccountService
from btrader.engine.risk import RiskManager
from btrader.engine.trading import TradingService
from btrader.exchange.rest import ExchangeError
from btrader.web import app as web_app
from btrader.web.app import ConditionalPayload
from btrader.web.routes import SettingsPayload, apply_settings
from btrader.web.stream import StreamHub

from fakes import FakeVenue

SYMBOL = "BTC/USDT:USDT"


# ------------------------------------------------ 交易所报错不能变成 500
@dataclass
class _State:
    """这些路由用到的进程级状态。"""

    ctx: object
    locks: dict
    account_lock: threading.RLock


class _Ctx:
    def __init__(self, venue: FakeVenue, db: Database) -> None:
        settings = AppSettings(network=Network.TESTNET.value)
        self.settings = settings
        self.network = Network.TESTNET
        self.market_type = MarketType.USDM
        self.venue = venue
        self.trading = TradingService(
            settings=settings,
            rest=venue,
            risk=RiskManager(settings.risk),
            db=db,
            notifier=None,
        )
        self.account = AccountService(
            rest_for=lambda market: venue,
            chart_rest_for=lambda market: venue,
            db=db,
            network=Network.TESTNET,
        )

    def rest_for(self, market: MarketType):
        return self.venue

    def quote_rest(self, market: MarketType):
        return self.venue


class _RefusingVenue(FakeVenue):
    """交易所对下单/撤单回了一句拒绝。"""

    def create_conditional(self, *args, **kwargs):
        raise ExchangeError("触发价不在允许范围内")

    def cancel_protective_orders(self, market, symbol):
        raise ExchangeError("撤单失败")


def _endpoint(app, path: str, method: str):
    for route in app.routes:
        if getattr(route, "path", "") == path and method in getattr(route, "methods", ()):
            return route.endpoint
    raise AssertionError(f"missing route {method} {path}")


@pytest.fixture()
def refusing(tmp_path, monkeypatch):
    venue = _RefusingVenue()
    venue._last[SYMBOL] = 100.0
    db = Database(tmp_path / "web.db")
    ctx = _Ctx(venue, db)
    state = _State(
        ctx=ctx,
        locks={market: threading.RLock() for market in MarketType},
        account_lock=threading.RLock(),
    )
    monkeypatch.setattr(web_app, "_state", state, raising=False)
    try:
        yield web_app.create_app()
    finally:
        db.close()


def test_a_rejected_conditional_order_reports_the_exchange_error(refusing):
    """交易所拒绝条件单时要回 400 加原因，不是 NameError 变成的 500。"""
    endpoint = _endpoint(refusing, "/api/orders/conditional", "POST")

    with pytest.raises(HTTPException) as caught:
        endpoint(
            ConditionalPayload(
                market="usdm", symbol=SYMBOL, side="buy", size=0.5, trigger=110.0
            )
        )

    assert caught.value.status_code == 400
    assert "触发价不在允许范围内" in str(caught.value.detail)


def test_a_failed_protection_cancel_reports_the_exchange_error(refusing):
    """撤保护单失败同样回 400：原来的 except 因为没导入而永远打不中。"""
    endpoint = _endpoint(refusing, "/api/positions/protection", "DELETE")

    with pytest.raises(HTTPException) as caught:
        endpoint(symbol=SYMBOL)

    assert caught.value.status_code == 400
    assert "撤单失败" in str(caught.value.detail)


# ------------------------------------------------------ 配置块类型不对
def test_a_config_block_of_the_wrong_type_does_not_stop_the_app(tmp_path):
    """手改 config.json 写成 ``"ai": true``：坏的那块按默认值，别的照读。"""
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"network": "testnet", "ai": True, "risk": ["a"], "theme": "dark"}),
        encoding="utf-8",
    )

    store = ConfigStore(path)

    assert store.settings.network == "testnet"
    assert store.settings.theme == "dark"
    assert store.settings.ai == AppSettings().ai
    assert store.settings.risk == AppSettings().risk


# -------------------------------------------- 保存被拒时不许留半改的内存
class _SavingCtx:
    """只实现 apply_settings 用到的那几个方法。"""

    def __init__(self, refuse: bool) -> None:
        self.settings = AppSettings()
        self.platform = "binance"
        self.network = Network.MAINNET
        self.market_type = MarketType.SPOT
        self.refuse = refuse
        self.proxied: list[str] = []

    def save_settings(self) -> None:
        if self.refuse:
            raise ConfigError("配置文件读不出来，这次没有保存")

    def apply_proxy(self, proxy: str) -> None:
        self.proxied.append(proxy)
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


def test_a_refused_save_leaves_memory_untouched() -> None:
    """配置文件坏了拒绝保存时，网络/市场/字段都要退回原样。"""
    ctx = _SavingCtx(refuse=True)
    ctx.settings.ai.model = "deepseek-v4.1-flash"

    with pytest.raises(ConfigError):
        apply_settings(
            ctx,
            SettingsPayload(
                network="testnet", market_type="usdm", last_symbol="ETH/USDT", theme="dark"
            ),
        )

    assert ctx.network is Network.MAINNET
    assert ctx.market_type is MarketType.SPOT
    assert ctx.settings.network == Network.MAINNET.value
    assert ctx.settings.market_type == MarketType.SPOT.value
    assert ctx.settings.last_symbol == AppSettings().last_symbol
    assert ctx.settings.theme == AppSettings().theme
    assert ctx.settings.ai.model == "deepseek-v4.1-flash"


def test_a_successful_save_keeps_the_changes() -> None:
    """回滚只在失败时发生，成功时该改的照改。"""
    ctx = _SavingCtx(refuse=False)

    changed = apply_settings(ctx, SettingsPayload(network="testnet", theme="dark"))

    assert changed == ["network", "theme"]
    assert ctx.network is Network.TESTNET
    assert ctx.settings.theme == "dark"


# -------------------------------------------- null 不许写成字符串 "None"
def test_null_ai_field_keeps_the_saved_value() -> None:
    """前端漏传字段变成 null 时，模型名不能被写成 "None"。"""
    ctx = _SavingCtx(refuse=False)
    ctx.settings.ai.model = "deepseek-v4.1-flash"
    ctx.settings.ai.base_url = "https://api.example.com/v1"

    apply_settings(ctx, SettingsPayload(ai={"model": None, "base_url": None}))

    assert ctx.settings.ai.model == "deepseek-v4.1-flash"
    assert ctx.settings.ai.base_url == "https://api.example.com/v1"


def test_null_switch_does_not_silently_turn_a_guard_off() -> None:
    """null 开关以前会被 bool(None) 变成 False，等于悄悄关掉急停。"""
    ctx = _SavingCtx(refuse=False)
    ctx.settings.risk.kill_switch = True
    ctx.settings.notify.enabled = True
    ctx.settings.notify.webhook_url = "https://example.com/hook"

    apply_settings(
        ctx,
        SettingsPayload(
            risk={"kill_switch": None},
            notify={"enabled": None, "webhook_url": None},
        ),
    )

    assert ctx.settings.risk.kill_switch is True
    assert ctx.settings.notify.enabled is True
    assert ctx.settings.notify.webhook_url == "https://example.com/hook"


# ------------------------------------------- 死掉的 watcher 挡住后续订阅
def test_a_dead_watcher_is_replaced_on_the_next_subscribe() -> None:
    """watcher 起不来就退出时，下一次订阅必须重新起一个，不能一条帧都没有。"""
    hub = StreamHub(Network.TESTNET)
    started: list[str] = []

    async def dying_watcher(market: MarketType, symbol: str) -> None:
        started.append(symbol)
        raise RuntimeError("交易所客户端建不起来")

    hub._watch_pair = dying_watcher  # type: ignore[assignment]

    async def scenario() -> None:
        await hub.subscribe(MarketType.SPOT, "BTC/USDT")
        for _ in range(5):
            await asyncio.sleep(0)
        await hub.subscribe(MarketType.SPOT, "BTC/USDT")
        for _ in range(5):
            await asyncio.sleep(0)

    asyncio.run(scenario())

    assert started == ["BTC/USDT", "BTC/USDT"]


# ------------------------------------------- 断线重连：退避 + 日志限频
def test_a_dead_stream_backs_off_and_stops_flooding_the_log(caplog) -> None:
    """币安域名不可达时，固定 3 秒重连会把 journal 刷成几万行。两件都要在：

    重连要按指数退避，同一条通道的失败日志每 5 分钟最多一条，收到一帧就清零并
    补一条「恢复」。原因原样带出，不吞。
    """
    hub = StreamHub(Network.TESTNET)
    with caplog.at_level(logging.WARNING, logger="btrader.web.stream"):
        delays = [hub._fail_sleep("ticker", SYMBOL, RuntimeError("boom")) for _ in range(8)]
    assert delays == [3.0, 6.0, 12.0, 24.0, 48.0, 60.0, 60.0, 60.0]
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert "boom" in warnings[0].getMessage()  # 原因照实写出来
    # 另一条通道自己数自己的，不受这一条影响
    assert hub._fail_sleep("盘口", SYMBOL, RuntimeError("boom")) == 3.0
    # 真的收到一帧：计数清零，补一条恢复
    with caplog.at_level(logging.INFO, logger="btrader.web.stream"):
        hub._recovered("ticker", SYMBOL)
    assert ("ticker", SYMBOL) not in hub._fails
    assert any("已恢复" in r.getMessage() for r in caplog.records)
    # 没断过、直接来帧的通道不写「恢复」
    before = len(caplog.records)
    hub._recovered("成交", SYMBOL)
    assert len(caplog.records) == before

