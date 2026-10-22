"""Symbol precision: ccxt reports steps, the UI needs decimal places."""

from __future__ import annotations

from types import SimpleNamespace

from btrader.core.types import MarketType
from btrader.exchange.symbols import load_symbols


def _rest(markets: dict) -> SimpleNamespace:
    return SimpleNamespace(markets=lambda market: markets)


def _market(symbol: str, **extra) -> dict:
    """A ccxt market shaped like the real thing (binance)."""
    base, quote = symbol.split("/")
    return {
        "symbol": symbol,
        "base": base,
        "quote": quote,
        "active": True,
        "contractSize": 1.0,
        "inverse": False,
        **extra,
    }


def _one(market: dict):
    info = load_symbols(_rest({market["symbol"]: market}), MarketType.SPOT)
    assert len(info) == 1
    return info[0]


def test_price_and_amount_precision_are_read_from_the_ccxt_precision_block() -> None:
    """ccxt 的 precision 里放的是步长（0.01 / 1e-05），要换成位数。

    以前读的是 market['amount'] / market['price'] 这两格（ccxt 里根本不存在），
    于是永远走 8 的兜底：BTC/USDT 的价格轴显示 8 位小数。
    """
    info = _one(
        _market(
            "BTC/USDT",
            precision={"amount": 1e-05, "price": 0.01},
            limits={
                "amount": {"min": 1e-05, "max": 9000.0},
                "price": {"min": 0.01, "max": 1_000_000.0},
                "cost": {"min": 5.0},
            },
        )
    )

    assert info.price_precision == 2
    assert info.amount_precision == 5


def test_precision_falls_back_to_eight_digits_when_ccxt_reports_nothing() -> None:
    """两处都读不到时保留原来的 8。"""
    info = _one(_market("ABC/USDT"))

    assert info.price_precision == 8
    assert info.amount_precision == 8


def test_precision_still_accepts_the_legacy_decimal_count_shape() -> None:
    """早期 ccxt 在这一格报的是小数位数本身，那个形状也要照认。"""
    info = _one(_market("XYZ/USDT", precision={"amount": 3, "price": 2}))

    assert info.price_precision == 2
    assert info.amount_precision == 3


def test_precision_falls_back_to_the_minimum_order_size_step() -> None:
    """没有 precision 块的老交易所，用最小下单量当步长。"""
    info = _one(
        _market(
            "ETH/USDT",
            limits={"amount": {"min": 0.001}, "price": {"min": 0.1}},
        )
    )

    assert info.price_precision == 1
    assert info.amount_precision == 3
