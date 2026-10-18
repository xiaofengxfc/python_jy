"""Donchian channel breakout trend following."""

from __future__ import annotations

import pandas as pd

from ..base import BoolParam, IntParam, Strategy


class DonchianBreakoutStrategy(Strategy):
    key = "donchian_breakout"
    name = "唐奇安通道突破"
    description = "突破 N 周期最高价做多，跌破 M 周期最低价做空，反向通道离场。"
    params_spec = [
        IntParam("entry", "入场通道周期", 20, 2, 500),
        IntParam("exit", "离场通道周期", 10, 1, 500),
        BoolParam("ema_filter", "用 EMA(200) 过滤方向", False),
    ]

    def generate(self, df: pd.DataFrame, params: dict) -> pd.DataFrame:
        entry = int(params["entry"])
        exit_n = int(params["exit"])
        high = df["high"]
        low = df["low"]
        upper = high.rolling(entry, min_periods=entry).max().shift(1)
        lower = low.rolling(entry, min_periods=entry).min().shift(1)
        exit_low = low.rolling(exit_n, min_periods=exit_n).min().shift(1)
        exit_high = high.rolling(exit_n, min_periods=exit_n).max().shift(1)
        ema200 = df["close"].ewm(span=200, adjust=False).mean() if params.get("ema_filter") else None

        close = df["close"].to_numpy()
        up, lo = upper.to_numpy(), lower.to_numpy()
        ex_lo, ex_hi = exit_low.to_numpy(), exit_high.to_numpy()
        trend = ema200.to_numpy() if ema200 is not None else None

        positions = [0.0] * len(close)
        state = 0.0
        for i in range(len(close)):
            if pd.isna(up[i]) or pd.isna(ex_lo[i]):
                positions[i] = 0.0
                continue
            if state == 0.0:
                long_ok = trend is None or close[i] > trend[i]
                short_ok = trend is None or close[i] < trend[i]
                if close[i] > up[i] and long_ok:
                    state = 1.0
                elif close[i] < lo[i] and short_ok and self.allow_short:
                    state = -1.0
            elif state > 0 and close[i] < ex_lo[i]:
                state = 0.0
            elif state < 0 and close[i] > ex_hi[i]:
                state = 0.0
            positions[i] = state

        out = pd.DataFrame({"target": positions}, index=df.index)
        out["upper"] = upper
        out["lower"] = lower
        return out

    def plot_series(self, df, params):
        entry = int(params["entry"])
        exit_n = int(params["exit"])
        return {
            "入场上轨": df["high"].rolling(entry, min_periods=entry).max().shift(1),
            "入场下轨": df["low"].rolling(entry, min_periods=entry).min().shift(1),
            "离场上轨": df["high"].rolling(exit_n, min_periods=exit_n).max().shift(1),
            "离场下轨": df["low"].rolling(exit_n, min_periods=exit_n).min().shift(1),
        }
