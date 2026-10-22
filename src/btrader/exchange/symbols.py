"""Symbol discovery and formatting helpers."""

from __future__ import annotations

import logging

from ..core.models import SymbolInfo, Ticker
from ..core.types import MarketType
from .rest import BinanceREST

log = logging.getLogger(__name__)


def load_symbols(rest: BinanceREST, market: MarketType) -> list[SymbolInfo]:
    """Return active symbols for the given market, most relevant first."""
    markets = rest.markets(market)
    quotes = market.quote_assets
    infos: list[SymbolInfo] = []
    for symbol, raw in markets.items():
        if not raw.get("active", True):
            continue
        quote = raw.get("quote") or ""
        if quote not in quotes:
            continue
        infos.append(
            SymbolInfo(
                symbol=symbol,
                market_type=market,
                base=raw.get("base") or "",
                quote=quote,
                active=True,
                amount_precision=_precision(raw, "amount"),
                price_precision=_precision(raw, "price"),
                min_amount=float((raw.get("limits", {}).get("amount") or {}).get("min") or 0),
                min_cost=float((raw.get("limits", {}).get("cost") or {}).get("min") or 0),
                contract_size=float(raw.get("contractSize") or 1.0),
                is_inverse=bool(raw.get("inverse", False)),
            )
        )
    return infos


def _precision(raw: dict | None, side: str) -> int:
    """Decimal places for ``side`` ("amount" or "price") of a ccxt market.

    ccxt 把步长放在 ``market['precision'][side]``：``0.01`` 是价格步长、
    ``1e-05`` 是数量步长，要换算成位数再用，直接当位数输出就差了六个数量级。
    早期 ccxt 在这一格报的是位数本身（int），那个形状也照原样认。
    两处都读不到时保留 8 做兜底。
    """
    if not raw:
        return 8
    precision = raw.get("precision")
    if isinstance(precision, dict):
        value = precision.get(side)
        if isinstance(value, int) and not isinstance(value, bool):
            # 老形状：这一格就是小数位数，不是步长。
            return max(0, value)
        digits = _digits(value)
        if digits is not None:
            return digits
    limits = (raw.get("limits") or {}).get(side) or {}
    digits = _digits(limits.get("min"))
    if digits is not None:
        return digits
    return 8


def _digits(step) -> int | None:
    """步长换算成小数位数；不是正数（或读不出来）就返回 None，交给下一处来源。"""
    try:
        value = float(step)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    text = f"{value:.12f}".rstrip("0")
    return len(text.split(".")[1]) if "." in text else 0


def ticker_from_ccxt(symbol: str, raw: dict) -> Ticker:
    return Ticker(
        symbol=symbol,
        last=float(raw.get("last") or 0.0),
        bid=float(raw.get("bid") or 0.0),
        ask=float(raw.get("ask") or 0.0),
        high=float(raw.get("high") or 0.0),
        low=float(raw.get("low") or 0.0),
        base_volume=float(raw.get("baseVolume") or 0.0),
        quote_volume=float(raw.get("quoteVolume") or 0.0),
        change_pct=float(raw.get("percentage") or 0.0),
        timestamp=int(raw.get("timestamp") or 0),
    )


def format_amount(value: float) -> str:
    if value == 0:
        return "0"
    if abs(value) >= 1000:
        return f"{value:,.2f}"
    if abs(value) >= 1:
        return f"{value:,.4f}".rstrip("0").rstrip(".")
    return f"{value:.8f}".rstrip("0").rstrip(".")
