"""Strategy registry, parameter validation and signal generation."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from btrader.strategy import StrategyRegistry
from btrader.strategy.base import Strategy, clamp_target
from btrader.strategy.builtin import (
    BollingerStrategy,
    DonchianBreakoutStrategy,
    MaCrossStrategy,
    RsiReversionStrategy,
)

from helpers import make_candles


def test_registry_loads_builtin():
    reg = StrategyRegistry()
    reg.discover()
    assert len(reg) == 4
    assert "ma_cross" in reg
    assert reg.errors == []
    assert isinstance(reg.get("ma_cross"), MaCrossStrategy)


def test_registry_loads_user_folder(tmp_path):
    plugin = tmp_path / "my_strategy.py"
    plugin.write_text(
        "from btrader.strategy.base import Strategy, IntParam\n"
        "import pandas as pd\n"
        "class MyStrategy(Strategy):\n"
        "    key = 'user_test'\n"
        "    name = 'User Test'\n"
        "    params_spec = [IntParam('n', 'n', 5, 1, 50)]\n"
        "    def generate(self, df, params):\n"
        "        return pd.DataFrame({'target': [1.0] * len(df)}, index=df.index)\n",
        encoding="utf-8",
    )
    reg = StrategyRegistry()
    reg.discover([tmp_path])
    assert "user_test" in reg
    assert len(reg.get("user_test").params_spec) == 1


def test_registry_reports_broken_plugin(tmp_path):
    bad = tmp_path / "broken.py"
    bad.write_text("this is not python", encoding="utf-8")
    reg = StrategyRegistry()
    reg.discover([tmp_path])
    assert reg.errors
    assert "broken.py" in reg.errors[0]


def test_registry_unknown_key():
    reg = StrategyRegistry()
    reg.discover()
    with pytest.raises(KeyError):
        reg.get("nope")


@pytest.mark.parametrize(
    "strategy",
    [MaCrossStrategy(), RsiReversionStrategy(), BollingerStrategy(), DonchianBreakoutStrategy()],
)
def test_builtin_strategies_generate_valid_targets(strategy):
    df = make_candles(400)
    out = strategy.generate(df, strategy.default_params())
    assert "target" in out
    assert len(out) == len(df)
    assert out["target"].between(-1, 1).all()
    assert out["target"].isin([-1.0, 0.0, 1.0]).all()
    assert out.index.equals(df.index)


@pytest.mark.parametrize(
    "strategy",
    [MaCrossStrategy(), RsiReversionStrategy(), BollingerStrategy(), DonchianBreakoutStrategy()],
)
def test_strategies_trade_somewhere(strategy):
    df = make_candles(800, volatility=0.02, seed=4)
    out = strategy.generate(df, strategy.default_params())
    assert (out["target"] != 0).sum() > 0


def test_param_validation_clamps():
    strategy = MaCrossStrategy()
    clean = strategy.validate_params({"fast": -5, "slow": 9999, "trend": 0, "ema": 1})
    assert clean["fast"] == 2
    assert clean["slow"] == 500
    assert clean["ema"] is True


def test_param_validation_defaults():
    strategy = RsiReversionStrategy()
    clean = strategy.validate_params({})
    assert clean["period"] == 14
    assert clean["oversold"] == 30


def test_clamp_target():
    assert clamp_target(5, True) == 1.0
    assert clamp_target(-5, True) == -1.0
    assert clamp_target(-0.5, False) == 0.0
    assert clamp_target(float("nan"), True) == 0.0
    assert clamp_target("bad", True) == 0.0


def test_plot_series_lengths():
    df = make_candles(300)
    for strategy in (MaCrossStrategy(), BollingerStrategy(), DonchianBreakoutStrategy()):
        series = strategy.plot_series(df, strategy.default_params())
        for line in series.values():
            assert len(line) == len(df)
