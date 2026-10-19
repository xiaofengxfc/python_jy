"""JSON backed application configuration."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .. import paths
from .types import MarketType, Network

log = logging.getLogger(__name__)


@dataclass
class RiskSettings:
    """Basic risk controls. Zero values disable a limit."""

    max_order_quote: float = 500.0
    max_position_quote: float = 5000.0
    max_leverage: float = 10.0
    max_daily_loss: float = 500.0
    max_daily_trades: int = 100
    max_consecutive_losses: int = 5
    require_confirmation: bool = True
    kill_switch: bool = False


@dataclass
class AppSettings:
    network: str = Network.MAINNET.value
    market_type: str = MarketType.SPOT.value
    last_symbol: str = "BTC/USDT"
    timeframe: str = "15m"
    default_order_quote: float = 100.0
    kline_limit: int = 1000
    risk: RiskSettings = field(default_factory=RiskSettings)
    notification_sound: bool = True
    theme: str = "light"

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
        known = {f for f in AppSettings.__dataclass_fields__}
        settings = AppSettings(**{k: v for k, v in raw.items() if k in known})
        risk_known = {f for f in RiskSettings.__dataclass_fields__}
        settings.risk = RiskSettings(**{k: v for k, v in risk_raw.items() if k in risk_known})
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
