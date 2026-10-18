"""Lightweight domain models independent of ccxt."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .types import MarketType


def _f(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _ts(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


@dataclass(slots=True)
class SymbolInfo:
    symbol: str
    market_type: MarketType
    base: str
    quote: str
    active: bool = True
    amount_precision: int = 8
    price_precision: int = 8
    min_amount: float = 0.0
    min_cost: float = 0.0
    contract_size: float = 1.0
    is_inverse: bool = False

    @property
    def display(self) -> str:
        if self.is_inverse and ":" in self.symbol:
            base, _, rest = self.symbol.partition("/")
            return f"{base}/{rest.split(':')[0]}"
        return self.symbol


@dataclass(slots=True)
class Ticker:
    symbol: str
    last: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    high: float = 0.0
    low: float = 0.0
    base_volume: float = 0.0
    quote_volume: float = 0.0
    change_pct: float = 0.0
    timestamp: int = 0

    @property
    def spread(self) -> float:
        if self.bid and self.ask:
            return self.ask - self.bid
        return 0.0


@dataclass(slots=True)
class OrderBook:
    symbol: str
    bids: list[tuple[float, float]] = field(default_factory=list)
    asks: list[tuple[float, float]] = field(default_factory=list)
    timestamp: int = 0

    @property
    def best_bid(self) -> float:
        return self.bids[0][0] if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return self.asks[0][0] if self.asks else 0.0


@dataclass(slots=True)
class Trade:
    id: str
    symbol: str
    side: str
    price: float
    amount: float
    cost: float
    timestamp: int
    fee: float = 0.0
    fee_currency: str = ""
    order_id: str = ""


@dataclass(slots=True)
class Order:
    id: str
    symbol: str
    side: str
    type: str
    price: float
    amount: float
    filled: float
    remaining: float
    cost: float
    status: str
    timestamp: int
    client_order_id: str = ""
    reduce_only: bool = False
    stop_price: float = 0.0
    average: float = 0.0

    @property
    def is_open(self) -> bool:
        return self.status == "open"


@dataclass(slots=True)
class Position:
    symbol: str
    side: str
    contracts: float
    entry_price: float
    mark_price: float
    unrealized_pnl: float
    leverage: float
    liquidation_price: float = 0.0
    notional: float = 0.0
    percentage: float = 0.0
    timestamp: int = 0

    @property
    def is_long(self) -> bool:
        return self.side == "long"

    @property
    def signed_size(self) -> float:
        return self.contracts if self.is_long else -self.contracts


@dataclass(slots=True)
class Balance:
    asset: str
    free: float = 0.0
    used: float = 0.0
    total: float = 0.0


@dataclass(slots=True)
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float


def ms_to_datetime(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def now_ms() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)
