"""Offline tests for the strategy source editor."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from btrader.strategy.registry import StrategyRegistry
from btrader.strategy.source import (
    DEFAULT_TEMPLATE,
    StrategySourceError,
    read_source,
    save_source,
    validate_source,
)

CODE = '''
from btrader.strategy.base import IntParam, Strategy
import pandas as pd


class EditorStrategy(Strategy):
    key = "editor_test"
    name = "编辑器测试"
    params_spec = [IntParam("period", "周期", 20, 2, 200)]

    def generate(self, df: pd.DataFrame, params: dict) -> pd.DataFrame:
        ma = df["close"].rolling(int(params["period"])).mean()
        return pd.DataFrame({"target": (df["close"] > ma).astype(float)}, index=df.index)
'''


def make_registry(tmp_path: Path) -> StrategyRegistry:
    registry = StrategyRegistry()
    registry.discover([tmp_path])
    return registry


def test_default_template_is_valid():
    report = validate_source(DEFAULT_TEMPLATE)
    assert report["ok"] is True, report
    assert report["key"] == "my_strategy"


def test_save_creates_user_strategy_and_reloads(tmp_path: Path):
    registry = make_registry(tmp_path)
    result = save_source(
        registry, code=CODE, key="editor_test", create=True, directory=tmp_path
    )
    assert result["ok"] is True
    assert (tmp_path / "editor_test.py").exists()
    assert "editor_test" in registry
    detail = read_source(registry, "editor_test", tmp_path)
    assert detail["editable"] is True
    assert detail["builtin"] is False
    assert "EditorStrategy" in detail["code"]


def test_save_updates_existing_file(tmp_path: Path):
    registry = make_registry(tmp_path)
    save_source(registry, code=CODE, key="editor_test", create=True, directory=tmp_path)
    changed = CODE.replace('name = "编辑器测试"', 'name = "改过的名字"')
    save_source(registry, code=changed, key="editor_test", directory=tmp_path)
    assert "改过的名字" in (tmp_path / "editor_test.py").read_text(encoding="utf-8")


def test_bad_import_is_rejected(tmp_path: Path):
    registry = make_registry(tmp_path)
    bad = CODE.replace("import pandas as pd", "import os\nimport pandas as pd")
    report = validate_source(bad)
    assert report["ok"] is False
    with pytest.raises(StrategySourceError) as caught:
        save_source(registry, code=bad, key="editor_test", create=True, directory=tmp_path)
    assert caught.value.status == 422
    assert not (tmp_path / "editor_test.py").exists()


def test_key_mismatch_is_rejected(tmp_path: Path):
    registry = make_registry(tmp_path)
    with pytest.raises(StrategySourceError):
        save_source(registry, code=CODE, key="another_key", create=True, directory=tmp_path)


def test_builtin_source_can_be_forked(tmp_path: Path):
    registry = make_registry(tmp_path)
    detail = read_source(registry, "ma_cross", tmp_path)
    assert "from btrader.strategy.base import" in detail["code"]
    forked = detail["code"].replace('key = "ma_cross"', 'key = "ma_cross_copy"')
    report = validate_source(forked)
    assert report["ok"] is True, report
    assert report["key"] == "ma_cross_copy"
    save_source(registry, code=forked, key="ma_cross_copy", create=True, directory=tmp_path)
    assert "ma_cross_copy" in registry


def test_builtin_is_read_only(tmp_path: Path):
    registry = make_registry(tmp_path)
    # The built-ins are loaded from the package, not from tmp_path.
    assert registry.source_path("ma_cross") is None
    detail = read_source(registry, "ma_cross", tmp_path)
    assert detail["editable"] is False
    assert detail["builtin"] is True
    with pytest.raises(StrategySourceError) as caught:
        save_source(
            registry,
            code=CODE.replace("editor_test", "ma_cross"),
            key="ma_cross",
            directory=tmp_path,
        )
    assert caught.value.status == 403
