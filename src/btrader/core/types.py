"""Enumerations shared across the application."""

from __future__ import annotations

import re
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

# 决策节奏：`bar` 表示「跟着主周期收盘」，其余是固定节拍。节奏与用于分析的
# K 线周期是两件事：4h 结构 + 每 15m 决策一次是合法组合，以前它们被焊在一起，
# 想快只能把分析周期也改小，于是模型每根都看同一根没走完的 K 线。
CADENCE_CHOICES: tuple[str, ...] = ("bar", "5m", "15m", "30m", "1h", "4h")
CADENCE_LABELS: dict[str, str] = {
    "bar": "跟主周期",
    "5m": "每 5 分钟",
    "15m": "每 15 分钟",
    "30m": "每 30 分钟",
    "1h": "每小时",
    "4h": "每 4 小时",
}


# Exchange-side protective orders, recognised by ccxt's normalized type.
STOP_TYPES = {
    "stop",
    "stop_market",
    "stop_loss",
    "stop_loss_limit",
    "trading_stop",
}
TAKE_PROFIT_TYPES = {
    "take_profit",
    "take_profit_market",
    "take_profit_limit",
}
PROTECTIVE_TYPES = STOP_TYPES | TAKE_PROFIT_TYPES

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


# 交割合约的符号在结算币后面还挂一段到期日，例如 BTC/USDT:USDT-250328。
_SYMBOL_EXPIRY = re.compile(r"-\d{6}$")


def split_symbol(symbol: str) -> tuple[str, str, str]:
    """(base, quote, settle) of a ccxt symbol; settle is "" for a spot pair.

    ``BTC/USDT`` -> (BTC, USDT, ""), ``BTC/USDT:USDT`` -> (BTC, USDT, USDT),
    交割 ``BTC/USDT:USDT-250328`` -> settle 也是 USDT —— 到期日要先切掉再判，
    否则 USDT-250328 不等于 USDT，这张交割单会被当成币本位，走错市场的客户端。
    判定市场类型的地方都从这里取，别再各写一份冒号切分。
    """
    text = str(symbol or "").strip()
    pair, _, settle = text.partition(":")
    base, _, quote = pair.partition("/")
    return base, quote, _SYMBOL_EXPIRY.sub("", settle)


def symbol_is_contract(symbol: str) -> bool:
    """合约写法（带结算币，交割还多一段到期日）；现货写法是 ``BASE/QUOTE``。"""
    return bool(split_symbol(symbol)[2])


def symbol_shape_error(symbol: str) -> str:
    """写法自洽返回空串，否则返回一句中文说明（说明里带上该写的样子）。

    现货是 ``BASE/QUOTE``，合约是 ``BASE/QUOTE:SETTLE``（交割再多一段 -YYMMDD）。
    整串最多一个冒号：结算币段里不许再出现冒号（``BTC/USDT:USDT:USDT`` 是写错）。
    这里只判「写得像不像交易对」，不判市场对不对：收藏是跨市场的，同一个列表里
    两种写法都合法；市场与写法的对应关系由 web 层的 ``_check_symbol_shape`` 判，
    那条路要 400 给用户看，所以它自己带文案。

    共用的理由和 ``split_symbol`` 一样：判定只写一份，别在收藏、批量涨幅、下单
    三个入口各猜一次。
    """
    text = str(symbol or "").strip()
    if not text:
        return "缺少交易对。"
    if any(char.isspace() for char in text):
        return f"交易对里不能有空格：「{text}」。"
    # 整串最多一个冒号。结算币段里再冒一个冒号不是「另一种写法」，是写错了：
    # 放过它的话 BTC/USDT:USDT:USDT 会一路进收藏、进 /api/tickers，到交易所那层才炸。
    if text.count(":") > 1:
        return (
            f"交易对写法不对：「{text}」里的冒号只能有一个，冒号后面写结算币。"
            "现货写 BTC/USDT，U 本位合约写 BTC/USDT:USDT，币本位写 BTC/USD:BTC，"
            "交割再带到期日，例如 BTC/USD:BTC-250926。"
        )
    pair, colon, settle = text.partition(":")
    base, _, quote = pair.partition("/")
    if not base or not quote or "/" in quote or "/" in settle:
        return f"交易对写法不对：「{text}」要写成「币/计价」，例如 BTC/USDT。"
    if colon and not _SYMBOL_EXPIRY.sub("", settle):
        return f"「{text}」冒号后面要写结算币，例如 BTC/USDT:USDT。"
    return ""


def market_for_symbol(symbol: str) -> MarketType:
    """Infer the market from a ccxt symbol when the caller did not say.

    ``BTC/USDT`` -> spot, ``BTC/USDT:USDT`` -> USDT-margined,
    ``BTC/USD:BTC`` -> coin-margined, ``BTC/USDT:USDT-250328`` -> USDT-margined
    (交割合约属于它结算币那一侧的市场).  Shared so that pricing a position and
    routing an order can never disagree about its venue.
    """
    settle = split_symbol(symbol)[2]
    if not settle:
        return MarketType.SPOT
    return MarketType.USDM if settle.upper() in ("USDT", "USDC") else MarketType.COINM


def timeframe_seconds(timeframe: str) -> int:
    try:
        return TIMEFRAME_SECONDS[timeframe]
    except KeyError as exc:
        raise ValueError(f"不支持的周期: {timeframe}") from exc


def cadence_seconds(cadence: str) -> int:
    """节拍秒数。`bar`（或空串）返回 0：由调用方按主周期换算。"""
    text = (cadence or "").strip()
    if not text or text == "bar":
        return 0
    return timeframe_seconds(text)
