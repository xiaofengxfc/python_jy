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
                amount_precision=_precision(raw.get("amount")),
                price_precision=_precision(raw.get("price")),
                min_amount=float((raw.get("limits", {}).get("amount") or {}).get("min") or 0),
                min_cost=float((raw.get("limits", {}).get("cost") or {}).get("min") or 0),
                contract_size=float(raw.get("contractSize") or 1.0),
                is_inverse=bool(raw.get("inverse", False)),
            )
        )
    return infos


def _precision(raw: dict | None) -> int:
    if not raw:
        return 8
    digits = raw.get("precision")
    if isinstance(digits, int):
        return digits
    step = raw.get("step") or raw.get("min")
    if step:
        text = f"{float(step):.12f}".rstrip("0")
        return len(text.split(".")[1]) if "." in text else 0
    return 8


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
