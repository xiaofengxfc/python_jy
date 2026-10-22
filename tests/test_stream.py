"""Market-stream URL tests: charting reads the public feed, trading does not.

Constructing a ccxt.pro client does not touch the network, so these tests can
inspect the resolved URLs directly.
"""

from __future__ import annotations

import pytest

from btrader.core.types import MarketType, Network
from btrader.web.stream import StreamHub


def test_spot_stream_uses_the_public_market_data_host() -> None:
    hub = StreamHub(Network.TESTNET)

    exchange = hub._exchange(MarketType.SPOT)

    spot = exchange.urls["api"]["ws"]["spot"]
    assert spot == StreamHub.PUBLIC_SPOT_WS
    assert "testnet" not in spot


def test_futures_keeps_the_session_network() -> None:
    """There is no public futures mirror, so futures must not be rewritten."""
    hub = StreamHub(Network.TESTNET)

    exchange = hub._exchange(MarketType.USDM)

    future = exchange.urls["api"]["ws"]["future"]
    assert MarketType.USDM.ccxt_id == "binanceusdm"
    assert "binancefuture" in future or "testnet" in future or "binance.com" in future


def test_stream_override_only_touches_known_keys() -> None:
    """An unexpected urls shape must be a no-op rather than a crash."""
    hub = StreamHub(Network.MAINNET)

    class Fake:
        urls = {"api": {"ws": "not-a-dict"}}

    hub._use_public_spot_stream(Fake())

    assert Fake.urls["api"]["ws"] == "not-a-dict"


def test_spot_stream_loads_markets_from_the_public_rest_host() -> None:
    """ccxt loads the market list before its first frame.

    That load defaults to hitting api.binance.com as well as the futures hosts.
    On a network where those time out, the socket stays silent and the UI shows
    no price at all, so the charting client must fetch markets from the public
    mirror and only for spot.
    """
    hub = StreamHub(Network.TESTNET)

    exchange = hub._exchange(MarketType.SPOT)

    assert exchange.urls["api"]["public"] == StreamHub.PUBLIC_SPOT_REST
    types = exchange.options["fetchMarkets"]["types"]
    assert types == ["spot"]


def test_futures_does_not_borrow_the_spot_rest_host() -> None:
    hub = StreamHub(Network.TESTNET)

    exchange = hub._exchange(MarketType.USDM)

    assert exchange.urls["api"]["public"] != StreamHub.PUBLIC_SPOT_REST


@pytest.mark.parametrize("network", [Network.MAINNET, Network.TESTNET])
def test_overriding_is_idempotent(network: Network) -> None:
    hub = StreamHub(network)
    exchange = hub._exchange(MarketType.SPOT)

    hub._use_public_spot_stream(exchange)

    assert exchange.urls["api"]["ws"]["spot"] == StreamHub.PUBLIC_SPOT_WS


# ---------------------------------------------------- ticker 帧里的买卖一
def test_the_ticker_frame_carries_the_book_top_when_the_venue_omits_it() -> None:
    """币安合约的 watch_ticker 没有买一卖一：帧里的买卖一来自盘口那一档。

    以前两个入口都把它压成 0，前端 `bid && ask` 判空，用户看到「有最新价、
    买卖价却是 —」。这一条同时钉住帧形状：字段一个都不许少、不许改名。
    """
    import asyncio
    import contextlib

    hub = StreamHub(Network.MAINNET)
    key = ("usdm", "BTC/USDT:USDT")
    queue: asyncio.Queue = asyncio.Queue(maxsize=4)
    hub._queues[key] = {queue}
    hub._top[key] = (81_613.5, 81_613.6)

    class FakeExchange:
        def __init__(self) -> None:
            self.served = 0

        async def watch_ticker(self, symbol: str) -> dict:
            self.served += 1
            if self.served > 1:
                # 只给一帧，然后停住：否则这个循环会一直空转，事件循环再也
                # 回不到测试里那句 queue.get()。
                await asyncio.sleep(3600)
            return {
                "symbol": symbol,
                "last": 81_613.5,
                "bid": None,
                "ask": None,
                "high": 84_875.2,
                "low": 81_264.1,
                "baseVolume": 1_000.0,
                "quoteVolume": 81_000_000.0,
                "percentage": 1.25,
                "timestamp": 1_792_685_211_080,
                "info": {"lastPrice": "81613.5"},
            }

    async def drive() -> dict:
        task = asyncio.create_task(hub._watch_ticker(FakeExchange(), key, key[1]))
        try:
            return await asyncio.wait_for(queue.get(), timeout=2)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    frame = asyncio.run(drive())

    assert frame["type"] == "ticker"
    assert frame["bid"] == 81_613.5
    assert frame["ask"] == 81_613.6
    assert set(frame) == {
        "type",
        "symbol",
        "last",
        "bid",
        "ask",
        "high",
        "low",
        "base_volume",
        "quote_volume",
        "change_pct",
        "timestamp",
    }


def test_the_ticker_frame_stays_zero_when_there_is_no_book_yet() -> None:
    """盘口帧还没来时留 0，前端显示「—」，不要编一个价。"""
    import asyncio
    import contextlib

    hub = StreamHub(Network.MAINNET)
    key = ("usdm", "BTC/USDT:USDT")
    queue: asyncio.Queue = asyncio.Queue(maxsize=4)
    hub._queues[key] = {queue}

    class FakeExchange:
        def __init__(self) -> None:
            self.served = 0

        async def watch_ticker(self, symbol: str) -> dict:
            self.served += 1
            if self.served > 1:
                await asyncio.sleep(3600)
            return {"last": 1.0, "bid": None, "ask": None, "info": {}}

    async def drive() -> dict:
        task = asyncio.create_task(hub._watch_ticker(FakeExchange(), key, key[1]))
        try:
            return await asyncio.wait_for(queue.get(), timeout=2)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    frame = asyncio.run(drive())

    assert frame["bid"] == 0.0
    assert frame["ask"] == 0.0

def test_the_ticker_frame_uses_the_raw_payload_when_the_unified_field_is_empty() -> None:
    """unified 的 bid 是 None、原始 payload 里有买一：帧里要出真价格。

    不只为了合约——ccxt 对某些现货市场的映射也会把 bid/ask 留空，
    原始字段里却是有的。两个入口现在共用同一个合并函数。
    """
    import asyncio
    import contextlib

    hub = StreamHub(Network.MAINNET)
    key = ("spot", "BTC/USDT")
    queue: asyncio.Queue = asyncio.Queue(maxsize=4)
    hub._queues[key] = {queue}

    class FakeExchange:
        def __init__(self) -> None:
            self.served = 0

        async def watch_ticker(self, symbol: str) -> dict:
            self.served += 1
            if self.served > 1:
                await asyncio.sleep(3600)
            return {
                "last": 81_654.0,
                "bid": None,
                "ask": None,
                "info": {"bidPrice": "81654.00", "askPrice": "81654.01"},
            }

    async def drive() -> dict:
        task = asyncio.create_task(hub._watch_ticker(FakeExchange(), key, key[1]))
        try:
            return await asyncio.wait_for(queue.get(), timeout=2)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    frame = asyncio.run(drive())

    assert frame["bid"] == 81_654.0
    assert frame["ask"] == 81_654.01