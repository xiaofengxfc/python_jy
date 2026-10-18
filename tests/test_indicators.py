"""Indicator sanity checks against hand-computed values."""

from __future__ import annotations

import numpy as np
import pandas as pd

from btrader.core import indicators as ta


def test_sma_and_ema():
    series = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    sma = ta.sma(series, 3)
    assert np.isnan(sma.iloc[1])
    assert sma.iloc[2] == 2.0
    assert sma.iloc[4] == 4.0

    ema = ta.ema(series, 3)
    assert np.isnan(ema.iloc[1])
    assert ema.iloc[-1] > ema.iloc[2]


def test_rsi_bounds_and_extremes():
    rising = pd.Series(np.arange(1, 40, dtype=float))
    values = ta.rsi(rising, 14).dropna()
    assert (values <= 100).all()
    assert values.iloc[-1] == 100.0

    falling = pd.Series(np.arange(40, 1, -1, dtype=float))
    assert ta.rsi(falling, 14).dropna().iloc[-1] == 0.0


def test_bollinger_band_ordering():
    series = pd.Series(np.sin(np.linspace(0, 20, 100)) + 10)
    lower, mid, upper = ta.bollinger(series, 20, 2.0)
    valid = mid.notna()
    assert (lower[valid] < mid[valid]).all()
    assert (mid[valid] < upper[valid]).all()


def test_atr_positive(candles):
    values = ta.atr(candles, 14).dropna()
    assert (values > 0).all()


def test_macd_histogram_identity(candles):
    line, signal, hist = ta.macd(candles["close"])
    valid = hist.notna()
    assert np.allclose((line - signal)[valid], hist[valid])
