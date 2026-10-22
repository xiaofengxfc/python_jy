"""AI 量化：候选批准（注册成策略）与回测记录的两个坑。

两条都在「候选 → 部署」这条链上，而且都是用户看不出来原因的那种：

* 批准时候候选源码里的 ``key = "..."`` 能不能改对。改写判据和注册时的静态检查
  必须是同一次 AST 走法，否则「类里的 key」会被改到别的行上，批准永远失败。
* 候选回测记录写库的指标必须是 JSON 能表达的数。NaN 写得进文件、读得回来，
  但接口序列化不接受，整个候选列表从此 500。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.responses import JSONResponse

from btrader.core.types import MarketType
from btrader.strategy.base import IntParam
from btrader.strategy.candidates import CandidateStore
from btrader.strategy.registry import StrategyRegistry
from btrader.web.routes_lab import (
    CandidateBacktestPayload,
    CandidateGeneratePayload,
    CandidateReviewPayload,
    LabController,
)


class FakeLLM:
    """只回一条固定回答的模型，接口形状和 llm.ask 一致。"""

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.settings = SimpleNamespace(model="fake-model")

    def ask(self, prompt: str, **kwargs) -> dict:
        return {
            "ok": True,
            "text": json.dumps(self.payload, ensure_ascii=False),
            "tokens": 1,
            "finish": "stop",
            "truncated": False,
        }


class FakeStrategies:
    def __init__(self, directory: Path) -> None:
        self.registry = StrategyRegistry()
        self.registry.discover([directory])

    def get(self, key: str):
        return self.registry.get(key)

    def source_path(self, key: str):
        return self.registry.source_path(key)

    def is_builtin(self, key: str) -> bool:
        return self.registry.is_builtin(key)

    def is_overridden(self, key: str) -> bool:
        return self.registry.is_overridden(key)

    def discover(self, directories=None) -> None:
        self.registry.discover(directories)

    def __contains__(self, key: str) -> bool:
        return key in self.registry

    @property
    def keys(self) -> list[str]:
        return self.registry.keys


def make_controller(tmp_path: Path, payload: dict) -> LabController:
    strategy_dir = tmp_path / "strategies"
    strategy_dir.mkdir(parents=True, exist_ok=True)
    ctx = SimpleNamespace(
        settings=SimpleNamespace(ai=SimpleNamespace(enabled=True)),
        ai=FakeLLM(payload),
        strategies=FakeStrategies(strategy_dir),
    )
    state = SimpleNamespace(ctx=ctx, locks={MarketType.SPOT: threading.RLock()})
    return LabController(lambda: state, CandidateStore(tmp_path), strategy_dir=strategy_dir)


# ------------------------------------- 批准：改的是类属性 key，不是别的同名赋值
# 模型给的代码里，类前面完全可能有同名的 ``key``：模块级常量，或者某个小工具
# 函数里的局部变量。旧实现用正则取「文件里第一个 key = "..."」，改的就是那一行。
TRICKY_CODE = '''key = "module_level_constant"

from btrader.strategy.base import IntParam, Strategy


def pick(frame):
    key = "close"
    return frame[key]


class Gen(Strategy):
    key = "gen_x"
    name = "生成的策略"
    params_spec = [IntParam("period", "周期", 20, 2, 200)]

    def generate(self, df, params):
        ma = df["close"].rolling(int(params["period"])).mean()
        return df.assign(target=(df["close"] > ma).astype(float))[["target"]]
'''


def test_approval_rewrites_the_class_key_not_a_module_constant(tmp_path: Path):
    """模块级常量、函数里的局部变量都叫 key 时，批准仍然要改对类属性那一行。

    旧实现改的是文件里第一个 ``key = "..."``（这里是模块级常量那一行），类里的
    key 一字未动，save_source 于是报「类里的 key='gen_x' 与文件名
    key='module_level_constant' 不一致」——用户既没写过这两个 key，也没法绕过。
    """
    controller = make_controller(
        tmp_path,
        {"strategy_key": "module_level_constant", "name": "生成", "code": TRICKY_CODE},
    )
    candidate_id = controller.generate(CandidateGeneratePayload())["candidate"]["id"]

    approved = controller.review(
        candidate_id, CandidateReviewPayload(approve=True, note="可以")
    )["candidate"]

    key = approved["registered_key"]
    assert key == "module_level_constant"
    written = (tmp_path / "strategies" / f"{key}.py").read_text(encoding="utf-8")
    # 模块级常量和小工具函数的局部变量都保持原样
    assert 'key = "module_level_constant"' in written.splitlines()[0]
    assert '    key = "close"' in written
    # 类属性被改成最终注册用的 key，注册表里也是这个 key
    class_lines = [line.strip() for line in written.splitlines()]
    assert class_lines.count(f'key = "{key}"') == 2  # 一行模块级常量 + 一行类属性
    assert key in controller.get_state().ctx.strategies
    assert controller.get_state().ctx.strategies.source_path(key) is not None


# ------------------------------------------- 回测记录：指标必须是 JSON 能表达的数
def test_a_nan_metric_cannot_brick_the_candidate_list(tmp_path: Path):
    """一条 NaN 指标不许把整个候选列表变成 500。

    ``json.dumps`` 默认把 NaN 写成 NaN：文件写得下，``json.loads`` 也读得回，
    但接口响应用的序列化（``allow_nan=False``）会直接抛 ValueError。候选列表是
    AI 量化页唯一的读接口，写坏一次就再也打不开，只能去数据目录手改 JSON。
    """
    store = CandidateStore(tmp_path)
    candidate = store.create('key = "x"\n', strategy_key="gen_x", name="生成")

    store.record_backtest(
        candidate.id,
        {
            "trades": 3,
            "total_return": 0.12,
            "sharpe": float("nan"),
            "max_drawdown": float("-inf"),
            "symbol": "BTC/USDT",
        },
    )

    stored = json.loads((tmp_path / f"{candidate.id}.json").read_text(encoding="utf-8"))
    assert stored["backtest"]["trades"] == 3
    assert stored["backtest"]["symbol"] == "BTC/USDT"
    assert "sharpe" not in stored["backtest"]
    assert "max_drawdown" not in stored["backtest"]
    # 列表接口要能把它序列化成响应，这一步在旧行为下直接 ValueError
    JSONResponse({"items": [item.to_dict() for item in store.list()]})


def test_the_backtest_record_keeps_the_keys_the_quant_page_reads(tmp_path: Path):
    """写库的键要和界面读的键对上：少一个，页面上就是「—」或部署门槛误判。

    界面（AIQuantPage / MetricGrid）读 total_return、max_drawdown、sharpe、trades，
    部署前还要用 trades 和 total_return 判「有没有回测成交、是不是正收益」。
    """
    controller = make_controller(
        tmp_path, {"strategy_key": "gen_x", "code": 'key = "gen_x"\n'}
    )
    candidate = controller.candidates.create(
        'key = "gen_x"\n', strategy_key="gen_x", name="生成"
    )

    result = controller.record_backtest(
        candidate.id,
        CandidateBacktestPayload(
            metrics={
                "total_return": 0.12,
                "max_drawdown": -0.05,
                "sharpe": 1.4,
                "trades": 7.0,
            },
            symbol="BTC/USDT",
            timeframe="1h",
        ),
    )

    record = result["candidate"]["backtest"]
    for key in ("total_return", "max_drawdown", "sharpe", "trades"):
        assert key in record, f"界面要读 {key}"
    assert record["trades"] == 7.0
    assert record["symbol"] == "BTC/USDT"
    assert record["timeframe"] == "1h"
    # 重新读一遍盘上的记录，字段不能在这一步丢掉
    assert controller.get_candidate(candidate.id)["candidate"]["backtest"] == record
