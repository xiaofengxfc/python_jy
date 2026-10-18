"""RSI mean reversion."""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..base import IntParam, Strategy
from ...core import indicators as ta


class RsiReversionStrategy(Strategy):
    key = "rsi_reversion"
    name = "RSI 超买超卖"
    description = "RSI 低于超卖阈值做多，高于超买阈值做空，回到中位区间离场。"
    params_spec = [
        IntParam("period", "RSI 周期", 14, 2, 100),
        IntParam("oversold", "超卖阈值", 30, 1, 49),
        IntParam("overbought", "超买阈值", 70, 51, 99),
        IntParam("exit_level", "离场中位", 50, 10, 90),
    ]

    def generate(self, df: pd.DataFrame, params: dict) -> pd.DataFrame:
        period = int(params["period"])
        oversold = float(params["oversold"])
        overbought = float(params["overbought"])
        exit_level = float(params["exit_level"])
        if oversold >= overbought:
            oversold, overbought = overbought - 10, overbought

        rsi = ta.rsi(df["close"], period)
        position = 0.0
        values = rsi.to_numpy(dtype=float)
        ts = np.zeros(len(df), dtype=float)
        for i, value in enumerate(values):
            if pd.isna(value):
                ts[i] = 0.0
                continue
            if position == 0.0:
                if value <= oversold:
                    position = 1.0
                elif value >= overbought and self.allow_short:
                    position = -1.0
            elif position > 0 and value >= exit_level:
                position = 0.0
            elif position < 0 and value <= exit_level:
                position = 0.0
            ts[i] = position
        target = pd.Series(ts, index=df.index)
        return pd.DataFrame({"target": target, "rsi": rsi}, index=df.index)
