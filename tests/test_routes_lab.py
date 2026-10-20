"""Phase 4-B 控制器离线测试：候选生成/审核、优化任务。"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
from fastapi import HTTPException

from btrader.core.types import MarketType
from btrader.strategy.base import IntParam, Strategy
from btrader.strategy.candidates import CandidateStore
from btrader.web.routes_lab import (
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
    def __init__(self, payload: dict | None = None, text: str | None = None) -> None:
        self.payload = payload
        self.text = text
        self.calls = 0
        self.settings = SimpleNamespace(model="fake-model")

    def ask(self, prompt: str, **kwargs) -> dict:
        self.calls += 1
        return {"ok": True, "text": self.text or json.dumps(self.payload), "tokens": 5}


class FakeStrategies:
    def get(self, key: str) -> Strategy:
        if key != "toy":
            raise KeyError(key)
        return ToyStrategy()


class FakeKlines:
    def fetch(self, market, symbol, timeframe, limit=1000):
        return make_frame()


def make_controller(payload: dict | None, tmp_path: Path, *, text: str | None = None, enabled=True) -> LabController:
    ctx = SimpleNamespace(
        settings=SimpleNamespace(ai=SimpleNamespace(enabled=enabled)),
        ai=FakeLLM(payload, text),
        strategies=FakeStrategies(),
        klines=FakeKlines(),
    )
    state = SimpleNamespace(ctx=ctx, locks={MarketType.SPOT: threading.RLock()})
    return LabController(lambda: state, CandidateStore(tmp_path))


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
    assert detail["code"] == SAFE_CODE


def test_review_approves_candidate(tmp_path: Path):
    controller = make_controller({"strategy_key": "gen_strategy", "code": SAFE_CODE}, tmp_path)
    candidate_id = controller.generate(CandidateGeneratePayload())["candidate"]["id"]
    result = controller.review(candidate_id, CandidateReviewPayload(approve=True, note="可以"))
    assert result["candidate"]["status"] == "approved"


def test_generate_rejects_unparsable_answer(tmp_path: Path):
    controller = make_controller(None, tmp_path, text="没有 JSON")
    try:
        controller.generate(CandidateGeneratePayload())
    except HTTPException as exc:
        assert exc.status_code == 502
    else:  # pragma: no cover
        raise AssertionError("expected HTTPException")


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
