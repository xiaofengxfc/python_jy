"""统一的「今天」：本机时区的一天，不是 UTC 的一天。

程序里有三处「当日」计数：风控熔断的当日亏损（``engine/risk.py``）、AI 决策预算
（``engine/ai_trader.TradeBudget``）、模型调用次数（``llm.py`` 的 ``calls_today``）。
以前风控按 UTC 算，另外两处按本机时区：北京时间（UTC+8）下 UTC 的一天在早上 8 点结束，
于是「当日亏损」熔断早上 8 点就清零，而决策次数要到午夜才清零——同一份审计里两个
「今日」根本不是同一天。统一到本机时区：界面上写的「今日」就是用户日历上的今天。
"""

from __future__ import annotations

import time


def local_day(now: float | None = None) -> str:
    """本机时区的日期字符串（``YYYY-MM-DD``）；``now`` 是 Unix 秒。"""
    stamp = time.time() if now is None else now
    return time.strftime("%Y-%m-%d", time.localtime(stamp))
