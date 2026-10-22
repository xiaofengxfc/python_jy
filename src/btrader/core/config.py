"""JSON backed application configuration."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .. import paths
from ..llm import AISettings
from ..notify import NotifySettings
from .types import MarketType, Network

log = logging.getLogger(__name__)


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
    # Best parameter sets explicitly applied from the strategy lab, keyed by
    # strategy. They never change without a human clicking “apply”.
    strategy_params: dict[str, dict[str, Any]] = field(default_factory=dict)


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

        risk_raw = raw.pop("risk", {}) or {}
        notify_raw = raw.pop("notify", {}) or {}
        ai_raw = raw.pop("ai", {}) or {}
        ai_auto_raw = raw.pop("ai_auto", {}) or {}
        # A settings file written by an older build may still carry a sidecar
        # block; it is dropped here so the field list stays authoritative.
        raw.pop("freqtrade", None)
        known = {f for f in AppSettings.__dataclass_fields__}
        settings = AppSettings(**{k: v for k, v in raw.items() if k in known})
        risk_known = {f for f in RiskSettings.__dataclass_fields__}
        settings.risk = RiskSettings(**{k: v for k, v in risk_raw.items() if k in risk_known})
        # Nested dataclasses must be rebuilt from their dicts; leaving a plain
        # dict here would break every ``settings.notify.<field>`` read.
        notify_known = {f for f in NotifySettings.__dataclass_fields__}
        settings.notify = NotifySettings(
            **{k: v for k, v in notify_raw.items() if k in notify_known}
        )
        ai_known = {f for f in AISettings.__dataclass_fields__}
        settings.ai = AISettings(**{k: v for k, v in ai_raw.items() if k in ai_known})
        ai_auto_known = {f for f in AIAutoSettings.__dataclass_fields__}
        settings.ai_auto = AIAutoSettings(
            **{k: v for k, v in ai_auto_raw.items() if k in ai_auto_known}
        )
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
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self.settings.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(self._path)

    def update(self, **kwargs: Any) -> None:
        for key, value in kwargs.items():
            if hasattr(self.settings, key):
                setattr(self.settings, key, value)
        self.save()
