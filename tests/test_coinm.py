"""Coin-margined (inverse) order sizing end-to-end through TradingService."""

from __future__ import annotations

import pandas as pd
import pytest

from btrader.core.config import AppSettings, RiskSettings
from btrader.core.contracts import ContractSpec
from btrader.core.credentials import Credentials
from btrader.core.storage import Database
from btrader.core.types import MarketType, Network, RunMode
from btrader.engine.auto import AutoTrader, AutoTraderConfig
from btrader.engine.paper import PaperBroker
from btrader.engine.risk import RiskManager
from btrader.engine.trading import OrderRejected, OrderRequest, TradingService
from btrader.exchange.rest import BinanceREST
from btrader.strategy.base import Strategy

from helpers import make_candles


class InverseStubREST(BinanceREST):
    """Mirrors Binance coin-M: whole contracts, 100 USD each."""

    created: list[tuple]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.created = []

    def contract_spec(self, market_type, symbol):
        if market_type is MarketType.COINM:
            return ContractSpec(
                contract_size=100.0,
                inverse=True,
                base="BTC",
                quote="USD",
                settle="BTC",
                min_amount=1.0,
            )
        return ContractSpec(contract_size=1.0, base="BTC", quote="USDT", min_amount=0.00001)

    def amount_to_precision(self, market_type, symbol, amount):
        if market_type is MarketType.COINM:
            return float(int(amount))
        return float(amount)

    def create_order(self, market_type, symbol, order_type, side, amount, price=None, params=None):
        self.created.append((symbol, order_type, side, amount, price, dict(params or {})))
        return {"id": "live-1", "status": "closed", "amount": amount, "symbol": symbol}


class FixedTarget(Strategy):
    key = "fixed"
    name = "fixed"
    allow_short = True

    def __init__(self, target: float) -> None:
        super().__init__()
        self.target = target

    def generate(self, df, params):
        import numpy as np

        return pd.DataFrame({"target": np.full(len(df), self.target)}, index=df.index)


class StubKlines:
    def __init__(self, frame):
        self.frame = frame

    def fetch(self, *args, **kwargs):
        return self.frame.copy()


@pytest.fixture
def service(tmp_path):
    settings = AppSettings(
        risk=RiskSettings(
            max_order_quote=0,
            max_position_quote=0,
            max_leverage=100,
            max_daily_trades=0,
            max_daily_loss=0,
            max_consecutive_losses=0,
        )
    )
    rest = InverseStubREST(Network.MAINNET, Credentials("test-key", "test-secret"))
    paper = PaperBroker(starting_balance=10_000, fee_rate=0.0, slippage_bps=0.0)
    risk = RiskManager(settings.risk)
    db = Database(tmp_path / "coinm.db")
    yield TradingService(settings, rest, risk, paper, db), paper, rest
    db.close()


def test_exchange_amount_converts_base_to_contracts(service):
    trading, _, _ = service
    # 0.05 BTC at 50,000 USD = 2,500 USD = 25 contracts of 100 USD
    exchange_amount, base_qty = trading.exchange_amount(
        MarketType.COINM, "BTC/USD:BTC", 0.05, 50_000.0
    )
    assert exchange_amount == 25.0
    assert base_qty == pytest.approx(0.05)


def test_exchange_amount_rounds_to_whole_contracts(service):
    trading, _, _ = service
    # 0.0123 BTC at 50,000 USD = 615 USD -> 6.15 contracts -> 6 contracts
    exchange_amount, base_qty = trading.exchange_amount(
        MarketType.COINM, "BTC/USD:BTC", 0.0123, 50_000.0
    )
    assert exchange_amount == 6.0
    assert base_qty == pytest.approx(0.012)


def test_submit_rejects_dust_below_one_contract(service):
    trading, _, _ = service
    with pytest.raises(OrderRejected, match="最小下单量"):
        trading.submit(
            OrderRequest(
                symbol="BTC/USD:BTC",
                side="buy",
                order_type="market",
                amount=0.001,  # 50 USD < 100 USD contract
                price=50_000.0,
                market_type=MarketType.COINM,
                mode=RunMode.PAPER,
            )
        )


def test_paper_position_is_in_base_coins(service):
    trading, paper, _ = service
    result = trading.submit(
        OrderRequest(
            symbol="BTC/USD:BTC",
            side="buy",
            order_type="market",
            amount=0.05,
            price=50_000.0,
            market_type=MarketType.COINM,
            mode=RunMode.PAPER,
        )
    )
    assert result.amount_exchange == 25.0
    assert result.amount_base == pytest.approx(0.05)
    position = paper.account.position_for("BTC/USD:BTC")
    assert position.contracts == pytest.approx(0.05)
    assert result.quote_notional == pytest.approx(2_500.0)


def test_live_submit_sends_contracts_to_exchange(service):
    trading, _, rest = service
    result = trading.submit(
        OrderRequest(
            symbol="BTC/USD:BTC",
            side="sell",
            order_type="market",
            amount=0.05,
            price=50_000.0,
            market_type=MarketType.COINM,
            mode=RunMode.LIVE,
        )
    )
    assert rest.created, "order never reached the exchange"
    symbol, order_type, side, amount, _price, _params = rest.created[0]
    assert (symbol, order_type, side) == ("BTC/USD:BTC", "market", "sell")
    assert amount == 25.0, "exchange must receive contracts, not BTC"
    assert result.amount_base == pytest.approx(0.05)


def test_risk_limit_uses_usd_notional_for_inverse(service):
    trading, _, rest = service
    trading.settings.risk.max_order_quote = 1_000.0
    trading.risk.settings = trading.settings.risk
    with pytest.raises(OrderRejected, match="超过上限"):
        trading.submit(
            OrderRequest(
                symbol="BTC/USD:BTC",
                side="buy",
                order_type="market",
                amount=0.05,  # 2,500 USD notional
                price=50_000.0,
                market_type=MarketType.COINM,
                mode=RunMode.LIVE,
            )
        )
    assert not rest.created


def test_auto_trader_rebalances_coinm_in_base_units(service):
    trading, paper, _ = service
    frame = make_candles(120, price=50_000.0, drift=0.0, volatility=0.0)
    trader = AutoTrader(
        AutoTraderConfig(
            symbol="BTC/USD:BTC",
            market_type=MarketType.COINM,
            timeframe="1h",
            mode=RunMode.PAPER,
            position_pct=0.25,
            leverage=1.0,
        ),
        FixedTarget(1.0),
        trading,
        StubKlines(frame),
    )
    trader._tick()
    # 25% of 10,000 USD => 2,500 USD => 0.05 BTC => 25 contracts
    position = paper.account.position_for("BTC/USD:BTC")
    assert position.contracts == pytest.approx(0.05, rel=1e-6)


class ZeroPrecisionStubREST(InverseStubREST):
    """Mimics ccxt rejecting an amount below the market minimum.

    ``BinanceREST.amount_to_precision`` converts that failure into 0; the
    trading service must refuse the order instead of forwarding the raw size.
    """

    def amount_to_precision(self, market_type, symbol, amount):
        return 0.0


def test_unrounded_amount_is_refused_not_degraded(tmp_path):
    """Regression: a failed precision conversion must never reach the venue.

    Before the fix ccxt's "below minimum" error was swallowed and the raw base
    quantity was sent as if it were a contract count, inflating the order.
    """
    settings = AppSettings(
        risk=RiskSettings(
            max_order_quote=0,
            max_position_quote=0,
            max_leverage=100,
            max_daily_trades=0,
            max_daily_loss=0,
            max_consecutive_losses=0,
        )
    )
    rest = ZeroPrecisionStubREST(Network.MAINNET, Credentials("test-key", "test-secret"))
    paper = PaperBroker(starting_balance=10_000, fee_rate=0.0, slippage_bps=0.0)
    db = Database(tmp_path / "zero.db")
    try:
        trading = TradingService(settings, rest, RiskManager(settings.risk), paper, db)
        with pytest.raises(OrderRejected, match="最小下单单位"):
            trading.submit(
                OrderRequest(
                    symbol="BTC/USD:BTC",
                    side="buy",
                    order_type="market",
                    amount=0.05,  # 25 contracts, clears the minimum
                    price=50_000.0,
                    market_type=MarketType.COINM,
                    mode=RunMode.PAPER,
                )
            )
        assert not rest.created
        assert not paper.account.positions.get("BTC/USD:BTC")
    finally:
        db.close()


def test_spot_minimum_is_enforced(service):
    """Spot must not accept an order under the exchange minimum either."""
    trading, _, rest = service
    with pytest.raises(OrderRejected, match="最小下单量"):
        trading.submit(
            OrderRequest(
                symbol="BTC/USDT",
                side="buy",
                order_type="market",
                amount=0.000001,  # below 0.00001 BTC
                price=50_000.0,
                market_type=MarketType.SPOT,
                mode=RunMode.LIVE,
            )
        )
    assert not rest.created
