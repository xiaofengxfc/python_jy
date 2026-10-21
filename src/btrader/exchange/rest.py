"""Thin synchronous REST wrapper around ccxt for Binance."""

from __future__ import annotations

import logging
import threading
from typing import Any

import ccxt

from ..core.contracts import DEFAULT_SPEC, ContractSpec
from ..core.credentials import Credentials
from ..core.types import MarketType, Network

log = logging.getLogger(__name__)


class ExchangeError(RuntimeError):
    """Raised for normalized, user-facing exchange errors."""


# Binance publishes read-only spot market data on its own host. Unlike
# api.binance.com it is reachable from networks where the trading host refuses
# connections, which is what lets a paper account chart real candles without
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
    ) -> None:
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
                "options": {"defaultType": "swap" if market.is_futures else "spot"},
            }
            client = klass(params)
            if self.proxy:
                # ccxt's sync clients use requests, which reads this attribute.
                # Applied before the first request so no traffic leaks direct.
                client.proxies = {"http": self.proxy, "https": self.proxy}
            if self.network is Network.TESTNET:
                # ccxt exposes sandbox mode per exchange.
                if getattr(client, "has", {}).get("sandbox", True):
                    try:
                        client.set_sandbox_mode(True)
                    except Exception as exc:
                        log.warning("无法启用 %s 测试网: %s", market.ccxt_id, exc)
            if self.market_data:
                self._prefer_public_data(client, market)
            if self.credentials.is_set:
                client.apiKey = self.credentials.api_key
                client.secret = self.credentials.api_secret
            self._clients[market] = client
            return client

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

        ``allow_network=False`` only reads the cache. Paper trading uses that so
        an order never blocks on a network round trip, and so an offline session
        can still place simulated orders.
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
            limits = (raw.get("limits") or {}).get("amount") or {}
            spec = ContractSpec(
                contract_size=float(raw.get("contractSize") or 1.0),
                inverse=bool(raw.get("inverse", False)),
                base=raw.get("base") or "",
                quote=raw.get("quote") or "",
                settle=raw.get("settle") or "",
                min_amount=float(limits.get("min") or 0.0),
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

    def fetch_order_book(self, market: MarketType, symbol: str, limit: int = 20) -> dict:
        try:
            return self.client(market).fetch_order_book(symbol, limit)
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
        self._require_auth()
        try:
            return self.client(market).cancel_order(order_id, symbol)
        except Exception as exc:
            raise ExchangeError(_message(exc)) from exc

    def set_leverage(self, symbol: str, leverage: float, market: MarketType) -> dict:
        self._require_auth()
        client = self.client(market)
        if not client.has.get("setLeverage"):
            return {}
        try:
            return client.set_leverage(leverage, symbol)
        except Exception as exc:
            raise ExchangeError(_message(exc)) from exc

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

    def _create_conditional(
        self,
        market: MarketType,
        symbol: str,
        side: str,
        amount: float,
        trigger_price: float,
        *,
        take_profit: bool,
    ) -> dict:
        self._require_auth()
        if not market.is_futures:
            raise ExchangeError("现货暂不支持交易所侧条件单，请使用合约或手动盯盘。")
        price = self.price_to_precision(market, symbol, trigger_price)
        params: dict = {
            "reduceOnly": True,
            "takeProfitPrice" if take_profit else "stopLossPrice": price,
        }
        try:
            return self.client(market).create_order(
                symbol, "market", side, amount, None, params
            )
        except Exception as exc:
            raise ExchangeError(_message(exc)) from exc

    def cancel_symbol_orders(self, market: MarketType, symbol: str) -> int:
        """Cancel every resting order on a symbol, returning how many went.

        Used before a close: a leftover ``reduceOnly`` stop would otherwise be
        re-triggered by the exit and flip the position instead of flattening
        it. Individual failures are logged and skipped so one stale order
        cannot block the close itself.
        """
        self._require_auth()
        client = self.client(market)
        try:
            resting = client.fetch_open_orders(symbol)
        except Exception as exc:
            raise ExchangeError(_message(exc)) from exc
        canceled = 0
        for order in resting or []:
            try:
                client.cancel_order(order.get("id"), symbol, order.get("params") or {})
                canceled += 1
            except Exception:  # already filled or canceled by the venue
                log.debug("撤销保护单失败 %s", order.get("id"), exc_info=True)
        return canceled

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
        "请到币安「API 管理」核对：①该 Key 是否勾选了当前市场的读取权限；"
        "②若设置了 IP 白名单，当前公网 IP 是否在其中。"
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
