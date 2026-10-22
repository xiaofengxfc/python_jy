"""预热不够时不许静默空转。

AutoTraderConfig.kline_limit 默认 500，而慢线填到 500 时需要 503 根：
_tick 每轮直接 return，界面显示在跑却什么都不做，日志里一个字都没有。
两件事：能多拉就多拉到够用（上限仍是交易所的 1000 根），真不够时给一条
限流日志并把状态放进 auto 的 status。
"""

from __future__ import annotations

import pandas as pd

from btrader.core.config import AppSettings, RiskSettings
from btrader.core.storage import Database
from btrader.core.types import MarketType
from btrader.engine.auto import AutoTrader, AutoTraderConfig
from btrader.engine.risk import RiskManager
from btrader.engine.trading import TradingService
from btrader.strategy.base import IntParam, Strategy
from btrader.strategy.builtin.ma_cross import MaCrossStrategy
from btrader.web.routes import AutoController

from fakes import FakeVenue
from helpers import make_candles


class StubKlines:
    """交易所替身：一次最多给 limit 根，并记下每次要了多少根。"""

    def __init__(self, available: int = 2000, drift: float = 0.0008) -> None:
        self.frame = make_candles(available, price=100.0, drift=drift, volatility=0.0)
        self.calls: list[int] = []

    def fetch(self, market, symbol, timeframe, limit: int = 1000, since=None, use_cache=True):
        self.calls.append(int(limit))
        return self.frame.tail(int(limit)).reset_index(drop=True)


class LongWindow(Strategy):
    """一个参数能填到 1200 的策略：预热根数超过交易所单次上限。"""

    key = "long_window"
    name = "长窗口"
    allow_short = False
    params_spec = [IntParam("window", "窗口", 1200, 2, 1500)]

    def generate(self, df, params):
        up = (df["close"] > df["close"].shift(1)).astype(float)
        return pd.DataFrame({"target": up}, index=df.index)


def _risk() -> RiskSettings:
    """上限全清零：这些用例关心的是预热，不是仓位限额。"""
    return RiskSettings(
        max_order_quote=0,
        max_order_pct=0,
        max_position_quote=0,
        max_leverage=20,
        max_daily_trades=0,
        max_daily_loss=0,
        max_consecutive_losses=0,
        require_confirmation=False,
    )


def _build(tmp_path, strategy, params, klines):
    settings = AppSettings(risk=_risk())
    venue = FakeVenue()
    db = Database(tmp_path / "warmup.db")
    trading = TradingService(settings, venue, RiskManager(settings.risk), db)
    config = AutoTraderConfig(
        symbol="BTC/USDT",
        market_type=MarketType.SPOT,
        timeframe="1h",
        params=params,
        position_pct=1.0,
        leverage=1.0,
    )
    trader = AutoTrader(config, strategy, trading, klines)
    logs: list[tuple[str, str]] = []
    trader.on_log(lambda level, message: logs.append((level, message)))
    return trader, venue, logs, db


def test_a_long_slow_period_pulls_enough_bars_instead_of_spinning(tmp_path):
    """慢线 500 要 503 根：拉到够用、正常出信号，而不是每轮静默返回。"""
    strategy = MaCrossStrategy()
    params = strategy.validate_params({"fast": 9, "slow": 500, "trend": 0, "ema": True})
    klines = StubKlines(available=2000)
    trader, venue, logs, db = _build(tmp_path, strategy, params, klines)
    try:
        trader._tick()
        status = trader.data_status()
    finally:
        db.close()

    # 默认的 500 根永远不够：要多少拉多少
    assert klines.calls == [503]
    assert status["required_bars"] == 503
    assert status["short"] is False and status["message"] == ""
    assert venue.fills, "拉到够用之后策略应该出信号并下单"
    assert logs, "出信号的那一轮要有日志，不能什么都说不出"


def test_a_warmup_beyond_the_venue_limit_is_reported_once(tmp_path):
    """要的根数超过 1000：说清楚「K 线不足 N 根，请把周期改小」，只说一次。"""
    strategy = LongWindow()
    params = strategy.validate_params({})
    klines = StubKlines(available=2000)
    trader, venue, logs, db = _build(tmp_path, strategy, params, klines)
    try:
        for _ in range(3):  # 轮询每 10 秒一轮，三轮回放
            trader._tick()
        status = trader.data_status()
    finally:
        db.close()

    warnings = [message for level, message in logs if level == "WARN"]
    assert klines.calls == [], "拉不到就别每轮白跑一次请求"
    assert venue.fills == []
    assert len(warnings) == 1, f"同一句不足只报一次，实际 {warnings}"
    assert "K 线不足" in warnings[0]
    assert "1203" in warnings[0]
    assert "请把周期改小" in warnings[0]
    assert status["short"] is True
    assert status["required_bars"] == 1203
    assert status["fetched_bars"] == 0
    assert status["max_bars"] == 1000
    assert "K 线不足" in status["message"]


def test_a_short_history_is_reported_instead_of_spinning(tmp_path):
    """交易所只给了几根（新上市）：同样要说出来，而不是静默空转。"""
    strategy = MaCrossStrategy()
    params = strategy.validate_params({"fast": 9, "slow": 21, "trend": 0, "ema": True})
    klines = StubKlines(available=10)
    trader, venue, logs, db = _build(tmp_path, strategy, params, klines)
    try:
        trader._tick()
        trader._tick()
        status = trader.data_status()
    finally:
        db.close()

    warnings = [message for level, message in logs if level == "WARN"]
    assert venue.fills == []
    assert len(warnings) == 1, f"两轮只报一次，实际 {warnings}"
    assert "K 线不足" in warnings[0]
    assert "10 根" in warnings[0] and "24 根" in warnings[0]
    assert status["short"] is True
    assert status["fetched_bars"] == 10


def test_the_auto_status_payload_carries_the_warmup_state(tmp_path):
    """状态里加了一个 data 字段，既有字段一个没动。"""
    strategy = LongWindow()
    params = strategy.validate_params({})
    trader, _venue, _logs, db = _build(tmp_path, strategy, params, StubKlines())
    try:
        trader._tick()
        controller = AutoController()
        controller._trader = trader
        controller.config = {"symbol": "BTC/USDT"}
        payload = controller.status()
    finally:
        db.close()

    assert {"running", "config", "last_signal", "seq", "events"} <= set(payload)
    assert payload["data"]["short"] is True
    assert payload["data"]["required_bars"] == 1203
    assert "K 线不足" in payload["data"]["message"]
    # 没有引擎的时候这个字段是 null，不会假装有数据
    controller._trader = None
    assert controller.status()["data"] is None