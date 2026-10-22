"""Backtester accounting and accounting invariants."""

from __future__ import annotations

import numpy as np
import pandas as pd

from btrader.backtest import BacktestConfig, expand_grid, grid_search, run_backtest
from btrader.strategy.base import Strategy
from btrader.strategy.builtin import DonchianBreakoutStrategy, MaCrossStrategy

from helpers import make_candles


class AlwaysLong(Strategy):
    key = "always_long"
    name = "always long"
    allow_short = False

    def generate(self, df, params):
        return pd.DataFrame({"target": np.ones(len(df))}, index=df.index)


class Toggle(Strategy):
    """Long for the first half, flat for the second."""

    key = "toggle"
    name = "toggle"

    def generate(self, df, params):
        target = np.zeros(len(df))
        target[: len(df) // 2] = 1.0
        return pd.DataFrame({"target": target}, index=df.index)


def test_config_deploy():
    cfg = BacktestConfig(position_pct=0.5, leverage=3.0)
    assert cfg.deploy == 1.5


def test_no_lookahead_and_fill_on_next_open():
    df = make_candles(20, drift=0.0, volatility=0.0, price=100.0)
    result = run_backtest(df, AlwaysLong(), {}, BacktestConfig(fee_rate=0.0, slippage_bps=0.0))
    # Signal at bar 0 close -> entry at bar 1 open.
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.entry_time == int(df["timestamp"].iloc[1])
    assert trade.entry_price == 100.0


def test_accounting_identity_with_fees():
    df = make_candles(300, seed=3)
    cfg = BacktestConfig(initial_capital=10_000, fee_rate=0.001, slippage_bps=2)
    result = run_backtest(df, MaCrossStrategy(), {"fast": 5, "slow": 20, "trend": 0, "ema": True}, cfg)
    # final equity must equal initial + sum(trade pnl) (fees included in pnl)
    total_pnl = sum(t.pnl for t in result.trades)
    assert abs(result.metrics["final_equity"] - (cfg.initial_capital + total_pnl)) < 1e-6


def test_flat_strategy_keeps_capital():
    df = make_candles(100)

    class Flat(Strategy):
        key = "flat"
        name = "flat"

        def generate(self, df, params):
            return pd.DataFrame({"target": np.zeros(len(df))}, index=df.index)

    result = run_backtest(df, Flat(), {}, BacktestConfig(initial_capital=5_000))
    assert result.metrics["final_equity"] == 5_000
    assert result.trades == []
    assert result.metrics["max_drawdown"] == 0.0


def test_long_profits_in_rising_market():
    df = make_candles(200, drift=0.003, volatility=0.002, seed=11)
    result = run_backtest(df, AlwaysLong(), {}, BacktestConfig(fee_rate=0, slippage_bps=0))
    assert result.metrics["total_return"] > 0
    assert result.metrics["exposure"] > 0.9


def test_stop_loss_triggers():
    df = make_candles(50, drift=-0.01, volatility=0.005, seed=5)
    cfg = BacktestConfig(fee_rate=0.0, slippage_bps=0.0, stop_loss_pct=0.02)
    result = run_backtest(df, AlwaysLong(), {}, cfg)
    assert result.trades
    assert result.trades[0].exit_reason == "stop_loss"
    assert result.trades[0].pnl_pct <= -0.019


def test_short_disabled_for_spot():
    df = make_candles(200, drift=-0.003, volatility=0.002, seed=2)
    strategy = DonchianBreakoutStrategy()
    cfg = BacktestConfig(allow_short=False, fee_rate=0)
    result = run_backtest(df, strategy, strategy.default_params(), cfg)
    assert all(t.side == "long" for t in result.trades)
    assert result.metrics["exposure"] >= 0


def test_expand_grid():
    combos = expand_grid({"a": [1, 2], "b": ["x", "y"]})
    assert len(combos) == 4
    assert {"a": 1, "b": "x"} in combos
    assert expand_grid({}) == [{}]


def test_grid_search_sorted_by_metric():
    df = make_candles(400, seed=8)
    strategy = MaCrossStrategy()
    grid = {"fast": [3, 5], "slow": [10, 20], "trend": [0], "ema": [True]}
    frame = grid_search(df, strategy, grid, BacktestConfig(fee_rate=0), metric="sharpe")
    assert not frame.empty
    assert len(frame) == 4
    sharpes = frame["m_sharpe"].to_list()
    assert sharpes == sorted(sharpes, reverse=True)

def test_a_partial_scale_out_keeps_the_remaining_cost_basis():
    """半仓平掉之后，剩下那半的成本价不能跟着成交价漂。

    部分平仓时如果把成交价按数量加权进 entry_price，已经落袋的那部分盈亏会被
    算第二次：所有 trade.pnl 之和不再等于权益变化，avg_trade / profit_factor 也跟着失真。
    """

    class ScaleOut(Strategy):
        key = "scale_out"
        name = "scale out"
        allow_short = False

        def generate(self, df, params):
            targets = np.zeros(len(df))
            targets[2:6] = 1.0
            targets[6:] = 0.5
            return pd.DataFrame({"target": targets}, index=df.index)

    prices = 100.0 + np.arange(12, dtype=float)
    df = pd.DataFrame(
        {
            "timestamp": np.arange(12, dtype=np.int64) * 3_600_000,
            "open": prices,
            "high": prices,
            "low": prices,
            "close": prices,
            "volume": np.ones(12),
        }
    )
    cfg = BacktestConfig(initial_capital=10_000.0, fee_rate=0.0, slippage_bps=0.0)
    result = run_backtest(df, ScaleOut(), {}, cfg)

    assert len(result.trades) == 2
    # 减仓不减成本：两笔交易的开仓价必须是同一个数。
    assert abs(result.trades[1].entry_price - result.trades[0].entry_price) < 1e-9
    total_pnl = sum(t.pnl for t in result.trades)
    assert abs(result.metrics["final_equity"] - (cfg.initial_capital + total_pnl)) < 1e-6
