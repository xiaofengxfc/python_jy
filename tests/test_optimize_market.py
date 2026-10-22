"""策略实验室的优化打分要按市场类型收死。

现货不能做空、也不能借杠杆，所以给候选参数打分的回测必须用「只有多头」的配置。
以前 ``start_optimize`` 用 ``BacktestConfig(timeframe=...)``，那是 ``allow_short=True``
的默认值——现货优化按「能做空」给参数打分，和 ``_validate_backtest`` 已经收死的口径
不一致：同一段下跌行情，靠做空吃到的收益会被当成好成绩。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

from btrader.core.types import MarketType
from btrader.strategy.base import Strategy
from btrader.strategy.candidates import CandidateStore
from btrader.strategy.registry import StrategyRegistry
from btrader.web.routes_lab import LabController, OptimizePayload


class ShortOnly(Strategy):
    """只会做空的策略：现货上一笔都不该成交，合约上该赚钱。"""

    key = "toy_short_only"
    name = "只会做空"
    description = "每根 K 线都想要满仓空头"
    params_spec: list = []

    def generate(self, df, params):
        return pd.DataFrame({"target": np.full(len(df), -1.0)}, index=df.index)


class _Strategies:
    def __init__(self) -> None:
        self.registry = StrategyRegistry()
        self.registry.discover(None)
        self.toy = ShortOnly()

    def get(self, key: str):
        return self.toy if key == ShortOnly.key else self.registry.get(key)

    def source_path(self, key: str):
        return self.registry.source_path(key)

    def is_builtin(self, key: str) -> bool:
        return self.registry.is_builtin(key)

    def is_overridden(self, key: str) -> bool:
        return self.registry.is_overridden(key)

    def discover(self, directories=None) -> None:
        self.registry.discover(directories)

    def __contains__(self, key: str) -> bool:
        return key == ShortOnly.key or key in self.registry

    @property
    def keys(self) -> list[str]:
        return [ShortOnly.key, *self.registry.keys]


class _FallingKlines:
    """一路下跌的行情：做空必赚，做多必亏。"""

    def fetch(self, market, symbol, timeframe, limit=1000):
        bars = 300
        close = np.linspace(300.0, 100.0, bars)
        return pd.DataFrame(
            {
                "timestamp": [1_700_000_000_000 + i * 3_600_000 for i in range(bars)],
                "open": close,
                "high": close + 0.2,
                "low": close - 0.2,
                "close": close,
                "volume": np.full(bars, 10.0),
            }
        )


class _FixedLLM:
    """每轮都回同一组参数：不再改善，一轮就停。"""

    def __init__(self) -> None:
        self.settings = SimpleNamespace(model="fake-model")
        self.calls = 0

    def ask(self, prompt: str, **kwargs) -> dict:
        self.calls += 1
        return {
            "ok": True,
            "text": json.dumps({"params": {}, "hypothesis": "不动", "reason": "没什么可调"}),
            "tokens": 3,
            "finish": "stop",
            "truncated": False,
        }


def _lab_controller(tmp_path: Path) -> LabController:
    strategy_dir = tmp_path / "strategies"
    strategy_dir.mkdir(parents=True, exist_ok=True)
    ctx = SimpleNamespace(
        settings=SimpleNamespace(ai=SimpleNamespace(enabled=True)),
        ai=_FixedLLM(),
        strategies=_Strategies(),
        klines=_FallingKlines(),
    )
    # 两个市场都要有锁：缺键会掉进备用行情源，测的就不是自己的行情了
    state = SimpleNamespace(ctx=ctx, locks={market: threading.RLock() for market in MarketType})
    return LabController(lambda: state, CandidateStore(tmp_path), strategy_dir=strategy_dir)


def _wait_job(controller: LabController, job_id: str, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    job: dict = {}
    while time.time() < deadline:
        job = controller.get_optimize(job_id)
        if job.get("status") in ("done", "failed", "cancelled"):
            return job
        time.sleep(0.02)
    raise AssertionError(f"job 没有结束：{job}")


def _run_optimize(tmp_path: Path, market: str) -> dict:
    controller = _lab_controller(tmp_path)
    job_id = controller.start_optimize(
        OptimizePayload(
            base_key=ShortOnly.key,
            market=market,
            symbol="BTC/USDT" if market == "spot" else "BTC/USDT:USDT",
            timeframe="1h",
            rounds=1,
        )
    )["job_id"]
    job = _wait_job(controller, job_id)
    assert job["status"] == "done", job
    return job["result"]


def test_a_spot_optimize_cannot_score_short_profits(tmp_path):
    """现货优化里「只做空」策略必须一分钱都拿不到。

    改前：``BacktestConfig`` 走默认 ``allow_short=True``，同一段下跌行情里这条策略
    是正收益，于是现货优化的排名会把它顶到前面——而现货根本卖不出去，那条曲线
    在真实账户上不存在。改后空头目标被夹成 0，一笔都不成交。
    """
    result = _run_optimize(tmp_path, "spot")
    baseline = result["best_metrics"]

    assert baseline["trades"] == 0
    assert abs(baseline["total_return"]) < 1e-9
    assert abs(baseline["exposure"]) < 1e-9

    # 每一轮的打分也一样：不给做空留任何入口
    for item in result["rounds"]:
        assert item["metrics"]["trades"] == 0, item
        assert abs(item["metrics"]["total_return"]) < 1e-9, item


def test_a_futures_optimize_still_scores_shorts(tmp_path):
    """合约那边照旧：同一条策略该靠做空赚钱，别把口子收过头。"""
    result = _run_optimize(tmp_path, "usdm")
    baseline = result["best_metrics"]

    assert baseline["trades"] > 0
    assert baseline["total_return"] > 0.3  # 300 → 100，做空吃掉大半跌幅
