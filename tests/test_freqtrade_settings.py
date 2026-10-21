"""Freqtrade sidecar settings validation."""

import pytest
from fastapi import HTTPException

from btrader.core.config import FreqtradeSettings
from btrader.web.routes import _apply_freqtrade


def test_apply_freqtrade_updates_supported_fields() -> None:
    target = FreqtradeSettings()
    _apply_freqtrade(
        target,
        {
            "enabled": False,
            "dry_run": False,
            "trade_port": 18082,
            "stake_amount": 25,
            "max_open_trades": 2,
            "exchange": "gate",
            "strategy": "SampleStrategy",
            "timeframe": "15m",
            "pairs": ["BTC/USDT", " ETH/USDT ", ""],
        },
    )

    assert target.enabled is False
    assert target.dry_run is False
    assert target.trade_port == 18082
    assert target.stake_amount == 25.0
    assert target.max_open_trades == 2
    assert target.timeframe == "15m"
    assert target.pairs == ["BTC/USDT", "ETH/USDT"]


@pytest.mark.parametrize(
    "values",
    [
        {"trade_port": 80},
        {"trade_port": 70000},
        {"stake_amount": 0},
        {"max_open_trades": 0},
        {"timeframe": "7m"},
        {"pairs": []},
        {"pairs": "BTC/USDT"},
    ],
)
def test_apply_freqtrade_rejects_invalid_values(values: dict) -> None:
    with pytest.raises(HTTPException) as caught:
        _apply_freqtrade(FreqtradeSettings(), values)

    assert caught.value.status_code == 400
