"""biTrader strategy template.

Copy this file into the strategies directory shown in Settings, then restart the
app (or press "重新加载"). Signal semantics:

    target = 1.0   fully long
    target = 0.0   flat
    target = -1.0  fully short (futures only)

Signals are evaluated on the close and executed at the next open by both the
backtester and the live/paper engine.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from btrader.strategy.base import FloatParam, IntParam, Strategy


class MyFirstStrategy(Strategy):
    key = "my_first_strategy"
    name = "我的第一个策略"
    description = "收盘价高于 N 周期均线做多，低于则空仓，并可在 3 倍 ATR 处离场。"
    allow_short = False

    params_spec = [
        IntParam("period", "均线周期", 30, 5, 300),
        FloatParam("atr_mult", "ATR 止损倍数", 3.0, 0.5, 10.0, 0.1),
        IntParam("atr_period", "ATR 周期", 14, 2, 100),
    ]

    def generate(self, df: pd.DataFrame, params: dict) -> pd.DataFrame:
        period = int(params["period"])
        atr_period = int(params["atr_period"])
        mult = float(params["atr_mult"])

        ma = df["close"].rolling(period, min_periods=period).mean()

        prev_close = df["close"].shift(1)
        tr = pd.concat(
            [
                df["high"] - df["low"],
                (df["high"] - prev_close).abs(),
                (df["low"] - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        atr = tr.ewm(alpha=1 / atr_period, adjust=False, min_periods=atr_period).mean()

        close = df["close"].to_numpy(dtype=float)
        ma_v = ma.to_numpy(dtype=float)
        atr_v = atr.to_numpy(dtype=float)

        targets = np.zeros(len(df), dtype=float)
        state = 0.0
        stop = 0.0
        for i in range(len(df)):
            if np.isnan(ma_v[i]) or np.isnan(atr_v[i]):
                continue
            if state == 0.0:
                if close[i] > ma_v[i]:
                    state = 1.0
                    stop = close[i] - mult * atr_v[i]
            else:
                stop = max(stop, close[i] - mult * atr_v[i])
                if close[i] < ma_v[i] or close[i] < stop:
                    state = 0.0
            targets[i] = state

        return pd.DataFrame({"target": targets, "ma": ma, "atr": atr}, index=df.index)

    def plot_series(self, df: pd.DataFrame, params: dict) -> dict[str, pd.Series]:
        ma = df["close"].rolling(int(params["period"])).mean()
        return {"均线": ma}
