"""Offline tests: a candidate can be backtested before it is approved.

The candidate directory is never imported by the shared registry — that is the
red line this module protects. ``CandidateStore.load_strategy`` loads the code
in a throwaway folder with a private registry so the lab can measure a
candidate without it appearing in the strategy list.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from btrader.strategy.candidates import CandidateError, CandidateStore
from btrader.strategy.registry import StrategyRegistry

GOOD = '''from btrader.strategy.base import IntParam, Strategy
import pandas as pd


class Cand(Strategy):
    key = "cand_under_test"
    name = "候选"
    params_spec = [IntParam("period", "周期", 20, 2, 200)]

    def generate(self, df: pd.DataFrame, params: dict) -> pd.DataFrame:
        ma = df["close"].rolling(int(params["period"])).mean()
        return pd.DataFrame({"target": (df["close"] > ma).astype(float)}, index=df.index)
'''


def make_store(tmp_path: Path) -> CandidateStore:
    return CandidateStore(tmp_path / "candidates")


def test_load_strategy_returns_the_candidate_class(tmp_path: Path):
    store = make_store(tmp_path)
    candidate = store.create(GOOD, strategy_key="cand_under_test", name="候选")
    strategy = store.load_strategy(candidate.id)
    assert strategy.meta.key == "cand_under_test"
    assert strategy.__class__.__name__ == "Cand"
    assert strategy.default_params() == {"period": 20}


def test_load_strategy_never_leaves_files_behind(tmp_path: Path):
    store = make_store(tmp_path)
    candidate = store.create(GOOD, strategy_key="cand_under_test")
    store.load_strategy(candidate.id)
    names = [path.name for path in store.directory.iterdir()]
    assert f"{candidate.id}.py" in names  # the candidate itself is untouched
    assert all(not name.startswith("cand_") or name.startswith(candidate.id) for name in names)


def test_the_shared_registry_never_learns_about_a_candidate(tmp_path: Path):
    store = make_store(tmp_path)
    candidate = store.create(GOOD, strategy_key="cand_under_test")
    store.load_strategy(candidate.id)
    shared = StrategyRegistry()
    shared.discover([tmp_path / "strategies"])
    assert "cand_under_test" not in shared


def test_load_strategy_refuses_code_that_fails_the_static_check(tmp_path: Path):
    store = make_store(tmp_path)
    bad = GOOD.replace("import pandas as pd", "import os\nimport pandas as pd")
    candidate = store.create(bad, strategy_key="cand_under_test")
    with pytest.raises(CandidateError) as caught:
        store.load_strategy(candidate.id)
    assert "静态检查" in str(caught.value)


def test_load_strategy_reports_unloadable_code(tmp_path: Path):
    store = make_store(tmp_path)
    broken = GOOD.replace("class Cand(Strategy):", 'raise RuntimeError("boom")\n\n\nclass Cand(Strategy):')
    candidate = store.create(broken, strategy_key="cand_under_test")
    with pytest.raises(CandidateError) as caught:
        store.load_strategy(candidate.id)
    assert "无法加载" in str(caught.value)


def test_rename_registered_key_follows_a_strategy_rename(tmp_path: Path):
    store = make_store(tmp_path)
    candidate = store.create(GOOD, strategy_key="cand_under_test")
    store.mark_registered(
        candidate.id, key="cand_under_test", strategy_path=str(tmp_path / "cand_under_test.py")
    )
    moved = store.rename_registered_key("cand_under_test", "renamed_candidate")
    assert moved == 1
    again = store.get(candidate.id)
    assert again.registered_key == "renamed_candidate"
    assert again.strategy_path.endswith("renamed_candidate.py")
