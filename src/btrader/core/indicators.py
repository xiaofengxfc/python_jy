"""Technical indicators implemented with pandas/numpy.

All functions return pandas Series aligned with the input index.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period, min_periods=period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.fillna(100.0).where(avg_loss.notna(), np.nan)


def bollinger(series: pd.Series, period: int = 20, std: float = 2.0):
    mid = sma(series, period)
    dev = series.rolling(period, min_periods=period).std(ddof=0)
    return mid - std * dev, mid, mid + std * dev


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    fast_ema = ema(series, fast)
    slow_ema = ema(series, slow)
    line = fast_ema - slow_ema
    sig = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return line, sig, line - sig


def stoch(df: pd.DataFrame, period: int = 14, smooth: int = 3):
    low_min = df["low"].rolling(period, min_periods=period).min()
    high_max = df["high"].rolling(period, min_periods=period).max()
    rng = (high_max - low_min).replace(0.0, np.nan)
    k = (df["close"] - low_min) / rng * 100
    return k.rolling(smooth, min_periods=smooth).mean()


def vwap(df: pd.DataFrame, period: int | None = None) -> pd.Series:
    typical = (df["high"] + df["low"] + df["close"]) / 3
    pv = typical * df["volume"]
    if period:
        return pv.rolling(period, min_periods=period).sum() / df["volume"].rolling(
            period, min_periods=period
        ).sum()
    return pv.cumsum() / df["volume"].cumsum()


def rolling_high(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period, min_periods=period).max()


def rolling_low(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period, min_periods=period).min()
