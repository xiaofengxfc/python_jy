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
