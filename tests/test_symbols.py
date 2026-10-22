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


# ------------------------------------------------ 每个市场只给自己的符号
def _spot(symbol: str = "BTC/USDT", **extra) -> dict:
    """ccxt 的现货市场：spot=True、没有合约标记。"""
    base, quote = symbol.split("/")
    return {
        "symbol": symbol,
        "base": base,
        "quote": quote,
        "active": True,
        "spot": True,
        "contract": False,
        "precision": {"amount": 1e-05, "price": 0.01},
        "limits": {"amount": {"min": 1e-05}, "cost": {"min": 5.0}},
        **extra,
    }


def _swap(symbol: str, inverse: bool = False, **extra) -> dict:
    """ccxt 的永续合约：带结算后缀（:USDT / :BTC），contract/swap 都是 True。"""
    base = symbol.split("/")[0]
    quote = symbol.split("/")[1].split(":")[0]
    settle = symbol.split(":")[-1]
    return {
        "symbol": symbol,
        "base": base,
        "quote": quote,
        "settle": settle,
        "active": True,
        "spot": False,
        "swap": True,
        "contract": True,
        "linear": not inverse,
        "inverse": inverse,
        "contractSize": 100.0 if inverse else 0.1,
        "limits": {"amount": {"min": 1.0 if inverse else 0.001}, "cost": {"min": 5.0}},
        **extra,
    }


def _delivery(symbol: str = "BTC/USDT:USDT-261225") -> dict:
    """带到期日的交割合约。"""
    market = _swap("BTC/USDT:USDT")
    market.update(
        {"symbol": symbol, "swap": False, "future": True, "expiry": 1_798_761_600_000}
    )
    return market


def _everything() -> dict:
    """一次 load_markets 就会返回的四种市场（现货客户端实测就是这样）。"""
    rows = [
        _spot("BTC/USDT"),
        _spot("ETH/BTC"),
        _swap("BTC/USDT:USDT"),
        _swap("ETH/USDC:USDC"),
        _swap("BTC/USD:BTC", inverse=True),
        _delivery(),
    ]
    return {row["symbol"]: row for row in rows}


def test_the_spot_list_never_carries_a_contract_symbol() -> None:
    """实测：/api/symbols?market=spot 返回 1461 项，混着 BTC/USDT:USDT 这种合约。

    ccxt 的现货客户端 load_markets 会把线性、反向、交割一起取回来，
    以前只按报价币过滤，于是合约符号直接进了现货列表：设置里的 last_symbol
    变成 BTC/USDT:USDT 之后，现货下单会把合约符号发给现货客户端。
    """
    items = load_symbols(_rest(_everything()), MarketType.SPOT)

    assert [item.symbol for item in items] == ["BTC/USDT", "ETH/BTC"]
    assert all(":" not in item.symbol for item in items)
    assert all(not item.symbol[-6:].isdigit() for item in items)


def test_the_futures_lists_never_carry_spot_symbols() -> None:
    """反方向也要挡住：合约列表里不许出现现货写法。"""
    markets = _everything()

    usdm = [item.symbol for item in load_symbols(_rest(markets), MarketType.USDM)]
    coinm = [item.symbol for item in load_symbols(_rest(markets), MarketType.COINM)]

    assert "BTC/USDT" not in usdm and "ETH/BTC" not in usdm
    assert "BTC/USDT:USDT" in usdm and "ETH/USDC:USDC" in usdm
    assert "BTC/USD:BTC" not in usdm, "币本位符号不该进 U 本位列表"
    assert coinm == ["BTC/USD:BTC"]


def test_a_contract_is_recognised_from_its_symbol_alone() -> None:
    """替身/老版本 ccxt 可能不给 swap/contract 标记，结算后缀本身就能判。"""
    bare = {"symbol": "ETH/USDT:USDT", "base": "ETH", "quote": "USDT", "active": True}

    assert load_symbols(_rest({bare["symbol"]: bare}), MarketType.SPOT) == []
    assert [item.symbol for item in load_symbols(_rest({bare["symbol"]: bare}), MarketType.USDM)] == [
        "ETH/USDT:USDT"
    ]


def test_a_coin_margined_contract_is_recognised_without_the_inverse_flag() -> None:
    """结算币和基础币相同（BTC/USD:BTC）就是反向合约，标志缺了也认得出来。"""
    bare = {"symbol": "BTC/USD:BTC", "base": "BTC", "quote": "USD", "active": True}

    assert load_symbols(_rest({bare["symbol"]: bare}), MarketType.USDM) == []
    assert [item.symbol for item in load_symbols(_rest({bare["symbol"]: bare}), MarketType.COINM)] == [
        "BTC/USD:BTC"
    ]

# ------------------------------------------------- /api/symbols 端到端
def _symbols_endpoint(monkeypatch, markets: dict):
    """把 /api/symbols 的端点函数取出来，用它自己的缓存跑一遍。"""
    import threading

    from btrader.web import app as web_app

    rest = SimpleNamespace(markets=lambda market: markets)
    state = SimpleNamespace(
        ctx=SimpleNamespace(market_rest_for=lambda market: rest),
        symbols={},
        loading={market: threading.RLock() for market in MarketType},
        locks={market: threading.RLock() for market in MarketType},
    )
    monkeypatch.setattr(web_app, "_state", state, raising=False)
    app = web_app.create_app()
    for route in app.routes:
        if getattr(route, "path", "") == "/api/symbols":
            return route.endpoint, state
    raise AssertionError("missing /api/symbols")


def test_the_spot_endpoint_returns_only_spot_symbols(monkeypatch) -> None:
    """/api/symbols?market=spot 里出现 BTC/USDT:USDT 就是这条链路的漏点。"""
    call, state = _symbols_endpoint(monkeypatch, _everything())
    payload = call(market="spot")

    assert payload["degraded"] is False, payload["error"]
    assert [item["symbol"] for item in payload["items"]] == ["BTC/USDT", "ETH/BTC"]
    assert set(state.symbols["spot"][0]) == {
        "symbol",
        "display",
        "base",
        "quote",
        "price_precision",
        "amount_precision",
        "min_amount",
        "min_cost",
        "is_inverse",
    }


def test_each_market_gets_its_own_cached_list(monkeypatch) -> None:
    """三个市场各用各的数据源，缓存也分开存。"""
    call, state = _symbols_endpoint(monkeypatch, _everything())

    spot = [item["symbol"] for item in call(market="spot")["items"]]
    usdm = [item["symbol"] for item in call(market="usdm")["items"]]
    coinm = [item["symbol"] for item in call(market="coinm")["items"]]

    assert spot == ["BTC/USDT", "ETH/BTC"]
    assert usdm == ["BTC/USDT:USDT", "ETH/USDC:USDC", "BTC/USDT:USDT-261225"]
    assert coinm == ["BTC/USD:BTC"]
    assert set(state.symbols) == {"spot", "usdm", "coinm"}
    assert all(item["is_inverse"] for item in call(market="coinm")["items"])