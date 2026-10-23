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
from btrader.web import routes as routes_mod
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


# ------------------------------------------------- 非有限数：NaN / Infinity
# 评审实测（P0）：PUT /api/settings 能写进 risk.max_order_quote=nan、exec.leverage=nan、
# ai_auto.decision_ttl_sec=inf，全部 200。写进去之后：
#   * json.dumps 把 NaN 落盘，GET /api/settings 每次都 500（allow_nan=False）；
#   * 999999 > nan 恒为 False，每单上限、持仓上限、决策有效期这些闸全部静默失效。
# 所以收数字的地方统一过 math.isfinite，save() 用 allow_nan=False，读盘也过一遍。
NAN = float("nan")
INF = float("inf")

NON_FINITE_CASES = [
    ("risk", {"risk": {"max_order_quote": NAN}}, "risk.max_order_quote"),
    ("risk", {"risk": {"max_position_quote": INF}}, "risk.max_position_quote"),
    ("risk", {"risk": {"max_order_quote": "-inf"}}, "risk.max_order_quote"),
    ("exec", {"exec": {"leverage": NAN}}, "exec.leverage"),
    ("exec", {"exec": {"position_pct": INF}}, "exec.position_pct"),
    ("ai_auto", {"ai_auto": {"decision_ttl_sec": INF}}, "ai_auto.decision_ttl_sec"),
    ("ai_auto", {"ai_auto": {"max_order_quote": "nan"}}, "ai_auto.max_order_quote"),
    ("ai", {"ai": {"temperature": NAN}}, "ai.temperature"),
    ("ai", {"ai": {"timeout": "Infinity"}}, "ai.timeout"),
]


@pytest.mark.parametrize("section,kwargs,where", NON_FINITE_CASES)
def test_a_non_finite_number_is_refused(section, kwargs, where) -> None:
    """nan / inf / "nan" / "Infinity" 全拒：400、说清是哪个字段、内存不动。"""
    ctx = FakeCtx()
    before = ctx.settings.to_dict()

    with pytest.raises(HTTPException) as caught:
        apply_settings(ctx, SettingsPayload(**kwargs))

    assert caught.value.status_code == 400
    detail = caught.value.detail
    assert where in detail, detail
    assert "有限" in detail or "nan" in detail.lower(), detail
    assert ctx.settings.to_dict() == before
    assert ctx.saved == 0


def test_a_boolean_is_not_a_number() -> None:
    """true 能被 float() 吃成 1.0，但把上限写成布尔是写错了。"""
    ctx = FakeCtx()

    with pytest.raises(HTTPException) as caught:
        apply_settings(ctx, SettingsPayload(risk={"max_order_quote": True}))

    assert caught.value.status_code == 400
    assert "risk.max_order_quote" in caught.value.detail


def test_a_null_number_is_still_refused() -> None:
    """null 不当 0：以前 type(current)(None) 直接 TypeError，现在要说人话。"""
    ctx = FakeCtx()

    with pytest.raises(HTTPException) as caught:
        apply_settings(ctx, SettingsPayload(exec={"leverage": None}))

    assert caught.value.status_code == 400
    assert "exec.leverage" in caught.value.detail


def test_a_finite_number_still_goes_through() -> None:
    """别把正常的数字一起拦了。"""
    ctx = FakeCtx()

    changed = apply_settings(
        ctx,
        SettingsPayload(
            risk={"max_order_quote": 1234.5, "max_daily_trades": 42},
            exec={"leverage": 3.0},
            ai_auto={"decision_ttl_sec": 90.0},
            ai={"temperature": 0.7},
        ),
    )

    assert set(changed) == {"risk", "exec", "ai_auto", "ai"}
    assert ctx.settings.risk.max_order_quote == pytest.approx(1234.5)
    assert ctx.settings.risk.max_daily_trades == 42
    assert ctx.settings.exec.leverage == pytest.approx(3.0)
    assert ctx.settings.ai_auto.decision_ttl_sec == pytest.approx(90.0)
    assert ctx.settings.ai.temperature == pytest.approx(0.7)


class _DiskCtx(FakeCtx):
    """真的写盘：用来验「400 之后磁盘上的文件一个字节都没变」。"""

    def __init__(self, store: ConfigStore) -> None:
        super().__init__()
        self.store = store
        self.settings = store.settings

    def save_settings(self) -> None:
        self.saved += 1
        self.store.save()


@pytest.mark.parametrize("kwargs,where", [(kw, where) for _s, kw, where in NON_FINITE_CASES[:5]])
def test_a_refused_number_leaves_the_file_alone(tmp_path, kwargs, where) -> None:
    path = tmp_path / "config.json"
    ctx = _DiskCtx(ConfigStore(path))
    apply_settings(ctx, SettingsPayload(risk={"max_order_quote": 100.0}))
    before = path.read_text(encoding="utf-8")
    assert "NaN" not in before and "Infinity" not in before

    with pytest.raises(HTTPException):
        apply_settings(ctx, SettingsPayload(**kwargs))

    assert path.read_text(encoding="utf-8") == before
    assert ConfigStore(path).settings.risk.max_order_quote == pytest.approx(100.0)


def test_save_refuses_to_write_a_non_finite_number(tmp_path) -> None:
    """save() 用 allow_nan=False：写进非有限数要当场抛，不是落盘等下次读盘炸。"""
    path = tmp_path / "config.json"
    store = ConfigStore(path)
    store.save()
    before = path.read_text(encoding="utf-8")

    store.settings.risk.max_order_quote = NAN
    with pytest.raises(ConfigError) as caught:
        store.save()

    assert "NaN" in str(caught.value) or "有限" in str(caught.value)
    assert path.read_text(encoding="utf-8") == before
    assert "NaN" not in path.read_text(encoding="utf-8")


def test_a_nan_in_the_saved_file_falls_back_to_the_default(tmp_path) -> None:
    """盘上已经写着 NaN 的配置要能读开：该字段退回默认值，别的字段照旧。"""
    path = tmp_path / "config.json"
    path.write_text(
        '{"risk": {"max_order_quote": NaN, "max_daily_trades": NaN, "max_leverage": 3.5},'
        ' "ai": {"temperature": NaN, "model": "deepseek-v4.1-flash"},'
        ' "ai_auto": {"decision_ttl_sec": Infinity},'
        ' "exec": {"leverage": NaN},'
        ' "theme": "dark"}',
        encoding="utf-8",
    )

    settings = ConfigStore(path).settings
    defaults = AppSettings()

    assert settings.risk.max_order_quote == defaults.risk.max_order_quote
    assert settings.risk.max_daily_trades == defaults.risk.max_daily_trades
    assert isinstance(settings.risk.max_daily_trades, int)
    assert settings.risk.max_leverage == pytest.approx(3.5)
    assert settings.ai.temperature == pytest.approx(defaults.ai.temperature)
    assert settings.ai.model == "deepseek-v4.1-flash"
    assert settings.ai_auto.decision_ttl_sec == pytest.approx(
        defaults.ai_auto.decision_ttl_sec
    )
    assert settings.exec.leverage == pytest.approx(defaults.exec.leverage)
    assert settings.theme == "dark"
    # 读开之后必须能再存出去：以前的 NaN 会让每次 GET /api/settings 都 500。
    json.dumps(settings.to_dict(), allow_nan=False)


class _Rest:
    has_credentials = False


class _State:
    def __init__(self, ctx: FakeCtx) -> None:
        self.ctx = ctx


def _settings_endpoint(ctx: FakeCtx):
    """直接取 GET /api/settings 的入口函数，不起 app、不碰网络。"""
    ctx.rest = _Rest()
    router = routes_mod.build_router(lambda: _State(ctx))
    for route in router.routes:
        if getattr(route, "path", None) == "/api/settings" and "GET" in route.methods:
            return route.endpoint
    raise AssertionError("没找到 GET /api/settings")


def test_the_settings_endpoint_survives_a_nan_in_the_file(tmp_path) -> None:
    """手写一份带 NaN 的 config.json：GET /api/settings 必须 200（以前每次 500）。"""
    path = tmp_path / "config.json"
    path.write_text('{"risk": {"max_order_quote": NaN}}', encoding="utf-8")
    ctx = FakeCtx()
    ctx.settings = ConfigStore(path).settings

    payload = _settings_endpoint(ctx)()

    assert payload["settings"]["risk"]["max_order_quote"] == pytest.approx(
        AppSettings().risk.max_order_quote
    )
    json.dumps(payload, allow_nan=False)


# ------------------------------------------- 只写请求体里真正出现过的段落
def test_a_concurrent_tab_keeps_the_section_it_saved(monkeypatch) -> None:
    """两个标签页改不同段落：后保存的那个不许把另一个的改动写回旧值。

    这里用 _apply_risk 里的一个钩子模拟「甲还在处理时，乙刚存完 ai_auto」——
    甲请求体里没有 ai_auto，草稿里却是打开页面时的那一份。
    """
    ctx = FakeCtx()
    original = routes_mod._apply_risk

    def hook(target, values):
        original(target, values)
        ctx.settings.ai_auto.decision_ttl_sec = 33.0

    monkeypatch.setattr(routes_mod, "_apply_risk", hook)

    changed = apply_settings(ctx, SettingsPayload(risk={"max_order_quote": 80.0}))

    assert changed == ["risk"]
    assert ctx.settings.risk.max_order_quote == pytest.approx(80.0)
    assert ctx.settings.ai_auto.decision_ttl_sec == pytest.approx(33.0)
