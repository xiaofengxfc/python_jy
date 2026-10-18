"""Automation engine behaviour with a stubbed market feed."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from btrader.core.config import AppSettings, RiskSettings
from btrader.core.storage import Database
from btrader.core.types import MarketType, RunMode
from btrader.engine.auto import AutoTrader, AutoTraderConfig
from btrader.engine.paper import PaperBroker
from btrader.engine.risk import RiskManager
from btrader.engine.trading import OrderRejected, OrderRequest, TradingService
from btrader.exchange.rest import BinanceREST
from btrader.core.types import Network
from btrader.strategy.base import Strategy

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


class StubREST(BinanceREST):
    """Offline REST double: fixed contract specs, integer contract rounding."""

    def amount_to_precision(self, market_type, symbol, amount):
        spec = self.contract_spec(market_type, symbol)
        if spec.inverse:
            return float(int(amount))  # Binance coin-M trades whole contracts
        return float(amount)

    def contract_spec(self, market_type, symbol):
        from btrader.core.contracts import ContractSpec

        if market_type is MarketType.COINM:
            return ContractSpec(
                contract_size=100.0, inverse=True, base="BTC", quote="USD", settle="BTC"
            )
        return ContractSpec(contract_size=1.0, inverse=False, base="BTC", quote="USDT")


@pytest.fixture
def service(tmp_path):
    settings = AppSettings(
        risk=RiskSettings(
            max_order_quote=0,
            max_position_quote=0,
            max_leverage=20,
            max_daily_trades=0,
            max_daily_loss=0,
            max_consecutive_losses=0,
        )
    )
    rest = StubREST(Network.MAINNET)
    paper = PaperBroker(starting_balance=10_000, fee_rate=0.0, slippage_bps=0.0)
    risk = RiskManager(settings.risk)
    db = Database(tmp_path / "test.db")
    yield TradingService(settings, rest, risk, paper, db), paper
    db.close()


def _trader(service, market: MarketType, target: float, symbol="BTC/USDT") -> AutoTrader:
    trading, _ = service
    frame = make_candles(120, price=100.0, drift=0.0, volatility=0.0)
    return AutoTrader(
        AutoTraderConfig(
            symbol=symbol,
            market_type=market,
            timeframe="1h",
            mode=RunMode.PAPER,
            position_pct=0.5,
            leverage=1.0,
        ),
        FixedTarget(target),
        trading,
        StubKlines(frame),
    )


def test_auto_opens_long_on_signal(service):
    trading, paper = service
    trader = _trader(service, MarketType.SPOT, 1.0)
    trader._tick()
    position = paper.account.position_for("BTC/USDT")
    assert position.contracts > 0
    assert paper.account.cash < 10_000  # capital deployed


def test_auto_flattens_on_zero_signal(service):
    trading, paper = service
    trader = _trader(service, MarketType.SPOT, 1.0)
    trader._tick()
    held = paper.account.position_for("BTC/USDT").contracts
    assert held > 0

    trader.strategy = FixedTarget(0.0)
    trader._last_candle_ts = 0  # force re-evaluation
    trader._tick()
    assert paper.account.position_for("BTC/USDT").contracts == 0


def test_auto_spot_never_shorts(service):
    trading, paper = service
    trader = _trader(service, MarketType.SPOT, -1.0)
    trader._tick()
    assert paper.account.position_for("BTC/USDT").contracts == 0
    assert trader._current_target == 0.0


def test_auto_futures_can_short(service):
    trading, paper = service
    trader = _trader(service, MarketType.USDM, -1.0, symbol="BTC/USDT:USDT")
    trader._tick()
    position = paper.account.position_for("BTC/USDT:USDT")
    assert position.contracts < 0


def test_auto_skips_unchanged_signal(service):
    trading, paper = service
    trader = _trader(service, MarketType.SPOT, 1.0)
    trader._tick()
    trades_after_first = len(paper.account.trades)
    trader._tick()
    assert len(paper.account.trades) == trades_after_first


def test_spot_sell_beyond_holding_rejected(service):
    trading, paper = service
    with pytest.raises(OrderRejected, match="现货不能做空"):
        trading.submit(
            OrderRequest(
                symbol="ETH/USDT",
                side="sell",
                order_type="market",
                amount=1.0,
                price=100.0,
                market_type=MarketType.SPOT,
                mode=RunMode.PAPER,
            )
        )
    assert paper.account.positions.get("ETH/USDT") is None or paper.account.positions["ETH/USDT"].contracts == 0


def test_paper_broker_rejects_short_when_not_allowed():
    broker = PaperBroker(starting_balance=1_000)
    order = broker.place_order(
        "BTC/USDT", "sell", "market", 1.0, price=100.0, allow_short=False
    )
    assert order["status"] == "rejected"
    assert broker.account.position_for("BTC/USDT").contracts == 0
