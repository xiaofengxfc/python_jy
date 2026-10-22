"""Automation engine behaviour against an in-memory venue."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from btrader.core.config import AppSettings, RiskSettings
from btrader.core.storage import Database
from btrader.core.types import MarketType
from btrader.engine.auto import AutoTrader, AutoTraderConfig
from btrader.engine.risk import RiskManager
from btrader.engine.trading import OrderRejected, OrderRequest, TradingService
from btrader.strategy.base import Strategy

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
