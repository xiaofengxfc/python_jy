"""Live market data fan-out over WebSocket.

The desktop shell keeps a persistent connection to ``/ws/market`` instead of
polling REST every couple of seconds: Binance allows far fewer REST calls per
minute than the UI would need, and a 2s poll is visibly stale on a trading
screen.

One ``ccxt.pro`` watcher pair (ticker + order book) runs per subscribed
``(market, symbol)``; every browser/Electron client gets its own bounded queue,
so a slow client drops old frames instead of stalling the others.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

import ccxt.pro as ccxtpro

from ..core.types import MarketType, Network

log = logging.getLogger(__name__)

# One queued frame per client is rarely enough for a charting UI; sixteen keeps
# a short burst without letting a stalled tab grow unbounded.
QUEUE_SIZE = 16
BOOK_DEPTH = 12
_RETRY_SECONDS = 3.0
# Trades arrive per fill; folding a short window into one frame keeps the
# bounded client queue from being flushed by a busy market.
TRADE_FLUSH_SECONDS = 0.25


class StreamHub:
    """Owns the exchange websockets and the subscriber queues."""

    def __init__(self, network: Network) -> None:
        self.network = network
        self._exchanges: dict[MarketType, Any] = {}
        self._tasks: dict[tuple[str, str], asyncio.Task] = {}
        self._queues: dict[tuple[str, str], set[asyncio.Queue]] = {}
        self._lock = asyncio.Lock()
        self._closed = False
        # ccxt's spot ``watch_ticker`` leaves bid/ask as None; the watcher below
        # remembers the last real book so the ticker frame still carries a
        # usable spread instead of zeroes.
        self._top: dict[tuple[str, str], tuple[float, float]] = {}

    # ------------------------------------------------------------- exchange
    def _exchange(self, market: MarketType) -> Any:
        exchange = self._exchanges.get(market)
        if exchange is not None:
            return exchange
        klass = getattr(ccxtpro, market.ccxt_id)
        exchange = klass(
            {
                "enableRateLimit": True,
                "options": {"defaultType": "swap" if market.is_futures else "spot"},
            }
        )
        if self.network is Network.TESTNET:
            try:
                exchange.set_sandbox_mode(True)
            except Exception as exc:  # not every venue supports sandbox websockets
                log.warning("WebSocket 测试网不可用，退回实盘行情: %s", exc)
        self._exchanges[market] = exchange
        return exchange

    # ---------------------------------------------------------- subscribers
    async def subscribe(
        self, market: MarketType, symbol: str
    ) -> tuple[asyncio.Queue, tuple[str, str]]:
        key = (market.value, symbol)
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_SIZE)
        async with self._lock:
            if self._closed:
                raise RuntimeError("行情服务已关闭")
            self._queues.setdefault(key, set()).add(queue)
            if key not in self._tasks:
                self._tasks[key] = asyncio.create_task(
                    self._watch_pair(market, symbol), name=f"ws:{key[0]}:{key[1]}"
                )
        return queue, key

    async def unsubscribe(self, key: tuple[str, str], queue: asyncio.Queue) -> None:
        async with self._lock:
            subscribers = self._queues.get(key)
            if subscribers is not None:
                subscribers.discard(queue)
            if subscribers:  # someone else still watching this symbol
                return
            self._queues.pop(key, None)
            task = self._tasks.pop(key, None)
            self._top.pop(key, None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    def _publish(self, key: tuple[str, str], message: dict[str, Any]) -> None:
        for queue in list(self._queues.get(key, ())):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                # Drop the oldest frame: the newest price matters, a backlog
                # of stale ticks does not.
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    queue.put_nowait(message)

    # -------------------------------------------------------------- watchers
    async def _watch_pair(self, market: MarketType, symbol: str) -> None:
        key = (market.value, symbol)
        exchange = self._exchange(market)
        try:
            await asyncio.gather(
                self._watch_ticker(exchange, key, symbol),
                self._watch_book(exchange, key, symbol),
                self._watch_trades(exchange, key, symbol),
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - defensive, gather already isolates
            log.exception("行情订阅异常 %s", symbol)

    async def _watch_ticker(self, exchange: Any, key, symbol: str) -> None:
        while True:
            try:
                ticker = await exchange.watch_ticker(symbol)
                bid = _num(ticker.get("bid"))
                ask = _num(ticker.get("ask"))
                if not bid or not ask:
                    # Spot tickers carry no bid/ask; fall back to the book we
                    # already stream, never to a misleading 0.
                    book_bid, book_ask = self._top.get(key, (0.0, 0.0))
                    bid = bid or book_bid
                    ask = ask or book_ask
                self._publish(
                    key,
                    {
                        "type": "ticker",
                        "symbol": symbol,
                        "last": _num(ticker.get("last")),
                        "bid": bid,
                        "ask": ask,
                        "high": _num(ticker.get("high")),
                        "low": _num(ticker.get("low")),
                        "base_volume": _num(ticker.get("baseVolume")),
                        "quote_volume": _num(ticker.get("quoteVolume")),
                        "change_pct": _num(ticker.get("percentage")),
                        "timestamp": int(ticker.get("timestamp") or 0),
                    },
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("ticker 推送中断 %s: %s", symbol, exc)
                await asyncio.sleep(_RETRY_SECONDS)

    async def _watch_book(self, exchange: Any, key, symbol: str) -> None:
        while True:
            try:
                book = await exchange.watch_order_book(symbol, BOOK_DEPTH)
                bids = [[_num(p), _num(q)] for p, q in (book.get("bids") or [])[:BOOK_DEPTH]]
                asks = [[_num(p), _num(q)] for p, q in (book.get("asks") or [])[:BOOK_DEPTH]]
                if bids and asks:
                    self._top[key] = (bids[0][0], asks[0][0])
                self._publish(
                    key,
                    {
                        "type": "book",
                        "symbol": symbol,
                        "bids": bids,
                        "asks": asks,
                        "timestamp": int(book.get("timestamp") or 0),
                    },
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("盘口推送中断 %s: %s", symbol, exc)
                await asyncio.sleep(_RETRY_SECONDS)

    async def _watch_trades(self, exchange: Any, key, symbol: str) -> None:
        """Fold public trades into short frames.

        A ticker carries the 24h volume, never the increment for the current
        bar, so the chart could not accumulate volume on its own. Public trades
        carry the traded size of every fill; a quarter-second budget keeps a
        busy pair from pushing the small client queue out.
        """
        started = int(time.time() * 1000)
        while True:
            try:
                trades = await exchange.watch_trades(symbol)
                volume = 0.0
                last_price = 0.0
                last_time = 0
                now_ms = int(time.time() * 1000)
                for trade in trades:
                    # The first watch replays recent fills. The REST snapshot
                    # the chart loaded already contains them, so only trades
                    # that happen after this subscription may add volume.
                    stamp = int(trade.get("timestamp") or now_ms)
                    if stamp < started:
                        continue
                    amount = _num(trade.get("amount"))
                    price = _num(trade.get("price"))
                    volume += amount
                    if price > 0:
                        last_price = price
                    last_time = max(last_time, stamp)
                if volume <= 0:
                    await asyncio.sleep(TRADE_FLUSH_SECONDS)
                    continue
                self._publish(
                    key,
                    {
                        "type": "trades",
                        "symbol": symbol,
                        "volume": volume,
                        "last": last_price,
                        "timestamp": last_time or int(time.time() * 1000),
                    },
                )
                # Binance pushes one message per fill; a short pause batches
                # them into the frame above instead of one frame each.
                await asyncio.sleep(TRADE_FLUSH_SECONDS)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("成交推送中断 %s: %s", symbol, exc)
                await asyncio.sleep(_RETRY_SECONDS)

    # --------------------------------------------------------------- teardown
    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            tasks = list(self._tasks.values())
            self._tasks.clear()
            self._queues.clear()
            exchanges = list(self._exchanges.values())
            self._exchanges.clear()

        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for exchange in exchanges:
            with contextlib.suppress(Exception):
                await exchange.close()


def _num(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
