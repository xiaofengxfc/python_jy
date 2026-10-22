"""策略信号：拿人造 K 线断言 target 序列。

这批测试不看「有没有信号」，而是看「信号必须落在哪一根上」：内置策略的
target 一旦被判错，回测和自动交易会照着错的方向下单，所以每一条都写死
一段能手算出答案的行情。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from btrader.strategy.builtin import (
    BollingerStrategy,
    DonchianBreakoutStrategy,
    MaCrossStrategy,
    RsiReversionStrategy,
)


def _frame(closes: list[float]) -> pd.DataFrame:
    """收盘价序列造 K 线；high/low 等于收盘价，不用高低点的地方不受影响。"""
    close = np.asarray(closes, dtype=float)
    n = len(close)
    return pd.DataFrame(
        {
            "timestamp": np.arange(n, dtype=np.int64) * 3_600_000,
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": np.ones(n),
        }
    )


def _targets(strategy, closes: list[float], params: dict) -> list[float]:
    return strategy.generate(_frame(closes), params)["target"].tolist()


def test_bollinger_gives_no_signal_when_the_band_collapses_on_a_flat_market() -> None:
    """横盘：上下轨和中轨挤成一个价，不能逐根开仓又平仓。

    标准差为 0 时「跌破下轨」(close <= lower) 和「回到中轨」(close >= mid)
    在同一个价位上同时成立，于是从第 period 根开始每根都开一次、下一根就平掉，
    800 根的回测里能刷出几百个来回，横盘不亏钱的地方全靠手续费亏。
    """
    targets = _targets(BollingerStrategy(), [100.0] * 40, {"period": 5, "std": 2.0})

    assert targets == [0.0] * 40


def test_bollinger_enters_on_the_touch_and_leaves_at_the_middle_band() -> None:
    """一段横盘之后一根下探：只在下探那根开仓，回到中轨那根离场。

    period=5、2 倍标准差：[10,10,10,10,10] 的轨道宽度是 0（第 5 根不算信号），
    下探到 8 的那根 mid=9.6、std=0.8、下轨正好 8，靠等号算「触及」；
    下一根 close=10 >= mid=9.6 离场。
    """
    closes = [10.0, 10.0, 10.0, 10.0, 10.0, 8.0, 10.0, 10.0, 10.0, 10.0]

    targets = _targets(BollingerStrategy(), closes, {"period": 5, "std": 2.0})

    assert targets == [0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0]


def test_ma_cross_marks_the_bars_where_the_fast_average_is_on_each_side() -> None:
    """快线 2、慢线 3、用 SMA：[1,2,3,4,5,4,3,2,1,0]。

    SMA2 = 1.5,2.5,3.5,4.5,4.5,3.5,2.5,1.5,0.5；SMA3 = 2,3,4,4.33,4,3,2,1。
    前两根没有慢线，target 必须是 0（预热期不出信号）；第 3 根起快线在上做多，
    第 7 根起快线在下做空（交叉策略翻方向不经过 0，这是设计如此）。
    """
    targets = _targets(
        MaCrossStrategy(), [1.0, 2.0, 3.0, 4.0, 5.0, 4.0, 3.0, 2.0, 1.0, 0.0],
        {"fast": 2, "slow": 3, "trend": 0, "ema": False},
    )

    assert targets == [0.0, 0.0, 1.0, 1.0, 1.0, 1.0, -1.0, -1.0, -1.0, -1.0]


def test_donchian_breaks_out_on_the_channel_and_leaves_on_the_shorter_one() -> None:
    """入场通道 3、离场通道 2：[10,10,10,11,12,11,9,8]。

    第 4 根收 11 冲过前 3 根的高点 10 做多；第 7 根收 9 跌破前 2 根的低点 11 离场；
    第 8 根收 8 跌破前 3 根的低点 9 做空。通道都用 shift(1) 的过去值，不看当根。
    """
    targets = _targets(
        DonchianBreakoutStrategy(), [10.0, 10.0, 10.0, 11.0, 12.0, 11.0, 9.0, 8.0],
        {"entry": 3, "exit": 2, "ema_filter": False},
    )

    assert targets == [0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 0.0, -1.0]


def test_rsi_reversion_waits_for_the_first_rsi_value() -> None:
    """RSI 周期 14：前 14 根没有 RSI，第 15 根才有第一个值。

    一路上涨的行情 RSI=100（没有下跌日），算超买，从第一根算得出 RSI 的那根做空。
    """
    strategy = RsiReversionStrategy()
    params = strategy.default_params()

    assert _targets(strategy, [100.0 * 1.01**i for i in range(14)], params) == [0.0] * 14
    assert _targets(strategy, [100.0 * 1.01**i for i in range(15)], params) == [0.0] * 14 + [-1.0]


def test_a_window_longer_than_the_data_emits_no_signal() -> None:
    """周期比 K 线还长：指标全是 NaN，四个策略都必须给 0，不能崩。"""
    closes = [100.0, 101.0, 102.0, 101.0, 100.0, 99.0, 98.0, 99.0, 100.0, 101.0, 102.0, 103.0]
    cases = [
        (MaCrossStrategy(), {"fast": 2, "slow": 500, "trend": 0, "ema": True}),
        (MaCrossStrategy(), {"fast": 2, "slow": 500, "trend": 500, "ema": False}),
        (RsiReversionStrategy(), {"period": 100, "oversold": 30, "overbought": 70, "exit_level": 50}),
        (BollingerStrategy(), {"period": 200, "std": 2.0}),
        (DonchianBreakoutStrategy(), {"entry": 200, "exit": 10, "ema_filter": False}),
    ]

    for strategy, params in cases:
        assert _targets(strategy, closes, params) == [0.0] * len(closes), strategy.key
