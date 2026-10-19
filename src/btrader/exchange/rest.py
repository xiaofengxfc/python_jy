"""Thin synchronous REST wrapper around ccxt for Binance."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import ccxt

from ..core.contracts import DEFAULT_SPEC, ContractSpec
from ..core.credentials import Credentials
from ..core.types import MarketType, Network

log = logging.getLogger(__name__)


class ExchangeError(RuntimeError):
    """Raised for normalized, user-facing exchange errors."""


class BinanceREST:
    """One ccxt client per market type, created lazily."""

    def __init__(self, network: Network, credentials: Credentials | None = None) -> None:
        self.network = network
        self.credentials = credentials or Credentials()
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
            if self.network is Network.TESTNET:
                # ccxt exposes sandbox mode per exchange.
                if getattr(client, "has", {}).get("sandbox", True):
                    try:
                        client.set_sandbox_mode(True)
                    except Exception as exc:
                        log.warning("无法启用 %s 测试网: %s", market.ccxt_id, exc)
            if self.credentials.is_set:
                client.apiKey = self.credentials.api_key
                client.secret = self.credentials.api_secret
            self._clients[market] = client
            return client

    def reset(self) -> None:
        with self._lock:
            self._clients.clear()
            self._specs.clear()

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
