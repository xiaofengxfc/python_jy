"""Moving average crossover with an optional trend filter."""

from __future__ import annotations

import pandas as pd

from ..base import BoolParam, IntParam, Strategy
from ...core import indicators as ta


class MaCrossStrategy(Strategy):
    key = "ma_cross"
    name = "双均线交叉"
    description = "快线上穿慢线做多，下穿做空（可关闭做空）。可选用长周期均线过滤方向。"
    params_spec = [
        IntParam("fast", "快线周期", 9, 2, 200),
        IntParam("slow", "慢线周期", 21, 3, 500),
        IntParam("trend", "趋势过滤周期 (0=关闭)", 0, 0, 500),
        BoolParam("ema", "使用 EMA", True),
    ]

    def generate(self, df: pd.DataFrame, params: dict) -> pd.DataFrame:
        fast_n = int(params["fast"])
        slow_n = int(params["slow"])
        if fast_n > slow_n:
            # 快慢填反了就换过来：两个周期都还是用户填的数，谁也不丢。以前是把
            # fast 静默改成 slow//2，用户填的 9 直接作废。相等时两条均线永远重合，
            # 没有交叉，自然也不出信号。
            fast_n, slow_n = slow_n, fast_n
        avg = ta.ema if params["ema"] else ta.sma
        fast = avg(df["close"], fast_n)
        slow = avg(df["close"], slow_n)

        target = pd.Series(0.0, index=df.index)
        target[fast > slow] = 1.0
        target[fast < slow] = -1.0 if self.allow_short else 0.0

        trend_n = int(params.get("trend", 0) or 0)
        if trend_n > 0:
            trend = ta.sma(df["close"], trend_n)
            target[(df["close"] < trend)] = target[(df["close"] < trend)].where(
                target[(df["close"] < trend)] < 0, 0.0
            )
            target[df["close"] > trend] = target[df["close"] > trend].where(
                target[df["close"] > trend] > 0, 0.0
            )

        target[slow.isna()] = 0.0
        out = pd.DataFrame({"target": target}, index=df.index)
        out["fast"] = fast
        out["slow"] = slow
        if trend_n > 0:
            out["trend"] = ta.sma(df["close"], trend_n)
        return out

    def plot_series(self, df, params):
        fast_n = int(params["fast"])
        slow_n = int(params["slow"])
        if fast_n > slow_n:
            # 和 generate 一样换过来，画出来的线必须是出信号的那条。
            fast_n, slow_n = slow_n, fast_n
        avg = ta.ema if params["ema"] else ta.sma
        return {"快线": avg(df["close"], fast_n), "慢线": avg(df["close"], slow_n)}
