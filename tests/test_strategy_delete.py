"""删除策略：走注册表的重载路径，删完别的策略不受影响。

这条接口原本不存在：界面上能「新建」、能「复制为新策略」，就是删不掉，
`restore` 只删内置的覆盖文件，纯自定义策略它直接 400——用户只能去
`%APPDATA%/biTrader/strategies/` 手删文件。

口径：

- 纯自定义策略：删文件 + 热加载；
- 覆盖版本（用户目录里顶掉内置的那种）：删掉就等于还原，内置原版回来；
- 纯内置：400，指向 `POST /api/strategies/{key}/restore`；
- 一个文件里还有别的策略：400 让先拆开（和 restore 同一口径，不猜作者的文件布局）；
- 引擎正在跑这条策略：409（`routes._strategy_in_use`）。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from btrader.strategy.registry import StrategyRegistry
from btrader.strategy.source import (
    StrategySourceError,
    delete_source,
    read_source,
    save_source,
)
from btrader.web.routes import AutoController, AITraderController, build_router

CODE = '''from btrader.strategy.base import IntParam, Strategy
import pandas as pd


class EditorStrategy(Strategy):
    key = "editor_test"
    name = "编辑器测试"
    params_spec = [IntParam("period", "周期", 20, 2, 200)]

    def generate(self, df: pd.DataFrame, params: dict) -> pd.DataFrame:
        ma = df["close"].rolling(int(params["period"])).mean()
        return pd.DataFrame({"target": (df["close"] > ma).astype(float)}, index=df.index)
'''

PAIR = '''from btrader.strategy.base import Strategy
import pandas as pd


class FirstOne(Strategy):
    key = "first_one"
    name = "第一条"

    def generate(self, df: pd.DataFrame, params: dict) -> pd.DataFrame:
        return pd.DataFrame({"target": 0.0}, index=df.index)


class SecondOne(Strategy):
    key = "second_one"
    name = "第二条"

    def generate(self, df: pd.DataFrame, params: dict) -> pd.DataFrame:
        return pd.DataFrame({"target": 0.0}, index=df.index)
'''


def make_registry(tmp_path: Path) -> StrategyRegistry:
    registry = StrategyRegistry()
    registry.discover([tmp_path])
    return registry


def seed_custom(tmp_path: Path) -> StrategyRegistry:
    registry = make_registry(tmp_path)
    save_source(registry, code=CODE, key="editor_test", create=True, directory=tmp_path)
    return registry


# --------------------------------------------------------------- 纯自定义策略
def test_deleting_a_custom_strategy_removes_it_and_reloads(tmp_path: Path):
    registry = seed_custom(tmp_path)
    assert "editor_test" in registry

    result = delete_source(registry, "editor_test", directory=tmp_path)

    assert result == {"ok": True, "key": "editor_test"}
    assert not (tmp_path / "editor_test.py").exists()
    assert "editor_test" not in registry
    # 别的策略不受影响
    assert "ma_cross" in registry


def test_deleting_one_strategy_leaves_the_others_alone(tmp_path: Path):
    registry = seed_custom(tmp_path)
    save_source(
        registry,
        code=CODE.replace("editor_test", "keep_me").replace("EditorStrategy", "KeepMe"),
        key="keep_me",
        create=True,
        directory=tmp_path,
    )

    delete_source(registry, "editor_test", directory=tmp_path)

    assert (tmp_path / "keep_me.py").exists()
    assert read_source(registry, "keep_me")["builtin"] is False
    assert "editor_test" not in registry


def test_a_builtin_without_an_override_cannot_be_deleted(tmp_path: Path):
    registry = make_registry(tmp_path)

    with pytest.raises(StrategySourceError) as caught:
        delete_source(registry, "ma_cross", directory=tmp_path)

    assert caught.value.status == 400
    message = str(caught.value)
    assert "内置策略不能删" in message and "restore" in message, message
    assert "ma_cross" in registry


def test_an_unknown_key_is_a_404(tmp_path: Path):
    registry = make_registry(tmp_path)

    with pytest.raises(StrategySourceError) as caught:
        delete_source(registry, "no_such_strategy", directory=tmp_path)

    assert caught.value.status == 404
    assert "no_such_strategy" in str(caught.value)


# ----------------------------------------------------------------- 覆盖版本
def test_deleting_an_override_brings_the_builtin_back(tmp_path: Path):
    registry = make_registry(tmp_path)
    forked = read_source(registry, "ma_cross")["code"]
    save_source(registry, code=forked, key="ma_cross", directory=tmp_path)
    assert registry.is_overridden("ma_cross") is True
    assert (tmp_path / "ma_cross.py").exists()

    result = delete_source(registry, "ma_cross", directory=tmp_path)

    assert result == {"ok": True, "key": "ma_cross"}
    assert not (tmp_path / "ma_cross.py").exists()
    assert registry.is_builtin("ma_cross") is True
    assert registry.is_overridden("ma_cross") is False
    assert registry.source_path("ma_cross") is None


# ------------------------------------------------------- 一个文件多条策略
def test_a_file_holding_another_strategy_is_refused(tmp_path: Path):
    (tmp_path / "pair.py").write_text(PAIR, encoding="utf-8")
    registry = make_registry(tmp_path)
    assert {"first_one", "second_one"} <= set(registry.keys)

    with pytest.raises(StrategySourceError) as caught:
        delete_source(registry, "first_one", directory=tmp_path)

    assert caught.value.status == 400
    assert "先拆开" in str(caught.value), str(caught.value)
    # 一个都不能少：文件还在，两条策略都还在
    assert (tmp_path / "pair.py").exists()
    assert {"first_one", "second_one"} <= set(registry.keys)


# ------------------------------------------------------------------ 目录越界
def test_a_strategy_outside_the_user_directory_is_refused(tmp_path: Path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "outside_one.py").write_text(
        PAIR.replace("first_one", "outside_one").replace("FirstOne", "OutsideOne"),
        encoding="utf-8",
    )
    registry = StrategyRegistry()
    registry.discover([tmp_path, outside])
    assert "outside_one" in registry

    with pytest.raises(StrategySourceError) as caught:
        delete_source(registry, "outside_one", directory=tmp_path)

    assert caught.value.status == 403
    assert (outside / "outside_one.py").exists()


# --------------------------------------------------------------------- 路由
@pytest.fixture
def user_dir(tmp_path: Path, monkeypatch) -> Path:
    """路由用的是默认数据目录：指到临时目录，绝不碰真实的 strategies 目录。"""
    from btrader import paths

    target = tmp_path / "strategies"
    target.mkdir()
    monkeypatch.setattr(paths, "user_strategies_dir", lambda: target)
    return target


def _endpoint(router, path: str, method: str):
    for route in router.routes:
        if getattr(route, "path", "") == path and method in getattr(route, "methods", ()):
            return route.endpoint
    raise AssertionError(f"missing route {method} {path}")


def _controller(router, kind):
    endpoint = _endpoint(router, "/api/strategies/{key}", "DELETE")
    for cell in endpoint.__closure__ or ():
        try:
            value = cell.cell_contents
        except ValueError:  # pragma: no cover - 空 cell 不会出现
            continue
        if isinstance(value, kind):
            return value
    raise AssertionError(f"找不到 {kind.__name__}")


def _router(registry: StrategyRegistry):
    state = SimpleNamespace(ctx=SimpleNamespace(strategies=registry))
    return build_router(lambda: state)


def test_the_route_deletes_a_custom_strategy(user_dir: Path):
    registry = seed_custom(user_dir)
    delete = _endpoint(_router(registry), "/api/strategies/{key}", "DELETE")

    assert delete("editor_test") == {"ok": True, "key": "editor_test"}

    assert "editor_test" not in registry


def test_the_route_maps_source_errors_to_their_status(user_dir: Path):
    registry = make_registry(user_dir)
    delete = _endpoint(_router(registry), "/api/strategies/{key}", "DELETE")

    with pytest.raises(HTTPException) as missing:
        delete("no_such_strategy")
    assert missing.value.status_code == 404

    with pytest.raises(HTTPException) as builtin:
        delete("ma_cross")
    assert builtin.value.status_code == 400
    assert "内置策略不能删" in str(builtin.value.detail)


def test_the_route_refuses_while_the_ai_trader_runs_the_key(user_dir: Path, monkeypatch):
    registry = seed_custom(user_dir)
    router = _router(registry)
    ai_ctl = _controller(router, AITraderController)
    monkeypatch.setattr(
        ai_ctl,
        "status",
        lambda since=0: {"state": "running", "config": {"strategy": "editor_test"}},
    )
    delete = _endpoint(router, "/api/strategies/{key}", "DELETE")

    with pytest.raises(HTTPException) as caught:
        delete("editor_test")

    assert caught.value.status_code == 409
    assert "AI 交易正在使用该策略" in str(caught.value.detail)
    assert (user_dir / "editor_test.py").exists()
    assert "editor_test" in registry


def test_the_route_refuses_while_the_auto_trader_runs_the_key(user_dir: Path, monkeypatch):
    registry = seed_custom(user_dir)
    router = _router(registry)
    auto = _controller(router, AutoController)
    monkeypatch.setattr(
        auto,
        "status",
        lambda since=0: {"running": True, "config": {"strategy": "editor_test"}},
    )
    delete = _endpoint(router, "/api/strategies/{key}", "DELETE")

    with pytest.raises(HTTPException) as caught:
        delete("editor_test")

    assert caught.value.status_code == 409
    assert "自动交易正在使用该策略" in str(caught.value.detail)
