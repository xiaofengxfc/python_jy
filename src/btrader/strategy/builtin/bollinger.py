"""Bollinger band mean reversion."""

from __future__ import annotations

import pandas as pd

from ..base import FloatParam, IntParam, Strategy
from ...core import indicators as ta


class BollingerStrategy(Strategy):
    key = "bollinger"
    name = "布林带回归"
    description = "价格触及下轨做多、触及上轨做空，回到中轨离场。"
    params_spec = [
        IntParam("period", "周期", 20, 5, 200),
        FloatParam("std", "标准差倍数", 2.0, 0.5, 5.0, 0.1),
    ]

    def generate(self, df: pd.DataFrame, params: dict) -> pd.DataFrame:
        period = int(params["period"])
        std = float(params["std"])
        lower, mid, upper = ta.bollinger(df["close"], period, std)

        close = df["close"].to_numpy()
        lo, mi, up = lower.to_numpy(), mid.to_numpy(), upper.to_numpy()
        positions = [0.0] * len(close)
        state = 0.0
        for i in range(len(close)):
            if pd.isna(mi[i]):
                positions[i] = 0.0
                continue
            if state == 0.0:
                if close[i] <= lo[i]:
                    state = 1.0
                elif close[i] >= up[i] and self.allow_short:
                    state = -1.0
            elif state > 0 and close[i] >= mi[i]:
                state = 0.0
            elif state < 0 and close[i] <= mi[i]:
                state = 0.0
            positions[i] = state

        out = pd.DataFrame({"target": positions}, index=df.index)
        out["lower"] = lower
        out["mid"] = mid
        out["upper"] = upper
        return out

    def plot_series(self, df, params):
        lower, mid, upper = ta.bollinger(df["close"], int(params["period"]), float(params["std"]))
        return {"下轨": lower, "中轨": mid, "上轨": upper}
