"""The backtest route accepts ``cand:<id>`` so a candidate can be measured
before approval. These are the seams that make that work, tested without HTTP.
"""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest
from fastapi import HTTPException

from btrader.strategy.base import IntParam, Strategy
from btrader.strategy.candidates import CandidateError, CandidateStore
from btrader.web import routes

CODE = '''from btrader.strategy.base import IntParam, Strategy
import pandas as pd


class Cand(Strategy):
    key = "cand_measured"
    name = "候选"
    params_spec = [IntParam("period", "周期", 20, 2, 200)]

    def generate(self, df: pd.DataFrame, params: dict) -> pd.DataFrame:
        ma = df["close"].rolling(int(params["period"])).mean()
        return pd.DataFrame({"target": (df["close"] > ma).astype(float)}, index=df.index)
'''


class ToyStrategy(Strategy):
    key = "toy"
    name = "玩具"

    def generate(self, df, params):
        return pd.DataFrame({"target": 0.0}, index=df.index)


class FakeStrategies:
    def get(self, key: str):
        if key != "toy":
            raise KeyError(f"未知策略: {key}")
        return ToyStrategy()


def test_candidate_prefix_is_recognised():
    assert routes._candidate_id("cand:abc-1") == "abc-1"
    assert routes._candidate_id("ma_cross") == ""


def test_strategy_for_resolves_a_registered_key():
    state = SimpleNamespace(ctx=SimpleNamespace(strategies=FakeStrategies()))
    assert routes._strategy_for(state, "toy").meta.key == "toy"


def test_strategy_for_reports_an_unknown_key_as_a_400():
    state = SimpleNamespace(ctx=SimpleNamespace(strategies=FakeStrategies()))
    with pytest.raises(HTTPException) as caught:
        routes._strategy_for(state, "nope")
    assert caught.value.status_code == 400


def test_strategy_for_loads_a_candidate_instead_of_importing_it(
    monkeypatch, tmp_path
):
    store = CandidateStore(tmp_path / "candidates")
    candidate = store.create(CODE, strategy_key="cand_measured")
    monkeypatch.setattr(routes, "default_store", lambda: store)
    state = SimpleNamespace(ctx=SimpleNamespace(strategies=FakeStrategies()))
    strategy = routes._strategy_for(state, f"cand:{candidate.id}")
    assert strategy.meta.key == "cand_measured"
    # The shared registry is untouched: the fake would still raise for it.
    with pytest.raises(KeyError):
        state.ctx.strategies.get("cand_measured")


def test_strategy_for_turns_a_broken_candidate_into_a_400(monkeypatch, tmp_path):
    store = CandidateStore(tmp_path / "candidates")
    candidate = store.create(CODE.replace("import pandas as pd", "import os\nimport pandas as pd"), strategy_key="cand_measured")
    monkeypatch.setattr(routes, "default_store", lambda: store)
    state = SimpleNamespace(ctx=SimpleNamespace(strategies=FakeStrategies()))
    with pytest.raises(HTTPException) as caught:
        routes._strategy_for(state, f"cand:{candidate.id}")
    assert caught.value.status_code == 400


def test_record_candidate_backtest_writes_the_metrics(monkeypatch, tmp_path):
    store = CandidateStore(tmp_path / "candidates")
    candidate = store.create(CODE, strategy_key="cand_measured")
    monkeypatch.setattr(routes, "default_store", lambda: store)
    routes._record_candidate_backtest(
        f"cand:{candidate.id}",
        {"trades": 4, "total_return": 0.12},
        symbol="BTC/USDT",
        timeframe="1h",
    )
    saved = store.get(candidate.id).backtest
    assert saved["trades"] == 4
    assert saved["symbol"] == "BTC/USDT"
    assert saved["source"] == "btrader"


def test_record_candidate_backtest_ignores_plain_strategies(monkeypatch):
    called = []
    monkeypatch.setattr(routes, "default_store", lambda: called.append(1))
    routes._record_candidate_backtest("ma_cross", {"trades": 1}, symbol="X", timeframe="1h")
    assert called == []


def test_an_engine_using_the_strategy_blocks_a_rename():
    class FakeAuto:
        def status(self):
            return {"running": True, "config": {"strategy": "toy"}}

    class FakeAI:
        def status(self):
            return {"state": "stopped", "config": {}}

    assert "自动交易" in routes._strategy_in_use(FakeAuto(), FakeAI(), "toy")


def test_a_stopped_engine_allows_a_rename():
    class FakeAuto:
        def status(self):
            return {"running": False, "config": {"strategy": "toy"}}

    class FakeAI:
        def status(self):
            return {"state": "stopped", "config": {}}

    assert routes._strategy_in_use(FakeAuto(), FakeAI(), "toy") == ""
