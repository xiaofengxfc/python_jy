"""自动交易的三条硬约束：现货按账本持仓、按市场取客户端、失败要重试。

三条都是实测撞出来的：

* 现货「持仓」以前读的是钱包余额。钱包里有用户自己的 1 BTC 时，策略给 ``target=0``
  会算出「应卖 0 - 1.0004」，把用户自己的币一起卖掉（AGENTS 第 4 节只堵住了
  ``/api/positions/close`` 那一处）。
* 引擎用 ``trading.rest``（全局市场客户端）读余额和持仓。运行中在设置页换个市场，
  这里就换成另一个市场的 Key，币安回 -2015，异常被吞，界面显示「运行中」却不成交。
* ``_current_target`` 在下单**之前**就记下了。被拒、网络抖动、权益读不到时那一笔
  什么都没发生，而下一根 K 线的「目标变了没有」恒为 False，于是不再重试。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from btrader.core.config import AppSettings, RiskSettings
from btrader.core.storage import Database
from btrader.core.types import MarketType
from btrader.engine.auto import AutoTrader, AutoTraderConfig
from btrader.engine.risk import RiskManager
from btrader.engine.trading import OrderRequest, TradingService
from btrader.strategy.base import Strategy

from fakes import FakeVenue
from helpers import make_candles

SPOT = "BTC/USDT"
USD_M = "BTC/USDT:USDT"


class FixedTarget(Strategy):
    key = "fixed"
    name = "fixed"
    allow_short = True

    def __init__(self, target: float) -> None:
        super().__init__()
        self.target = target

    def generate(self, df, params):
        return pd.DataFrame({"target": np.full(len(df), self.target)}, index=df.index)


class StubKlines:
    def __init__(self, frame: pd.DataFrame) -> None:
        self.frame = frame

    def fetch(self, *args, **kwargs):
        return self.frame.copy()


class RejectingVenue(FakeVenue):
    """交易所拒单：下单必炸，用来验证「失败不记 target」。"""

    attempts = 0

    def create_order(self, *args, **kwargs):
        type(self).attempts += 1
        raise RuntimeError("下单被交易所拒绝")


def _settings() -> AppSettings:
    return AppSettings(
        risk=RiskSettings(
            max_order_quote=0,
            max_position_quote=0,
            max_leverage=20,
            max_daily_trades=0,
            max_daily_loss=0,
            max_consecutive_losses=0,
        )
    )


def _trader(
    trading,
    market: MarketType,
    target: float,
    symbol: str = SPOT,
    position_pct: float = 0.5,
) -> AutoTrader:
    frame = make_candles(120, price=100.0, drift=0.0, volatility=0.0)
    return AutoTrader(
        AutoTraderConfig(
            symbol=symbol,
            market_type=market,
            timeframe="1h",
            position_pct=position_pct,
            leverage=1.0,
        ),
        FixedTarget(target),
        trading,
        StubKlines(frame),
    )


def test_spot_close_sells_the_ledger_not_the_wallet(tmp_path):
    """钱包里有用户自己的 1 BTC：平仓只能卖本程序买到的那 0.1。"""
    settings = _settings()
    venue = FakeVenue()
    venue._last[SPOT] = 100.0
    # 用户自己充进来的 1 BTC
    venue.balances[MarketType.SPOT] = {"USDT": 10_000.0, "BTC": 1.0}
    db = Database(tmp_path / "spot-ledger.db")
    trading = TradingService(settings, venue, RiskManager(settings.risk), db)
    try:
        # 权益 10,000 × 0.1% = 10 USDT = 0.1 BTC：程序自己买的那一小笔
        trader = _trader(trading, MarketType.SPOT, 1.0, position_pct=0.001)
        trader._tick()
        assert venue.sides() == ["buy"]
        assert venue.fills[0]["amount"] == pytest.approx(0.1)
        assert venue.balances[MarketType.SPOT]["BTC"] == pytest.approx(1.1)

        trader.strategy = FixedTarget(0.0)
        trader._last_candle_ts = 0  # 同一根 K 线，强制重新评估
        trader._tick()

        # 只卖掉账本那 0.1，用户自己的 1 BTC 一分不动
        assert venue.sides() == ["buy", "sell"]
        assert venue.fills[1]["amount"] == pytest.approx(0.1)
        assert venue.balances[MarketType.SPOT]["BTC"] == pytest.approx(1.0)
    finally:
        db.close()


def test_a_rejected_order_is_retried_on_the_next_candle(tmp_path):
    """被拒的那一笔不能把 target 记成「已达成」，否则永远不再试。"""
    settings = _settings()
    venue = RejectingVenue()
    venue._last[USD_M] = 100.0
    db = Database(tmp_path / "retry.db")
    trading = TradingService(settings, venue, RiskManager(settings.risk), db)
    try:
        trader = _trader(trading, MarketType.USDM, 1.0, symbol=USD_M)
        trader._tick()
        assert trader._current_target == 0.0, "被拒的单不该把目标记成已达"

        RejectingVenue.attempts = 0
        trader._last_candle_ts = 0  # 同一根目标，强制重新评估
        trader._tick()
        assert RejectingVenue.attempts == 1, "目标没变也应当重试，而不是静默放弃"
    finally:
        db.close()


def test_the_engine_reads_its_own_market_not_the_global_one(tmp_path):
    """引擎跑起来之后换个全局市场，不能把引擎的账户读成另一个市场的。"""
    settings = _settings()
    venues = {market: FakeVenue() for market in MarketType}
    for venue in venues.values():
        venue._last[USD_M] = 100.0
    # 全局市场是现货，引擎跑的是 U 本位：两边钱包差得远，读错一眼就能看出来。
    venues[MarketType.SPOT].balances[MarketType.SPOT] = {"USDT": 0.0}
    venues[MarketType.USDM].balances[MarketType.USDM] = {"USDT": 10_000.0}
    db = Database(tmp_path / "market.db")
    asked: list[MarketType] = []

    def rest_for(market: MarketType):
        asked.append(market)
        return venues[market]

    trading = TradingService(
        settings, venues[MarketType.SPOT], RiskManager(settings.risk), db, rest_for=rest_for
    )
    try:
        trader = _trader(trading, MarketType.USDM, 1.0, symbol=USD_M)
        trader._tick()

        assert venues[MarketType.USDM].sides() == ["buy"]
        assert venues[MarketType.USDM].position_base(USD_M) > 0
        assert venues[MarketType.SPOT].fills == []
        assert MarketType.USDM in asked
    finally:
        db.close()


def test_coins_locked_in_an_open_order_still_count_as_position(tmp_path):
    """可用余额为 0、总额不为 0（币挂在卖单里）时不能当成空仓再买一份。

    「本程序持有多少」= 账本封顶于钱包。封顶原来只看 free：币挂在委托里被冻结时
    free = 0，于是读到「空仓」，照着同一个 target 又买了一份，实际敞口翻倍。
    """
    settings = _settings()
    venue = FakeVenue()
    venue._last[SPOT] = 100.0
    venue.balances[MarketType.SPOT] = {"USDT": 10_000.0, "BTC": 1.0}
    db = Database(tmp_path / "locked.db")
    trading = TradingService(settings, venue, RiskManager(settings.risk), db)
    try:
        trader = _trader(trading, MarketType.SPOT, 1.0, position_pct=0.001)
        trader._tick()
        assert venue.sides() == ["buy"]
        assert venue.fills[0]["amount"] == pytest.approx(0.1)

        # 钱包里那 1.1 BTC 全挂在卖单上：可用 0，总额仍是 1.1。
        real_balance = venue.fetch_balance

        def frozen(market):
            payload = real_balance(market)
            if market is MarketType.SPOT:
                payload["free"] = dict(payload["total"])
                payload["free"]["BTC"] = 0.0
            return payload

        venue.fetch_balance = frozen
        trader.strategy = FixedTarget(0.0)  # 策略说平仓，但币正挂在卖单里
        trader._last_candle_ts = 0  # 同一根 K 线，强制重新评估
        trader._tick()

        # 账本里那 0.1 仍是仓位：要卖的是它，不能再买回来
        assert venue.sides() == ["buy", "sell"], "币被挂单冻结时不能当成空仓再买一份"
        assert venue.fills[1]["amount"] == pytest.approx(0.1)
    finally:
        db.close()
