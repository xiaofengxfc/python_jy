"""持仓行的两个量纲：contracts 是交易所事实（张），size_base 是基础币。

界面上的数量、100% 按钮、部分平仓都读 size_base。以前没有这个字段，前端直接
把 contracts 当币用：币本位 10 张（1,000 USD）被读成 10 BTC，差 500 倍；资产页
走的是不带 symbol 的全钱包读，那时后端也不取合约规格，同一张仓在两页显示 1,000
和 500,000 两个数。这一份把两个读法和两个市场都钉住。
"""

from __future__ import annotations

import pytest

from btrader.core.contracts import ContractSpec
from btrader.core.storage import Database
from btrader.core.types import MarketType, Network
from btrader.engine.account import AccountService

from fakes import FakeVenue

COINM = "BTC/USD:BTC"
USDM = "BTC/USDT:USDT"
# 币本位永续：一张 100 USD。0.02 BTC @ 50,000 = 1,000 USD = 10 张。
PRICE = 50_000.0
BASE_QTY = 0.02
CONTRACTS = 10.0


def _service(tmp_path, venue: FakeVenue) -> AccountService:
    db = Database(tmp_path / "units.db")
    return AccountService(
        rest_for=lambda market: venue,
        chart_rest_for=lambda market: venue,
        db=db,
        network=Network.TESTNET,
    )


def _seed(venue: FakeVenue, symbol: str, base: float) -> None:
    venue._book[symbol] = base
    venue._entry[symbol] = PRICE
    venue._last[symbol] = PRICE


def test_a_coinm_row_reports_base_coins_not_contracts(tmp_path):
    """币本位：10 张 = 0.02 BTC，市值按 1 张 100 USD 算 = 1,000。"""
    venue = FakeVenue()
    _seed(venue, COINM, BASE_QTY)
    service = _service(tmp_path, venue)

    row = service.snapshot(MarketType.COINM, COINM).positions[0]

    assert row["contracts"] == pytest.approx(CONTRACTS)
    assert row["size_base"] == pytest.approx(BASE_QTY)
    assert row["quote_value"] == pytest.approx(1000.0)


def test_the_whole_wallet_read_agrees_with_the_pair_read(tmp_path):
    """不带 symbol 的全钱包读（资产页、侧栏）给出同一组数。"""
    venue = FakeVenue()
    _seed(venue, COINM, BASE_QTY)
    service = _service(tmp_path, venue)

    pair = service.snapshot(MarketType.COINM, COINM).positions[0]
    wallet = service.snapshot(MarketType.COINM).positions[0]

    assert wallet["size_base"] == pytest.approx(pair["size_base"])
    assert wallet["quote_value"] == pytest.approx(1000.0)
    assert wallet["quote_value"] == pytest.approx(pair["quote_value"])


def test_a_linear_row_keeps_contracts_equal_to_base(tmp_path):
    """U 本位一块钱一张时，size_base 与 contracts 同值（BTC/USDT 不是特例）。"""
    venue = FakeVenue()
    _seed(venue, USDM, 0.5)
    service = _service(tmp_path, venue)

    row = service.snapshot(MarketType.USDM, USDM).positions[0]

    assert row["contracts"] == pytest.approx(0.5)
    assert row["size_base"] == pytest.approx(0.5)
    assert row["quote_value"] == pytest.approx(0.5 * PRICE)


def test_a_contract_size_of_a_thousand_is_divided_out(tmp_path):
    """1000SHIB 那种一张 1,000 币的线性合约：10,000 张 = 1,000 万币。"""
    symbol = "1000SHIB/USDT:USDT"
    venue = FakeVenue(
        spec_overrides={
            MarketType.USDM: ContractSpec(
                contract_size=1000.0,
                inverse=False,
                base="1000SHIB",
                quote="USDT",
                settle="USDT",
                min_amount=1.0,
            )
        }
    )
    venue._book[symbol] = 10_000_000.0  # 基础币 1,000 万 = 10,000 张
    venue._entry[symbol] = 0.00001
    venue._last[symbol] = 0.00001
    service = _service(tmp_path, venue)

    row = service.snapshot(MarketType.USDM, symbol).positions[0]

    assert row["contracts"] == pytest.approx(10_000.0)
    assert row["size_base"] == pytest.approx(10_000_000.0)
    assert row["quote_value"] == pytest.approx(10_000.0 * 1000.0 * 0.00001)


def test_an_unknown_spec_is_none_rather_than_1_to_1(tmp_path):
    """币本位拿到「不是反向合约」的规格说明这次没读出来：size_base 留 None。

    默认规格是 contract_size=1、不是反向合约。拿它折算等于把 10 张当 10 个币，
    差 500 倍；所以这一层宁可说不知道，也不给一个看着像数的错数。
    """
    venue = FakeVenue(
        spec_overrides={MarketType.COINM: ContractSpec(contract_size=1.0, inverse=False)}
    )
    _seed(venue, COINM, BASE_QTY)
    service = _service(tmp_path, venue)

    row = service.snapshot(MarketType.COINM, COINM).positions[0]

    assert row["size_base"] is None


def test_a_spot_ledger_row_carries_the_same_number_twice(tmp_path):
    """现货的「张」就是币本身：两个字段同值，前端一条读法覆盖两个市场。"""
    symbol = "ETH/USDT"
    venue = FakeVenue(
        balances={MarketType.SPOT: {"USDT": 10_000.0, "ETH": 1.0}},
    )
    venue._book[symbol] = 1.0
    venue._entry[symbol] = 100.0
    venue._last[symbol] = 100.0
    service = _service(tmp_path, venue)
    # 账本里记 0.25 ETH 是这台机器买的，其余 0.75 是用户自己的。
    service._ledger = lambda: {symbol: {"size": 0.25, "entry": 100.0, "realized": 0.0, "fees": 0.0}}

    rows = service.snapshot(MarketType.SPOT, symbol).positions
    ledger_row = next(item for item in rows if item.get("origin") == "ledger")

    assert ledger_row["contracts"] == pytest.approx(0.25)
    assert ledger_row["size_base"] == pytest.approx(0.25)