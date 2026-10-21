"""Offline tests for renaming a user strategy.

A rename has to move the file, rewrite the class's ``key`` literal, keep the
registry in step, and roll back if the new file cannot be loaded. Everything
here runs against a temporary strategy directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from btrader.strategy.registry import StrategyRegistry
from btrader.strategy.source import StrategySourceError, rename_source, save_source

CODE = '''from btrader.strategy.base import IntParam, Strategy
import pandas as pd


class RenameMe(Strategy):
    key = "rename_me"
    name = "待重命名"
    params_spec = [IntParam("period", "周期", 20, 2, 200)]

    def generate(self, df: pd.DataFrame, params: dict) -> pd.DataFrame:
        ma = df["close"].rolling(int(params["period"])).mean()
        return pd.DataFrame({"target": (df["close"] > ma).astype(float)}, index=df.index)
'''


def make_registry(tmp_path: Path) -> StrategyRegistry:
    registry = StrategyRegistry()
    registry.discover([tmp_path])
    return registry


def seed(tmp_path: Path) -> StrategyRegistry:
    registry = make_registry(tmp_path)
    save_source(registry, code=CODE, key="rename_me", create=True, directory=tmp_path)
    return registry


def test_rename_moves_file_and_key(tmp_path: Path):
    registry = seed(tmp_path)
    result = rename_source(registry, key="rename_me", new_key="renamed_ok", directory=tmp_path)
    assert result["key"] == "renamed_ok"
    assert result["previous"] == "rename_me"
    assert not (tmp_path / "rename_me.py").exists()
    assert (tmp_path / "renamed_ok.py").exists()
    assert "renamed_ok" in registry
    assert "rename_me" not in registry
    text = (tmp_path / "renamed_ok.py").read_text(encoding="utf-8")
    assert 'key = "renamed_ok"' in text
    assert 'key = "rename_me"' not in text


def test_rename_can_set_a_display_name(tmp_path: Path):
    registry = seed(tmp_path)
    rename_source(
        registry, key="rename_me", new_key="renamed_ok", new_name="新名字", directory=tmp_path
    )
    text = (tmp_path / "renamed_ok.py").read_text(encoding="utf-8")
    assert 'name = "新名字"' in text
    assert registry.get("renamed_ok").meta.name == "新名字"


def test_rename_adds_a_name_when_the_class_had_none(tmp_path: Path):
    registry = make_registry(tmp_path)
    code = CODE.replace('    name = "待重命名"\n', "")
    save_source(registry, code=code, key="rename_me", create=True, directory=tmp_path)
    rename_source(
        registry, key="rename_me", new_key="renamed_ok", new_name="补上的名字", directory=tmp_path
    )
    text = (tmp_path / "renamed_ok.py").read_text(encoding="utf-8")
    assert 'name = "补上的名字"' in text
    assert registry.get("renamed_ok").meta.name == "补上的名字"


def test_rename_refuses_an_existing_target(tmp_path: Path):
    registry = seed(tmp_path)
    save_source(
        registry,
        code=CODE.replace("rename_me", "taken_key"),
        key="taken_key",
        create=True,
        directory=tmp_path,
    )
    with pytest.raises(StrategySourceError) as caught:
        rename_source(registry, key="rename_me", new_key="taken_key", directory=tmp_path)
    assert "已存在" in str(caught.value)
    assert (tmp_path / "rename_me.py").exists()


def test_rename_refuses_a_builtin(tmp_path: Path):
    registry = make_registry(tmp_path)
    with pytest.raises(StrategySourceError) as caught:
        rename_source(registry, key="ma_cross", new_key="ma_cross_v2", directory=tmp_path)
    assert caught.value.status == 403


def test_rename_refuses_a_bad_key(tmp_path: Path):
    registry = seed(tmp_path)
    with pytest.raises(StrategySourceError):
        rename_source(registry, key="rename_me", new_key="Not A Key", directory=tmp_path)
    assert (tmp_path / "rename_me.py").exists()


def test_rename_refuses_the_same_key(tmp_path: Path):
    registry = seed(tmp_path)
    with pytest.raises(StrategySourceError):
        rename_source(registry, key="rename_me", new_key="rename_me", directory=tmp_path)


def test_a_failing_reload_rolls_the_rename_back(monkeypatch, tmp_path: Path):
    """If the new file does not load, the user keeps the strategy they had."""
    registry = seed(tmp_path)
    real = StrategyRegistry.discover
    calls = {"n": 0}

    def flaky(self, extra_dirs=None):
        calls["n"] += 1
        if calls["n"] == 1:
            # Simulate a candidate file that saves but cannot be imported.
            return
        return real(self, extra_dirs)

    monkeypatch.setattr(StrategyRegistry, "discover", flaky)
    with pytest.raises(StrategySourceError) as caught:
        rename_source(registry, key="rename_me", new_key="renamed_ok", directory=tmp_path)
    assert caught.value.status == 422
    assert (tmp_path / "rename_me.py").exists()
    assert not (tmp_path / "renamed_ok.py").exists()
    monkeypatch.undo()
    renamed = StrategyRegistry()
    renamed.discover([tmp_path])
    assert "rename_me" in renamed
    assert "renamed_ok" not in renamed
