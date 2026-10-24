"""Thin synchronous REST wrapper around ccxt for Binance."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import ccxt

from ..core.contracts import DEFAULT_SPEC, ContractSpec
from ..core.credentials import Credentials
from ..core.types import PROTECTIVE_TYPES, MarketType, Network, split_symbol

log = logging.getLogger(__name__)


class ExchangeError(RuntimeError):
    """Raised for normalized, user-facing exchange errors."""


# Binance 把触发类订单（止损、止盈、条件单）放进独立的算法单端点，返回 algoId /
# algoType: CONDITIONAL。普通挂单接口（fetch_open_orders / cancel_order）里一条
# 都查不到，ccxt 4.5.88 也没有统一封装，所以直接用原始端点。键是市场类型，
# 值分别是「列挂着的算法单」和「撤一张算法单」。
_ALGO_ENDPOINTS: dict[MarketType, tuple[str, str]] = {
    MarketType.USDM: ("fapiPrivateGetOpenAlgoOrders", "fapiPrivateDeleteAlgoOrder"),
    MarketType.COINM: ("dapiPrivateGetOpenAlgoOrders", "dapiPrivateDeleteAlgoOrder"),
}


def _looks_unknown_order(exc: BaseException) -> bool:
    """交易所说这张单不存在（币安 -2011），也就是该换算法单端点再试一次。"""
    text = str(exc).lower()
    return "-2011" in text or "unknown order" in text


# 币安的深度只认这几档（现货与三种合约同一套）。别的数字不会「凑合返回」，直接回
# -4021「12 is not valid depth limit」——GET /api/orderbook 的默认值曾经是 12，
# 于是合约盘口每次都 503，现货恰好能用，看着像「只有合约坏了」。
DEPTH_LIMITS: tuple[int, ...] = (5, 10, 20, 50, 100, 500, 1000)
# 路由的默认档位也走这个数：写死 12 就是当年那条 503。
DEFAULT_DEPTH = 20


def _amount_step(raw: dict | None) -> float:
    """Amount step of one ccxt market, in the market's own amount unit.

    ccxt 把步长放在 ``precision.amount``：现版本是绝对步长（``1e-05``），
    老版本这一格是小数位数（``5``）。读不出来就回 0，让调用方不截断 —— 
    把位数当步长会把数量放大六个数量级。
    """
    precision = (raw or {}).get("precision")
    if not isinstance(precision, dict):
        return 0.0
    value = precision.get("amount")
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, int):
        return 10.0 ** -value if value > 0 else 0.0
    try:
        step = float(value)
    except (TypeError, ValueError):
        return 0.0
    return step if step > 0 else 0.0


def snap_depth(limit: Any) -> int:
    """把想要的档数换成交所认的那一档：向上取，超过最大档就取最大档。"""
    try:
        want = int(limit)
    except (TypeError, ValueError):
        want = DEPTH_LIMITS[0]
    for step in DEPTH_LIMITS:
        if want <= step:
            return step
    return DEPTH_LIMITS[-1]


# ccxt 把 BadSymbol 压成普通 ExchangeError 之后只剩一句英文，调用方只能靠这组词根
# 把「没有这个交易对」和「交易所暂时连不上」分开：前者 400，后者才是 503。
SYMBOL_MISSING_MARKERS: tuple[str, ...] = (
    "does not have market symbol",
    "invalid symbol",
    "market symbol not found",
    "invalid market",
)


def looks_unknown_symbol(exc: BaseException) -> bool:
    """这个错误是不是「交易所没有这个交易对」（而不是网络或限流）。"""
    text = str(exc).lower()
    return any(marker in text for marker in SYMBOL_MISSING_MARKERS)


# HTTP 451：交易所按**出口 IP** 判了「受限地区」。ccxt 把它归进 NetworkError，
# 界面上只剩一句英文法律声明，看着像网络抖动——用户会去重启、去换 DNS，怎么弄都不好。
# 判据只用这两句原文：币安 451 的正文里有它们，别的错误里没有。
RESTRICTED_MARKERS: tuple[str, ...] = (
    "restricted location",
    "unavailable for legal reasons",
)


def looks_restricted(exc: BaseException) -> bool:
    """这个错误是不是「交易所不服务这台机器的 IP」（HTTP 451）。"""
    text = str(exc).lower()
    return any(marker in text for marker in RESTRICTED_MARKERS)


def _as_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


# 买卖一的来源。币安的合约 24 小时 ticker 里根本没有买一卖一，只有 bookTicker
# 端点给这两档（权重 2，不用拉整档盘口）。
_BOOK_TICKER: dict[MarketType, str] = {
    MarketType.SPOT: "publicGetTickerBookTicker",
    MarketType.USDM: "fapiPublicGetTickerBookTicker",
    MarketType.COINM: "dapiPublicGetTickerBookTicker",
}


def ticker_bid_ask(
    ticker: dict, book_bid: float = 0.0, book_ask: float = 0.0
) -> tuple[float, float]:
    """(bid, ask) out of a ccxt ticker, topped up from the book when empty.

    Binance's futures ticker carries no bid/ask at all (ccxt leaves them
    None, which used to be flattened to 0.0 and shown as an empty spread);
    spot's does. 0.0 means "the venue did not tell us" — callers turn that
    into a dash, never into an invented price.
    """
    bid = _as_float(ticker.get("bid"))
    ask = _as_float(ticker.get("ask"))
    if not bid or not ask:
        info = ticker.get("info") or {}
        bid = bid or _as_float(info.get("bidPrice"))
        ask = ask or _as_float(info.get("askPrice"))
    return bid or book_bid, ask or book_ask


def _quite_float(value: Any) -> float | None:
    """float or None — 缺字段是 None，真的 0 就是 0.0，两者不能混。"""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ticker_last(tick: dict) -> float | None:
    """最新价：unified last → 原始 lastPrice → close。"""
    info = tick.get("info") or {}
    for value in (tick.get("last"), info.get("lastPrice"), info.get("close"), tick.get("close")):
        number = _quite_float(value)
        if number is not None:
            return number
    return None


def _ticker_change_pct(tick: dict) -> float | None:
    """24 小时涨跌幅，百分数：unified percentage → priceChangePercent → priceChange 折算。

    实测三个市场的原始 payload 里都有 ``priceChangePercent``；``priceChange`` 是**绝对**
    变化（现货 -1083.41、币本位 -1174.5），直接当百分比用会差两个数量级，必须按
    ``openPrice`` 折算；连开盘价都没有就判不出来，回 None 而不是编一个。
    """
    info = tick.get("info") or {}
    for value in (tick.get("percentage"), info.get("priceChangePercent")):
        number = _quite_float(value)
        if number is not None:
            return number
    change = _quite_float(info.get("priceChange"))
    open_price = _quite_float(info.get("openPrice"))
    if change is not None and open_price:
        return change / open_price * 100.0
    return None


def _ticker_quote_volume(tick: dict) -> float | None:
    """24 小时成交额（计价币）：原始 quoteVolume 优先，其次 ccxt 归一值。

    交易所自己公布的数才是网页上那个数；ccxt 对合约有时按 volume×last 重算，
    实测 U 本位差五万（12598069692.64 对 12598018471.89），别拿估算值冒充。
    """
    info = tick.get("info") or {}
    for value in (info.get("quoteVolume"), tick.get("quoteVolume")):
        number = _quite_float(value)
        if number is not None:
            return number
    return None


# Binance publishes read-only spot market data on its own host. Unlike
# api.binance.com it is reachable from networks where the trading host refuses
# connections, which is what lets a charting client read real candles without
# sending a single order to the exchange. Candles, tickers and the book are
# the same feed the trading host serves.
PUBLIC_SPOT_HOST = "https://data-api.binance.vision/api/v3"
# Futures has no separate read-only mirror, but fapi/dapi themselves answer on
# networks where api.binance.com does not. A charting client must read these,
# otherwise a testnet trading session shows simulated futures prices that are
# hundreds of dollars away from the venue the user compares against.
PUBLIC_FUTURES_HOSTS: dict[MarketType, dict[str, str]] = {
    MarketType.USDM: {
        "fapiPublic": "https://fapi.binance.com/fapi/v1",
        "fapiPublicV2": "https://fapi.binance.com/fapi/v2",
        "fapiPublicV3": "https://fapi.binance.com/fapi/v3",
    },
    MarketType.COINM: {
        "dapiPublic": "https://dapi.binance.com/dapi/v1",
    },
}

# 交易所清单。只有币安接了完整链路（下单、风控、模拟盘），其余先占位：
# 界面上能看到，但选不了，避免「切过去了却下不了单」这种半成品状态。
PLATFORMS: list[dict[str, Any]] = [
    {"key": "binance", "label": "币安", "enabled": True},
    {"key": "okx", "label": "OKX", "enabled": False},
    {"key": "bybit", "label": "Bybit", "enabled": False},
]
SUPPORTED_PLATFORMS = {item["key"]: item["label"] for item in PLATFORMS if item["enabled"]}
DEFAULT_PLATFORM = "binance"


CLOCK_DRIFT_MARKERS = (
    "-1021",
    "ahead of the server's time",
    "Timestamp for this request",
)


def _is_clock_drift(exc: BaseException) -> bool:
    """这条错误是不是「本机时钟和交易所差太多」造成的。"""
    text = str(exc)
    return any(marker in text for marker in CLOCK_DRIFT_MARKERS)

class BinanceREST:
    """One ccxt client per market type, created lazily.

    ``market_data=True`` builds clients for charting only: spot is pointed at
    Binance's public read-only host and no credentials are used. Orders and
    balances always go through a client built without that flag, so the
    trading network and the charting source can differ.
    """

    def __init__(
        self,
        network: Network,
        credentials: Credentials | None = None,
        *,
        market_data: bool = False,
        proxy: str = "",
        platform: str = DEFAULT_PLATFORM,
    ) -> None:
        self.platform = platform if platform in SUPPORTED_PLATFORMS else DEFAULT_PLATFORM
        if platform != self.platform:
            log.warning("暂不支持 %s 平台，改用币安", platform)
        self.network = network
        self.credentials = credentials or Credentials()
        self.market_data = market_data
        # Optional HTTP(S) proxy for networks where Binance is blocked or its
        # DNS is poisoned (fapi/dapi resolve to unrelated hosts there).
        self.proxy = (proxy or "").strip()
        self._clients: dict[MarketType, ccxt.Exchange] = {}
        self._specs: dict[tuple[MarketType, str], ContractSpec] = {}
        self._lock = threading.RLock()

    # --------------------------------------------------------------- clients
    def client(self, market: MarketType) -> ccxt.Exchange:
        with self._lock:
            client = self._clients.get(market)
            if client is not None:
                return client

            klass = getattr(ccxt, market.ccxt_id)
            params: dict[str, Any] = {
                "enableRateLimit": True,
                "timeout": 20_000,
                "options": {
                    "defaultType": "swap" if market.is_futures else "spot",
                    # The 委托 tab and the protection column need every open
                    # order, not just one pair's. Acknowledged here so ccxt
                    # stops raising; the account cache keeps the cost low.
                    "fetchOpenOrders": {"warnWithoutSymbol": False},
                },
            }
            client = klass(params)
            if self.proxy:
                # ccxt's sync clients use requests, which reads this attribute.
                # Applied before the first request so no traffic leaks direct.
                client.proxies = {"http": self.proxy, "https": self.proxy}
            if self.network is Network.TESTNET:
                self._apply_test_environment(client, market)
            if self.market_data:
                self._prefer_public_data(client, market)
            if self.credentials.is_set:
                client.apiKey = self.credentials.api_key
                client.secret = self.credentials.api_secret
            self._install_clock_guard(client)
            self._clients[market] = client
            return client

    @staticmethod
    def _install_clock_guard(client: ccxt.Exchange) -> None:
        """签名请求遇到「本机时钟偏了」就校准一次再重试。

        币安对时间戳的容忍度很小：实测本机快了 1.3 秒，所有签名请求都被
        -1021 Timestamp for this request was 1000ms ahead of the server 拒掉。
        界面上看到的是「无法读取当前持仓，为避免重复开仓已拒绝本次下单」，
        用户完全不知道该怎么办。这个版本的 ccxt python 客户端没有
        adjustForTimeDifference，所以在这里兜一次：只拦这一种错误，校准完
        重试一次，其它错误照旧往上抛。
        """
        original = getattr(client, "fetch", None)
        if not callable(original) or getattr(client, "_btrader_clock_guard", False):
            return

        def fetch_with_clock_retry(*args: Any, **kwargs: Any):
            try:
                return original(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - 只处理时钟这一类，其余原样抛
                if not _is_clock_drift(exc):
                    raise
                try:
                    offset = client.load_time_difference()
                except Exception:
                    raise exc from None
                log.warning("本机时钟与交易所差 %s 毫秒，校准后重试一次", offset)
                return original(*args, **kwargs)

        try:
            client.fetch = fetch_with_clock_retry
            client._btrader_clock_guard = True
        except AttributeError:  # 只读的替身对象，跳过
            log.debug("客户端不接受挂载校时守卫，跳过")

    @staticmethod
    def _apply_test_environment(client: ccxt.Exchange, market: MarketType) -> None:
        """Point a client at the exchange test environment.

        Binance retired the futures testnet: ccxt raises "testnet/sandbox mode
        is not supported for futures anymore" for binanceusdm and binancecoinm,
        which surfaced as "连接测试失败" whatever the key was. Futures keys now
        go to demo trading (demo-fapi / demo-dapi); spot keeps its testnet.
        """
        if market.is_futures:
            enable = getattr(client, "enable_demo_trading", None)
            if enable is None:
                log.warning("%s 不支持合约模拟盘，ccxt 版本过旧", market.ccxt_id)
                return
            try:
                enable(True)
            except Exception as exc:
                log.warning("无法启用 %s 模拟盘: %s", market.ccxt_id, exc)
            return
        if getattr(client, "has", {}).get("sandbox", True):
            try:
                client.set_sandbox_mode(True)
            except Exception as exc:
                log.warning("无法启用 %s 测试网: %s", market.ccxt_id, exc)

    def reset(self) -> None:
        with self._lock:
            self._clients.clear()
            self._specs.clear()

    @staticmethod
    def _prefer_public_data(client: ccxt.Exchange, market: MarketType) -> None:
        """Point a charting client at real market data, never at a sandbox.

        Spot goes to Binance's public mirror. Futures has no mirror, but its
        public endpoints are served by fapi/dapi directly, so the sandbox
        rewrite from ``set_sandbox_mode`` is undone for them. Only public keys
        are touched: a charting client must stay unable to sign a request.
        """
        urls = client.urls.get("api")
        if not isinstance(urls, dict):
            return
        if market is MarketType.SPOT:
            urls["public"] = PUBLIC_SPOT_HOST
            # ccxt asks "private" for some public endpoints; keeping it on the
            # public host guarantees a charting client can never send a signed
            # request even if a caller slips through.
            urls["private"] = PUBLIC_SPOT_HOST
            return
        for key, host in PUBLIC_FUTURES_HOSTS.get(market, {}).items():
            if key in urls:
                urls[key] = host

    def contract_spec(
        self, market: MarketType, symbol: str, allow_network: bool = True
    ) -> ContractSpec:
        """Contract size / minimum amount for a symbol, cached after first use.

        ``allow_network=False`` only reads the cache, so a preview never blocks on
        a network round trip.
        """
        key = (market, symbol)
        with self._lock:
            cached = self._specs.get(key)
            if cached is not None:
                return cached
        if not allow_network:
            return DEFAULT_SPEC
        spec = DEFAULT_SPEC
        resolved = False
        try:
            client = self.client(market)
            client.load_markets()
            raw = client.market(symbol) or {}
            limits = raw.get("limits") or {}
            amount_limit = limits.get("amount") or {}
            cost_limit = limits.get("cost") or {}
            spec = ContractSpec(
                contract_size=float(raw.get("contractSize") or 1.0),
                inverse=bool(raw.get("inverse", False)),
                base=raw.get("base") or "",
                quote=raw.get("quote") or "",
                settle=raw.get("settle") or "",
                min_amount=float(amount_limit.get("min") or 0.0),
                min_cost=float(cost_limit.get("min") or 0.0),
                amount_step=_amount_step(raw),
            )
            resolved = True
        except Exception as exc:
            log.debug("无法读取 %s 的合约规格: %s", symbol, exc)
        # Only cache a real answer: a transient failure must not pin the
        # fallback spec (1 contract = 1 coin, no minimum) for the session.
        if resolved:
            with self._lock:
                self._specs[key] = spec
        return spec

    @property
    def has_credentials(self) -> bool:
        return self.credentials.is_set

    @staticmethod
    def rate_limit_sleep() -> float:
        return 0.12

    # ----------------------------------------------------------- public data
    def markets(self, market: MarketType) -> dict[str, dict]:
        try:
            return self.client(market).load_markets()
        except Exception as exc:
            raise ExchangeError(_message(exc)) from exc

    def fetch_ohlcv(
        self,
        market: MarketType,
        symbol: str,
        timeframe: str = "15m",
        since: int | None = None,
        limit: int = 500,
    ) -> list[list[float]]:
        try:
            return self.client(market).fetch_ohlcv(symbol, timeframe, since=since, limit=limit)
        except Exception as exc:
            raise ExchangeError(_message(exc)) from exc

    def fetch_funding_rate_history(
        self,
        market: MarketType,
        symbol: str,
        since: int | None = None,
        limit: int = 1000,
    ) -> list[dict]:
        """Settled funding rates as ccxt rows (``timestamp`` ms, decimal rate).

        Only contracts have them: spot answers ``[]`` without a request. The
        callers are backtests, so nothing here raises — an unsupported venue
        (testnet), a timeout or an empty answer all come back as ``[]`` and the
        run continues without funding instead of dying on it.
        """
        if not market.is_futures or not self.has_credentials:
            return []
        try:
            rows = self.client(market).fetch_funding_rate_history(
                symbol, since=since, limit=limit
            )
        except Exception as exc:
            log.debug("读取资金费率失败 %s %s: %s", market.value, symbol, exc)
            return []
        return list(rows or [])

    def fetch_tickers(self, market: MarketType, symbols: list[str] | None = None) -> dict[str, dict]:
        try:
            return self.client(market).fetch_tickers(symbols)
        except Exception as exc:
            raise ExchangeError(_message(exc)) from exc

    def fetch_ticker(self, market: MarketType, symbol: str) -> dict:
        try:
            return self.client(market).fetch_ticker(symbol)
        except Exception as exc:
            raise ExchangeError(_message(exc)) from exc

    def ticker_snapshots(self, market: MarketType, symbols: list[str]) -> list[dict]:
        """24 小时快照，一次问一批：``symbol`` / ``last`` / ``change_pct`` / ``quote_volume``.

    公开行情，不需要密钥。三个值都可能是 None（交易所没给），**不是 0** —— "恰好
    不涨不跌"和"读不到"是两件事，界面据此显示「—」。空列表直接回空，不去拉整个
    市场（现货那是 1400 多项）。拿不到就抛 ExchangeError，文案是中文。
        """
        wanted = [str(symbol) for symbol in (symbols or []) if str(symbol).strip()]
        if not wanted:
            return []
        try:
            raw = self.client(market).fetch_tickers(wanted)
        except Exception as exc:
            raise ExchangeError(_message(exc)) from exc
        rows: list[dict] = []
        for symbol in wanted:
            tick = raw.get(symbol) if isinstance(raw, dict) else None
            if not isinstance(tick, dict):
                # 交易所没回这一对：跳过，不补一行全 0 的占位。
                continue
            rows.append(
                {
                    "symbol": symbol,
                    "last": _ticker_last(tick),
                    "change_pct": _ticker_change_pct(tick),
                    "quote_volume": _ticker_quote_volume(tick),
                }
            )
        return rows

    def fetch_bid_ask(self, market: MarketType, symbol: str) -> tuple[float, float]:
        """Top of book (bid, ask) without pulling the depth ladder.

        Public data, no credentials needed: the charting client reads it for
        the spread next to 最新价, and the venue's ticker has no such field.
        """
        client = self.client(market)
        name = _BOOK_TICKER[market]
        call = getattr(client, name, None)
        if call is None:
            log.debug("%s 客户端没有 %s，买卖一读不到", market.ccxt_id, name)
            return 0.0, 0.0
        try:
            client.load_markets()
        except Exception:  # pragma: no cover - symbol mapping only
            log.debug("加载市场失败，买卖一按原样交易对请求", exc_info=True)
        venue_symbol = self._venue_symbol(client, symbol)
        try:
            payload = call({"symbol": venue_symbol})
        except Exception as exc:
            raise ExchangeError(_message(exc)) from exc
        row = payload
        if isinstance(payload, list):
            # 币本位带 symbol 也返回数组。
            row = next(
                (item for item in payload if str(item.get("symbol")) == venue_symbol),
                payload[0] if payload else {},
            )
        if not isinstance(row, dict):
            return 0.0, 0.0
        return _as_float(row.get("bidPrice")), _as_float(row.get("askPrice"))

    @classmethod
    def _venue_symbol(cls, client: ccxt.Exchange, symbol: str) -> str:
        """ccxt symbol -> venue id, with a plain-text fallback for odd clients."""
        base, quote, _settle = split_symbol(symbol)
        return cls._market_id(client, symbol) or f"{base}{quote}"

    def fetch_order_book(self, market: MarketType, symbol: str, limit: int = 20) -> dict:
        # 先换成交易所认的那一档再发：ccxt 不会替我们凑，币安对 12 这种数直接回
        # -4021，合约盘口就整条 503。
        try:
            return self.client(market).fetch_order_book(symbol, snap_depth(limit))
        except Exception as exc:
            raise ExchangeError(_message(exc)) from exc

    # ---------------------------------------------------------- private data
    def fetch_balance(self, market: MarketType) -> dict:
        self._require_auth()
        try:
            return self.client(market).fetch_balance()
        except Exception as exc:
            raise ExchangeError(_message(exc)) from exc

    def fetch_positions(self, market: MarketType, symbols: list[str] | None = None) -> list[dict]:
        self._require_auth()
        client = self.client(market)
        if not client.has.get("fetchPositions"):
            return []
        try:
            return client.fetch_positions(symbols)
        except Exception as exc:
            raise ExchangeError(_message(exc)) from exc

    def fetch_open_orders(self, market: MarketType, symbol: str | None = None) -> list[dict]:
        self._require_auth()
        try:
            return self.client(market).fetch_open_orders(symbol)
        except Exception as exc:
            raise ExchangeError(_message(exc)) from exc

    def fetch_my_trades(self, market: MarketType, symbol: str, limit: int = 50) -> list[dict]:
        self._require_auth()
        try:
            return self.client(market).fetch_my_trades(symbol, limit=limit)
        except Exception as exc:
            raise ExchangeError(_message(exc)) from exc

    def create_order(
        self,
        market: MarketType,
        symbol: str,
        order_type: str,
        side: str,
        amount: float,
        price: float | None = None,
        params: dict | None = None,
    ) -> dict:
        self._require_auth()
        try:
            return self.client(market).create_order(symbol, order_type, side, amount, price, params or {})
        except Exception as exc:
            raise ExchangeError(_message(exc)) from exc

    def cancel_order(self, market: MarketType, order_id: str, symbol: str) -> dict:
        """Cancel one resting order, whichever endpoint holds it.

        A conditional order lives on the algo endpoint, so the ordinary
        cancel answers -2011 Unknown order for a perfectly live stop. The
        fallback is what makes a stop reachable from the order list.
        """
        self._require_auth()
        client = self.client(market)
        try:
            return client.cancel_order(order_id, symbol)
        except Exception as exc:
            if not market.is_futures or not _looks_unknown_order(exc):
                raise ExchangeError(_message(exc)) from exc
            row = self._find_algo(market, symbol, order_id)
            try:
                result = self.cancel_algo_order(market, order_id)
            except ExchangeError:
                # 两条链路都没有这张单：报原来那句，路由会翻成「没有这个挂单」。
                raise ExchangeError(_message(exc)) from exc
            order = dict(row or {})
            order.update(
                {"id": str(order_id), "symbol": order.get("symbol") or symbol, "status": "canceled"}
            )
            if not order.get("info"):
                order["info"] = result
            return order

    def _find_algo(self, market: MarketType, symbol: str, order_id: str) -> dict | None:
        try:
            for order in self.fetch_algo_orders(market, symbol):
                if str(order.get("id")) == str(order_id):
                    return order
        except ExchangeError:
            log.debug("读取算法单失败，撤单只用算法单号", exc_info=True)
        return None

    # 改杠杆只重试一次，而且只在明显的抖动（网络 / 限频）上重试：
    # 一次抖动就让整张合约单 400，等于把用户挡在门外。
    LEVERAGE_RETRIES = 1
    LEVERAGE_RETRY_DELAY = 0.3

    def set_leverage(self, symbol: str, leverage: float, market: MarketType) -> dict:
        self._require_auth()
        client = self.client(market)
        if not client.has.get("setLeverage"):
            # 以前这里安静地 return {}：面板写着 10 倍，交易所什么都没收到。
            raise ExchangeError(
                "这家交易所的客户端不支持设置杠杆，没法按指定倍数开仓。"
            )
        attempt = 0
        while True:
            try:
                return client.set_leverage(leverage, symbol)
            except (ccxt.NetworkError, ccxt.RateLimitExceeded) as exc:
                attempt += 1
                if attempt > self.LEVERAGE_RETRIES:
                    raise ExchangeError(_message(exc)) from exc
                log.debug("设置杠杆失败，重试一次：%s", exc)
                time.sleep(self.LEVERAGE_RETRY_DELAY)
            except Exception as exc:
                raise ExchangeError(_message(exc)) from exc

    # ------------------------------------------------------- algo orders
    def fetch_algo_orders(
        self, market: MarketType, symbol: str | None = None
    ) -> list[dict]:
        """Open conditional orders, normalized into ccxt's order shape.

        They are the stops, targets and trigger entries the venue holds for
        us; without this call the app cannot see (or cancel) a single guard
        it armed itself.
        """
        self._require_auth()
        if not market.is_futures:
            # 现货没有交易所侧触发单，也就没有算法单可读。
            return []
        client = self.client(market)
        fetch_name = _ALGO_ENDPOINTS[market][0]
        fetch = getattr(client, fetch_name, None)
        if fetch is None:
            log.debug("%s 客户端没有 %s，算法单不可读", market.ccxt_id, fetch_name)
            return []
        try:
            client.load_markets()
        except Exception:  # pragma: no cover - symbol mapping only
            log.debug("加载市场失败，算法单交易对按交易所原文返回", exc_info=True)
        params: dict[str, Any] = {}
        if symbol:
            venue_symbol = self._market_id(client, symbol)
            if venue_symbol:
                params["symbol"] = venue_symbol
        try:
            payload = fetch(params) or {}
        except Exception as exc:
            raise ExchangeError(_message(exc)) from exc
        rows = payload.get("orders") if isinstance(payload, dict) else payload
        out: list[dict] = []
        for row in rows or []:
            order = self._algo_order(client, row, market)
            if order is not None:
                out.append(order)
        return out

    def cancel_algo_order(self, market: MarketType, order_id: str) -> dict:
        """Delete one conditional order on the algo endpoint."""
        self._require_auth()
        if not market.is_futures:
            raise ExchangeError("现货没有交易所侧条件单，无需撤销。")
        client = self.client(market)
        delete_name = _ALGO_ENDPOINTS[market][1]
        delete = getattr(client, delete_name, None)
        if delete is None:
            raise ExchangeError(f"{market.ccxt_id} 客户端不支持撤销算法单，请升级 ccxt。")
        try:
            return delete({"algoId": str(order_id)}) or {}
        except Exception as exc:
            raise ExchangeError(_message(exc)) from exc

    def _algo_order(
        self, client: ccxt.Exchange, row: dict, market: MarketType
    ) -> dict | None:
        """One algo row in the same shape as a ccxt order dict."""
        algo_id = row.get("algoId") or row.get("algoID") or row.get("id")
        if not algo_id:
            return None
        kind = str(row.get("orderType") or row.get("algoType") or "conditional")
        return {
            "id": str(algo_id),
            "clientOrderId": row.get("clientAlgoId") or "",
            "symbol": self._unified_symbol(client, row.get("symbol"), market.is_futures),
            "side": str(row.get("side") or "buy").lower(),
            # STOP_MARKET / TAKE_PROFIT_MARKET 转成小写和 ccxt 对齐，
            # _protections 与 PROTECTIVE_TYPES 认的就是这种写法。
            "type": kind.lower(),
            "amount": _as_float(row.get("quantity")),
            "price": _as_float(row.get("price")),
            "triggerPrice": _as_float(row.get("triggerPrice") or row.get("stopPrice")),
            "reduceOnly": bool(row.get("reduceOnly") or row.get("closePosition")),
            "filled": 0.0,
            "status": "open",
            "timestamp": int(row.get("createTime") or 0),
            # 撤销时按这个标记换端点，普通挂单走 client.cancel_order。
            "algo": True,
            "info": dict(row),
        }

    @staticmethod
    def _market_id(client: ccxt.Exchange, symbol: str) -> str:
        """ccxt symbol -> venue id ("BTC/USDT:USDT" -> "BTCUSDT")."""
        try:
            return str((client.market(symbol) or {}).get("id") or "")
        except Exception:
            log.debug("找不到 %s 的交易所代码", symbol, exc_info=True)
            return ""

    @staticmethod
    def _unified_symbol(client: ccxt.Exchange, raw: Any, futures: bool = True) -> str:
        """venue id -> ccxt symbol, so algo rows line up with open orders."""
        text = str(raw or "")
        if not text:
            return ""
        fallback = ""
        markets = getattr(client, "markets", None)
        if isinstance(markets, dict):
            for market in markets.values():
                if str(market.get("id") or "") != text:
                    continue
                symbol = str(market.get("symbol") or text)
                # 合约客户端里同一个 id 也挂着现货市场（BTCUSDT），触发单一定属于
                # 合约那一份。挑错的话保护单会和持仓对不上号。
                contract = bool(
                    market.get("contract") or market.get("swap") or market.get("inverse")
                )
                if contract == futures:
                    return symbol
                fallback = fallback or symbol
        return fallback or text

    # ---------------------------------------------------- protection orders
    def create_stop_market(
        self,
        market: MarketType,
        symbol: str,
        side: str,
        amount: float,
        stop_price: float,
    ) -> dict:
        """Place a venue-side ``STOP_MARKET`` exit.

        The exchange keeps this order, so it fires even when the app is closed
        — which is the whole point of a protective stop. ``stopLossPrice`` is
        the ccxt field that maps onto ``STOP_MARKET`` for contracts; a bare
        ``triggerPrice`` would leave the type at ``MARKET`` on the algo
        endpoint instead.
        """
        return self._create_conditional(
            market, symbol, side, amount, stop_price, take_profit=False
        )

    def create_take_profit_market(
        self,
        market: MarketType,
        symbol: str,
        side: str,
        amount: float,
        target_price: float,
    ) -> dict:
        """Place a venue-side ``TAKE_PROFIT_MARKET`` exit.

        ccxt maps ``takeProfitPrice`` onto ``TAKE_PROFIT_MARKET`` for contracts
        and requires it to be the only trigger field: supplying ``stopPrice``
        as well makes the request a stop-loss order instead.
        """
        return self._create_conditional(
            market, symbol, side, amount, target_price, take_profit=True
        )

    def create_conditional(
        self,
        market: MarketType,
        symbol: str,
        side: str,
        amount: float,
        trigger_price: float,
        *,
        take_profit: bool,
        reduce_only: bool = False,
    ) -> dict:
        """Arm a standalone trigger order: it only reaches the market once
        the price touches the trigger.

        ``reduce_only=False`` is an entry, ``True`` an exit. The protective
        wrappers above always pass ``True``, because a stop that cannot close
        is worse than no stop at all.
        """
        return self._create_conditional(
            market,
            symbol,
            side,
            amount,
            trigger_price,
            take_profit=take_profit,
            reduce_only=reduce_only,
        )

    def _create_conditional(
        self,
        market: MarketType,
        symbol: str,
        side: str,
        amount: float,
        trigger_price: float,
        *,
        take_profit: bool,
        reduce_only: bool = True,
    ) -> dict:
        self._require_auth()
        if not market.is_futures:
            raise ExchangeError("现货暂不支持交易所侧条件单，请使用合约或手动盯盘。")
        price = self.price_to_precision(market, symbol, trigger_price)
        params: dict = {
            "reduceOnly": reduce_only,
            "takeProfitPrice" if take_profit else "stopLossPrice": price,
        }
        try:
            return self.client(market).create_order(
                symbol, "market", side, amount, None, params
            )
        except Exception as exc:
            raise ExchangeError(_message(exc)) from exc

    def cancel_protective_orders(self, market: MarketType, symbol: str) -> int:
        """Cancel just the stop / take-profit orders on a symbol.

        Re-arming a guard must replace the old one, but blanket-cancelling
        everything would also kill a resting limit entry the user still wants.
        Both chains are read: the venue keeps triggers on the algo endpoint,
        and a guard that is not in ``fetch_open_orders`` is exactly the one
        that used to survive every re-arm.
        """
        self._require_auth()
        client = self.client(market)
        try:
            resting = list(client.fetch_open_orders(symbol) or [])
        except Exception as exc:
            raise ExchangeError(_message(exc)) from exc
        # 算法单读不到就别假装撤完了：留下旧止损比报错更危险。
        resting.extend(self.fetch_algo_orders(market, symbol))
        canceled = 0
        for order in resting:
            kind = str(order.get("type") or "").lower()
            if kind not in PROTECTIVE_TYPES:
                continue
            if self._cancel_resting(client, market, order, symbol):
                canceled += 1
        return canceled

    def cancel_symbol_orders(self, market: MarketType, symbol: str) -> int:
        """Cancel every resting order on a symbol, returning how many went.

        Used before a close: a leftover ``reduceOnly`` stop would otherwise be
        re-triggered by the exit and flip the position instead of flattening
        it — and that stop lives on the algo endpoint, so both chains are
        cleared here. Individual failures are logged and skipped so one stale
        order cannot block the close itself.
        """
        self._require_auth()
        client = self.client(market)
        try:
            resting = list(client.fetch_open_orders(symbol) or [])
        except Exception as exc:
            raise ExchangeError(_message(exc)) from exc
        resting.extend(self.fetch_algo_orders(market, symbol))
        canceled = 0
        for order in resting:
            if self._cancel_resting(client, market, order, symbol):
                canceled += 1
        return canceled

    def _cancel_resting(
        self, client: ccxt.Exchange, market: MarketType, order: dict, symbol: str
    ) -> bool:
        """Cancel one resting order, algo or plain; False when it is already gone."""
        order_id = order.get("id")
        try:
            if order.get("algo"):
                self.cancel_algo_order(market, str(order_id))
            else:
                client.cancel_order(order_id, symbol, order.get("params") or {})
            return True
        except Exception:  # already filled or canceled by the venue
            log.debug("撤销挂单失败 %s", order_id, exc_info=True)
            return False

    # ---------------------------------------------------------------- helpers
    def _require_auth(self) -> None:
        if not self.credentials.is_set:
            raise ExchangeError("尚未配置 API Key，请先在设置中填写。")

    def amount_to_precision(self, market_type: MarketType, symbol: str, amount: float) -> float:
        client = self.client(market_type)
        try:
            client.load_markets()
            return float(client.amount_to_precision(symbol, amount))
        except Exception as exc:
            # ccxt rejects amounts below the market minimum. Return 0 so the
            # caller refuses the order instead of sending an unrounded (and
            # potentially far too large) quantity to the exchange.
            log.debug("数量精度处理失败 %s %s: %s", symbol, amount, exc)
            return 0.0

    def price_to_precision(self, market_type: MarketType, symbol: str, price: float) -> float:
        client = self.client(market_type)
        try:
            client.load_markets()
            return float(client.price_to_precision(symbol, price))
        except Exception:
            return float(price)


# Binance returns its own numeric codes. ccxt maps some of them to a generic
# exception, and the text alone ("Invalid API-key, IP, or permissions for
# action") does not tell a user which of the three is wrong. Translate the
# codes that matter for account access into an actionable sentence.
_BINANCE_CODES: dict[str, str] = {
    "-2008": "API Key 无效（可能复制时多了空格，或该 Key 已被删除）。",
    "-2014": "API Key 格式不正确（位数不对，或混入了其它字符）。",
    "-2015": (
        "API Key 无效、IP 未加入白名单，或该 Key 没有这个市场的权限。"
        "到币安「API 管理」核对两件事。一是该 Key 有没有勾选当前市场的读取权限。"
        "二是设了 IP 白名单的话，当前公网 IP 在不在里面。"
    ),
    "-2019": "保证金不足。",
    "-2021": "订单会被立即触发，已被拒绝。",
    "-1013": "下单数量或金额不满足交易所的最小单位 / 步进要求。",
    "-1021": "本机时间与服务器相差过大，请校准系统时间后重试。",
    "-1022": "请求签名不匹配，通常是 API Secret 填错。",
    "-1100": "请求参数有误（交易所拒绝了该参数组合）。",
    "-1121": "下单价格超出交易所允许的偏离范围。",
    "-4131": "价格偏离市场太多，被交易所的价格保护规则拒绝。",
    "-4164": "下单金额低于交易所的最小名义价值（通常 5 USDT）。",
}

# ccxt 抛的英文弃用公告。用户读到的第一句必须是中文，并且指向真正的下一步：
# 合约测试网已停用，要换模拟盘（Demo）的 Key，而不是他填错了什么。
_CCXT_DEPRECATION = "testnet/sandbox mode is not supported for futures"
_DEMO_HINT = (
    "币安已停用合约测试网。"
    "到币安合约模拟盘（Demo Trading）申请一对新 Key，"
    "凭证区的网络选「模拟盘」再填。现货测试网的 Key 不能用在合约上。"
)


def _binance_code(exc: Exception) -> str | None:
    text = str(exc)
    marker = '"code":'
    index = text.find(marker)
    if index < 0:
        marker = "'code':"
        index = text.find(marker)
    if index < 0:
        return None
    start = index + len(marker)
    while start < len(text) and text[start].isspace():
        start += 1
    end = start
    if end < len(text) and text[end] == "-":
        end += 1
    while end < len(text) and text[end].isdigit():
        end += 1
    code = text[start:end].strip()
    return code or None


def _message(exc: Exception) -> str:
    text = str(exc) or exc.__class__.__name__
    if _CCXT_DEPRECATION in text:
        return _DEMO_HINT
    if looks_restricted(exc):
        return (
            "交易所按出口 IP 拒绝了这台机器（HTTP 451，地域限制）：它不服务这个 IP 所在的地区，"
            "跟 API Key、DNS、网络抖动都无关，重启也不会好。两条路：一是把服务放到它允许的地区；"
            "二是在设置页「网络代理」填一个能出去的 HTTP 代理（形如 http://127.0.0.1:7890），"
            "保存后会重建交易所客户端、立刻按代理走。（HTTP 451）"
        )
    code = _binance_code(exc)
    hint = _BINANCE_CODES.get(code or "")
    if hint:
        return f"{hint}（{text}）"
    if isinstance(exc, ccxt.PermissionDenied):
        return f"权限不足：{text}"
    if isinstance(exc, ccxt.AuthenticationError):
        return f"认证失败，请检查 API Key 与权限：{text}"
    if isinstance(exc, ccxt.InsufficientFunds):
        return f"余额不足：{text}"
    if isinstance(exc, ccxt.InvalidOrder):
        return f"订单参数无效：{text}"
    if isinstance(exc, ccxt.RateLimitExceeded):
        return f"触发限频，请稍后再试：{text}"
    if isinstance(exc, ccxt.NetworkError):
        return f"网络错误：{text}"
    return text
