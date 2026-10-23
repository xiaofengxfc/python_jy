"""资金费率历史：合约读交易所，现货空手而归，出错也不抛。

回测要按结算时刻扣费，费率只能从交易所那一侧拿。三条约定：现货没有这东西
（一个请求都不发）、拿不到就是空列表（网络抖动不该让回测跑不起来）、返回的是
ccxt 的 unified 形状（``timestamp`` 毫秒 + ``fundingRate`` 小数）。
"""

from __future__ import annotations

import logging

import ccxt
import pytest

from btrader.core.credentials import Credentials
from btrader.core.types import MarketType, Network
from btrader.exchange.rest import BinanceREST

SPOT = "BTC/USDT"
USDM = "BTC/USDT:USDT"
COINM = "BTC/USD:BTC"

ROWS = [
    {"timestamp": 1_792_713_600_006, "fundingRate": 0.00005369, "symbol": USDM},
    {"timestamp": 1_792_684_800_006, "fundingRate": -0.00012, "symbol": USDM},
]


class FakeVenue:
    """只实现资金费率历史这一条的替身，记下每次调用。"""

    def __init__(self, rows=None, boom: BaseException | None = None) -> None:
        self.rows = ROWS if rows is None else rows
        self.boom = boom
        self.calls: list[dict] = []

    def load_markets(self) -> dict:
        return {}

    def fetch_funding_rate_history(self, symbol, since=None, limit=1000):
        self.calls.append({"symbol": symbol, "since": since, "limit": limit})
        if self.boom is not None:
            raise self.boom
        return list(self.rows)


def _rest(market: MarketType, venue, *, keys: bool = True) -> BinanceREST:
    credentials = Credentials(api_key="key", api_secret="secret") if keys else None
    rest = BinanceREST(Network.MAINNET, credentials)
    rest._clients[market] = venue
    return rest


# --------------------------------------------------------------- 去哪儿问
def test_a_spot_pair_has_no_funding_rate_and_costs_no_request() -> None:
    """现货没有资金费率：直接空列表，别发请求。"""
    venue = FakeVenue()
    rest = _rest(MarketType.SPOT, venue)

    assert rest.fetch_funding_rate_history(MarketType.SPOT, SPOT) == []
    assert venue.calls == []


def test_without_credentials_it_answers_empty_instead_of_raising() -> None:
    """没配 Key 的路径（图表、离线预览）拿它也不该炸。"""
    venue = FakeVenue()
    rest = _rest(MarketType.USDM, venue, keys=False)

    assert rest.fetch_funding_rate_history(MarketType.USDM, USDM) == []
    assert venue.calls == []


def test_the_futures_client_is_asked_with_the_callers_window() -> None:
    """since / limit 原样转给 ccxt，符号也原样（合约写法本来就是交易所那一对）。"""
    venue = FakeVenue()
    rest = _rest(MarketType.USDM, venue)

    rows = rest.fetch_funding_rate_history(MarketType.USDM, USDM, since=1_792_000_000_000, limit=500)

    assert venue.calls == [{"symbol": USDM, "since": 1_792_000_000_000, "limit": 500}]
    assert rows == ROWS
    assert rows[0]["timestamp"] == 1_792_713_600_006
    assert rows[0]["fundingRate"] == pytest.approx(0.00005369)


def test_coin_margined_reads_its_own_client() -> None:
    """币本位走 binancecoinm，别拿 U 本位的客户端去问。"""
    usdm = FakeVenue()
    coinm = FakeVenue([{"timestamp": 1_792_713_600_006, "fundingRate": 0.0001, "symbol": COINM}])
    rest = BinanceREST(Network.MAINNET, Credentials(api_key="key", api_secret="secret"))
    rest._clients[MarketType.USDM] = usdm
    rest._clients[MarketType.COINM] = coinm

    rows = rest.fetch_funding_rate_history(MarketType.COINM, COINM)

    assert [row["symbol"] for row in rows] == [COINM]
    assert usdm.calls == []
    assert coinm.calls == [{"symbol": COINM, "since": None, "limit": 1000}]


# --------------------------------------------------------- 出错不许抛出去
def test_an_unsupported_venue_is_just_empty() -> None:
    """交易所没这个端点（测试网常见）：空列表，不抛。"""
    venue = FakeVenue(boom=ccxt.NotSupported("binanceusdm fetchFundingRateHistory not supported"))

    assert _rest(MarketType.USDM, venue).fetch_funding_rate_history(MarketType.USDM, USDM) == []


def test_a_network_failure_is_logged_and_swallowed(caplog) -> None:
    """一次网络抖动不该让回测跑不起来：记 debug，回空列表。"""
    venue = FakeVenue(boom=ccxt.NetworkError("fapi 超时"))
    rest = _rest(MarketType.USDM, venue)

    with caplog.at_level(logging.DEBUG, logger="btrader.exchange.rest"):
        assert rest.fetch_funding_rate_history(MarketType.USDM, USDM) == []

    assert any("资金费率" in record.getMessage() for record in caplog.records)


def test_an_empty_answer_is_not_an_error() -> None:
    """交易所只回了空数组（新上市的交易对）：照原样回空。"""
    assert _rest(MarketType.USDM, FakeVenue([])).fetch_funding_rate_history(
        MarketType.USDM, USDM
    ) == []


# ------------------------------------------------- 合约规格：最小量与步长
class SpecVenue:
    """contract_spec 需要的两个方法：load_markets 与 market。"""

    def __init__(self, raw: dict) -> None:
        self.raw = raw

    def load_markets(self) -> dict:
        return {self.raw["symbol"]: self.raw}

    def market(self, symbol: str) -> dict:
        return self.raw


def _spec(raw: dict):
    rest = _rest(MarketType.USDM, SpecVenue(raw))
    return rest.contract_spec(MarketType.USDM, raw["symbol"])


def test_the_spec_carries_the_minimum_amount_and_the_step() -> None:
    """回测要按步长截断、按最小量跳过：两样都得从交易所规格里出来。"""
    spec = _spec(
        {
            "symbol": USDM,
            "contractSize": 0.1,
            "precision": {"amount": 1e-05, "price": 0.01},
            "limits": {"amount": {"min": 0.001}, "cost": {"min": 5.0}},
        }
    )

    assert spec.min_amount == pytest.approx(0.001)
    assert spec.amount_step == pytest.approx(1e-05)


def test_the_legacy_digit_shape_is_read_as_a_step_not_a_count() -> None:
    """老版本 ccxt 在这一格报的是位数：3 位小数就是 0.001 的步长。"""
    spec = _spec({"symbol": USDM, "precision": {"amount": 3}})

    assert spec.amount_step == pytest.approx(0.001)


def test_a_venue_that_publishes_no_step_leaves_it_at_zero() -> None:
    """没公布步长就是 0：调用方据此不截断，别猜一个值。"""
    assert _spec({"symbol": USDM}).amount_step == 0.0