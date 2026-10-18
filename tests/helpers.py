"""Synthetic OHLCV generator shared by tests (no network access)."""

from __future__ import annotations

import numpy as np
import pandas as pd


def make_candles(
    n: int = 500,
    start: int = 1_700_000_000_000,
    step_ms: int = 3_600_000,
    price: float = 100.0,
    drift: float = 0.0005,
    volatility: float = 0.01,
    seed: int = 7,
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    returns = drift + volatility * rng.standard_normal(n)
    close = price * np.exp(np.cumsum(returns))
    open_ = np.concatenate([[price], close[:-1]])
    high = np.maximum(open_, close) * (1 + np.abs(rng.standard_normal(n)) * volatility * 0.5)
    low = np.minimum(open_, close) * (1 - np.abs(rng.standard_normal(n)) * volatility * 0.5)
    volume = rng.uniform(10, 100, n)
    return pd.DataFrame(
        {
            "timestamp": start + np.arange(n) * step_ms,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        }
    )
