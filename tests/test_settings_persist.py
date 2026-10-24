"""AI 交易 / AI 量化的运行配置与执行台参数必须活过刷新。

漏点是这样的：页面上改的单笔金额、杠杆、止损止盈、策略参数、交易对、周期只在本地
state 里，刷新就回默认；执行台的仓位比例 / 杠杆 / 轮询间隔更是字面默认值。这一组
钉住三件事——

1. `ai_auto` 的每个字段 PUT 之后 GET 还在，重启（重新读盘）也在；
2. 执行台那三项（`exec`）同样；
3. `POST /api/settings/reset` 只重置点名的段落，别的段落一个字节都不动。
"""

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from btrader.core.config import AppSettings, ConfigStore
from btrader.core.types import MarketType, Network
from btrader.web.routes import (
    SettingsPayload,
    SettingsResetPayload,
    apply_settings,
    reset_settings,
)

SPOT = "BTC/USDT"
USDM = "BTC/USDT:USDT"
LIMIT = 100


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


# ------------------------------------------------------------- ai_auto 持久化
def test_an_ai_auto_field_survives_a_put_and_a_reload(tmp_path):
    """改一个字段 -> GET 还在；重新读盘（= 刷新页面 + 重启）也还在。"""
    ctx, path = _ctx(tmp_path)

    apply_settings(ctx, SettingsPayload(ai_auto={"min_confidence": 0.85}))

    assert ctx.settings.ai_auto.min_confidence == pytest.approx(0.85)
    assert ConfigStore(path).settings.ai_auto.min_confidence == pytest.approx(0.85)


def test_every_runtime_field_of_ai_auto_persists(tmp_path):
    """页面上那几档一起改：金额、杠杆、止损开关、交易对、周期、深度、策略参数。"""
    ctx, path = _ctx(tmp_path)

    apply_settings(
        ctx,
        SettingsPayload(
            ai_auto={
                "max_order_quote": 120.0,
                "max_position_quote": 500.0,
                "leverage": 3.0,
                "require_stop": False,
                "symbol": USDM,
                "timeframe": "1h",
                "depth": "deep",
                "poll_sec": 30.0,
                "strategy_key": "ma_cross",
                "strategy_params": {"ma_cross": {"fast": 9, "slow": 21}},
            }
        ),
    )

    saved = ConfigStore(path).settings.ai_auto

    assert saved.max_order_quote == pytest.approx(120.0)
    assert saved.max_position_quote == pytest.approx(500.0)
    assert saved.leverage == pytest.approx(3.0)
    assert saved.require_stop is False
    assert saved.symbol == USDM
    assert saved.timeframe == "1h"
    assert saved.depth == "deep"
    assert saved.poll_sec == pytest.approx(30.0)
    assert saved.strategy_key == "ma_cross"
    assert saved.strategy_params == {"ma_cross": {"fast": 9, "slow": 21}}


def test_the_page_strategy_and_market_persist(tmp_path):
    """AI 量化那条页签选过的策略与市场也得记住，否则重开就换了一对。"""
    ctx, path = _ctx(tmp_path)

    apply_settings(ctx, SettingsPayload(ai_auto={"strategy_key": "rsi_reversal", "market_type": "usdm"}))

    saved = ConfigStore(path).settings.ai_auto
    assert saved.strategy_key == "rsi_reversal"
    assert saved.market_type == "usdm"


def test_a_bad_market_in_ai_auto_is_rejected(tmp_path):
    ctx, _path = _ctx(tmp_path)

    with pytest.raises(HTTPException) as caught:
        apply_settings(ctx, SettingsPayload(ai_auto={"market_type": "options"}))

    assert caught.value.status_code == 400
    assert ctx.settings.ai_auto.market_type == ""
    assert ctx.saved == 0


def test_a_bad_depth_is_rejected(tmp_path):
    ctx, _path = _ctx(tmp_path)

    with pytest.raises(HTTPException) as caught:
        apply_settings(ctx, SettingsPayload(ai_auto={"depth": "quantum"}))

    assert caught.value.status_code == 400
    assert ctx.settings.ai_auto.depth == "standard"


# ------------------------------------------------------------- 执行台三项
def test_the_exec_panel_survives_a_put_and_a_reload(tmp_path):
    ctx, path = _ctx(tmp_path)

    apply_settings(
        ctx, SettingsPayload(exec={"position_pct": 0.5, "leverage": 3.0, "poll_seconds": 5.0})
    )

    saved = ConfigStore(path).settings.exec
    assert saved.position_pct == pytest.approx(0.5)
    assert saved.leverage == pytest.approx(3.0)
    assert saved.poll_seconds == pytest.approx(5.0)


def test_the_exec_panel_defaults_leave_room_under_the_risk_cap(tmp_path):
    """仓位比例默认 0.2 而不是 1：名义额 = 权益 × 比例 × 杠杆，单笔上限是
    「500 USDT 与权益 25% 取大的」。填 1 时名义额是上限的 4 倍，执行台每一轮
    都被拒成「单笔金额超过上限」，看着像杠杆没生效。也不取 0.25——那正好压在
    上限线上，引擎自己读的权益与风控读的权益差一分钱就翻。"""
    ctx, _path = _ctx(tmp_path)

    assert ctx.settings.exec.position_pct == pytest.approx(0.2)
    assert ctx.settings.exec.leverage == 1.0
    assert ctx.settings.exec.poll_seconds == 10.0
    assert ctx.settings.exec.position_pct < ctx.settings.risk.max_order_pct, "默认仓位压在上限下面"


def test_exec_rejects_a_share_above_one(tmp_path):
    """position_pct 是 0 到 1 的比例（1 = 全部可用余额），不是百分数。"""
    ctx, _path = _ctx(tmp_path)

    with pytest.raises(HTTPException) as caught:
        apply_settings(ctx, SettingsPayload(exec={"position_pct": 50}))

    assert caught.value.status_code == 400
    assert "1" in str(caught.value.detail)
    assert ctx.saved == 0


def test_exec_rejects_a_zero_leverage(tmp_path):
    ctx, _path = _ctx(tmp_path)

    with pytest.raises(HTTPException):
        apply_settings(ctx, SettingsPayload(exec={"leverage": 0}))

    assert ctx.settings.exec.leverage == 1.0


# ----------------------------------------------------------------- 恢复默认
def test_reset_only_touches_the_named_section(tmp_path):
    """只重置 ai_auto：执行台三项、收藏、别的设置都留着。"""
    ctx, path = _ctx(tmp_path)
    apply_settings(
        ctx,
        SettingsPayload(
            ai_auto={"min_confidence": 0.9, "max_order_quote": 300.0},
            exec={"position_pct": 0.25, "leverage": 2.0, "poll_seconds": 3.0},
            favorites=[SPOT, "ETH/USDT"],
            timeframe="4h",
        ),
    )

    out = reset_settings(ctx, "ai_auto")

    assert out["section"] == "ai_auto"
    reloaded = ConfigStore(path).settings
    assert reloaded.ai_auto == type(reloaded.ai_auto)()
    assert reloaded.exec.position_pct == pytest.approx(0.25)
    assert reloaded.exec.leverage == pytest.approx(2.0)
    assert reloaded.favorites == [SPOT, "ETH/USDT"]
    assert reloaded.timeframe == "4h"


def test_reset_the_exec_panel_keeps_ai_auto(tmp_path):
    ctx, path = _ctx(tmp_path)
    apply_settings(
        ctx,
        SettingsPayload(ai_auto={"min_confidence": 0.9}, exec={"leverage": 5.0}),
    )

    reset_settings(ctx, "exec")

    reloaded = ConfigStore(path).settings
    assert reloaded.exec == type(reloaded.exec)()
    assert reloaded.ai_auto.min_confidence == pytest.approx(0.9)


def test_reset_all_restores_the_runtime_blocks_only(tmp_path):
    """「全部」重置的是运行配置：网络、市场、主题这些不属于运行配置，不许动。"""
    ctx, path = _ctx(tmp_path)
    apply_settings(
        ctx,
        SettingsPayload(
            ai_auto={"min_confidence": 0.9},
            exec={"position_pct": 0.4},
            favorites=[SPOT],
            recent_symbols=[SPOT],
            theme="dark",
            timeframe="1h",
        ),
    )

    out = reset_settings(ctx, "all")

    reloaded = ConfigStore(path).settings
    assert reloaded.ai_auto.min_confidence == pytest.approx(0.6)
    assert reloaded.exec.position_pct == pytest.approx(0.2)
    assert reloaded.favorites == []
    assert reloaded.recent_symbols == []
    assert reloaded.theme == "dark"
    assert reloaded.timeframe == "1h"
    assert reloaded.market_type == AppSettings().market_type
    assert out["settings"]["exec"]["position_pct"] == pytest.approx(0.2)


def test_an_unknown_section_is_rejected(tmp_path):
    """点错了段落就 400 说清有哪些，别静默当成「全部」重置。"""
    ctx, _path = _ctx(tmp_path)
    apply_settings(ctx, SettingsPayload(ai_auto={"min_confidence": 0.9}))

    with pytest.raises(HTTPException) as caught:
        reset_settings(ctx, "everything")

    assert caught.value.status_code == 400
    assert "ai_auto" in str(caught.value.detail)
    assert ctx.settings.ai_auto.min_confidence == pytest.approx(0.9)


# ------------------------------------------------------------------- 收藏
def test_favorites_are_deduped(tmp_path):
    ctx, path = _ctx(tmp_path)

    apply_settings(ctx, SettingsPayload(favorites=[SPOT, SPOT, "ETH/USDT", SPOT]))

    assert ConfigStore(path).settings.favorites == [SPOT, "ETH/USDT"]


def test_more_than_the_cap_is_a_400_not_a_silent_truncation(tmp_path):
    """超过 100 个当场拒绝：静默截断会让第 101 个收藏点了没反应。"""
    ctx, _path = _ctx(tmp_path)
    names = [f"COIN{index}/USDT" for index in range(LIMIT)]

    with pytest.raises(HTTPException) as caught:
        apply_settings(ctx, SettingsPayload(favorites=[SPOT, *names]))

    assert caught.value.status_code == 400
    assert str(LIMIT) in str(caught.value.detail)
    assert ctx.settings.favorites == []
    assert ctx.saved == 0


def test_exactly_the_cap_is_accepted(tmp_path):
    ctx, path = _ctx(tmp_path)
    names = [f"COIN{index}/USDT" for index in range(LIMIT)]

    apply_settings(ctx, SettingsPayload(favorites=names))

    assert len(ConfigStore(path).settings.favorites) == LIMIT


def test_a_hand_edited_list_is_capped_on_load(tmp_path):
    """读盘这条路上不报错（整份配置得能打开），多出来的丢掉。"""
    path = tmp_path / "config.json"
    names = [f"COIN{index}/USDT" for index in range(LIMIT + 20)]
    path.write_text(json.dumps({"favorites": names}), encoding="utf-8")

    assert len(ConfigStore(path).settings.favorites) == LIMIT


def test_recent_symbols_keep_the_latest_first_order(tmp_path):
    ctx, path = _ctx(tmp_path)

    apply_settings(ctx, SettingsPayload(recent_symbols=["ETH/USDT", SPOT, "ETH/USDT"]))

    assert ConfigStore(path).settings.recent_symbols == ["ETH/USDT", SPOT]


def test_a_bad_symbol_shape_is_rejected(tmp_path):
    ctx, _path = _ctx(tmp_path)

    with pytest.raises(HTTPException) as caught:
        apply_settings(ctx, SettingsPayload(favorites=["BTC"]))

    assert caught.value.status_code == 400
    assert "BTC/USDT" in str(caught.value.detail)
    assert ctx.settings.favorites == []
    assert ctx.saved == 0


def test_favorites_keep_both_spellings(tmp_path):
    """收藏是跨市场的：现货写法与合约写法都合法，未知交易对也不拦。"""
    ctx, path = _ctx(tmp_path)

    apply_settings(
        ctx,
        SettingsPayload(favorites=[SPOT, USDM, "NOTLISTED/USDT:USDT"]),
    )

    assert ConfigStore(path).settings.favorites == [SPOT, USDM, "NOTLISTED/USDT:USDT"]


def test_a_hand_edited_config_with_a_bad_symbol_still_loads(tmp_path):
    """手改 config.json 写坏一个收藏：丢掉那一个，别的照样读得出来。"""
    path = tmp_path / "config.json"
    path.write_text(
        '{"favorites": ["BTC/USDT", "oops", "ETH/USDT", "BTC/USDT"], "theme": "dark"}',
        encoding="utf-8",
    )

    settings = ConfigStore(path).settings

    assert settings.favorites == ["BTC/USDT", "ETH/USDT"]
    assert settings.theme == "dark"


def test_the_reset_payload_defaults_to_ai_auto():
    assert SettingsResetPayload().section == "ai_auto"
