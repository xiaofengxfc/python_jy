"""Offline tests for the Freqtrade compatibility adapter.

A biTrader strategy is a ``Strategy.generate()`` plugin, not an IStrategy.
The adapter rendered by :mod:`btrader.freqtrade_compat` is what makes the two
engines able to run the same signal logic; these tests keep it runnable and
keep the target-to-signal mapping honest.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pandas as pd

from btrader.core.config import FreqtradeSettings
from btrader.freqtrade import FreqtradeBridge
from btrader.freqtrade_compat import (
    adapter_class_name,
    ensure_adapter,
    remove_adapter,
    render_adapter,
    target_to_signals,
)
from btrader.strategy.registry import StrategyRegistry


def make_bridge(tmp_path: Path) -> FreqtradeBridge:
    settings = FreqtradeSettings(python=sys.executable, exchange="gate", trade_port=18092)
    return FreqtradeBridge(settings, root=tmp_path)


def make_registry(tmp_path: Path) -> StrategyRegistry:
    registry = StrategyRegistry()
    registry.discover([tmp_path])
    return registry


def test_target_becomes_entry_and_exit_signals():
    frame = pd.DataFrame({"bt_target": [0.0, 1.0, 1.0, 0.0, -1.0, 0.0]})
    out = target_to_signals(frame)
    assert list(out["enter_long"]) == [0, 1, 0, 0, 0, 0]
    assert list(out["exit_long"]) == [0, 0, 0, 1, 0, 0]
    assert list(out["enter_short"]) == [0, 0, 0, 0, 1, 0]
    assert list(out["exit_short"]) == [0, 0, 0, 0, 0, 1]


def test_target_column_can_be_missing():
    frame = pd.DataFrame({"close": [1.0, 2.0]})
    out = target_to_signals(frame)
    for column in ("enter_long", "exit_long", "enter_short", "exit_short"):
        assert list(out[column]) == [0, 0]


def test_adapter_class_name_is_a_valid_identifier():
    name = adapter_class_name("bollinger-atr.v2")
    assert name == "Btrader_bollingeratrv2"
    assert name.isidentifier()


def test_rendered_adapter_parses_and_disables_freqtrade_exits():
    code = render_adapter(
        key="ma_cross",
        class_name=adapter_class_name("ma_cross"),
        project_src=str(Path.cwd() / "src"),
        params={"fast": 5, "slow": 20},
        load={"kind": "module", "module": "btrader.strategy.builtin.ma_cross"},
        timeframe="1h",
        startup=40,
        can_short=True,
    )
    tree = ast.parse(code)
    classes = [node.name for node in tree.body if isinstance(node, ast.ClassDef)]
    assert classes == ["Btrader_ma_cross"]
    # The adapter must not re-introduce Freqtrade's own exits, otherwise the
    # same strategy reports different numbers on the two engines.
    assert "stoploss = -0.99" in code
    assert 'minimal_roi = {"0": 1000}' in code
    assert "can_short = True" in code
    assert "'fast': 5" in code


def test_ensure_adapter_writes_a_generated_strategy_file(tmp_path: Path):
    bridge = make_bridge(tmp_path)
    registry = make_registry(tmp_path / "user")
    info = ensure_adapter(
        bridge,
        registry,
        "ma_cross",
        params={"fast": 5, "slow": 20},
        timeframe="1h",
        allow_short=False,
        project_src=str(Path.cwd() / "src"),
    )
    assert info["name"] == "Btrader_ma_cross"
    path = bridge.strategy_dir / "Btrader_ma_cross.py"
    assert path.exists()
    code = path.read_text(encoding="utf-8")
    ast.parse(code)
    assert "can_short = False" in code  # the venue said spot
    listed = {item["name"]: item for item in bridge.strategies()}
    assert listed["Btrader_ma_cross"]["generated"] is True


def test_ensure_adapter_refreshes_params_on_every_run(tmp_path: Path):
    bridge = make_bridge(tmp_path)
    registry = make_registry(tmp_path / "user")
    ensure_adapter(bridge, registry, "ma_cross", params={"fast": 5, "slow": 20})
    ensure_adapter(bridge, registry, "ma_cross", params={"fast": 9, "slow": 30})
    code = (bridge.strategy_dir / "Btrader_ma_cross.py").read_text(encoding="utf-8")
    assert "'fast': 9" in code
    assert "'fast': 5" not in code


def test_remove_adapter_deletes_only_that_file(tmp_path: Path):
    bridge = make_bridge(tmp_path)
    registry = make_registry(tmp_path / "user")
    ensure_adapter(bridge, registry, "ma_cross")
    assert remove_adapter(bridge, "ma_cross") is True
    assert remove_adapter(bridge, "ma_cross") is False
    assert not (bridge.strategy_dir / "Btrader_ma_cross.py").exists()


def test_adapter_renders_bool_params_as_python_literals():
    """JSON's ``true`` is a NameError in the generated module (real bug)."""
    code = render_adapter(
        key="ma_cross",
        class_name=adapter_class_name("ma_cross"),
        project_src=str(Path.cwd() / "src"),
        params={"trend": True, "fast": 5},
        load={"kind": "module", "module": "btrader.strategy.builtin.ma_cross"},
    )
    assert "\"trend\": true" not in code
    assert "True" in code
    ast.parse(code)
    line = next(item for item in code.splitlines() if item.startswith("_PARAMS ="))
    namespace: dict = {}
    exec(line, namespace)  # noqa: S102 - the line is a literal dict
    assert namespace["_PARAMS"]["trend"] is True
