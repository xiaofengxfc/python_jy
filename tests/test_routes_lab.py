"""策略实验室控制器的离线测试：生成候选、审核、优化任务。"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from fastapi import HTTPException

from btrader import llm
from btrader.core.types import MarketType
from btrader.strategy.base import IntParam, Strategy
from btrader.strategy.candidates import CandidateStore
from btrader.strategy.registry import StrategyRegistry
from btrader.web.routes_lab import (
    CandidateBacktestPayload,
    CandidateGeneratePayload,
    CandidateReviewPayload,
    LabController,
    OptimizePayload,
)

SAFE_CODE = '''
from btrader.strategy.base import Strategy, IntParam

class GenStrategy(Strategy):
    key = "gen_strategy"
    name = "生成策略"
    params_spec = [IntParam("period", "周期", 20, 2, 200)]

    def generate(self, df, params):
        ma = df["close"].rolling(params["period"]).mean()
        return df.assign(target=(df["close"] > ma).astype(float))[["target"]]
'''


class ToyStrategy(Strategy):
    key = "toy"
    name = "玩具"
    params_spec = [IntParam("period", "周期", 10, 2, 60)]

    def generate(self, df, params):
        ma = df["close"].rolling(int(params["period"])).mean()
        return pd.DataFrame({"target": (df["close"] > ma).astype(float)})


def make_frame(bars: int = 200) -> pd.DataFrame:
    rng = np.random.default_rng(11)
    close = 100 + np.cumsum(rng.normal(0, 0.5, bars))
    return pd.DataFrame(
        {
            "timestamp": [1_700_000_000_000 + i * 3_600_000 for i in range(bars)],
            "open": close,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": np.full(bars, 10.0),
        }
    )


class FakeLLM:
    def __init__(
        self, payload: dict | None = None, text: str | None = None, *, truncated: bool = False
    ) -> None:
        self.payload = payload
        self.text = text
        self.truncated = truncated
        self.calls = 0
        self.kwargs: dict = {}
        self.settings = SimpleNamespace(model="fake-model")

    def ask(self, prompt: str, **kwargs) -> dict:
        self.calls += 1
        self.kwargs = kwargs
        return {
            "ok": True,
            "text": self.text or json.dumps(self.payload),
            "tokens": 5,
            "finish": "length" if self.truncated else "stop",
            "truncated": self.truncated,
        }


class FakeStrategies:
    def __init__(self, directory: Path | None = None) -> None:
        self.registry = StrategyRegistry()
        self.registry.discover([directory] if directory else None)

    def get(self, key: str) -> Strategy:
        if key == "toy":
            return ToyStrategy()
        return self.registry.get(key)

    def source_path(self, key: str):
        return self.registry.source_path(key)

    def is_builtin(self, key: str) -> bool:
        return self.registry.is_builtin(key)

    def is_overridden(self, key: str) -> bool:
        return self.registry.is_overridden(key)

    def discover(self, directories: list[Path] | None = None) -> None:
        self.registry.discover(directories)

    def __contains__(self, key: str) -> bool:
        return key in self.registry

    @property
    def keys(self) -> list[str]:
        return self.registry.keys


class FakeKlines:
    def fetch(self, market, symbol, timeframe, limit=1000):
        return make_frame()


def make_controller(
    payload: dict | None,
    tmp_path: Path,
    *,
    text: str | None = None,
    enabled: bool = True,
    truncated: bool = False,
) -> LabController:
    strategy_dir = tmp_path / "strategies"
    strategy_dir.mkdir(parents=True, exist_ok=True)
    ctx = SimpleNamespace(
        settings=SimpleNamespace(ai=SimpleNamespace(enabled=enabled)),
        ai=FakeLLM(payload, text, truncated=truncated),
        strategies=FakeStrategies(strategy_dir),
        klines=FakeKlines(),
    )
    state = SimpleNamespace(ctx=ctx, locks={MarketType.SPOT: threading.RLock()})
    return LabController(
        lambda: state,
        CandidateStore(tmp_path),
        strategy_dir=strategy_dir,
    )


def test_generate_stores_candidate(tmp_path: Path):
    controller = make_controller(
        {"strategy_key": "gen_strategy", "name": "生成", "code": SAFE_CODE}, tmp_path
    )
    result = controller.generate(CandidateGeneratePayload())
    candidate = result["candidate"]
    assert candidate["status"] == "pending"
    assert candidate["checks"]["ok"] is True
    assert controller.list_candidates()["items"][0]["id"] == candidate["id"]
    detail = controller.get_candidate(candidate["id"])
    # 解析会把首尾空行规整掉，比的是源码本身
    assert detail["code"].strip() == SAFE_CODE.strip()


def test_review_approves_candidate(tmp_path: Path):
    controller = make_controller({"strategy_key": "gen_strategy", "code": SAFE_CODE}, tmp_path)
    candidate_id = controller.generate(CandidateGeneratePayload())["candidate"]["id"]
    result = controller.review(candidate_id, CandidateReviewPayload(approve=True, note="可以"))
    candidate = result["candidate"]
    assert candidate["status"] == "approved"
    assert candidate["registered_key"] == "gen_strategy"
    assert (tmp_path / "strategies" / "gen_strategy.py").exists()
    assert "gen_strategy" in controller.get_state().ctx.strategies


def test_generate_names_prose_without_code(tmp_path: Path):
    """模型只给了说明文字、没给代码：502 要点名这件事，别让用户猜。"""
    controller = make_controller(None, tmp_path, text="## 改进思路\n\n这里只讲道理，没有代码。")

    with pytest.raises(HTTPException) as info:
        controller.generate(CandidateGeneratePayload())

    assert info.value.status_code == 502
    assert "没有策略源码" in info.value.detail
    assert "代码块" in info.value.detail

def test_generate_reports_truncation_with_its_own_message(tmp_path: Path):
    """截断和 JSON 坏了是两种不同的失败，文案必须自己说清是哪一种。"""
    controller = make_controller(
        None,
        tmp_path,
        text='{"strategy_key": "gen_strategy", "code": "from btrader.strategy.base import',
        truncated=True,
    )

    with pytest.raises(HTTPException) as info:
        controller.generate(CandidateGeneratePayload())

    assert info.value.status_code == 502
    assert info.value.detail == "模型输出被截断（上限 8000 tokens），把改进要求写短一点再试"
    assert "设置" not in info.value.detail


def test_generate_asks_for_the_code_budget(tmp_path: Path):
    """写代码走 OUTPUT_TOKENS_CODE，别再退回 2400 那种让 JSON 断掉的值。"""
    controller = make_controller(
        {"strategy_key": "gen_strategy", "code": SAFE_CODE}, tmp_path
    )

    controller.generate(CandidateGeneratePayload())

    assert controller.get_state().ctx.ai.kwargs["max_tokens"] == llm.OUTPUT_TOKENS_CODE
    assert controller.get_state().ctx.ai.kwargs["max_tokens"] == 8000


def test_generate_succeeds_and_writes_the_candidate_to_disk(tmp_path: Path):
    """正常一路走完是 200：返回 ok，代码和元数据都落盘。"""
    controller = make_controller(
        {"strategy_key": "gen_strategy", "name": "生成", "code": SAFE_CODE}, tmp_path
    )

    result = controller.generate(CandidateGeneratePayload())

    assert result["ok"] is True
    candidate_id = result["candidate"]["id"]
    assert result["candidate"]["checks"]["ok"] is True
    assert (tmp_path / f"{candidate_id}.py").read_text(encoding="utf-8").strip() == SAFE_CODE.strip()
    assert (tmp_path / f"{candidate_id}.json").exists()


def test_unknown_candidate_is_404(tmp_path: Path):
    controller = make_controller({"strategy_key": "x", "code": SAFE_CODE}, tmp_path)
    try:
        controller.get_candidate("../nope")
    except HTTPException as exc:
        assert exc.status_code == 404
    else:  # pragma: no cover
        raise AssertionError("expected HTTPException")


def test_ai_disabled_is_reported(tmp_path: Path):
    controller = make_controller(None, tmp_path, enabled=False)
    try:
        controller.start_optimize(OptimizePayload(base_key="toy"))
    except HTTPException as exc:
        assert exc.status_code == 400
    else:  # pragma: no cover
        raise AssertionError("expected HTTPException")


def test_optimize_job_runs_to_completion(tmp_path: Path):
    controller = make_controller(
        {"params": {"period": 20}, "hypothesis": "更长周期", "reason": "更平滑"}, tmp_path
    )
    job_id = controller.start_optimize(
        OptimizePayload(base_key="toy", symbol="BTC/USDT", timeframe="1h", rounds=2)
    )["job_id"]
    deadline = time.time() + 10
    job = {}
    while time.time() < deadline:
        job = controller.get_optimize(job_id)
        if job["status"] in ("done", "failed"):
            break
        time.sleep(0.05)
    assert job["status"] == "done", job
    assert len(job["result"]["rounds"]) == 2
    assert job["result"]["best"]["period"] in (10, 20)


def test_unknown_job_is_404(tmp_path: Path):
    controller = make_controller({"params": {}}, tmp_path)
    try:
        controller.get_optimize("nope")
    except HTTPException as exc:
        assert exc.status_code == 404
    else:  # pragma: no cover
        raise AssertionError("expected HTTPException")


def test_record_candidate_backtest(tmp_path: Path):
    controller = make_controller({"params": {}}, tmp_path)
    candidate = controller.candidates.create(
        SAFE_CODE, strategy_key="gen_strategy", name="生成策略"
    )
    result = controller.record_backtest(
        candidate.id,
        CandidateBacktestPayload(
            metrics={"trades": 4, "total_return": 0.12, "max_drawdown": 0.05},
            symbol="BTC/USDT",
            timeframe="1h",
            source="btrader",
        ),
    )
    stored = result["candidate"]["backtest"]
    assert stored["trades"] == 4
    assert stored["total_return"] == 0.12
    assert stored["symbol"] == "BTC/USDT"
    assert stored["source"] == "btrader"


# ------------------------------------------------- replies that are prose + fences
# 模型最常见的样子：说明文字 + 一个 python 代码块 + 尾部参数表格，全文没有 JSON。
# 这条按审计里那条真实回答的形状（截了一段）拼出来，旧代码在这里直接 502。
PROSE_HEAD = """## 改进思路

带宽明显发散时才允许开新仓，平盘不交易。下面是改进后的策略代码。

```python
"""

PROSE_TAIL = """
```

| 参数 | 说明 |
| --- | --- |
| period | 均线周期 |
"""


def prose_with_code() -> str:
    return PROSE_HEAD + SAFE_CODE + PROSE_TAIL


def test_generate_accepts_a_complete_code_block_even_when_truncated(tmp_path: Path):
    """被截断的是后面的散文，代码整段在——这种就该收下，不必让用户重试。"""
    controller = make_controller(None, tmp_path, text=prose_with_code(), truncated=True)

    result = controller.generate(CandidateGeneratePayload())

    candidate = result["candidate"]
    assert result["ok"] is True
    assert candidate["strategy_key"] == "gen_strategy"
    assert candidate["checks"]["ok"] is True
    assert "python 代码块" in candidate["note"]
    stored = controller.get_candidate(candidate["id"])
    assert stored["code"].strip() == SAFE_CODE.strip()


def test_generate_renames_a_key_that_would_shadow_a_builtin(tmp_path: Path):
    """模型会照抄 key = "bollinger"。核准后它会把内置策略顶掉，所以先改名。"""
    code = SAFE_CODE.replace('key = "gen_strategy"', 'key = "bollinger"')
    controller = make_controller({"strategy_key": "bollinger", "code": code}, tmp_path)

    result = controller.generate(CandidateGeneratePayload())

    candidate = result["candidate"]
    assert candidate["strategy_key"] == "bollinger_ai"
    assert "已被占用" in candidate["note"]
    assert candidate["checks"]["ok"] is True


def test_two_generates_at_once_do_not_collide_on_the_candidate_file(tmp_path: Path):
    """手快连点两次「生成」：同一秒、同一个 key 会算出同一个候选 id。

    快照和落盘之间的窗口里，后一个请求把前一个的 ``cand-<秒>-<key>.py`` 顶掉，
    Windows 上直接 PermissionError 冒出成 500，前一个候选也没了。两个候选
    必须各自落盘、各自有 key。
    """
    barrier = threading.Barrier(2)

    class RacingLLM:
        settings = SimpleNamespace(model="fake-model")

        def ask(self, prompt: str, **kwargs) -> dict:
            barrier.wait(5.0)
            return {
                "ok": True,
                "text": json.dumps({"strategy_key": "gen_strategy", "code": SAFE_CODE}),
                "tokens": 5,
                "finish": "stop",
                "truncated": False,
            }

    controller = make_controller({"strategy_key": "gen_strategy", "code": SAFE_CODE}, tmp_path)
    controller.get_state().ctx.ai = RacingLLM()

    results: list[dict] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            results.append(controller.generate(CandidateGeneratePayload())["candidate"])
        except BaseException as exc:  # 让线程里的异常在断言里现身
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(results) == 2
    assert len({item["id"] for item in results}) == 2
    assert len({item["strategy_key"] for item in results}) == 2
    assert len(list(tmp_path.glob("cand-*.json"))) == 2
    assert len(list(tmp_path.glob("cand-*.py"))) == 2


def test_generate_tells_the_model_which_keys_are_taken(tmp_path: Path):
    """提示词里念一遍现有 key，比让模型猜要省一次重试。"""
    controller = make_controller({"strategy_key": "gen_strategy", "code": SAFE_CODE}, tmp_path)

    controller.generate(CandidateGeneratePayload())

    system = controller.get_state().ctx.ai.kwargs["system"]
    assert "bollinger" in system
    assert "不许和已有策略重复" in system
