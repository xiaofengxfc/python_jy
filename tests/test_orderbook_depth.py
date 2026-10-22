"""盘口档位：GET /api/orderbook 的默认值曾经是 12，合约整条 503。

币安的 depth 只认 5/10/20/50/100/500/1000。ccxt 不替调用方凑档位，
发 12 就直接回 -4021「12 is not valid depth limit」，路由把它翻成 503——
现货恰好接受 12，所以看着像「只有合约盘口坏了」。
"""

from __future__ import annotations

import pytest

from btrader.core.types import MarketType
from btrader.exchange.rest import (
    DEFAULT_DEPTH,
    DEPTH_LIMITS,
    BinanceREST,
    ExchangeError,
    snap_depth,
)

SYMBOL = "BTC/USDT:USDT"


class _Recorder:
    """只记住要了几档，不联网。"""

    def __init__(self, error: Exception | None = None) -> None:
        self.limits: list[int] = []
        self.error = error

    def fetch_order_book(self, symbol: str, limit: int | None = None):
        self.limits.append(limit)
        if self.error is not None:
            raise self.error
        return {"bids": [[100.0, 1.0]], "asks": [[101.0, 1.0]]}


def _rest(client: _Recorder) -> BinanceREST:
    rest = BinanceREST.__new__(BinanceREST)  # 不构造 ccxt 客户端：只测这一层
    rest.client = lambda market: client
    return rest


def test_snap_depth_always_returns_an_accepted_depth():
    for want in (1, 2, 5, 6, 10, 11, 12, 20, 21, 50, 51, 99, 100, 101, 500, 999, 1000, 5000):
        assert snap_depth(want) in DEPTH_LIMITS


def test_snap_depth_rounds_up_and_caps_at_the_largest_step():
    assert snap_depth(12) == 20
    assert snap_depth(5) == 5
    assert snap_depth(7) == 10
    assert snap_depth(51) == 100
    assert snap_depth(5000) == DEPTH_LIMITS[-1]


def test_snap_depth_falls_back_on_junk():
    assert snap_depth(None) == DEPTH_LIMITS[0]
    assert snap_depth("abc") == DEPTH_LIMITS[0]
    assert snap_depth("20") == 20


def test_the_route_default_is_a_depth_binance_accepts():
    """默认值写回 12 就是当年那条 503：这个数必须是交易所认的档位。"""
    assert DEFAULT_DEPTH in DEPTH_LIMITS


def test_order_book_never_sends_an_unaccepted_depth():
    client = _Recorder()
    rest = _rest(client)

    rest.fetch_order_book(MarketType.USDM, SYMBOL, 12)

    assert client.limits == [20]


def test_order_book_keeps_an_accepted_depth_as_is():
    client = _Recorder()
    rest = _rest(client)

    rest.fetch_order_book(MarketType.SPOT, "BTC/USDT", 50)

    assert client.limits == [50]


def test_order_book_still_wraps_venue_failures():
    rest = _rest(_Recorder(RuntimeError("boom")))

    with pytest.raises(ExchangeError):
        rest.fetch_order_book(MarketType.USDM, SYMBOL, 20)
