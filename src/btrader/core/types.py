"""Enumerations shared across the application."""

from __future__ import annotations

from enum import Enum

TIMEFRAME_SECONDS: dict[str, int] = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "6h": 21600,
    "8h": 28800,
    "12h": 43200,
    "1d": 86400,
    "3d": 259200,
    "1w": 604800,
}

TIMEFRAMES = ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d", "1w"]


class MarketType(str, Enum):
    SPOT = "spot"
    USDM = "usdm"
    COINM = "coinm"

    @property
    def label(self) -> str:
        return {
            MarketType.SPOT: "现货",
            MarketType.USDM: "U 本位合约",
            MarketType.COINM: "币本位合约",
        }[self]

    @property
    def is_futures(self) -> bool:
        return self is not MarketType.SPOT

    @property
    def ccxt_id(self) -> str:
        return {
            MarketType.SPOT: "binance",
            MarketType.USDM: "binanceusdm",
            MarketType.COINM: "binancecoinm",
        }[self]

    @property
    def quote_assets(self) -> tuple[str, ...]:
        return {
            MarketType.SPOT: ("USDT", "USDC", "FDUSD", "BTC", "ETH", "BNB"),
            MarketType.USDM: ("USDT", "USDC"),
            MarketType.COINM: ("USD",),
        }[self]


class Network(str, Enum):
    MAINNET = "mainnet"
    TESTNET = "testnet"

    @property
    def label(self) -> str:
        return "主网" if self is Network.MAINNET else "测试网"


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"

    @property
    def label(self) -> str:
        return "买入" if self is OrderSide.BUY else "卖出"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"

    @property
    def label(self) -> str:
        return "市价" if self is OrderType.MARKET else "限价"


class OrderStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    CANCELED = "canceled"
    EXPIRED = "expired"
    REJECTED = "rejected"
    UNKNOWN = "unknown"

    @property
    def label(self) -> str:
        return {
            "open": "未成交",
            "closed": "已成交",
            "canceled": "已撤销",
            "expired": "已过期",
            "rejected": "已拒绝",
            "unknown": "未知",
        }[self.value]


class RunMode(str, Enum):
    """What a run is: a backtest, or a real order on the configured network.

    The paper mode is gone on purpose: practice now happens on the exchange
    testnet, so "simulated" and "real" are the same code path and the only
    question left is which network it points at.
    """

    BACKTEST = "backtest"
    LIVE = "live"

    @property
    def label(self) -> str:
        return {"backtest": "回测", "live": "实盘"}[self.value]


def market_for_symbol(symbol: str) -> MarketType:
    """Infer the market from a ccxt symbol when the caller did not say.

    ``BTC/USDT`` -> spot, ``BTC/USDT:USDT`` -> USDT-margined,
    ``BTC/USD:BTC`` -> coin-margined.  Shared so that pricing a position
    and routing an order can never disagree about its venue.
    """
    if ":" not in symbol:
        return MarketType.SPOT
    settle = symbol.rsplit(":", 1)[1]
    return MarketType.USDM if settle.upper() in ("USDT", "USDC") else MarketType.COINM


def timeframe_seconds(timeframe: str) -> int:
    try:
        return TIMEFRAME_SECONDS[timeframe]
    except KeyError as exc:
        raise ValueError(f"不支持的周期: {timeframe}") from exc
