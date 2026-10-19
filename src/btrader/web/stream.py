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
# Binance accepts these depth limits only; 12 is rejected with -4021.
BOOK_DEPTH = 10
_RETRY_SECONDS = 3.0
# Trades arrive per fill; folding a short window into one frame keeps the
# bounded client queue from being flushed by a busy market.
TRADE_FLUSH_SECONDS = 0.25


class StreamHub:
    """Owns the exchange websockets and the subscriber queues."""

    # Binance's public market-data websocket. It serves the same trades and
    # books as the trading host but stays reachable from networks that block
    # stream.binance.com, so a testnet trading session can still chart real
    # prices instead of simulated ones.
    PUBLIC_SPOT_WS = "wss://data-stream.binance.vision/ws"
    # The matching read-only REST host. ccxt loads the market list before it
    # can push a frame; without this the load goes to api.binance.com, which
    # times out on such networks and leaves the socket silent.
    PUBLIC_SPOT_REST = "https://data-api.binance.vision/api/v3"
    # Futures publishes no separate mirror, but its mainnet hosts answer where
    # the spot trading host does not. Charting must read these: a testnet
    # futures session otherwise shows simulated prices hundreds of dollars
    # away from the venue the user compares against.
    PUBLIC_FUTURES_WS: dict[MarketType, tuple[str, str]] = {
        MarketType.USDM: ("wss://fstream.binance.com/ws", "future"),
        MarketType.COINM: ("wss://dstream.binance.com/ws", "inverse"),
    }
    PUBLIC_FUTURES_REST: dict[MarketType, tuple[str, ...]] = {
        MarketType.USDM: (
            "https://fapi.binance.com/fapi/v1",
            "https://fapi.binance.com/fapi/v2",
            "https://fapi.binance.com/fapi/v3",
        ),
        MarketType.COINM: ("https://dapi.binance.com/dapi/v1",),
    }

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
        if market is MarketType.SPOT:
            self._use_public_spot_stream(exchange)
        else:
            self._use_public_futures_stream(exchange, market)
        self._exchanges[market] = exchange
        return exchange

    def _use_public_spot_stream(self, exchange: Any) -> None:
        """Chart spot from the public feed while orders go to the testnet.

        Futures has no separate public host, so it keeps the session's
        network. This is applied after ``set_sandbox_mode`` on purpose: the
        sandbox switch rewrites the whole ``api`` block, and this override has
        to win.

        The REST host matters as much as the socket: ccxt loads the market
        list before it can push a frame, and its default fetch probes spot,
        linear and inverse — one timeout there and no ticker ever arrives.
        Restricting it to spot and serving that from the public mirror keeps
        the socket working on networks that block the trading hosts.
        """
        api = exchange.urls.get("api")
        if isinstance(api, dict):
            api["public"] = self.PUBLIC_SPOT_REST
            api["private"] = self.PUBLIC_SPOT_REST
            ws = api.get("ws")
            if isinstance(ws, dict):
                ws["spot"] = self.PUBLIC_SPOT_WS
                ws["margin"] = self.PUBLIC_SPOT_WS
        options = getattr(exchange, "options", None)
        if isinstance(options, dict):
            fetch = options.get("fetchMarkets")
            if isinstance(fetch, dict):
                fetch["types"] = ["spot"]
            else:
                options["fetchMarkets"] = ["spot"]

    def _use_public_futures_stream(self, exchange: Any, market: MarketType) -> None:
        """Chart futures from the mainnet feed while orders go to the testnet.

        Futures has no separate read-only mirror, but its mainnet hosts are
        reachable and serve exactly the public data the chart needs. Leaving a
        testnet session on the sandbox feed would show simulated prices that
        drift from the venue the user compares against, which reads as "the
        chart is wrong" rather than "you are on the testnet".

        Applied after ``set_sandbox_mode`` for the same reason as spot: the
        sandbox switch rewrites the whole ``api`` block, and this has to win.
        Only public endpoints are touched, so the charting client still cannot
        sign anything.
        """
        entry = self.PUBLIC_FUTURES_WS.get(market)
        if entry is None:
            return
        ws_host, ws_key = entry
        rest_hosts = self.PUBLIC_FUTURES_REST.get(market, ())
        api = exchange.urls.get("api")
        if isinstance(api, dict):
            ws = api.get("ws")
            if isinstance(ws, dict):
                ws[ws_key] = ws_host
            # USD-M splits its public endpoints across v1/v2/v3; COIN-M uses
            # dapi only. Rewrite whichever keys this venue actually has.
            for index, key in enumerate(("fapiPublic", "fapiPublicV2", "fapiPublicV3")):
                if key in api and index < len(rest_hosts):
                    api[key] = rest_hosts[index]
            if "dapiPublic" in api and rest_hosts:
                api["dapiPublic"] = rest_hosts[0]
        # Load only this venue's markets. The default probes spot, linear and
        # inverse together; the spot probes go to hosts that may time out and
        # cost the socket its first frame.
        options = getattr(exchange, "options", None)
        if isinstance(options, dict):
            wanted = "inverse" if market is MarketType.COINM else "linear"
            fetch = options.get("fetchMarkets")
            if isinstance(fetch, dict):
                fetch["types"] = [wanted]
            else:
                options["fetchMarkets"] = [wanted]

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
