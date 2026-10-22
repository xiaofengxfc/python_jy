"""回测与部署的四道护栏。

这一组都是「收紧」方向：现货回测不许借合约的杠杆和做空、执行台不许用交易所没有的
周期启动、部署不许把市场写进 config.json、策略参数写错要是 400 并点名是哪个参数。
"""

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

from btrader.context import AppContext
from btrader.core.credentials import Credentials
from btrader.core.types import MarketType, Network
from btrader.strategy.base import IntParam, Strategy
from btrader.strategy.candidates import CandidateStore
from btrader.strategy.registry import StrategyRegistry
from btrader.web import routes
from btrader.web.routes import AutoController, AutoStartPayload, BacktestPayload
from btrader.web.routes_lab import LabController, OptimizePayload

from fakes import FakeVenue


class Toy(Strategy):
    """一条带整数参数的策略，参数名自己造，不依赖内置策略。"""

    key = "toy_guard"
    name = "护栏"
    params_spec = [IntParam("period", "周期", 20, 2, 200)]

    def generate(self, df, params):
        ma = df["close"].rolling(int(params["period"])).mean()
        return pd.DataFrame({"target": (df["close"] > ma).astype(float)}, index=df.index)


class _Strategies:
    """真的注册表 + 一条测试策略，避免依赖内置策略的参数名。"""

    def __init__(self) -> None:
        self.registry = StrategyRegistry()
        self.registry.discover(None)
        self.toy = Toy()

    def get(self, key: str):
        return self.toy if key == Toy.key else self.registry.get(key)

    def source_path(self, key: str):
        return self.registry.source_path(key)

    def is_builtin(self, key: str) -> bool:
        return self.registry.is_builtin(key)

    def is_overridden(self, key: str) -> bool:
        return self.registry.is_overridden(key)

    def discover(self, directories=None) -> None:
        self.registry.discover(directories)

    def __contains__(self, key: str) -> bool:
        return key == Toy.key or key in self.registry

    @property
    def keys(self) -> list[str]:
        return [Toy.key, *self.registry.keys]


class _Venue(FakeVenue):
    """替身交易所：补上 AppContext 切市场时要碰的两个属性。"""

    def __init__(self) -> None:
        super().__init__()
        self.credentials = Credentials()

    def reset(self) -> None:
        return None


class _FakeTrader:
    """不启线程的 AutoTrader：只记下配置和有没有 start。"""

    instances: list["_FakeTrader"] = []

    def __init__(self, config, strategy, trading, klines) -> None:
        self.config = config
        self.strategy = strategy
        self.running = False
        self.started = False
        _FakeTrader.instances.append(self)

    def on_log(self, callback) -> None:
        return None

    def on_signal(self, callback) -> None:
        return None

    def on_state(self, callback) -> None:
        return None

    def on_candle(self, callback) -> None:
        return None

    def start(self) -> None:
        self.started = True
        self.running = True

    def stop(self) -> None:
        self.running = False


def _app_context(tmp_path: Path, monkeypatch, venue: _Venue) -> AppContext:
    """真 AppContext：config.json 落在 tmp，起手是「测试网 + 合约」。"""
    monkeypatch.setenv("BITRADER_DATA_DIR", str(tmp_path))
    (tmp_path / "config.json").write_text(
        json.dumps({"network": Network.TESTNET.value, "market_type": MarketType.USDM.value}),
        encoding="utf-8",
    )
    ctx = AppContext()
    ctx.rest = venue
    ctx._market_rest = {market: venue for market in MarketType}
    ctx._data_rest = {market: venue for market in MarketType}
    ctx.strategies = _Strategies()
    ctx.settings.ai.enabled = True
    return ctx


def _state(ctx) -> SimpleNamespace:
    return SimpleNamespace(
        ctx=ctx,
        locks={market: threading.RLock() for market in MarketType},
        account_lock=threading.RLock(),
    )


def _saved_market(path: Path) -> str:
    return json.loads(path.read_text(encoding="utf-8")).get("market_type", "")


@pytest.fixture(autouse=True)
def _clean_traders():
    _FakeTrader.instances.clear()
    yield
    _FakeTrader.instances.clear()


def _auto_payload(**overrides) -> AutoStartPayload:
    values = dict(
        symbol="BTC/USDT",
        market="spot",
        timeframe="1h",
        strategy=Toy.key,
        params={},
        position_pct=1.0,
        leverage=1.0,
        poll_seconds=10.0,
    )
    values.update(overrides)
    return AutoStartPayload(**values)


# ------------------------------------------- 现货回测不许借杠杆，也不许做空
def test_a_spot_backtest_cannot_borrow_leverage_or_short(tmp_path, monkeypatch):
    """现货回测把 leverage=10、allow_short=true 原样用上，跑的是不存在的曲线。

    ``BacktestConfig.deploy = position_pct * max(1, leverage)``：现货给 10 倍，
    仓位放大十倍；``allow_short`` 又让负目标留下来。现货既不能借也不能空。
    自动交易那条路径早就写了「非合约恒 1」，回测这条漏了同一句话。
    """
    state = _state(_app_context(tmp_path, monkeypatch, _Venue()))

    _, _, _, spot = routes._validate_backtest(
        state,
        BacktestPayload(
            market="spot",
            symbol="BTC/USDT",
            strategy=Toy.key,
            timeframe="1h",
            leverage=10,
            allow_short=True,
        ),
    )
    assert spot.leverage == 1.0
    assert spot.allow_short is False
    assert spot.deploy == 1.0  # position_pct 1 × 无杠杆

    # 合约照旧：杠杆和做空都还在
    _, _, _, futures = routes._validate_backtest(
        state,
        BacktestPayload(
            market="usdm",
            symbol="BTC/USDT:USDT",
            strategy=Toy.key,
            timeframe="1h",
            leverage=10,
            allow_short=True,
        ),
    )
    assert futures.leverage == 10.0
    assert futures.allow_short is True
    assert futures.deploy == 10.0


# ------------------------------- 执行台不许用交易所没有的周期启动
def test_the_execution_console_refuses_a_timeframe_the_exchange_has_not(tmp_path, monkeypatch):
    """7m 以前能启动成功，然后每轮「循环异常」，界面在跑却永不下单。"""
    ctx = _app_context(tmp_path, monkeypatch, _Venue())
    monkeypatch.setattr(routes, "AutoTrader", _FakeTrader)
    auto = AutoController()

    with pytest.raises(HTTPException) as caught:
        auto.start(ctx, _auto_payload(timeframe="7m"))

    assert caught.value.status_code == 400
    assert "7m" in caught.value.detail
    for timeframe in ("1m", "1h", "1d"):  # 文案要把可用周期说清
        assert timeframe in caught.value.detail
    assert _FakeTrader.instances == []  # 一个引擎都没造出来

    # 正常周期照旧能启动
    auto.start(ctx, _auto_payload())
    assert _FakeTrader.instances[-1].started is True


# ------------------------------------------- 部署不许把市场写进 config.json
def test_deploying_does_not_persist_the_market_to_disk(tmp_path, monkeypatch):
    """部署顺手 set_market_type（默认落盘）：替用户改了设置还写盘。

    界面跟着引擎换市场没问题，但那是临时状态；写进 config.json 就是替用户改设置，
    写盘失败还会让整个部署 500。
    """
    ctx = _app_context(tmp_path, monkeypatch, _Venue())
    monkeypatch.setattr(routes, "AutoTrader", _FakeTrader)
    config_file = tmp_path / "config.json"
    assert _saved_market(config_file) == MarketType.USDM.value

    AutoController().start(ctx, _auto_payload(market="spot"))

    # 界面（内存里的市场）跟着引擎走，这一条保留
    assert ctx.market_type is MarketType.SPOT
    assert ctx.settings.market_type == MarketType.SPOT.value
    # 但设置文件不该被部署改写
    assert _saved_market(config_file) == MarketType.USDM.value


# ---------------------------------------------------- 参数写错是 400，点名到参数
def test_a_non_numeric_param_is_a_400_that_names_the_parameter(tmp_path, monkeypatch):
    """``validate_params`` 只夹范围，``int("abc")`` 抛 ValueError 变成 500。

    用户看到的是 Internal Server Error，也不知道是哪个参数写错了。三个会走到
    校验的入口（回测、执行台、优化）都该是 400 + 参数名。
    """
    ctx = _app_context(tmp_path, monkeypatch, _Venue())
    state = _state(ctx)

    for bad in ("abc", None):
        with pytest.raises(HTTPException) as caught:
            routes._validate_backtest(
                state,
                BacktestPayload(
                    market="spot",
                    symbol="BTC/USDT",
                    strategy=Toy.key,
                    timeframe="1h",
                    params={"period": bad},
                ),
            )
        assert caught.value.status_code == 400
        assert "period" in caught.value.detail

    monkeypatch.setattr(routes, "AutoTrader", _FakeTrader)
    with pytest.raises(HTTPException) as caught:
        AutoController().start(ctx, _auto_payload(params={"period": "abc"}))
    assert caught.value.status_code == 400
    assert "period" in caught.value.detail

    controller = _lab_controller(tmp_path / "lab")
    with pytest.raises(HTTPException) as caught:
        controller.start_optimize(
            OptimizePayload(
                base_key=Toy.key,
                symbol="BTC/USDT",
                timeframe="1h",
                params={"period": "abc"},
            )
        )
    assert caught.value.status_code == 400
    assert "period" in caught.value.detail


# ------------------------------------------------ 取消优化就是取消，不是「完成」
class _SlowLLM:
    """第一次调用就卡住，等测试点完取消再放行。"""

    def __init__(self) -> None:
        self.settings = SimpleNamespace(model="fake-model")
        self.called = threading.Event()
        self.release = threading.Event()

    def ask(self, prompt: str, **kwargs) -> dict:
        self.called.set()
        self.release.wait(10.0)
        return {
            "ok": True,
            "text": json.dumps(
                {"params": {"period": 30}, "hypothesis": "更长周期", "reason": "更平滑"}
            ),
            "tokens": 5,
            "finish": "stop",
            "truncated": False,
        }


class _FakeKlines:
    def fetch(self, market, symbol, timeframe, limit=1000):
        bars = 300
        rng = np.random.default_rng(7)
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


def _lab_controller(tmp_path: Path) -> LabController:
    strategy_dir = tmp_path / "strategies"
    strategy_dir.mkdir(parents=True, exist_ok=True)
    ctx = SimpleNamespace(
        settings=SimpleNamespace(ai=SimpleNamespace(enabled=True)),
        ai=_SlowLLM(),
        strategies=_Strategies(),
        klines=_FakeKlines(),
    )
    state = SimpleNamespace(ctx=ctx, locks={MarketType.SPOT: threading.RLock()})
    return LabController(lambda: state, CandidateStore(tmp_path), strategy_dir=strategy_dir)


def _wait_job(controller: LabController, job_id: str, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    job: dict = {}
    while time.time() < deadline:
        job = controller.get_optimize(job_id)
        if job.get("status") in ("done", "failed", "cancelled"):
            return job
        time.sleep(0.02)
    raise AssertionError(f"job 没有结束：{job}")


def test_cancelling_an_optimize_is_reported_as_cancelled(tmp_path):
    """点了「取消」，最后却报「优化完成」——前端那个 cancelled 分支永远是死的。"""
    controller = _lab_controller(tmp_path)
    job_id = controller.start_optimize(
        OptimizePayload(base_key=Toy.key, symbol="BTC/USDT", timeframe="1h", rounds=5)
    )["job_id"]

    assert controller.get_state().ctx.ai.called.wait(10.0)
    controller.cancel_optimize(job_id)
    controller.get_state().ctx.ai.release.set()

    job = _wait_job(controller, job_id)
    assert job["status"] == "cancelled"
    assert (job.get("result") or {}).get("stopped") == "cancel"
