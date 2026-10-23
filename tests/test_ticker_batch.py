"""批量 24 小时涨幅：三个市场各一份真实字段，缺的就给 None，不写 0。

实测（主网，2026-10-23）：三个市场的原始 ticker 里都有 ``lastPrice`` 与
``priceChangePercent``，``priceChange`` 是**绝对变化**不是百分比（现货 -1083.41 /
币本位 -1174.5），不能直接当涨幅用；币本位那份 payload 里**没有** ``quoteVolume``
（只有 ``baseVolume`` 与 ``volume``），报价额只能走 ccxt 归一后的 ``quoteVolume``。
"""

from __future__ import annotations

import ccxt
import pytest

from btrader.core.credentials import Credentials
from btrader.core.types import MarketType, Network
from btrader.exchange.rest import BinanceREST, ExchangeError

SPOT = "BTC/USDT"
USDM = "BTC/USDT:USDT"
COINM = "BTC/USD:BTC"

# 下面三份都是实测抓下来的形状（值也是当时的值）。
SPOT_TICK = {
    "symbol": SPOT,
    "last": 81684.01,
    "percentage": -1.309,
    "change": -1083.41,
    "quoteVolume": 1460414449.6652935,
    "bid": 81684.0,
    "ask": 81684.01,
    "info": {
        "lastPrice": "81684.01000000",
        "openPrice": "82767.42000000",
        "priceChange": "-1083.41000000",
        "priceChangePercent": "-1.309",
        "quoteVolume": "1460414449.66529340",
        "bidPrice": "81684.00000000",
        "askPrice": "81684.01000000",
    },
}
USDM_TICK = {
    "symbol": USDM,
    "last": 81655.0,
    "percentage": -1.345,
    "quoteVolume": 12599116506.66,
    "info": {
        "lastPrice": "81655.00",
        "openPrice": "82768.20",
        "priceChange": "-1113.20",
        "priceChangePercent": "-1.345",
        "quoteVolume": "12599116506.66",
    },
}
COINM_TICK = {
    "symbol": COINM,
    "last": 81526.2,
    "percentage": -1.42,
    "quoteVolume": 865737192.2381723,
    "info": {
        "lastPrice": "81526.2",
        "openPrice": "82700.7",
        "priceChange": "-1174.5",
        "priceChangePercent": "-1.420",
        "baseVolume": "869496880.59",
    },
}


class FakeTickers:
    """只实现 fetch_tickers 的替身：返回给定的 payload，记下被问到的符号。"""

    def __init__(self, payload: dict | None = None, boom: BaseException | None = None) -> None:
        self.payload = payload or {}
        self.boom = boom
        self.calls: list[list[str]] = []

    def load_markets(self) -> dict:
        return {}

    def fetch_tickers(self, symbols=None) -> dict:
        self.calls.append(list(symbols or []))
        if self.boom is not None:
            raise self.boom
        wanted = set(symbols or [])
        return {key: value for key, value in self.payload.items() if not wanted or key in wanted}


def _rest(market: MarketType, venue, *, keys: bool = False) -> BinanceREST:
    credentials = Credentials(api_key="key", api_secret="secret") if keys else None
    rest = BinanceREST(Network.MAINNET, credentials, market_data=True)
    rest._clients[market] = venue
    return rest


def _one(market: MarketType, symbol: str, tick: dict) -> dict:
    rest = _rest(market, FakeTickers({symbol: tick}))
    rows = rest.ticker_snapshots(market, [symbol])
    assert len(rows) == 1
    return rows[0]


# --------------------------------------------------------------- 三个市场
def test_a_spot_snapshot_carries_the_three_numbers() -> None:
    """现货：涨跌幅 -1.309%、最新价 81684.01、24h 成交额 14.60 亿。"""
    row = _one(MarketType.SPOT, SPOT, SPOT_TICK)

    assert row == {
        "symbol": SPOT,
        "last": pytest.approx(81684.01),
        "change_pct": pytest.approx(-1.309),
        "quote_volume": pytest.approx(1460414449.6652935),
    }


def test_a_usdm_snapshot_matches_the_web_number() -> None:
    """U 本位：-1.345%。合约 24h ticker 里没有 bid/ask，这里也不碰。"""
    row = _one(MarketType.USDM, USDM, USDM_TICK)

    assert row["change_pct"] == pytest.approx(-1.345)
    assert row["last"] == pytest.approx(81655.0)
    assert row["quote_volume"] == pytest.approx(12599116506.66)


def test_the_exchanges_own_quote_volume_wins_over_the_normalised_one() -> None:
    """原始 quoteVolume 是网页上那个数；ccxt 对合约有时按 volume×last 重算。"""
    tick = {
        "symbol": USDM,
        "quoteVolume": 12598018471.89,
        "info": {"lastPrice": "81691.70", "quoteVolume": "12598069692.64"},
    }

    assert _one(MarketType.USDM, USDM, tick)["quote_volume"] == pytest.approx(12598069692.64)


def test_a_coinm_snapshot_falls_back_to_the_unified_quote_volume() -> None:
    """币本位 payload 里没有 quoteVolume：报价额只能取 ccxt 归一后的那个。"""
    row = _one(MarketType.COINM, COINM, COINM_TICK)

    assert row["change_pct"] == pytest.approx(-1.42)
    assert row["quote_volume"] == pytest.approx(865737192.2381723)


# --------------------------------------------------- 字段缺失不能当 0 用
def test_the_raw_payload_is_enough_when_ccxt_normalises_nothing() -> None:
    """unified 的 percentage 是 None、原始 payload 里有：取 priceChangePercent。"""
    tick = {
        "symbol": USDM,
        "last": None,
        "percentage": None,
        "info": {"lastPrice": "81655.00", "priceChangePercent": "-1.345"},
    }

    row = _one(MarketType.USDM, USDM, tick)

    assert row["last"] == pytest.approx(81655.0)
    assert row["change_pct"] == pytest.approx(-1.345)
    assert row["quote_volume"] is None


def test_an_absolute_price_change_is_turned_into_a_percentage() -> None:
    """只有 priceChange（绝对变化）时按 openPrice 折成百分比，别把 -1174.5 当涨幅。"""
    tick = {
        "symbol": COINM,
        "last": 81526.2,
        "info": {"lastPrice": "81526.2", "priceChange": "-1174.5", "openPrice": "82700.7"},
    }

    row = _one(MarketType.COINM, COINM, tick)

    assert row["change_pct"] == pytest.approx(-1174.5 / 82700.7 * 100.0, rel=1e-9)
    assert row["change_pct"] == pytest.approx(-1.42, abs=5e-3)


def test_an_absolute_change_without_an_open_price_is_not_guessed() -> None:
    """连 openPrice 都没有就判不出来：None，不许把绝对变化当百分比塞出去。"""
    tick = {"symbol": COINM, "info": {"lastPrice": "81526.2", "priceChange": "-1174.5"}}

    assert _one(MarketType.COINM, COINM, tick)["change_pct"] is None


def test_unknown_fields_are_none_rather_than_zero() -> None:
    """什么都读不到就给 None：0 是「恰好不涨不跌」，和「不知道」不是一回事。"""
    row = _one(MarketType.SPOT, SPOT, {"symbol": SPOT, "info": {}})

    assert row == {"symbol": SPOT, "last": None, "change_pct": None, "quote_volume": None}
    assert row["change_pct"] is not 0.0


def test_a_flat_market_reports_a_real_zero() -> None:
    """真的是 0.000% 就照实给 0.0——这条和上一条一起钉住「None ≠ 0」。"""
    tick = {"symbol": SPOT, "last": 1.0, "percentage": 0.0, "info": {"priceChangePercent": "0.000"}}

    assert _one(MarketType.SPOT, SPOT, tick)["change_pct"] == 0.0


# ------------------------------------------------------------ 批量与失败
def test_an_empty_batch_asks_the_venue_nothing() -> None:
    """没有要问的符号就别去打整市场（那是 1400 项）。"""
    venue = FakeTickers({SPOT: SPOT_TICK})

    assert _rest(MarketType.SPOT, venue).ticker_snapshots(MarketType.SPOT, []) == []
    assert venue.calls == []


def test_the_batch_keeps_the_callers_order_and_skips_unknown_symbols() -> None:
    """返回顺序跟着调用方；交易所没给的那一项跳过，不补 0 行。"""
    venue = FakeTickers({SPOT: SPOT_TICK, USDM: USDM_TICK})
    rest = _rest(MarketType.SPOT, venue)

    rows = rest.ticker_snapshots(MarketType.SPOT, ["ETH/USDT", SPOT])

    assert [row["symbol"] for row in rows] == [SPOT]
    assert venue.calls == [["ETH/USDT", SPOT]]


def test_it_works_without_credentials_because_this_is_public_data() -> None:
    """批量涨幅是公开行情：没配 Key 的图表客户端也要能取。"""
    venue = FakeTickers({SPOT: SPOT_TICK})
    rest = _rest(MarketType.SPOT, venue, keys=False)

    assert rest.has_credentials is False
    assert rest.ticker_snapshots(MarketType.SPOT, [SPOT])[0]["change_pct"] == pytest.approx(-1.309)


def test_a_venue_failure_is_a_chinese_exchange_error() -> None:
    """取不到就抛 ExchangeError，文案要是中文（界面直接显示这一句）。"""
    venue = FakeTickers(boom=ccxt.NetworkError("fapi request timeout"))
    rest = _rest(MarketType.USDM, venue)

    with pytest.raises(ExchangeError) as caught:
        rest.ticker_snapshots(MarketType.USDM, [USDM])

    message = str(caught.value)
    assert "fapi request timeout" in message
    assert any("\u4e00" <= char <= "\u9fff" for char in message)