"""Coin-margined (inverse) order sizing end-to-end through TradingService."""

from __future__ import annotations

import pandas as pd
import pytest

from btrader.core.config import AppSettings, RiskSettings
from btrader.core.contracts import ContractSpec
from btrader.core.credentials import Credentials
from btrader.core.storage import Database
from btrader.core.types import MarketType, Network
from btrader.engine.auto import AutoTrader, AutoTraderConfig
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
        self.leverage_calls: list[tuple] = []
        self.protection: list[tuple] = []
        self.canceled_symbols: list[tuple] = []
        self.fail_stop = False

    def create_stop_market(self, market, symbol, side, amount, stop_price):
        if self.fail_stop:
            from btrader.exchange.rest import ExchangeError

            raise ExchangeError("止损单被交易所拒绝")
        self.protection.append(("stop", symbol, side, amount, stop_price))
        return {"id": "stop-1", "status": "open", "type": "STOP_MARKET"}

    def create_take_profit_market(self, market, symbol, side, amount, target_price):
        self.protection.append(("target", symbol, side, amount, target_price))
        return {"id": "tp-1", "status": "open", "type": "TAKE_PROFIT_MARKET"}

    def cancel_symbol_orders(self, market, symbol):
        self.canceled_symbols.append((market, symbol))
        return 2

    def set_leverage(self, symbol, leverage, market):
        self.leverage_calls.append((symbol, leverage, market))
        return {}

    def contract_spec(self, market_type, symbol, allow_network=True):
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

    def fetch_positions(self, market_type, symbols=None):
        # The venue reports no open positions in this stub. Returning a list
        # (not raising) is what tells the risk layer "flat", as opposed to
        # "unknown", which now refuses new entries.
        return []

    def fetch_balance(self, market_type):
        # Coin-M wallet: 0.2 BTC is 10,000 USD at the 50,000 price these tests
        # use, which is what the position sizing multiplies.
        if market_type is MarketType.COINM:
            total = {"BTC": 0.2}
        else:
            total = {"USDT": 10_000.0}
        return {"free": dict(total), "used": {}, "total": total, "info": {}}


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
    risk = RiskManager(settings.risk)
    db = Database(tmp_path / "coinm.db")
    yield TradingService(settings, rest, risk, db), rest
    db.close()


def test_exchange_amount_converts_base_to_contracts(service):
    trading, _rest = service
    # 0.05 BTC at 50,000 USD = 2,500 USD = 25 contracts of 100 USD
    exchange_amount, base_qty = trading.exchange_amount(
        MarketType.COINM, "BTC/USD:BTC", 0.05, 50_000.0
    )
    assert exchange_amount == 25.0
    assert base_qty == pytest.approx(0.05)


def test_exchange_amount_rounds_to_whole_contracts(service):
    trading, _rest = service
    # 0.0123 BTC at 50,000 USD = 615 USD -> 6.15 contracts -> 6 contracts
    exchange_amount, base_qty = trading.exchange_amount(
        MarketType.COINM, "BTC/USD:BTC", 0.0123, 50_000.0
    )
    assert exchange_amount == 6.0
    assert base_qty == pytest.approx(0.012)


def test_submit_rejects_dust_below_one_contract(service):
    trading, _rest = service
    with pytest.raises(OrderRejected, match="最小下单量"):
        trading.submit(
            OrderRequest(
                symbol="BTC/USD:BTC",
                side="buy",
                order_type="market",
                amount=0.001,  # 50 USD < 100 USD contract
                price=50_000.0,
                market_type=MarketType.COINM,
            )
        )


def test_order_size_is_reported_in_base_coins(service):
    """The panel speaks base coins even though the venue takes contracts."""
    trading, rest = service
    result = trading.submit(
        OrderRequest(
            symbol="BTC/USD:BTC",
            side="buy",
            order_type="market",
            amount=0.05,
            price=50_000.0,
            market_type=MarketType.COINM,
        )
    )
    assert result.amount_exchange == 25.0
    assert result.amount_base == pytest.approx(0.05)
    assert result.quote_notional == pytest.approx(2_500.0)
    assert rest.created[0][3] == 25.0


def test_live_submit_sends_contracts_to_exchange(service):
    trading, rest = service
    result = trading.submit(
        OrderRequest(
            symbol="BTC/USD:BTC",
            side="sell",
            order_type="market",
            amount=0.05,
            price=50_000.0,
            market_type=MarketType.COINM,
        )
    )
    assert rest.created, "order never reached the exchange"
    symbol, order_type, side, amount, _price, _params = rest.created[0]
    assert (symbol, order_type, side) == ("BTC/USD:BTC", "market", "sell")
    assert amount == 25.0, "exchange must receive contracts, not BTC"
    assert result.amount_base == pytest.approx(0.05)


def test_live_entry_arms_venue_side_stop_and_target(service):
    """A live position must be protected at the venue, not only in the app."""
    trading, rest = service
    result = trading.submit(
        OrderRequest(
            symbol="BTC/USD:BTC",
            side="sell",
            order_type="market",
            amount=0.05,
            price=50_000.0,
            stop_loss=55_000.0,
            take_profit=45_000.0,
            market_type=MarketType.COINM,
        )
    )
    assert [item[0] for item in rest.protection] == ["stop", "target"]
    for _kind, symbol, side, amount, _price in rest.protection:
        assert symbol == "BTC/USD:BTC"
        assert side == "buy", "a short is protected by a buy-back exit"
        assert amount == 25.0, "protection must use contracts, like the entry"
    assert result.warnings == []
    assert {item["type"] for item in result.protection} == {
        "STOP_MARKET",
        "TAKE_PROFIT_MARKET",
    }


def test_live_exit_cancels_resting_orders_first(service):
    """A leftover reduceOnly stop would otherwise flip the position."""
    trading, rest = service
    trading.submit(
        OrderRequest(
            symbol="BTC/USD:BTC",
            side="sell",
            order_type="market",
            amount=0.05,
            price=50_000.0,
            reduce_only=True,
            market_type=MarketType.COINM,
        )
    )
    assert rest.canceled_symbols == [(MarketType.COINM, "BTC/USD:BTC")]
    assert rest.protection == [], "an exit must not arm a new stop"


def test_failed_protection_is_reported_not_swallowed(service):
    """The entry already filled; a rejected stop must surface as a warning."""
    trading, rest = service
    rest.fail_stop = True
    result = trading.submit(
        OrderRequest(
            symbol="BTC/USD:BTC",
            side="buy",
            order_type="market",
            amount=0.05,
            price=50_000.0,
            stop_loss=45_000.0,
            take_profit=55_000.0,
            market_type=MarketType.COINM,
        )
    )
    assert result.order["id"] == "live-1"
    assert any("止损单挂单失败" in item for item in result.warnings)
    assert [item[0] for item in rest.protection] == ["target"]


def test_live_entry_pushes_leverage_before_ordering(service):
    """The venue sizes with its own leverage; the panel's choice must reach it."""
    trading, rest = service
    trading.submit(
        OrderRequest(
            symbol="BTC/USD:BTC",
            side="buy",
            order_type="market",
            amount=0.05,
            price=50_000.0,
            leverage=5.0,
            market_type=MarketType.COINM,
        )
    )
    assert rest.leverage_calls == [("BTC/USD:BTC", 5.0, MarketType.COINM)]
    # A second order at the same leverage must not spend another signed call.
    trading.submit(
        OrderRequest(
            symbol="BTC/USD:BTC",
            side="buy",
            order_type="market",
            amount=0.05,
            price=50_000.0,
            leverage=5.0,
            market_type=MarketType.COINM,
        )
    )
    assert len(rest.leverage_calls) == 1


def test_reduce_only_exit_skips_leverage(service):
    """A close must not be blocked by a leverage call that can fail."""
    trading, rest = service
    trading.submit(
        OrderRequest(
            symbol="BTC/USD:BTC",
            side="sell",
            order_type="market",
            amount=0.05,
            price=50_000.0,
            leverage=7.0,
            reduce_only=True,
            market_type=MarketType.COINM,
        )
    )
    assert rest.leverage_calls == []
    assert rest.created, "the exit never reached the exchange"


def test_risk_limit_uses_usd_notional_for_inverse(service):
    trading, rest = service
    trading.settings.risk.max_order_quote = 1_000.0
    # The percentage rule scales with equity, so it would raise the cap to 25%
    # of the wallet; disable it to measure the fixed ceiling in USD.
    trading.settings.risk.max_order_pct = 0.0
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
            )
        )
    assert not rest.created


def test_auto_trader_sizes_coinm_in_contracts(service):
    """The runner speaks base coins; the venue must still receive contracts."""
    trading, rest = service
    frame = make_candles(120, price=50_000.0, drift=0.0, volatility=0.0)
    trader = AutoTrader(
        AutoTraderConfig(
            symbol="BTC/USD:BTC",
            market_type=MarketType.COINM,
            timeframe="1h",
            position_pct=0.25,
            leverage=1.0,
        ),
        FixedTarget(1.0),
        trading,
        StubKlines(frame),
    )
    trader._tick()
    # 25% of the 0.2 BTC wallet (10,000 USD at 50,000) => 2,500 USD
    # => 0.05 BTC => 25 contracts of 100 USD
    assert rest.created, "the rebalance never reached the exchange"
    symbol, order_type, side, amount, _price, _params = rest.created[0]
    assert (symbol, order_type, side) == ("BTC/USD:BTC", "market", "buy")
    assert amount == pytest.approx(25.0)


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
    db = Database(tmp_path / "zero.db")
    try:
        trading = TradingService(settings, rest, RiskManager(settings.risk), db)
        with pytest.raises(OrderRejected, match="最小下单单位"):
            trading.submit(
                OrderRequest(
                    symbol="BTC/USD:BTC",
                    side="buy",
                    order_type="market",
                    amount=0.05,  # 25 contracts, clears the minimum
                    price=50_000.0,
                    market_type=MarketType.COINM,
                )
            )
        assert not rest.created
    finally:
        db.close()


def test_spot_minimum_is_enforced(service):
    """Spot must not accept an order under the exchange minimum either."""
    trading, rest = service
    with pytest.raises(OrderRejected, match="最小下单量"):
        trading.submit(
            OrderRequest(
                symbol="BTC/USDT",
                side="buy",
                order_type="market",
                amount=0.000001,  # below 0.00001 BTC
                price=50_000.0,
                market_type=MarketType.SPOT,
            )
        )
    assert not rest.created
