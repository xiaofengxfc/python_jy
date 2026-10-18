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

    def contract_spec(self, market: MarketType, symbol: str) -> ContractSpec:
        """Contract size / minimum amount for a symbol, cached after first use."""
        key = (market, symbol)
        with self._lock:
            cached = self._specs.get(key)
            if cached is not None:
                return cached
        spec = DEFAULT_SPEC
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
        except Exception as exc:
            log.debug("无法读取 %s 的合约规格: %s", symbol, exc)
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


def _message(exc: Exception) -> str:
    text = str(exc) or exc.__class__.__name__
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
