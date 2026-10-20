"""JSON backed application configuration."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .. import paths
from ..llm import AISettings
from ..notify import NotifySettings
from .types import MarketType, Network

log = logging.getLogger(__name__)


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
    """AI 自主交易的持久化配置（默认关闭、纸面模式）。

    ``live_enabled`` 只是“允许实盘”的一半条件：每次启动仍要显式传
    ``confirm_live``，并且必须主网 + 未急停（见 ``engine/ai_trader.py``）。
    """

    enabled: bool = False
    mode: str = "paper"
    live_enabled: bool = False
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


@dataclass
class AppSettings:
    network: str = Network.MAINNET.value
    market_type: str = MarketType.SPOT.value
    last_symbol: str = "BTC/USDT"
    timeframe: str = "15m"
    default_order_quote: float = 100.0
    kline_limit: int = 1000
    # HTTP(S) proxy for exchange requests, e.g. "http://127.0.0.1:7890".
    # Needed where Binance's domains are blocked or DNS-poisoned.
    proxy: str = ""
    risk: RiskSettings = field(default_factory=RiskSettings)
    notification_sound: bool = True
    theme: str = "light"
    # WeChat push via the local OpenClaw gateway (see ``btrader.notify``).
    notify: NotifySettings = field(default_factory=NotifySettings)
    # Optional LLM assistant (see ``btrader.llm``). The API key is not here: it
    # lives in the OS keyring, so a settings file that gets copied or shared
    # never carries a credential.
    ai: AISettings = field(default_factory=AISettings)
    # AI 自主交易（Phase 3）：默认关闭，实盘另有显式开关。
    ai_auto: AIAutoSettings = field(default_factory=AIAutoSettings)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ConfigStore:
    """Loads and saves settings; never stores secrets."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or paths.config_file()
        self.settings = self._load()

    def _load(self) -> AppSettings:
        if not self._path.exists():
            return AppSettings()
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("读取配置失败，使用默认配置: %s", exc)
            return AppSettings()

        risk_raw = raw.pop("risk", {}) or {}
        notify_raw = raw.pop("notify", {}) or {}
        ai_raw = raw.pop("ai", {}) or {}
        ai_auto_raw = raw.pop("ai_auto", {}) or {}
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

    def save(self) -> None:
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
