"""Backtesting package."""

from .engine import (
    BacktestConfig,
    BacktestResult,
    TradeRecord,
    compute_metrics,
    expand_grid,
    grid_search,
    run_backtest,
)

__all__ = [
    "BacktestConfig",
    "BacktestResult",
    "TradeRecord",
    "compute_metrics",
    "expand_grid",
    "grid_search",
    "run_backtest",
]
