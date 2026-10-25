"""Automation engine behaviour against an in-memory venue."""

from __future__ import annotations

import time

import numpy as np
import pandas as pd
import pytest

from btrader.core.config import AppSettings, RiskSettings
from btrader.core.storage import Database
from btrader.core.types import MarketType
from btrader.engine.auto import (
    CLOSE_SETTLE_SECONDS,
    CLOSE_WINDOW_AFTER,
    AutoTrader,
    AutoTraderConfig,
)
from btrader.engine.risk import RiskManager
from btrader.engine.trading import OrderRejected, OrderRequest, TradingService
from btrader.strategy.base import IntParam, Strategy

from fakes import FakeVenue
from helpers import make_candles


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


class BlindVenue(FakeVenue):
    """A venue whose position and balance reads fail, as a dropped link would."""

    def fetch_positions(self, market_type, symbols=None):
        raise RuntimeError("network down")

    def fetch_balance(self, market_type):
        raise RuntimeError("network down")


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


@pytest.fixture
def service(tmp_path):
    settings = _settings()
    venue = FakeVenue()
    # 现货持仓按本地成交账本算，账本记的是**成交价**；替身交易所要有一个最后价
    # 才报得出成交（和 test_spot_fill_sync 一样先喂价）。
    venue._last["BTC/USDT"] = 100.0
    venue._last["BTC/USDT:USDT"] = 100.0
    db = Database(tmp_path / "test.db")
    yield TradingService(settings, venue, RiskManager(settings.risk), db), venue
    db.close()


def _trader(trading, market: MarketType, target: float, symbol="BTC/USDT") -> AutoTrader:
    frame = make_candles(120, price=100.0, drift=0.0, volatility=0.0)
    return AutoTrader(
        AutoTraderConfig(
            symbol=symbol,
            market_type=market,
            timeframe="1h",
            position_pct=0.5,
            leverage=1.0,
        ),
        FixedTarget(target),
        trading,
        StubKlines(frame),
    )


def test_auto_opens_long_on_signal(service):
    trading, venue = service
    trader = _trader(trading, MarketType.SPOT, 1.0)
    trader._tick()
    assert venue.sides() == ["buy"]
    assert venue.position_base("BTC/USDT") > 0


def test_auto_flattens_on_zero_signal(service):
    trading, venue = service
    trader = _trader(trading, MarketType.SPOT, 1.0)
    trader._tick()
    assert venue.position_base("BTC/USDT") > 0

    trader.strategy = FixedTarget(0.0)
    trader._last_candle_ts = 0  # force re-evaluation
    trader._tick()
    assert venue.position_base("BTC/USDT") == pytest.approx(0.0, abs=1e-9)
    assert venue.sides() == ["buy", "sell"]


def test_auto_spot_never_shorts(service):
    """Spot cannot go short: a negative target is clamped to flat, no order."""
    trading, venue = service
    trader = _trader(trading, MarketType.SPOT, -1.0)
    trader._tick()
    assert venue.fills == []
    assert trader._current_target == 0.0


def test_auto_futures_can_short(service):
    trading, venue = service
    trader = _trader(trading, MarketType.USDM, -1.0, symbol="BTC/USDT:USDT")
    trader._tick()
    assert venue.sides() == ["sell"]
    assert venue.position_base("BTC/USDT:USDT") < 0


def test_auto_skips_unchanged_signal(service):
    trading, venue = service
    trader = _trader(trading, MarketType.SPOT, 1.0)
    trader._tick()
    trader._tick()
    assert len(venue.fills) == 1


def test_auto_waits_for_the_next_close_instead_of_polling(service):
    """节奏跟着 K 线周期：这根收盘之前不醒。以前固定每 poll_seconds（10 秒）拉一次 K 线。"""
    trading, _venue = service
    trader = _trader(trading, MarketType.SPOT, 1.0)
    hour = 3600.0
    poll = trader.config.poll_seconds

    # 下一根还有 100 秒才收盘：一觉睡到收盘后 CLOSE_SETTLE_SECONDS。
    trader._pending_close = time.time() + 100.0
    assert trader._next_wait() == pytest.approx(100.0 + CLOSE_SETTLE_SECONDS, abs=1.0)
    # 界面上的「下次」也读这个锚，不再按轮询间隔报。
    assert trader.next_run_at() == pytest.approx(
        trader._pending_close + CLOSE_SETTLE_SECONDS, abs=1.0
    )

    # 收盘点了、这根还没落地：按 poll_seconds 小步问。
    trader._pending_close = time.time() - 5.0
    assert trader._next_wait() == pytest.approx(min(poll, CLOSE_WINDOW_AFTER - 5.0), abs=1.0)

    # 这一根的窗口也过了：顺延到墙上的下一根，不再每 10 秒醒一次。
    trader._pending_close = time.time() - CLOSE_WINDOW_AFTER - 60.0
    wait = trader._next_wait()
    assert 0.0 < wait <= hour + CLOSE_SETTLE_SECONDS


def test_auto_ignores_a_candle_older_than_the_last_one(service):
    """行情往回退时（镜像源）别把旧的那根当新 K 线再调一次仓。"""
    trading, venue = service
    frame = make_candles(120, price=100.0, drift=0.0, volatility=0.0)
    trader = _trader(trading, MarketType.SPOT, 1.0)
    trader.klines = StubKlines(frame)
    trader._tick()
    assert venue.sides() == ["buy"]

    trader.klines = StubKlines(frame.iloc[:-1].reset_index(drop=True))
    trader._tick()
    assert venue.sides() == ["buy"]


def test_auto_anchors_the_cadence_on_the_bar_it_just_ate(service):
    """吃完一根就把锚挪到下一根收盘：_pending_close = 那根开盘 + 2 个周期。"""
    trading, _venue = service
    trader = _trader(trading, MarketType.SPOT, 1.0)
    trader._tick()
    assert trader._pending_close == pytest.approx(
        trader._last_candle_ts / 1000.0 + 2 * 3600.0
    )
    # 替身 K 线的时间戳是历史数据，锚已经过期，于是 next_run_at 顺延到墙上下一根：
    # 无论哪种情况它都得是「将来的一个收盘点」，不能是过去。
    assert trader.next_run_at() > time.time()


def test_live_equity_reads_nested_total():
    """ccxt returns {"total": {"USDT": 5000}}; reading the mapping gave zero."""

    class BalanceVenue(FakeVenue):
        def fetch_balance(self, market_type):
            return {
                "free": {"USDT": 4_000.0, "BTC": 0.1},
                "used": {"USDT": 1_000.0},
                "total": {"USDT": 5_000.0, "BTC": 0.1},
            }

    settings = _settings()
    service = TradingService(
        settings,
        BalanceVenue(),
        RiskManager(settings.risk),
        Database(":memory:"),
    )
    request = OrderRequest(
        symbol="BTC/USDT",
        side="buy",
        order_type="market",
        amount=0.01,
        price=100.0,
        market_type=MarketType.SPOT,
    )
    assert service._equity(request, 100.0) == pytest.approx(5_000.0)


def _blind_service(tmp_path):
    """A service whose venue cannot be read at all."""
    settings = _settings()
    venue = BlindVenue()
    db = Database(tmp_path / "blind.db")
    return TradingService(settings, venue, RiskManager(settings.risk), db), venue, db


def test_auto_skips_rebalance_when_live_position_unknown(tmp_path):
    """A failed position read used to look like "flat" and re-buy the target."""
    trading, venue, db = _blind_service(tmp_path)
    try:
        frame = make_candles(120, price=100.0, drift=0.0, volatility=0.0)
        trader = AutoTrader(
            AutoTraderConfig(
                symbol="BTC/USDT",
                market_type=MarketType.SPOT,
                timeframe="1h",
                position_pct=0.5,
                leverage=1.0,
            ),
            FixedTarget(1.0),
            trading,
            StubKlines(frame),
        )
        trader._tick()
        assert venue.fills == [], "unknown exposure must not become an order"
    finally:
        db.close()


def test_live_entry_refused_when_position_unknown(tmp_path):
    """The position cap cannot be checked without the exposure figure."""
    trading, venue, db = _blind_service(tmp_path)
    try:
        with pytest.raises(OrderRejected, match="无法读取当前持仓"):
            trading.submit(
                OrderRequest(
                    symbol="BTC/USDT",
                    side="buy",
                    order_type="market",
                    amount=0.1,
                    price=100.0,
                    market_type=MarketType.SPOT,
                )
            )
        assert venue.fills == []
    finally:
        db.close()

def test_auto_flip_to_short_is_not_marked_reduce_only(service):
    """A long->short flip has to open the short half.

    Marking the whole flip reduce-only makes the venue drop (or reject) that
    half while _current_target already says short, so the engine sits flat and
    never retries the position it believes it holds.
    """
    trading, venue = service
    symbol = "BTC/USDT:USDT"
    trader = _trader(trading, MarketType.USDM, 1.0, symbol=symbol)
    trader._tick()
    assert venue.position_base(symbol) > 0

    trader.strategy = FixedTarget(-0.5)
    trader._last_candle_ts = 0  # force re-evaluation
    trader._tick()

    assert not venue.fills[-1]["info"].get("reduceOnly"), "翻转不是减仓"
    assert venue.position_base(symbol) < 0


class NoBalanceVenue(FakeVenue):
    """Wallet reads fail while the position endpoint still answers."""

    def fetch_balance(self, market_type):
        raise RuntimeError("network down")


def test_auto_never_sizes_an_order_against_an_unknown_equity(tmp_path):
    """A failed wallet read used to fall back to a hardcoded 10,000."""
    settings = _settings()
    venue = NoBalanceVenue()
    db = Database(tmp_path / "nobalance.db")
    try:
        trading = TradingService(settings, venue, RiskManager(settings.risk), db)
        trader = _trader(trading, MarketType.USDM, 1.0, symbol="BTC/USDT:USDT")
        trader._tick()
        assert trader._equity is None
        assert venue.fills == [], "权益未知时不能按假余额下单"
    finally:
        db.close()


class HalfFilledVenue(FakeVenue):
    """Answers a market order with a half fill, as a thin book does."""

    def create_order(
        self, market, symbol, order_type, side, amount, price=None, params=None
    ):
        order = super().create_order(
            market, symbol, order_type, side, amount, price, params
        )
        order["filled"] = amount / 2.0
        order["status"] = "partially_filled"
        return order


def test_stop_covers_the_filled_size_not_the_requested_one(tmp_path):
    """A reduce-only stop larger than the position is rejected by the venue.

    That left the filled half unprotected and the entry looking successful.
    """
    settings = _settings()
    venue = HalfFilledVenue()
    db = Database(tmp_path / "partial.db")
    try:
        trading = TradingService(settings, venue, RiskManager(settings.risk), db)
        result = trading.submit(
            OrderRequest(
                symbol="BTC/USDT:USDT",
                side="buy",
                order_type="market",
                amount=1.0,
                price=100.0,
                market_type=MarketType.USDM,
                stop_loss=95.0,
            )
        )
        assert result.order["filled"] == pytest.approx(0.5)
        kind, _symbol, side, size, trigger = venue.stop_orders[0]
        assert (kind, side, trigger) == ("stop", "sell", 95.0)
        assert size == pytest.approx(0.5)
    finally:
        db.close()


def test_history_write_failure_is_not_reported_as_a_failed_order(tmp_path):
    """The order is already at the venue; a raised error invites a second one."""
    class BrokenHistory(Database):
        def save_order(self, *args, **kwargs):
            raise RuntimeError("disk full")

    settings = _settings()
    venue = FakeVenue()
    db = BrokenHistory(tmp_path / "broken.db")
    try:
        trading = TradingService(settings, venue, RiskManager(settings.risk), db)
        result = trading.submit(
            OrderRequest(
                symbol="BTC/USDT",
                side="buy",
                order_type="market",
                amount=1.0,
                price=100.0,
                market_type=MarketType.SPOT,
            )
        )
        assert result.order.get("status") == "closed"
        assert venue.position_base("BTC/USDT") == pytest.approx(1.0)
    finally:
        db.close()


class WarmupTarget(Strategy):
    """周期可调的固定目标：用来验证预热门槛是不是按实际参数算的。"""

    key = "warmup_target"
    name = "warmup target"
    params_spec = [IntParam("period", "周期", 20, 2, 500)]

    def generate(self, df, params):
        return pd.DataFrame({"target": np.ones(len(df))}, index=df.index)


def test_auto_waits_for_the_warmup_the_parameters_ask_for(service) -> None:
    """慢线填 500 要 501 根：门槛按默认值算的 22 根，会在指标还没有值时就开始下单。"""
    trading, venue = service
    frame = make_candles(120, price=100.0, drift=0.0, volatility=0.0)
    trader = AutoTrader(
        AutoTraderConfig(
            symbol="BTC/USDT",
            market_type=MarketType.SPOT,
            timeframe="1h",
            params={"period": 500},
            position_pct=0.5,
            leverage=1.0,
        ),
        WarmupTarget(),
        trading,
        StubKlines(frame),
    )

    trader._tick()
    assert venue.fills == []

    trader.config.params = {"period": 20}
    trader._tick()
    assert venue.sides() == ["buy"]