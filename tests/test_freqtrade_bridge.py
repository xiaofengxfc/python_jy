"""Offline tests for the Freqtrade sidecar bridge (no venv required)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from btrader.core.config import FreqtradeSettings
from btrader.freqtrade import FreqtradeBridge, FreqtradeError, _map_backtest


def make_bridge(tmp_path: Path) -> FreqtradeBridge:
    settings = FreqtradeSettings(
        python=sys.executable,
        exchange="gate",
        trade_port=18091,
        pairs=["BTC/USDT"],
        strategy="SampleStrategy",
    )
    return FreqtradeBridge(settings, root=tmp_path)


def test_config_is_written_with_local_api(tmp_path: Path):
    bridge = make_bridge(tmp_path)
    path = bridge.write_config()
    config = json.loads(path.read_text(encoding="utf-8"))
    assert config["exchange"]["name"] == "gate"
    assert config["exchange"]["pair_whitelist"] == ["BTC/USDT"]
    assert config["api_server"]["listen_ip_address"] == "127.0.0.1"
    assert config["api_server"]["listen_port"] == 18091
    assert config["dry_run"] is True
    assert config["dataformat_ohlcv"] == "json"
    credentials = json.loads(bridge.credentials_path.read_text(encoding="utf-8"))
    assert config["api_server"]["password"] == credentials["password"]
    assert len(credentials["jwt_secret"]) >= 20


def test_credentials_are_stable(tmp_path: Path):
    bridge = make_bridge(tmp_path)
    first = bridge._credentials()
    second = bridge._credentials()
    assert first == second


def test_strategies_are_listed_and_saved(tmp_path: Path):
    bridge = make_bridge(tmp_path)
    code = """
from freqtrade.strategy import IStrategy

class SampleStrategy(IStrategy):
    timeframe = "1h"
    minimal_roi = {"0": 0.1}
    stoploss = -0.05

    def populate_indicators(self, dataframe, metadata):
        return dataframe
"""
    bridge.save_strategy("SampleStrategy", code)
    items = bridge.strategies()
    assert [item["name"] for item in items] == ["SampleStrategy"]
    assert "SampleStrategy" in items[0]["classes"]
    assert bridge.strategy_source("SampleStrategy") == code.strip() + "\n"


def test_save_strategy_rejects_non_strategy(tmp_path: Path):
    bridge = make_bridge(tmp_path)
    with pytest.raises(FreqtradeError):
        bridge.save_strategy("Bad", "print('hello')\n")


def test_strategy_source_rejects_traversal(tmp_path: Path):
    bridge = make_bridge(tmp_path)
    with pytest.raises(FreqtradeError):
        bridge.strategy_source("../config")


def test_write_ohlcv_uses_freqtrade_filename(tmp_path: Path):
    bridge = make_bridge(tmp_path)
    result = bridge.write_ohlcv(
        "BTC/USDT",
        "1h",
        [
            [1_704_067_200_000, 100.0, 101.0, 99.0, 100.5, 12.0],
            [1_704_070_800_000, 100.5, 102.0, 100.0, 101.5, 15.0],
        ],
    )
    path = tmp_path / "user_data" / "data" / "gate" / "BTC_USDT-1h.json"
    assert result["rows"] == 2
    assert path.exists()
    rows = json.loads(path.read_text(encoding="utf-8"))
    assert rows[0][0] == 1_704_067_200_000
    assert len(rows[0]) == 6


def test_write_ohlcv_rejects_bad_pair(tmp_path: Path):
    bridge = make_bridge(tmp_path)
    with pytest.raises(FreqtradeError):
        bridge.write_ohlcv("BTCUSDT", "1h", [[1, 1, 1, 1, 1, 1]])


def test_map_backtest_to_ui_shape():
    data = {
        "trades": [
            {
                "open_timestamp": 1_700_000_000_000,
                "close_timestamp": 1_700_003_600_000,
                "open_rate": 100.0,
                "close_rate": 110.0,
                "amount": 1.0,
                "profit_abs": 10.0,
                "is_short": False,
                "exit_reason": "roi",
            }
        ],
        "starting_balance": 1000.0,
        "profit_total": 0.01,
        "cagr": 0.1,
        "max_drawdown_account": 0.02,
        "sharpe": 1.5,
        "winrate": 1.0,
        "profit_factor": 2.0,
        "total_trades": 1,
        "total_volume": 200.0,
        "timeframe": "1h",
    }
    result = _map_backtest(data, "SampleStrategy")
    assert result["metrics"]["total_return"] == 0.01
    assert result["metrics"]["trades"] == 1.0
    assert result["equity"][-1]["equity"] == 1010.0
    assert result["trades"][0]["side"] == "buy"
    assert result["trades"][1]["side"] == "sell"
    assert result["source"] == "freqtrade"
