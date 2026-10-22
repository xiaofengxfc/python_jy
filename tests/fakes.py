"""Shared doubles for the offline tests.

The suite used to lean on the paper broker as a fake exchange. That broker is
gone — orders now only exist on a real venue — so the tests need a venue they
can drive: this module keeps balances, positions and fills in memory and
records every call the trading service makes.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from btrader.core.contracts import ContractSpec
from btrader.core.types import MarketType, Network


def spec_for(market: MarketType, symbol: str) -> ContractSpec:
    """Contract spec of the stub venue: 100 USD contracts on coin-M only."""
    if market is MarketType.COINM:
        return ContractSpec(
            contract_size=100.0,
            inverse=True,
            base=symbol.split("/")[0] or "BTC",
            quote="USD",
            settle=symbol.split(":")[-1] if ":" in symbol else "BTC",
            min_amount=1.0,
        )
    return ContractSpec(
        contract_size=1.0,
        inverse=False,
        base=symbol.split("/")[0] or "BTC",
        quote="USDT",
        min_amount=0.00001,
    )


@dataclass
class FakeVenue:
    """An exchange that lives in memory.

    Only the surface TradingService and AutoTrader use is implemented: balance,
    positions, open orders, order placement and leverage. Orders fill instantly
    at the price they were sent with, which is enough to assert "the engine
    sent the right size to the right place".
    """

    has_credentials: bool = True
    network: Network = Network.MAINNET
    # Wallet per market, in the market's own settlement asset.
    balances: dict[MarketType, dict[str, float]] = field(default_factory=dict)
    fills: list[dict] = field(default_factory=list)
    leverage_calls: list[tuple] = field(default_factory=list)
    canceled: list[tuple] = field(default_factory=list)
    stop_orders: list[tuple] = field(default_factory=list)
    open_orders: list[dict] = field(default_factory=list)
    spec_overrides: dict[MarketType, ContractSpec] = field(default_factory=dict)
    _book: dict[str, float] = field(default_factory=dict)
    _entry: dict[str, float] = field(default_factory=dict)
    _last: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.balances:
            self.balances = {
                MarketType.SPOT: {"USDT": 10_000.0},
                MarketType.USDM: {"USDT": 10_000.0},
                MarketType.COINM: {"BTC": 0.2},
            }
        self.proxy = ""

    # ------------------------------------------------------------------ reads
    def contract_spec(self, market: MarketType, symbol: str, allow_network: bool = True):
        return self.spec_overrides.get(market) or spec_for(market, symbol)

    def amount_to_precision(self, market: MarketType, symbol: str, amount: float) -> float:
        spec = self.contract_spec(market, symbol)
        return float(int(amount)) if spec.inverse else float(amount)

    def price_to_precision(self, market: MarketType, symbol: str, price: float) -> float:
        return float(price)

    def fetch_balance(self, market: MarketType) -> dict:
        total = dict(self.balances.get(market) or {})
        return {"free": dict(total), "used": {}, "total": total, "info": {}}

    def fetch_positions(self, market: MarketType, symbols=None) -> list[dict]:
        prices = list(symbols or self._book.keys())
        out = []
        for symbol in prices:
            base = self._book.get(symbol) or 0.0
            if abs(base) <= 1e-12:
                continue
            spec = self.contract_spec(market, symbol)
            last = self._last.get(symbol) or self._entry.get(symbol, 0.0)
            contracts = abs(spec.base_to_contracts(base, last))
            entry = self._entry.get(symbol) or last
            out.append(
                {
                    "symbol": symbol,
                    "contracts": contracts,
                    "side": "short" if base < 0 else "long",
                    "entryPrice": entry,
                    "markPrice": last,
                    "unrealizedPnl": (last - entry) * base,
                }
            )
        return out

    def fetch_open_orders(self, market: MarketType, symbol: str | None = None) -> list[dict]:
        return [dict(item) for item in self.open_orders]

    def fetch_tickers(self, market: MarketType, symbols=None) -> dict:
        wanted = list(symbols or self._last.keys())
        return {name: {"last": self._last.get(name, 0.0)} for name in wanted}

    def fetch_my_trades(self, market: MarketType, symbol: str, limit: int = 50) -> list[dict]:
        return [dict(item) for item in self.fills if item.get("symbol") == symbol][-limit:]

    def fetch_ticker(self, market: MarketType, symbol: str) -> dict:
        return {"last": self._last.get(symbol, 0.0)}

    # ----------------------------------------------------------------- writes
    def create_order(
        self, market: MarketType, symbol: str, order_type: str, side: str,
        amount: float, price: float | None = None, params: dict | None = None,
    ) -> dict:
        price = float(price or self._last.get(symbol) or 0.0)
        spec = self.contract_spec(market, symbol)
        base = spec.contracts_to_base(amount, price)
        signed = base if side == "buy" else -base
        old = self._book.get(symbol, 0.0)
        new = old + signed
        if abs(new) <= 1e-12:
            self._book.pop(symbol, None)
            self._entry.pop(symbol, None)
        else:
            self._book[symbol] = new
            if old == 0 or (old > 0) != (new > 0):
                self._entry[symbol] = price
            elif abs(new) > abs(old):
                self._entry[symbol] = (self._entry.get(symbol, price) * abs(old) + price * abs(signed)) / abs(new)
        self._last[symbol] = price
        if market is MarketType.SPOT:
            # A spot fill moves the base coin and the quote balance, which is
            # how the engine reads "what am I holding" on the next candle.
            wallet = self.balances.setdefault(market, {})
            base_asset = spec.base or symbol.split("/")[0]
            quote_asset = spec.quote or "USDT"
            wallet[base_asset] = wallet.get(base_asset, 0.0) + signed
            wallet[quote_asset] = wallet.get(quote_asset, 0.0) - signed * price
        order = {
            "id": f"ord-{len(self.fills) + 1}",
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "amount": amount,
            "filled": amount,
            "price": price,
            "average": price,
            "cost": abs(base) * price,
            "status": "closed",
            "timestamp": int(time.time() * 1000),
            "info": dict(params or {}),
        }
        self.fills.append({**order, "market": market.value})
        return order

    def set_leverage(self, symbol: str, leverage: float, market: MarketType) -> dict:
        self.leverage_calls.append((symbol, leverage, market))
        return {}

    def cancel_symbol_orders(self, market: MarketType, symbol: str) -> int:
        self.canceled.append((market, symbol))
        return 0

    def cancel_order(self, market: MarketType, order_id: str, symbol: str | None = None) -> dict:
        return {"id": order_id, "symbol": symbol or "", "status": "canceled"}

    def create_stop_market(self, market: MarketType, symbol: str, side: str, amount: float, stop_price: float) -> dict:
        self.stop_orders.append(("stop", symbol, side, amount, stop_price))
        return {"id": f"stop-{len(self.stop_orders)}", "status": "open", "type": "STOP_MARKET"}

    def create_take_profit_market(self, market: MarketType, symbol: str, side: str, amount: float, target_price: float) -> dict:
        self.stop_orders.append(("target", symbol, side, amount, target_price))
        return {"id": f"tp-{len(self.stop_orders)}", "status": "open", "type": "TAKE_PROFIT_MARKET"}

    # ---------------------------------------------------------------- helpers
    def position_base(self, symbol: str) -> float:
        """Signed base-coin size the venue currently holds."""
        return self._book.get(symbol, 0.0)

    def sides(self) -> list[str]:
        return [item["side"] for item in self.fills]
