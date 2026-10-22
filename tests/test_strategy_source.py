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
    rename_source,
    restore_builtin,
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
    detail = read_source(registry, "editor_test")
    assert detail["editable"] is True
    assert detail["builtin"] is False
    assert detail["overridden"] is False
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
    detail = read_source(registry, "ma_cross")
    assert "from btrader.strategy.base import" in detail["code"]
    forked = detail["code"].replace('key = "ma_cross"', 'key = "ma_cross_copy"')
    report = validate_source(forked)
    assert report["ok"] is True, report
    assert report["key"] == "ma_cross_copy"
    save_source(registry, code=forked, key="ma_cross_copy", create=True, directory=tmp_path)
    assert "ma_cross_copy" in registry


def test_builtin_can_be_overridden_and_restored(tmp_path: Path):
    registry = make_registry(tmp_path)
    # The built-ins are loaded from the package, not from tmp_path.
    assert registry.source_path("ma_cross") is None
    detail = read_source(registry, "ma_cross")
    assert detail["editable"] is True
    assert detail["builtin"] is True
    assert detail["overridden"] is False
    assert detail["filename"] == ""

    edited = detail["code"] + "\n# 用户改的\n"
    result = save_source(registry, code=edited, key="ma_cross", directory=tmp_path)
    assert result["ok"] is True
    assert result["filename"] == "ma_cross.py"
    assert result["builtin"] is True
    assert result["overridden"] is True

    # The override is a user file, and it is the one that runs.
    path = registry.source_path("ma_cross")
    assert path is not None
    assert path.parent == tmp_path.resolve()
    assert type(registry.get("ma_cross")).__module__.startswith("btrader_user_strategy_")
    saved = read_source(registry, "ma_cross")
    assert saved["filename"] == "ma_cross.py"
    assert saved["overridden"] is True
    assert saved["builtin"] is True
    assert saved["editable"] is True
    assert "# 用户改的" in saved["code"]

    restored = restore_builtin(registry, "ma_cross", tmp_path)
    assert restored == {"ok": True, "key": "ma_cross", "builtin": True, "overridden": False}
    assert not (tmp_path / "ma_cross.py").exists()
    assert registry.is_overridden("ma_cross") is False
    assert registry.is_builtin("ma_cross") is True
    assert registry.source_path("ma_cross") is None
    back = read_source(registry, "ma_cross")
    assert back["overridden"] is False
    assert back["builtin"] is True
    assert back["editable"] is True
    assert back["filename"] == ""
    assert "# 用户改的" not in back["code"]


def test_overridden_builtin_cannot_be_renamed(tmp_path: Path):
    registry = make_registry(tmp_path)
    detail = read_source(registry, "ma_cross")
    save_source(registry, code=detail["code"], key="ma_cross", directory=tmp_path)
    with pytest.raises(StrategySourceError) as caught:
        rename_source(registry, key="ma_cross", new_key="ma_cross_copy", directory=tmp_path)
    assert caught.value.status == 403
    assert "内置策略不能改名，先还原或另存为新策略" in str(caught.value)
    assert not (tmp_path / "ma_cross_copy.py").exists()


def test_restore_refuses_a_strategy_that_is_not_an_override(tmp_path: Path):
    registry = make_registry(tmp_path)
    with pytest.raises(StrategySourceError) as caught:
        restore_builtin(registry, "ma_cross", tmp_path)
    assert caught.value.status == 400
    assert str(caught.value) == "这个策略不是覆盖版本"
    # A plain user strategy is not an override either: restoring would just
    # delete the only copy of it.
    save_source(registry, code=CODE, key="editor_test", create=True, directory=tmp_path)
    with pytest.raises(StrategySourceError) as caught:
        restore_builtin(registry, "editor_test", tmp_path)
    assert caught.value.status == 400
    assert (tmp_path / "editor_test.py").exists()


def test_save_that_does_not_take_effect_is_rejected(tmp_path: Path):
    # The file passes the static check but blows up on import, so the built-in
    # stays registered. The save must not report success.
    registry = make_registry(tmp_path)
    detail = read_source(registry, "ma_cross")
    broken = detail["code"] + "\n\n1 / 0\n"
    with pytest.raises(StrategySourceError) as caught:
        save_source(registry, code=broken, key="ma_cross", directory=tmp_path)
    assert caught.value.status == 422
    # One readable line for the editor banner, not a traceback.
    assert str(caught.value) == "保存后没有生效，内置版本仍在生效：ma_cross.py: 加载失败"
    assert registry.is_overridden("ma_cross") is False
    assert registry.source_path("ma_cross") is None
    # Nothing half-broken is left behind: the file was removed again and the
    # next reload has no error to report.
    assert not (tmp_path / "ma_cross.py").exists()
    registry.discover([tmp_path])
    assert registry.errors == []


def test_meta_list_reports_builtin_and_overridden(tmp_path: Path):
    # This is the shape GET /api/strategies serves, so the list and the editor
    # must agree about which rows are overridden.
    registry = make_registry(tmp_path)
    entry = {item["key"]: item for item in registry.meta_list()}["ma_cross"]
    assert entry["builtin"] is True
    assert entry["overridden"] is False
    detail = read_source(registry, "ma_cross")
    save_source(registry, code=detail["code"], key="ma_cross", directory=tmp_path)
    entry = {item["key"]: item for item in registry.meta_list()}["ma_cross"]
    assert entry["builtin"] is True
    assert entry["overridden"] is True
    restore_builtin(registry, "ma_cross", tmp_path)
    entry = {item["key"]: item for item in registry.meta_list()}["ma_cross"]
    assert entry["builtin"] is True
    assert entry["overridden"] is False
    assert registry.builtin_keys == ["bollinger", "donchian_breakout", "ma_cross", "rsi_reversion"]


def test_restore_refuses_a_file_that_holds_another_strategy(tmp_path: Path):
    # One file, two strategies: deleting it would take the sibling with it.
    registry = make_registry(tmp_path)
    detail = read_source(registry, "ma_cross")
    sibling = CODE.replace("EditorStrategy", "SiblingStrategy").replace("editor_test", "sibling_strategy")
    (tmp_path / "combo.py").write_text(detail["code"] + "\n\n" + sibling, encoding="utf-8")
    registry.discover([tmp_path])
    assert registry.is_overridden("ma_cross") is True
    assert "sibling_strategy" in registry
    with pytest.raises(StrategySourceError) as caught:
        restore_builtin(registry, "ma_cross", tmp_path)
    assert caught.value.status == 400
    assert "sibling_strategy" in str(caught.value)
    assert (tmp_path / "combo.py").exists()
    assert "sibling_strategy" in registry


def test_two_user_overrides_of_one_builtin_conflict(tmp_path: Path):
    # The second file must not silently take the first one's place: the editor
    # would show neither and restore would delete the wrong one.
    registry = make_registry(tmp_path)
    detail = read_source(registry, "ma_cross")
    (tmp_path / "aaa.py").write_text(detail["code"], encoding="utf-8")
    (tmp_path / "zzz.py").write_text(detail["code"].replace('name = "', 'name = "改: '), encoding="utf-8")
    registry.discover([tmp_path])
    assert registry.is_overridden("ma_cross") is True
    assert any("策略 key 重复" in error for error in registry.errors)
    assert registry.source_path("ma_cross").name == "aaa.py"
    # Restoring deletes aaa.py, then zzz.py takes the key over. Reporting ok
    # there would paint the row as "not overridden" while it still is.
    with pytest.raises(StrategySourceError) as caught:
        restore_builtin(registry, "ma_cross", tmp_path)
    assert caught.value.status == 400
    # The deletion really happened, so the message says which file went away.
    assert str(caught.value) == "还原后仍有别的用户文件覆盖这个策略：已删除 aaa.py，zzz.py 仍在覆盖"
    assert not (tmp_path / "aaa.py").exists()
    assert registry.is_overridden("ma_cross") is True
    # A second restore finishes the job and lands on the built-in.
    restored = restore_builtin(registry, "ma_cross", tmp_path)
    assert restored == {"ok": True, "key": "ma_cross", "builtin": True, "overridden": False}
    assert not (tmp_path / "zzz.py").exists()
    assert registry.is_overridden("ma_cross") is False


def test_two_user_files_with_the_same_key_still_conflict(tmp_path: Path):
    registry = make_registry(tmp_path)
    (tmp_path / "aaa.py").write_text(CODE, encoding="utf-8")
    (tmp_path / "bbb.py").write_text(CODE.replace("编辑器测试", "重复的"), encoding="utf-8")
    registry.discover([tmp_path])
    assert "editor_test" in registry
    assert any("策略 key 重复" in error for error in registry.errors)
    assert registry.is_overridden("editor_test") is False
