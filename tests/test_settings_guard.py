"""设置保存的两条底线。

1. 配置文件读不出来时不能拿默认值把用户的设置盖掉。
2. 一次保存要么全成，要么什么都不动——老实现切网络/切市场会先落盘。
"""

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from btrader.context import AppContext
from btrader.core.config import AppSettings, ConfigError, ConfigStore
from btrader.core.types import MarketType, Network
from btrader.web.routes import SettingsPayload, apply_settings


def test_a_broken_file_is_backed_up_and_never_overwritten(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text("{ 这不是 JSON", encoding="utf-8")

    store = ConfigStore(path)

    # 内存里是默认值，但磁盘上的原件必须留着。
    assert store.settings.network == AppSettings().network
    backups = list(tmp_path.glob("config.bad-*.json"))
    assert backups and backups[0].read_text(encoding="utf-8") == "{ 这不是 JSON"
    with pytest.raises(ConfigError) as caught:
        store.save()
    assert "没有保存" in str(caught.value)
    assert backups[0].name in str(caught.value)


def test_a_missing_file_is_not_an_error(tmp_path) -> None:
    store = ConfigStore(tmp_path / "config.json")

    store.save()

    assert json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))


def test_platform_survives_a_round_trip(tmp_path) -> None:
    path = tmp_path / "config.json"
    store = ConfigStore(path)
    store.settings.platform = "binance"
    store.save()

    assert ConfigStore(path).settings.platform == "binance"


class FakeCtx:
    """只带 apply_settings 用到的成员，不碰网络和磁盘。"""

    def __init__(self) -> None:
        self.settings = AppSettings()
        self.platform = "binance"
        self.network = Network.MAINNET
        self.market_type = MarketType.SPOT
        self.saved = 0
        self.proxied: list[str] = []

    def save_settings(self) -> None:
        self.saved += 1

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


def test_one_bad_field_saves_nothing() -> None:
    """周期不合法时，网络也不该被改。"""
    ctx = FakeCtx()

    with pytest.raises(HTTPException):
        apply_settings(ctx, SettingsPayload(network="testnet", timeframe="7m"))

    assert ctx.network is Network.MAINNET
    assert ctx.settings.network == Network.MAINNET.value
    assert ctx.saved == 0


def test_a_bad_notify_setting_saves_nothing() -> None:
    ctx = FakeCtx()

    with pytest.raises(HTTPException):
        apply_settings(ctx, SettingsPayload(last_symbol="ETH/USDT", notify={"webhook_url": "不是地址"}))

    assert ctx.settings.last_symbol == AppSettings().last_symbol
    assert ctx.saved == 0


def test_only_the_given_fields_change() -> None:
    """只发一个字段，别处的设置不能被清掉。"""
    ctx = FakeCtx()
    ctx.settings.ai.model = "deepseek-v4.1-flash"
    ctx.settings.notify.webhook_url = "https://example.com/hook"

    changed = apply_settings(ctx, SettingsPayload(last_symbol="ETH/USDT"))

    assert changed == ["last_symbol"]
    assert ctx.settings.last_symbol == "ETH/USDT"
    assert ctx.settings.ai.model == "deepseek-v4.1-flash"
    assert ctx.settings.notify.webhook_url == "https://example.com/hook"
    assert ctx.saved == 1


def test_an_unknown_platform_is_rejected() -> None:
    ctx = FakeCtx()

    with pytest.raises(HTTPException) as caught:
        apply_settings(ctx, SettingsPayload(platform="okx"))

    assert caught.value.status_code == 400
    assert ctx.platform == "binance"


def test_the_app_context_starts_from_the_saved_platform(tmp_path, monkeypatch) -> None:
    """上下文按文件里的平台起，不然面板读的和存的对不上。"""
    monkeypatch.setenv("BITRADER_DATA_DIR", str(tmp_path))
    path = tmp_path / "config.json"
    store = ConfigStore(path)
    store.settings.platform = "binance"
    store.save()

    ctx = AppContext()

    assert ctx.platform == "binance"


def test_an_old_config_with_live_enabled_still_loads(tmp_path) -> None:
    """ai_auto.live_enabled 删了，旧配置里残留的这个键要被安静忽略。

    用户手上的 config.json 还留着 "live_enabled": true。加载按字段白名单过滤，
    多一个键不该让整份配置报错、也不该退回默认值——真报错了，界面一保存就会把
    用户原来的设置整份盖掉。
    """
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "network": "testnet",
                "ai_auto": {
                    "enabled": True,
                    "live_enabled": True,
                    "symbol": "ETH/USDT",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    store = ConfigStore(path)

    assert store.settings.network == "testnet"
    assert store.settings.ai_auto.enabled is True
    assert store.settings.ai_auto.symbol == "ETH/USDT"
    assert not hasattr(store.settings.ai_auto, "live_enabled")
    # 存回去时这个键也不会再出现，等于下一次保存顺手清干净。
    store.save()
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert "live_enabled" not in saved["ai_auto"]
