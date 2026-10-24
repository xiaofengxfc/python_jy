"""JSON backed application configuration."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from .. import paths
from ..llm import AISettings
from ..notify import NotifySettings
from .types import MarketType, Network, symbol_shape_error

log = logging.getLogger(__name__)


def _as_float(value: Any) -> float | None:
    """能当数字用就返回 float，否则 None（手改配置里的 "abc" 当没写）。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _coerce_block(name: str, cls: type, raw: dict[str, Any]) -> dict[str, Any]:
    """按 dataclass 声明的类型把一段配置收一遍：数字字段要有限，int 字段保持 int。

    手改过的 config.json 里可能写着 NaN 或 Infinity —— 它们是合法 JSON，json 能读
    进来，但存着会让 999999 > nan 恒 False（每单上限、持仓上限、决策有效期全部
    静默失效），也会让每条 GET /api/settings 因为 allow_nan=False 变成 500。
    这类值一律退回默认值并记一条 warning：整份配置必须还能打开。
    """
    specs = {spec.name: spec for spec in fields(cls)}
    out: dict[str, Any] = {}
    for key, value in raw.items():
        spec = specs.get(key)
        if spec is None:
            continue
        kind = str(spec.type)
        if kind == "float":
            number = _as_float(value)
            if number is None:
                log.warning("配置里的 %s.%s 不是有限数字（%r），按默认值处理", name, key, value)
                continue
            out[key] = number
        elif kind == "int":
            number = None if isinstance(value, bool) else _as_float(value)
            if number is None or number != int(number):
                log.warning("配置里的 %s.%s 不是整数（%r），按默认值处理", name, key, value)
                continue
            out[key] = int(number)
        else:
            out[key] = value
    return out

class ConfigError(RuntimeError):
    """配置文件写入被拒绝。用户读到的话必须说明文件在哪、怎么修。"""


@dataclass
class RiskSettings:
    """Basic risk controls. Zero values disable a limit."""

    max_order_quote: float = 500.0
    # Per-order cap as a share of account equity (0 disables it). This is what
    # keeps "全部" usable: a fixed 500 USDT ceiling silently rejected a full
    # buy from a 10,000 balance. The effective limit is the tighter of this and
    # ``max_order_quote``.
    max_order_pct: float = 0.25
    max_position_quote: float = 5000.0
    max_leverage: float = 10.0
    max_daily_loss: float = 500.0
    max_daily_trades: int = 100
    max_consecutive_losses: int = 5
    require_confirmation: bool = True
    kill_switch: bool = False


@dataclass
class AIAutoSettings:
    """AI 自主交易的持久化配置（默认关闭）。

    启动条件只有三条：AI 助手已启用、这条市场配了凭据、主网还要调用方显式
    说清这是真钱（confirm_live）。急停、AIGuard、风控各自在
    engine/ai_trader.py 里独立把关，这里没有多余的开关。
    """

    enabled: bool = False
    symbol: str = ""
    timeframe: str = ""
    leverage: float = 1.0
    max_order_quote: float = 50.0
    max_position_quote: float = 200.0
    max_decisions_per_day: int = 96
    max_orders_per_hour: int = 12
    min_confidence: float = 0.6
    decision_ttl_sec: float = 120.0
    max_price_drift_pct: float = 0.005
    cooldown_sec: float = 30.0
    poll_sec: float = 60.0
    require_stop: bool = True
    # 决策深度：quick 一次调用，standard 四段，deep 六段。
    depth: str = "standard"
    # 页面选过的策略与市场。不记的话，AI 交易/AI 量化每次打开都回落到「第一条
    # 策略」和全局市场，用户挑的那一对就白挑了。启动时优先级仍是
    # 请求体 > 这里 > 全局，见 routes.AITraderController._build。
    strategy_key: str = ""
    market_type: str = ""
    # Best parameter sets explicitly applied from the strategy lab, keyed by
    # strategy. They never change without a human clicking “apply”.
    strategy_params: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class ExecSettings:
    """执行台（手动下单面板）最后用过的那一组参数。

    这三项是「下次打开还按上次那样填」，不是风控：真正的上限在 RiskSettings 和
    AITrader 的闸里，把它们写进配置改不动任何一条闸。
    """

    # 仓位比例的默认值不能是 1：执行台的名义额是「权益 × position_pct × leverage」，
    # 而单笔上限是「500 USDT 与权益 max_order_pct（默认 25%）取大的」。填 1 时名义额
    # 是上限的 4 倍，引擎每一轮都被风控拒成「单笔金额超过上限」，看着像杠杆没生效。
    # 也不取 0.25：那正好压在上限线上，引擎自己读的权益与风控读的权益差一分钱就翻。
    position_pct: float = 0.2  # 下单金额占可用余额的比例，1 = 全部
    leverage: float = 1.0
    poll_seconds: float = 10.0


# 手动下单只给合约记住杠杆。现货恒 1 倍，存一个 "spot" 键等于在界面上造一个假
# 的杠杆开关。币安两种合约的杠杆上限都是 125x。
TRADE_LEVERAGE_MARKETS = ("usdm", "coinm")
TRADE_LEVERAGE_MAX = 125


@dataclass
class TradeSettings:
    """手动下单框按市场记住的杠杆（只存合约市场，默认各 1 倍）。

    这是「下次打开还按上次那样填」，不是风控：真正的上限仍归
    ``RiskSettings.max_leverage``——下单时取两者较小值，被收口会在响应里说明，
    见 ``routes.resolve_order_leverage``。默认全是 1，和没有这一层时逐位一样。
    """

    leverage: dict[str, int] = field(
        default_factory=lambda: {name: 1 for name in TRADE_LEVERAGE_MARKETS}
    )


def clean_trade_leverage(values: Any) -> dict[str, int]:
    """读盘用：只留合约市场的整数杠杆，写坏的那一项退回 1。

    手改 config.json 写进 0、999、"abc"、把 "spot" 也塞进来，都只丢那一项，
    整份配置照样打得开——和 ``clean_symbol_list`` 同一条理由。PUT 那边要 400
    给用户看，走的是它自己的校验（``routes._apply_trade``）。
    """
    out = {name: 1 for name in TRADE_LEVERAGE_MARKETS}
    if not isinstance(values, dict):
        if values not in (None, {}, []):
            log.warning("配置里的 trade.leverage 不是对象（%r），按默认值处理", values)
        return out
    for key, value in values.items():
        name = str(key or "").strip().lower()
        if name not in TRADE_LEVERAGE_MARKETS:
            log.warning("配置里的 trade.leverage.%s 不是合约市场，已忽略", key)
            continue
        number = None if isinstance(value, bool) else _as_float(value)
        if number is None or number != round(number) or not 1 <= number <= TRADE_LEVERAGE_MAX:
            log.warning(
                "配置里的 trade.leverage.%s 不是 1-%d 的整数（%r），按 1x 处理",
                name,
                TRADE_LEVERAGE_MAX,
                value,
            )
            continue
        out[name] = int(round(number))
    return out


# 收藏与最近使用最多记这么多对：再多界面上也没人翻，config.json 却会一直涨。
SYMBOL_LIST_LIMIT = 100


def clean_symbol_list(values: Any, limit: int = SYMBOL_LIST_LIMIT) -> list[str]:
    """去重、保序、截到上限，写法不对的丢掉。

    读盘用这个：手改 config.json 写坏一个收藏，不该让整份配置打不开——和「坏掉
    的那一块退回默认值」同一条理由。PUT 那边要 400 给用户看，走的是它自己的校验。
    """
    out: list[str] = []
    for value in values if isinstance(values, (list, tuple)) else ():
        text = str(value or "").strip()
        if not text or text in out or symbol_shape_error(text):
            continue
        out.append(text)
        if len(out) >= limit:
            break
    return out


@dataclass
class AppSettings:
    # 交易所。只有币安接了完整链路，其余平台先占位。
    platform: str = "binance"
    network: str = Network.MAINNET.value
    market_type: str = MarketType.SPOT.value
    last_symbol: str = "BTC/USDT"
    timeframe: str = "15m"
    # 下单金额和 K 线条数不再进配置：金额由下单框自己记，K 线固定拉满。
    # HTTP(S) proxy for exchange requests, e.g. "http://127.0.0.1:7890".
    # Needed where Binance's domains are blocked or DNS-poisoned.
    proxy: str = ""
    risk: RiskSettings = field(default_factory=RiskSettings)
    notification_sound: bool = True
    theme: str = "light"
    # 企业微信通知：一条通用 Webhook（见 ``btrader.notify``）。
    notify: NotifySettings = field(default_factory=NotifySettings)
    # Optional LLM assistant (see ``btrader.llm``). The API key is not here: it
    # lives in the OS keyring, so a settings file that gets copied or shared
    # never carries a credential.
    ai: AISettings = field(default_factory=AISettings)
    # AI 交易：默认关闭。没有单独的实盘总闸，凭据决定能不能下单。
    ai_auto: AIAutoSettings = field(default_factory=AIAutoSettings)
    # 执行台最后用过的那一组参数（仓位比例 / 杠杆 / 轮询间隔）。
    exec: ExecSettings = field(default_factory=ExecSettings)
    # 手动下单按市场记住的杠杆（只有合约市场）。风控上限仍在 risk.max_leverage。
    trade: TradeSettings = field(default_factory=TradeSettings)
    # 交易对收藏与最近用过的（最近的在前）。未知交易对也照原样留着：换个市场
    # 之后它可能暂时不在列表里，删掉用户的选择比留一个空行更糟。
    favorites: list[str] = field(default_factory=list)
    recent_symbols: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ConfigStore:
    """Loads and saves settings; never stores secrets."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or paths.config_file()
        # 配置文件读不出来时置位：这时内存里是默认值，一旦保存就会把用户
        # 原来的设置整份盖掉，所以保存要拒绝，等人把坏文件处理掉。
        self._refuse_save = False
        self._backup: Path | None = None
        self.settings = self._load()

    def _load(self) -> AppSettings:
        if not self._path.exists():
            return AppSettings()
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self._refuse_save = True
            self._backup = self._back_up_broken_file()
            log.error("配置文件读不出来，已备份，本次不保存任何设置: %s", exc)
            return AppSettings()
        if not isinstance(raw, dict):
            self._refuse_save = True
            self._backup = self._back_up_broken_file()
            log.error("配置文件结构不对（顶层不是对象），已备份，本次不保存任何设置")
            return AppSettings()
        # 手改 config.json 时把某一层写错（``"ai": true``、写成列表）会让下面的
        # ``.items()`` 抛 AttributeError，整份配置起不来。坏的那一块退回默认值，
        # 其它块照常读——和坏 JSON 一样，都要人还能把程序打开。
        blocks: dict[str, Any] = {
            "risk": raw.pop("risk", {}),
            "notify": raw.pop("notify", {}),
            "ai": raw.pop("ai", {}),
            "ai_auto": raw.pop("ai_auto", {}),
            "exec": raw.pop("exec", {}),
            "trade": raw.pop("trade", {}),
        }
        for name, block in blocks.items():
            if block and not isinstance(block, dict):
                log.warning("配置里的 %s 不是对象，已忽略，按默认值处理", name)
                blocks[name] = {}
        risk_raw = blocks["risk"] or {}
        notify_raw = blocks["notify"] or {}
        ai_raw = blocks["ai"] or {}
        ai_auto_raw = blocks["ai_auto"] or {}
        exec_raw = blocks["exec"] or {}
        trade_raw = blocks["trade"] or {}
        # A settings file written by an older build may still carry a sidecar
        # block; it is dropped here so the field list stays authoritative.
        raw.pop("freqtrade", None)
        known = {f for f in AppSettings.__dataclass_fields__}
        settings = AppSettings(**{k: v for k, v in raw.items() if k in known})
        # Nested dataclasses must be rebuilt from their dicts; leaving a plain
        # dict here would break every ``settings.notify.<field>`` read.
        # 每一段都过 _coerce_block：手改出来的 "1.5"、true、"abc"、NaN 都按类型收，
        # 收不了的退回默认值（以前只有 exec 走这条路，risk / ai / ai_auto 原样读回，
        # 盘上一个 NaN 就让整份配置再也存不出去）。
        settings.risk = RiskSettings(**_coerce_block("risk", RiskSettings, risk_raw))
        settings.notify = NotifySettings(
            **_coerce_block("notify", NotifySettings, notify_raw)
        )
        settings.ai = AISettings(**_coerce_block("ai", AISettings, ai_raw))
        settings.ai_auto = AIAutoSettings(
            **_coerce_block("ai_auto", AIAutoSettings, ai_auto_raw)
        )
        settings.exec = ExecSettings(**_coerce_block("exec", ExecSettings, exec_raw))
        # trade 段只认合约市场的整数杠杆，写坏的退回 1（不整段丢掉）。
        settings.trade = TradeSettings(
            leverage=clean_trade_leverage(trade_raw.get("leverage"))
        )
        settings.favorites = clean_symbol_list(settings.favorites)
        settings.recent_symbols = clean_symbol_list(settings.recent_symbols)
        return settings

    def _back_up_broken_file(self) -> Path | None:
        """把读不出来的配置文件挪一份，留着用户手工救回。"""
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = self._path.with_name(f"{self._path.stem}.bad-{stamp}.json")
        try:
            backup.write_bytes(self._path.read_bytes())
            return backup
        except OSError as exc:
            log.error("备份坏配置失败: %s", exc)
            return None

    def save(self) -> None:
        if self._refuse_save:
            where = f"，原文件已备份为 {self._backup.name}" if self._backup else ""
            raise ConfigError(
                f"配置文件读不出来，为避免覆盖你的设置，这次没有保存{where}。"
                "修好或删掉坏文件后重启程序再改。"
            )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # allow_nan=False：NaN / Infinity 不是合法 JSON。写下去之后 json 读得回来，
            # 但每条读设置的响应都会因为 allow_nan=False 变成 500，重启也救不回来。
            # 让它在这里当场炸，别落盘。
            text = json.dumps(
                self.settings.to_dict(), indent=2, ensure_ascii=False, allow_nan=False
            )
        except ValueError as exc:
            raise ConfigError(
                f"设置里出现了非有限数（NaN / Infinity），这次没有保存：{exc}。"
                "把对应的数字改回正常值再保存。"
            ) from exc
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(self._path)

    def update(self, **kwargs: Any) -> None:
        for key, value in kwargs.items():
            if hasattr(self.settings, key):
                setattr(self.settings, key, value)
        self.save()
