"""K-line cache partitioning tests (no network access)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from btrader.core.types import MarketType, Network
from btrader.exchange.klines import KlineStore


def _rest(network: Network) -> SimpleNamespace:
    # Only ``network`` is read when a source is not passed explicitly.
    return SimpleNamespace(network=network, rate_limit_sleep=lambda: 0.0)


def test_cache_is_partitioned_by_explicit_source(tmp_path: Path) -> None:
    store = KlineStore(_rest(Network.TESTNET), tmp_path, source="mainnet")

    path = store._path(MarketType.SPOT, "BTC/USDT", "15m")

    assert path == tmp_path / "mainnet" / "spot" / "BTC_USDT" / "15m.parquet"
    assert path.parent.is_dir()


def test_cache_falls_back_to_the_session_network(tmp_path: Path) -> None:
    testnet = KlineStore(_rest(Network.TESTNET), tmp_path)
    mainnet = KlineStore(_rest(Network.MAINNET), tmp_path)

    assert "testnet" in testnet._path(MarketType.SPOT, "BTC/USDT", "1h").parts
    assert "mainnet" in mainnet._path(MarketType.SPOT, "BTC/USDT", "1h").parts


def test_the_two_sources_never_share_a_file(tmp_path: Path) -> None:
    """A simulated candle series must not be merged into a real one."""
    testnet = KlineStore(_rest(Network.TESTNET), tmp_path)
    mainnet = KlineStore(_rest(Network.MAINNET), tmp_path)

    spot = MarketType.SPOT
    assert testnet._path(spot, "ETH/USDT", "1d") != mainnet._path(spot, "ETH/USDT", "1d")


def test_symbol_separators_do_not_create_subfolders(tmp_path: Path) -> None:
    store = KlineStore(_rest(Network.MAINNET), tmp_path)

    path = store._path(MarketType.USDM, "BTC/USDT:USDT", "1h")

    assert path.parent.name == "BTC_USDT-USDT"
    assert path.parent.parent.name == "usdm"
