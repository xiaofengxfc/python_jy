"""回测模型：强平、资金费率、跳空成交、最小下单量。

四件真实成本，外加一条护栏：新成本关掉时（``funding=False, liquidate=False``、
没给最小量与步长）引擎的数字必须和加这些功能之前**逐位相同**。护栏用改前引擎
跑出来的数直接钉住，改一行都要能解释。
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from btrader.backtest import BacktestConfig, grid_search, run_backtest
from btrader.core.contracts import ContractSpec
from btrader.core.types import MarketType
from btrader.strategy.base import Strategy
from btrader.strategy.builtin import MaCrossStrategy
from btrader.web.routes import (
    BacktestPayload,
    _apply_funding,
    _funding_rates,
    _serialize_result,
    _validate_backtest,
)

from helpers import make_candles

START = 1_700_000_000_000
STEP = 3_600_000


class AlwaysLong(Strategy):
    key = "always_long"
    name = "always long"
    allow_short = False

    def generate(self, df, params):
        return pd.DataFrame({"target": np.ones(len(df))}, index=df.index)


class AlwaysShort(Strategy):
    key = "always_short"
    name = "always short"
    allow_short = True

    def generate(self, df, params):
        return pd.DataFrame({"target": -np.ones(len(df))}, index=df.index)


def bars(rows) -> pd.DataFrame:
    """rows: [(open, high, low, close), ...] -> K 线，时间戳按小时递增。"""
    frame = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    frame["timestamp"] = START + np.arange(len(frame)) * STEP
    frame["volume"] = 1.0
    return frame[["timestamp", "open", "high", "low", "close", "volume"]]


def flat(n: int, price: float = 100.0) -> pd.DataFrame:
    return bars([(price, price, price, price)] * n)


def _cfg(**kwargs) -> BacktestConfig:
    base = dict(initial_capital=10_000.0, fee_rate=0.0, slippage_bps=0.0)
    base.update(kwargs)
    return BacktestConfig(**base)


# --------------------------------------------------------------- 1) 强平
def test_a_long_is_liquidated_at_the_maintenance_margin_price():
    # 10 倍多单，维持保证金 0.5% -> 强平价 = 100 * (1 - 0.1 + 0.005) = 90.5
    data = bars([(100, 101, 99, 100), (100, 101, 99, 100), (100, 101, 88, 89), (100, 101, 99, 100)])
    result = run_backtest(data, AlwaysLong(), {}, _cfg(leverage=10.0))
    liquidation = [t for t in result.trades if t.exit_reason == "liquidation"]
    assert len(liquidation) == 1
    assert liquidation[0].exit_price == pytest.approx(90.5)
    assert result.metrics["liquidations"] == 1
    # 亏掉本金的 95%，cash 不为负
    assert result.metrics["final_equity"] == pytest.approx(500.0)
    assert result.metrics["final_equity"] >= 0.0


def test_a_short_is_liquidated_on_the_way_up():
    # 空：强平价 = 100 * (1 + 0.1 - 0.005) = 109.5
    data = bars([(100, 100, 100, 100), (100, 101, 99, 100), (100, 112, 100, 111), (100, 101, 99, 100)])
    result = run_backtest(data, AlwaysShort(), {}, _cfg(leverage=10.0))
    liquidation = [t for t in result.trades if t.exit_reason == "liquidation"]
    assert len(liquidation) == 1
    assert liquidation[0].side == "short"
    assert liquidation[0].exit_price == pytest.approx(109.5)


def test_liquidation_is_off_at_leverage_one():
    # 现货就是这一档：L=1 时强平价 = entry * m，永远不会到
    data = bars([(100, 101, 99, 100), (100, 101, 80, 81), (100, 101, 99, 100)])
    result = run_backtest(data, AlwaysLong(), {}, _cfg(leverage=1.0))
    assert result.metrics["liquidations"] == 0
    assert all(t.exit_reason != "liquidation" for t in result.trades)


def test_liquidation_can_be_switched_off():
    data = bars([(100, 101, 99, 100), (100, 101, 99, 100), (100, 101, 88, 89), (100, 101, 99, 100)])
    result = run_backtest(data, AlwaysLong(), {}, _cfg(leverage=10.0, liquidate=False))
    assert result.metrics["liquidations"] == 0
    # 没强平就是浮亏继续扛着，最后按收盘平掉
    assert result.metrics["final_equity"] > 0.0


def test_a_gapped_liquidation_fills_at_the_open():
    # 开盘就在强平价之外：成交价是开盘价 85，不是 90.5
    data = bars([(100, 101, 99, 100), (100, 101, 99, 100), (85, 86, 84, 85)])
    result = run_backtest(data, AlwaysLong(), {}, _cfg(leverage=10.0))
    liquidation = [t for t in result.trades if t.exit_reason == "liquidation"]
    assert liquidation[0].exit_price == pytest.approx(85.0)
    # 亏光的账户归零，不出现负现金
    assert result.metrics["final_equity"] == pytest.approx(0.0)
    assert any("亏光" in w for w in result.warnings)


def test_no_re_entry_on_the_bar_that_liquidated():
    data = bars([(100, 101, 99, 100), (100, 101, 99, 100), (100, 101, 88, 89), (100, 101, 99, 100), (100, 101, 99, 100)])
    result = run_backtest(data, AlwaysLong(), {}, _cfg(leverage=10.0))
    # 目标一直是 1，但强平那根之后不许再进场：只剩强平这一笔
    assert len(result.trades) == 1
    assert result.equity["position"].iloc[-1] == 0.0


# --------------------------------------------------------------- 2) 资金费率
def test_funding_is_charged_to_a_long():
    data = flat(6)
    rates = {START + 2 * STEP: 0.001}
    plain = run_backtest(data, AlwaysLong(), {}, _cfg(funding=False))
    charged = run_backtest(data, AlwaysLong(), {}, _cfg(funding=True), funding_rates=rates)
    # 2 倍杠杆、100 本金价位：qty = 10000 * 1 / 100 = 100 个币
    expected = 100.0 * 100.0 * 0.001
    assert charged.metrics["funding_paid"] == pytest.approx(expected)
    assert charged.metrics["final_equity"] == pytest.approx(
        plain.metrics["final_equity"] - expected
    )


def test_funding_pays_a_short():
    data = flat(6)
    rates = {START + 2 * STEP: 0.001}
    plain = run_backtest(data, AlwaysShort(), {}, _cfg(funding=False))
    charged = run_backtest(data, AlwaysShort(), {}, _cfg(funding=True), funding_rates=rates)
    # 空头 qty 为负，同样的费率是收钱
    assert charged.metrics["funding_paid"] == pytest.approx(-10.0)
    assert charged.metrics["final_equity"] == pytest.approx(
        plain.metrics["final_equity"] + 10.0
    )


def test_no_funding_data_warns_instead_of_pretending():
    data = flat(6)
    result = run_backtest(data, AlwaysLong(), {}, _cfg(funding=True), funding_rates=None)
    assert result.metrics["funding_paid"] == 0.0
    assert any("资金费率" in w for w in result.warnings)
    # 关掉时不放这条警告：用户本来就没要这个成本
    off = run_backtest(data, AlwaysLong(), {}, _cfg(funding=False))
    assert not any("资金费率" in w for w in off.warnings)


# ------------------------------------------------------- 3) 跳空穿过止损止盈
def test_a_gap_through_the_stop_fills_at_the_open():
    # 止损 98，第二根开盘 95：按 95 成交，不是 98
    data = bars([(100, 100, 100, 100), (100, 100, 100, 100), (95, 96, 94, 95)])
    result = run_backtest(data, AlwaysLong(), {}, _cfg(stop_loss_pct=0.02))
    trade = result.trades[0]
    assert trade.exit_reason == "stop_loss"
    assert trade.exit_price == pytest.approx(95.0)


def test_a_gap_through_the_take_profit_fills_at_the_open():
    data = bars([(100, 100, 100, 100), (100, 100, 100, 100), (106, 107, 105, 106)])
    result = run_backtest(data, AlwaysLong(), {}, _cfg(take_profit_pct=0.02))
    trade = result.trades[0]
    assert trade.exit_reason == "take_profit"
    assert trade.exit_price == pytest.approx(106.0)


def test_a_normal_stop_still_fills_at_the_level():
    data = bars([(100, 100, 100, 100), (100, 100, 100, 100), (100, 100, 97, 98)])
    result = run_backtest(data, AlwaysLong(), {}, _cfg(stop_loss_pct=0.02))
    assert result.trades[0].exit_price == pytest.approx(98.0)


# ------------------------------------------------------ 4) 最小下单量与精度
def test_sizes_below_the_minimum_are_skipped_with_a_warning():
    data = flat(6)
    result = run_backtest(data, AlwaysLong(), {}, _cfg(min_amount=1_000.0))
    assert result.trades == []
    assert result.metrics["final_equity"] == pytest.approx(10_000.0)
    assert any("最小下单量" in w and "1" in w for w in result.warnings)


def test_sizes_are_truncated_to_the_amount_step():
    data = flat(6)
    result = run_backtest(data, AlwaysLong(), {}, _cfg(amount_step=3.0))
    # 100 个币按 3 截断 = 99
    assert result.trades[0].qty == pytest.approx(99.0)


def test_the_step_never_rounds_a_size_up():
    data = flat(6)
    result = run_backtest(data, AlwaysLong(), {}, _cfg(amount_step=7.0))
    assert result.trades[0].qty == pytest.approx(98.0)


# ------------------------------------------------------------ 5) 护栏：老数字
def test_the_legacy_numbers_survive_when_the_new_costs_are_off():
    """funding=False + liquidate=False 必须和加这些成本之前的引擎逐位相同。

    这两个数是拿改前的引擎跑出来的（同一份 make_candles），不是重新算的近似值。
    """
    data = make_candles(300, seed=3)
    legacy = run_backtest(
        data,
        MaCrossStrategy(),
        {"fast": 5, "slow": 20, "trend": 0, "ema": True},
        BacktestConfig(
            initial_capital=10_000,
            fee_rate=0.001,
            slippage_bps=2,
            leverage=5.0,
            funding=False,
            liquidate=False,
        ),
    )
    assert legacy.metrics["final_equity"] == 17317.298315455417
    assert legacy.metrics["sharpe"] == pytest.approx(6.027765781916432, rel=0, abs=0)
    assert legacy.metrics["max_drawdown"] == -0.5485435868339945
    assert len(legacy.trades) == 14
    first = legacy.trades[0]
    assert (first.side, first.bars, first.exit_reason) == ("short", 3, "signal")
    assert first.entry_price == 98.34624881183427
    assert first.exit_price == 100.63964467490406
    assert first.pnl == pytest.approx(-1266.8928964301087, rel=0, abs=0)
    assert float(legacy.metrics["fee_paid"]) == pytest.approx(1046.49574041109, rel=0, abs=0)


def test_the_legacy_stop_numbers_survive_too():
    data = make_candles(50, drift=-0.01, volatility=0.005, seed=5)
    legacy = run_backtest(
        data,
        AlwaysLong(),
        {},
        BacktestConfig(
            fee_rate=0.0, slippage_bps=0.0, stop_loss_pct=0.02, funding=False, liquidate=False
        ),
    )
    assert legacy.metrics["final_equity"] == 9800.0
    assert legacy.trades[0].exit_price == 96.636626101154
    assert legacy.trades[0].exit_reason == "stop_loss"


# --------------------------------------------------------------- 6) 路由
class _Registry:
    def __init__(self, strategy: Strategy) -> None:
        self._strategy = strategy

    def get(self, key: str) -> Strategy:
        if key != self._strategy.key:
            raise KeyError(f"未知策略: {key}")
        return self._strategy


class _SpecRest:
    def __init__(self, spec: ContractSpec) -> None:
        self.spec = spec

    def contract_spec(self, market, symbol, allow_network=True):
        return self.spec


def _state(spec: ContractSpec | None = None, rest=None):
    target = rest or _SpecRest(spec or ContractSpec())
    ctx = SimpleNamespace(
        strategies=_Registry(MaCrossStrategy()),
        # 费率要带 Key 的那个客户端（rest_for）；行情客户端（market_rest_for）没有凭据，
        # 拿它取费率永远是空的。替身两边都给同一个对象。
        rest_for=lambda market: target,
        market_rest_for=lambda market: target,
    )
    return SimpleNamespace(ctx=ctx, locks={market: threading.RLock() for market in MarketType})


def _payload(**overrides) -> BacktestPayload:
    base = dict(market="usdm", symbol="BTC/USDT:USDT", timeframe="1h", strategy="ma_cross")
    base.update(overrides)
    return BacktestPayload(**base)


def test_the_route_carries_the_new_fields_into_the_config():
    state = _state(ContractSpec(contract_size=1.0, base="BTC", quote="USDT", min_amount=0.001))
    _market_type, _strategy, _params, config = _validate_backtest(state, _payload())
    assert config.liquidate is True
    assert config.funding is True
    assert config.maintenance_margin_rate == pytest.approx(0.005)
    assert config.min_amount == pytest.approx(0.001)


def test_spot_gets_no_funding_and_no_liquidation():
    """现货没有资金费，也强平不了：这两条按市场收死，和杠杆、做空同一条口径。"""
    state = _state(ContractSpec(contract_size=1.0, base="BTC", quote="USDT", min_amount=0.00001))
    _market_type, _strategy, _params, config = _validate_backtest(
        state, _payload(market="spot", symbol="BTC/USDT", leverage=10.0)
    )
    assert config.funding is False
    assert config.liquidate is False
    assert config.leverage == 1.0
    assert config.min_amount == pytest.approx(0.00001)


def test_the_payload_can_switch_funding_and_liquidation_off():
    state = _state()
    _market_type, _strategy, _params, config = _validate_backtest(
        state, _payload(funding=False, liquidate=False)
    )
    assert config.funding is False
    assert config.liquidate is False


def test_the_payload_can_override_the_spec_minimum():
    state = _state(ContractSpec(contract_size=1.0, base="BTC", quote="USDT", min_amount=0.001))
    _market_type, _strategy, _params, config = _validate_backtest(
        state, _payload(min_amount=0.5, amount_step=0.1)
    )
    assert config.min_amount == pytest.approx(0.5)
    assert config.amount_step == pytest.approx(0.1)


class _FundingRest:
    """假的交易所：按 since 往后给一页，最多 limit 条（不传 since 的调用这里不模拟）。

    `page_cap` 用它模拟「交易所只肯给这么多」的翻不满情形。
    """

    def __init__(self, rows, error: Exception | None = None, page_cap: int | None = None) -> None:
        self.rows = sorted(rows, key=lambda row: row["timestamp"])
        self.error = error
        self.page_cap = page_cap
        self.calls: list[tuple] = []

    def fetch_funding_rate_history(self, market, symbol, since, limit):
        if self.error:
            raise self.error
        self.calls.append((since, limit))
        rows = [row for row in self.rows if since is None or row["timestamp"] >= since]
        if self.page_cap is not None:
            rows = rows[: self.page_cap]
        return rows[:limit]


def test_funding_rates_are_keyed_by_settlement_time():
    frame = flat(6)
    rest = _FundingRest([{"timestamp": START + STEP, "rate": 0.0001}])
    rates, note = _funding_rates(_state(rest=rest), MarketType.USDM, "BTC/USDT:USDT", frame)
    assert rates == {START + STEP: 0.0001}
    assert isinstance(note, str)


def test_funding_rates_understand_the_ccxt_row_shape():
    """交易所那位的函数返回 ccxt 原样行：字段叫 fundingRate，不是 rate。"""
    frame = flat(6)
    rest = _FundingRest([{"timestamp": START + STEP, "fundingRate": 0.0002}])
    rates, note = _funding_rates(_state(rest=rest), MarketType.USDM, "BTC/USDT:USDT", frame)
    assert rates == {START + STEP: 0.0002}
    assert isinstance(note, str)

def test_funding_rates_are_none_when_the_venue_has_no_history():
    frame = flat(6)
    # 交易所还没提供这个接口：不许 import 不存在的名字，也不许假装有数据
    assert _funding_rates(_state(rest=object()), MarketType.USDM, "BTC/USDT:USDT", frame)[0] is None
    broken = _FundingRest([], error=RuntimeError("boom"))
    assert _funding_rates(_state(rest=broken), MarketType.USDM, "BTC/USDT:USDT", frame)[0] is None
    empty = _FundingRest([])
    assert _funding_rates(_state(rest=empty), MarketType.USDM, "BTC/USDT:USDT", frame)[0] is None


def test_funding_rates_are_not_fetched_for_spot():
    frame = flat(6)
    rest = _FundingRest([{"timestamp": START, "rate": 0.01}])
    assert _funding_rates(_state(rest=rest), MarketType.SPOT, "BTC/USDT", frame)[0] is None


def test_the_serialized_result_carries_the_warnings():
    data = flat(6)
    result = run_backtest(data, AlwaysLong(), {}, _cfg(funding=True), funding_rates=None)
    payload = _serialize_result(result, {})
    assert payload["warnings"] == result.warnings
    assert payload["metrics"]["funding_paid"] == 0.0


# -------------------------------------- 7) 步长接规格 + 资金费翻页（t59）
def funding_frame(days: int, step_ms: int = 8 * 3_600_000) -> pd.DataFrame:
    n = days * 24 * 3_600_000 // step_ms
    frame = pd.DataFrame(
        {
            "open": 100.0,
            "high": 100.0,
            "low": 100.0,
            "close": 100.0,
        },
        index=range(n),
    )
    frame["timestamp"] = START + np.arange(n) * step_ms
    frame["volume"] = 1.0
    return frame[["timestamp", "open", "high", "low", "close", "volume"]]


def _funding_rows_between(frame: pd.DataFrame, rate: float = 0.0001) -> list[dict]:
    return [{"timestamp": int(stamp), "fundingRate": rate} for stamp in frame["timestamp"]]


def test_the_spec_step_is_used_when_the_payload_does_not_send_one():
    """t57 把步长放进 ContractSpec 了：调用方不传就别自己空着。"""
    state = _state(
        ContractSpec(
            contract_size=1.0, base="BTC", quote="USDT", min_amount=0.001, amount_step=0.001
        )
    )
    _market_type, _strategy, _params, config = _validate_backtest(state, _payload())
    assert config.min_amount == pytest.approx(0.001)
    assert config.amount_step == pytest.approx(0.001)
    assert config.notes == []


def test_the_payload_still_wins_over_the_spec():
    state = _state(
        ContractSpec(
            contract_size=1.0, base="BTC", quote="USDT", min_amount=0.001, amount_step=0.001
        )
    )
    _market_type, _strategy, _params, config = _validate_backtest(
        state, _payload(min_amount=0.5, amount_step=0.25)
    )
    assert config.min_amount == pytest.approx(0.5)
    assert config.amount_step == pytest.approx(0.25)


def test_a_spot_step_reaches_the_engine():
    state = _state(
        ContractSpec(
            contract_size=1.0, base="BTC", quote="USDT", min_amount=1e-05, amount_step=1e-05
        )
    )
    _market_type, _strategy, _params, config = _validate_backtest(
        state, _payload(market="spot", symbol="BTC/USDT", initial_capital=10_000.0)
    )
    assert config.amount_step == pytest.approx(1e-05)
    result = run_backtest(flat(6), AlwaysLong(), {}, config)
    # 10000 / 100 = 100 个币，按 1e-05 截断还是 100
    assert result.trades[0].qty == pytest.approx(100.0)
    assert result.warnings == []


def test_the_coin_margined_limits_are_not_folded_without_a_price():
    """币本位的最小量与步长都是「张」。折成币要价格，而引擎的数量单位是币——
    与其一边折一边不折，不如两样都不折，并且说出来。"""
    state = _state(
        ContractSpec(
            contract_size=100.0,
            inverse=True,
            base="BTC",
            quote="USD",
            settle="BTC",
            min_amount=1.0,
            amount_step=1.0,
        )
    )
    _market_type, _strategy, _params, config = _validate_backtest(
        state, _payload(market="coinm", symbol="BTC/USD:BTC")
    )
    assert config.min_amount is None
    assert config.amount_step is None
    assert any("币本位" in note and "折算" in note for note in config.notes)


def test_the_coin_margined_note_reaches_the_result_warnings():
    state = _state(
        ContractSpec(contract_size=100.0, inverse=True, base="BTC", quote="USD", settle="BTC")
    )
    _market_type, _strategy, _params, config = _validate_backtest(
        state, _payload(market="coinm", symbol="BTC/USD:BTC", funding=False)
    )
    result = run_backtest(flat(6), AlwaysLong(), {}, config)
    assert any("币本位" in warning for warning in result.warnings)


def test_funding_history_pages_until_the_window_is_covered():
    """800 天的窗口要翻三页：一页 1000 条只够 333 天，后半段静默漏掉就是假乐观。"""
    frame = funding_frame(800)
    rest = _FundingRest(_funding_rows_between(frame))
    rates, note = _funding_rates(_state(rest=rest), MarketType.USDM, "BTC/USDT:USDT", frame)
    assert rates is not None
    assert len(rest.calls) > 1
    assert min(rates) == int(frame["timestamp"].iloc[0])
    assert max(rates) == int(frame["timestamp"].iloc[-1])
    assert len(rates) == len(frame)
    assert note == ""


def test_funding_pages_that_stop_short_say_where_they_stopped():
    frame = funding_frame(800)
    rest = _FundingRest(_funding_rows_between(frame), page_cap=200)
    rates, note = _funding_rates(_state(rest=rest), MarketType.USDM, "BTC/USDT:USDT", frame)
    assert rates is not None
    assert max(rates) < int(frame["timestamp"].iloc[-1])
    assert len(rest.calls) == 5, "翻页要有上限，不能无限翻"
    assert "只覆盖到" in note


def test_the_optimize_path_receives_the_funding_rates():
    """参数优化走的是同一个 run_backtest，别只有单次回测扣了资金费。"""
    data = flat(6)
    rates = {START + 2 * STEP: 0.001}
    table = grid_search(
        data, AlwaysLong(), {}, _cfg(funding=True), funding_rates=rates
    )
    assert float(table["m_funding_paid"].iloc[0]) == pytest.approx(10.0)


def test_a_short_funding_page_ends_up_in_the_result_warnings():
    """翻不满这件事要一路走到用户眼前：routes 挂到 config.notes，引擎并进 warnings。"""
    frame = funding_frame(800)
    rest = _FundingRest(_funding_rows_between(frame), page_cap=200)
    config = _cfg(funding=True)
    rates = _apply_funding(_state(rest=rest), MarketType.USDM, "BTC/USDT:USDT", frame, config)
    result = run_backtest(frame, AlwaysLong(), {}, config, funding_rates=rates)
    assert any("只覆盖到" in warning for warning in result.warnings)
    # 同一条说明不重复堆：优化那条路会把同一份 config 用很多次
    _apply_funding(_state(rest=rest), MarketType.USDM, "BTC/USDT:USDT", frame, config)
    assert sum(1 for note in config.notes if "只覆盖到" in note) == 1


def test_every_job_takes_its_funding_through_the_same_helper():
    """单次回测、参数优化、AI 复核三条路都走 _apply_funding。

    直调 _funding_rates 会漏掉「只覆盖到 X」那条说明（它现在返回 (rates, note)），
    漏一次就等于静默少扣资金费——这条是防回退的。
    """
    import inspect

    from btrader.web import routes as routes_module

    source = inspect.getsource(routes_module)
    assert "_funding_rates(state, market, payload.symbol, frame)" not in source
    assert source.count("_apply_funding(state, market, payload.symbol, frame, config)") == 3

def test_funding_comes_from_the_signed_client_not_the_charting_one(monkeypatch):
    """费率要用带 Key 的交易客户端取。

    行情客户端（market_rest_for）是只读公开的那种，fetch_funding_rate_history 见到没
    凭据就直接回 []，于是测试网这一路永远「没有资金费率数据」，静默按零成本算完。
    实测在 8765 上撞到：三组回测（不算成本 / 加资金费+强平 / 只加资金费）数字一模一样。
    """
    from types import SimpleNamespace

    import pandas as pd

    from btrader.core.types import MarketType
    from btrader.web import routes as routes_mod

    frame = pd.DataFrame({"timestamp": [1_700_000_000_000, 1_700_028_800_000]})
    calls: list[str] = []

    class _Rest:
        def fetch_funding_rate_history(self, market, symbol, since=None, limit=1000):
            calls.append("signed")
            return [{"timestamp": 1_700_028_800_000, "fundingRate": 0.0001}]

    class _PublicRest(_Rest):
        def fetch_funding_rate_history(self, market, symbol, since=None, limit=1000):
            calls.append("public")
            return []

    signed, public = _Rest(), _PublicRest()
    ctx = SimpleNamespace(
        rest_for=lambda market: signed,
        market_rest_for=lambda market: public,
    )
    state = SimpleNamespace(ctx=ctx, locks={m: __import__("threading").RLock() for m in MarketType})
    rates, note = routes_mod._funding_rates(state, MarketType.USDM, "BTC/USDT:USDT", frame)
    assert calls == ["signed"]
    assert rates == {1_700_028_800_000: 0.0001}
    assert note == ""

def test_a_window_that_ends_before_the_next_settlement_is_fully_covered():
    """数据末尾 11:00、最后一条结算 08:00 是正常覆盖，不该报「只覆盖到」。

    资金费 8 小时结一次，末尾差不到一个周期就说明这一段的每一次结算都拿到了。
    以前直接拿 max(rates) < last 判，于是每一份真实数据都会挂一条假警报。
    """
    from types import SimpleNamespace

    import pandas as pd

    from btrader.core.types import MarketType
    from btrader.web import routes as routes_mod

    hour = 3600_000
    settled = 1_700_006_400_000
    last_bar = settled + 3 * hour  # 08:00 结算，数据到 11:00
    frame = pd.DataFrame({"timestamp": [settled - 40 * hour, last_bar]})

    class _Rest:
        def fetch_funding_rate_history(self, market, symbol, since=None, limit=1000):
            return [{"timestamp": settled, "fundingRate": 0.0001}]

    rest = _Rest()
    ctx = SimpleNamespace(rest_for=lambda market: rest, market_rest_for=lambda market: rest)
    state = SimpleNamespace(ctx=ctx, locks={m: __import__("threading").RLock() for m in MarketType})
    rates, note = routes_mod._funding_rates(state, MarketType.USDM, "BTC/USDT:USDT", frame)
    assert note == ""
    assert rates == {settled: 0.0001}


def test_funding_off_means_no_fetch_and_no_note():
    """关了资金费就别去取费率，更别把「只覆盖到 X」挂上去。"""
    from types import SimpleNamespace

    import pandas as pd

    from btrader.core.types import MarketType
    from btrader.web import routes as routes_mod

    frame = pd.DataFrame({"timestamp": [1_700_000_000_000, 1_700_028_800_000]})
    calls: list[int] = []

    class _Rest:
        def fetch_funding_rate_history(self, market, symbol, since=None, limit=1000):
            calls.append(1)
            return []

    rest = _Rest()
    ctx = SimpleNamespace(rest_for=lambda market: rest, market_rest_for=lambda market: rest)
    state = SimpleNamespace(ctx=ctx, locks={m: __import__("threading").RLock() for m in MarketType})
    config = SimpleNamespace(funding=False, notes=[])
    assert routes_mod._apply_funding(state, MarketType.USDM, "BTC/USDT:USDT", frame, config) is None
    assert calls == []
    assert config.notes == []
