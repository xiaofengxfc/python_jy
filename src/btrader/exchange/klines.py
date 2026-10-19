"""Market data access: historical klines with on-disk Parquet cache."""

from __future__ import annotations

import logging
import time
from pathlib import Path

import pandas as pd

from ..core.types import MarketType, timeframe_seconds
from .rest import BinanceREST

log = logging.getLogger(__name__)

COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]

# A REST snapshot is reused only this briefly. The tail candle is still
# forming, so any longer window leaves the chart showing a stale close while
# the ticker has already moved on.
CACHE_TTL_MS = 3_000


class KlineStore:
    """Loads OHLCV data from the Binance REST API and caches it as Parquet.

    Cache files are partitioned by data source, market type and symbol:
        <cache>/klines/mainnet/usdm/BTC_USDT/15m.parquet

    The source partition is what keeps a testnet session from reading candles
    that a live session downloaded (or the other way round): simulated candles
    and real ones share a timestamp grid but not a price scale.
    """

    def __init__(
        self, rest: BinanceREST, cache_root: Path, source: str | None = None
    ) -> None:
        self._rest = rest
        self._root = cache_root
        # Candles are cached per data source. Without this, a session that
        # switches between testnet and mainnet data would merge simulated
        # candles into the real series (and vice versa), producing exactly the
        # sort of 30%-wide bars that make a chart unreadable.
        self._source = source or rest.network.value
        # Last network refresh per (market, symbol, timeframe), so a burst of
        # identical requests shares one download without letting the tail
        # candle go stale.
        self._refreshed: dict[tuple[str, str, str], float] = {}

    def _path(self, market: MarketType, symbol: str, timeframe: str) -> Path:
        safe = symbol.replace("/", "_").replace(":", "-")
        folder = self._root / self._source / market.value / safe
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{timeframe}.parquet"

    # ------------------------------------------------------------------ cache
    def _read_cache(self, path: Path) -> pd.DataFrame:
        if not path.exists():
            return pd.DataFrame(columns=COLUMNS)
        try:
            df = pd.read_parquet(path)
        except Exception as exc:  # corrupted cache should not break the app
            log.warning("K 线缓存损坏，将重新下载: %s (%s)", path, exc)
            return pd.DataFrame(columns=COLUMNS)
        return df

    def _write_cache(self, path: Path, df: pd.DataFrame) -> None:
        if df.empty:
            return
        tmp = path.with_suffix(".tmp.parquet")
        df.to_parquet(tmp, index=False, compression="snappy")
        tmp.replace(path)

    @staticmethod
    def _merge(old: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
        if old.empty:
            merged = new.copy()
        elif new.empty:
            merged = old.copy()
        else:
            merged = pd.concat([old, new], ignore_index=True)
        if merged.empty:
            return merged
        merged = merged.drop_duplicates(subset=["timestamp"], keep="last")
        return merged.sort_values("timestamp").reset_index(drop=True)

    # ------------------------------------------------------------------- api
    def fetch(
        self,
        market: MarketType,
        symbol: str,
        timeframe: str,
        limit: int = 1000,
        since: int | None = None,
        use_cache: bool = True,
    ) -> pd.DataFrame:
        """Return OHLCV candles, newest last.

        When ``since`` is None only the most recent ``limit`` candles are
        requested and the cache is used when it is fresh enough.
        """
        step_ms = timeframe_seconds(timeframe) * 1000
        path = self._path(market, symbol, timeframe)
        cache = self._read_cache(path) if use_cache else pd.DataFrame(columns=COLUMNS)

        if since is not None:
            return self._fetch_range(market, symbol, timeframe, since, limit, step_ms, path, cache)

        key = (market.value, symbol, timeframe)
        if use_cache and not cache.empty:
            age = time.monotonic() - self._refreshed.get(key, 0.0)
            # The cache holds the *closed* bars. Reusing it is safe only while
            # a forming candle cannot have advanced past the stored tail;
            # otherwise the newest bar is missing and the chart trails the
            # ticker by a whole interval.
            if age * 1000 < CACHE_TTL_MS and len(cache) >= limit:
                return cache.tail(limit).reset_index(drop=True)

        from_ts = int(time.time() * 1000) - limit * step_ms
        fresh = self._rest.fetch_ohlcv(market, symbol, timeframe, since=from_ts, limit=limit)
        fresh = self._normalize(fresh)
        merged = self._merge(cache, fresh)
        if not merged.empty:
            self._write_cache(path, merged)
        self._refreshed[key] = time.monotonic()
        return merged.tail(limit).reset_index(drop=True)

    def _fetch_range(
        self,
        market: MarketType,
        symbol: str,
        timeframe: str,
        since: int,
        limit: int,
        step_ms: int,
        path: Path,
        cache: pd.DataFrame,
    ) -> pd.DataFrame:
        """Download ``limit`` candles starting at ``since`` (used by backtests)."""
        rows: list = []
        cursor = since
        remaining = limit
        while remaining > 0:
            batch = self._rest.fetch_ohlcv(
                market, symbol, timeframe, since=cursor, limit=min(1000, remaining)
            )
            if not batch:
                break
            rows.extend(batch)
            cursor = int(batch[-1][0]) + step_ms
            remaining -= len(batch)
            if len(batch) < 1000:
                break
            time.sleep(self._rest.rate_limit_sleep())
        new = self._normalize(rows)
        merged = self._merge(cache, new)
        if not merged.empty:
            self._write_cache(path, merged)
        window = merged[merged["timestamp"] >= since]
        return window.head(limit).reset_index(drop=True)

    @staticmethod
    def _normalize(rows) -> pd.DataFrame:
        if isinstance(rows, pd.DataFrame):
            return rows
        if not rows:
            return pd.DataFrame(columns=COLUMNS)
        df = pd.DataFrame(rows, columns=COLUMNS)
        for col in COLUMNS[1:]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["timestamp"] = df["timestamp"].astype("int64")
        return df.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)
