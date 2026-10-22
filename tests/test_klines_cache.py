"""K-line cache partitioning tests (no network access)."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

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

class _CappedRest:
    """A REST stand-in that behaves like the venue: one request, at most 1000 candles."""

    def __init__(self, bars: int = 20_000, step_ms: int = 3_600_000, delay: float = 0.0) -> None:
        self.network = Network.MAINNET
        self.bars = bars
        self.step_ms = step_ms
        self.delay = delay
        self.limits: list[int] = []

    def rate_limit_sleep(self) -> float:
        return 0.0

    def fetch_ohlcv(self, market, symbol, timeframe, since=None, limit=500):
        self.limits.append(limit)
        if limit > 1000:
            raise RuntimeError("binance 一次最多给 1000 根 K 线")
        if self.delay:
            time.sleep(self.delay)
        end = int(time.time() * 1000) // self.step_ms * self.step_ms
        start = end - (self.bars - 1) * self.step_ms
        rows = [[start + i * self.step_ms, 1.0, 1.0, 1.0, 1.0, 1.0] for i in range(self.bars)]
        if since is not None:
            rows = [row for row in rows if row[0] >= since]
        return rows[:limit]


def test_a_window_longer_than_one_request_is_paged(tmp_path: Path) -> None:
    """回测页最多能填 5000 根，交易所单次只收 1000 根。

    整段窗口原样发给交易所会被拒（Limit is too large），回测还没开始就失败。
    """
    rest = _CappedRest()
    store = KlineStore(rest, tmp_path)

    frame = store.fetch(MarketType.SPOT, "BTC/USDT", "1h", limit=3000)

    assert len(frame) == 3000
    assert max(rest.limits) <= 1000
    assert len(rest.limits) == 3
    assert frame["timestamp"].diff().dropna().eq(3_600_000).all()


def test_a_live_tick_does_not_shrink_the_stored_history(tmp_path: Path) -> None:
    """use_cache=False 的意思是「必须联网」，不是「只存这次要的条数」。

    自动交易每根 K 线都用 use_cache=False 取几百根；写回时若只写这一窗，
    图表攒下的历史缓存会被截短，下次回测又得整个重下。
    """
    KlineStore(_CappedRest(), tmp_path).fetch(
        MarketType.SPOT, "BTC/USDT", "1h", limit=1000
    )
    path = KlineStore(_CappedRest(), tmp_path)._path(MarketType.SPOT, "BTC/USDT", "1h")
    assert len(pd.read_parquet(path)) == 1000

    KlineStore(_CappedRest(), tmp_path).fetch(
        MarketType.SPOT, "BTC/USDT", "1h", limit=300, use_cache=False
    )

    assert len(pd.read_parquet(path)) == 1000


def test_concurrent_fetches_of_the_same_key_do_not_collide(tmp_path: Path) -> None:
    """图表、自动交易、回测会同时取同一个缓存文件。

    两条线程共用一个 .tmp.parquet 时，先落地的那条把临时文件挪走，后一条
    replace 直接抛 PermissionError，取 K 线就整个失败了。
    """
    store = KlineStore(_CappedRest(delay=0.05), tmp_path)
    errors: list[BaseException] = []

    def work() -> None:
        try:
            store.fetch(MarketType.SPOT, "ETH/USDT", "1h", limit=500, use_cache=False)
        except BaseException as exc:  # noqa: BLE001 - 这就是要断言的东西
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    path = store._path(MarketType.SPOT, "ETH/USDT", "1h")
    assert len(pd.read_parquet(path)) == 500
