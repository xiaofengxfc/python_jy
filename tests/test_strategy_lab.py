"""Phase 4-A 离线测试：静态检查、候选存储、多轮回测反思循环。"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from btrader.ai_optimize import optimize_params
from btrader.backtest import BacktestConfig
from btrader.strategy.base import IntParam, Strategy
from btrader.strategy.candidates import CandidateError, CandidateStore
from btrader.strategy.sandbox import check_code

SAFE_CODE = '''
from btrader.strategy.base import Strategy, IntParam

class MyStrategy(Strategy):
    key = "my_strategy"
    name = "我的策略"
    params_spec = [IntParam("period", "周期", 20, 2, 200)]

    def generate(self, df, params):
        ma = df["close"].rolling(params["period"]).mean()
        return df.assign(target=(df["close"] > ma).astype(float))[["target"]]
'''


def make_frame(bars: int = 240) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    close = 100 + np.cumsum(rng.normal(0, 0.6, bars))
    return pd.DataFrame(
        {
            "timestamp": [1_700_000_000_000 + i * 900_000 for i in range(bars)],
            "open": close,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": np.full(bars, 10.0),
        }
    )


class ToyStrategy(Strategy):
    key = "toy"
    name = "玩具策略"
    params_spec = [IntParam("period", "周期", 10, 2, 60)]

    def generate(self, df, params):
        ma = df["close"].rolling(int(params["period"])).mean()
        return pd.DataFrame({"target": (df["close"] > ma).astype(float)})


class FakeLLM:
    def __init__(self, answers: list[str], ok: bool = True) -> None:
        self.answers = answers
        self.ok = ok
        self.calls = 0

    def ask(self, prompt: str, **kwargs) -> dict:
        text = self.answers[min(self.calls, len(self.answers) - 1)]
        self.calls += 1
        if not self.ok:
            return {"ok": False, "error": "超时"}
        return {"ok": True, "text": text, "tokens": 12}


# ------------------------------------------------------------------ sandbox
def test_safe_candidate_passes():
    report = check_code(SAFE_CODE)
    assert report.ok, report.issues
    assert "MyStrategy" in report.classes


def test_banned_import_is_rejected():
    report = check_code("import os\n" + SAFE_CODE)
    assert not report.ok
    assert any("os" in issue.message for issue in report.issues)


def test_banned_call_and_dunder_are_rejected():
    code = SAFE_CODE.replace(
        'ma = df["close"].rolling(params["period"]).mean()',
        'ma = eval("1+1")',
    )
    report = check_code(code)
    assert not report.ok
    assert any("eval" in issue.message for issue in report.issues)


def test_syntax_error_and_missing_class_are_rejected():
    assert not check_code("def broken(:").ok
    assert not check_code("x = 1").ok


def test_oversized_code_is_rejected():
    assert not check_code("x = 1\n" * 20_000).ok


# --------------------------------------------------------------- candidates
def test_candidate_create_and_review(tmp_path: Path):
    store = CandidateStore(tmp_path)
    candidate = store.create(
        SAFE_CODE, strategy_key="my_strategy", name="我的策略", model="fake"
    )
    assert candidate.status == "pending"
    assert store.get(candidate.id).source_sha256 == candidate.source_sha256
    assert store.read_code(candidate.id) == SAFE_CODE

    approved = store.review(candidate.id, approve=True, note="ok", reviewer="lead")
    assert approved.status == "approved"
    assert store.list()[0].status == "approved"


def test_bad_candidate_is_auto_rejected_and_cannot_be_approved(tmp_path: Path):
    store = CandidateStore(tmp_path)
    candidate = store.create("import os\n" + SAFE_CODE, strategy_key="bad")
    assert candidate.status == "rejected"
    with pytest.raises(CandidateError):
        store.review(candidate.id, approve=True)


def test_candidate_path_traversal_is_rejected(tmp_path: Path):
    store = CandidateStore(tmp_path)
    with pytest.raises(CandidateError):
        store.get("../../etc/passwd")


def test_record_backtest_persists_metrics(tmp_path: Path):
    store = CandidateStore(tmp_path)
    candidate = store.create(SAFE_CODE, strategy_key="my_strategy")
    store.record_backtest(candidate.id, {"sharpe": 1.2, "total_return": 0.1})
    assert store.get(candidate.id).backtest["sharpe"] == pytest.approx(1.2)


# ----------------------------------------------------------------- optimize
def test_optimize_runs_rounds_and_returns_best():
    frame = make_frame()
    config = BacktestConfig(timeframe="15m", initial_capital=10_000.0)
    llm = FakeLLM(
        [
            json.dumps({"params": {"period": 20}, "hypothesis": "更长均线", "reason": "降噪"}),
            json.dumps({"params": {"period": 30}, "hypothesis": "更稳", "reason": "趋势"}),
        ]
    )
    result = optimize_params(
        llm, ToyStrategy(), frame, {"period": 10}, config,
        bounds={"period": [2, 60]}, rounds=2, metric="sharpe",
    )
    assert result["ok"] is True
    assert len(result["rounds"]) == 2
    assert result["calls"] == 2
    assert result["stopped"] == "rounds"
    assert result["best"]["period"] in (10, 20, 30)


def test_optimize_stops_on_budget_before_extra_calls():
    frame = make_frame()
    config = BacktestConfig(timeframe="15m")
    llm = FakeLLM([json.dumps({"params": {"period": 20}})])
    result = optimize_params(
        llm, ToyStrategy(), frame, {"period": 10}, config,
        rounds=5, max_calls=1,
    )
    assert result["calls"] == 1
    assert result["stopped"] == "budget"


def test_optimize_survives_garbage_and_model_failure():
    frame = make_frame()
    config = BacktestConfig(timeframe="15m")
    garbage = FakeLLM(["我不想输出 JSON", "还是不想"])
    result = optimize_params(garbage, ToyStrategy(), frame, {"period": 10}, config, rounds=3)
    assert result["ok"] is True
    assert result["stopped"] == "no_improvement"
    assert all(not row["accepted"] for row in result["rounds"])

    failing = FakeLLM(["{}"], ok=False)
    result = optimize_params(failing, ToyStrategy(), frame, {"period": 10}, config, rounds=3)
    assert result["stopped"] == "no_improvement"
    assert result["calls"] == 2


def test_optimize_respects_cancel():
    frame = make_frame()
    config = BacktestConfig(timeframe="15m")
    llm = FakeLLM([json.dumps({"params": {"period": 20}})])
    result = optimize_params(
        llm, ToyStrategy(), frame, {"period": 10}, config,
        rounds=5, should_stop=lambda: True,
    )
    assert result["stopped"] == "cancel"
    assert result["calls"] == 0
