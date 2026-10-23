"""现货持仓拆两行：程序的账本一行、用户自己的余额一行。

t64 之后界面实测撞到的一件事：钱包里有自己充的 1 ETH，程序又买了 0.0025，现货持仓只出
**一行**——数量列是钱包事实 1.0025、`origin=ledger`、按钮「平仓」（只平账本 0.0025）。
于是那段时间用户自己那 1 ETH 没有任何卖出入口，必须先把账本平掉才回来。

改后的口径：

- 账本行：`origin=ledger`、`contracts = ledger_size`（这一行数量就是账本数量，不再拿钱包
  事实充数）、`unrealized_basis="ledger"`、带未实现盈亏，界面按钮是「平仓」；
- 自有余额行：`origin=wallet`、`contracts = 钱包 − 账本`、`entry_price` 空、未实现盈亏恒 0，
  界面按钮是「卖出」（预填这一行的数量）；**只有差额 > 0 时才出这一行**；
- 钱包 ≤ 账本（币被提走之类）时不出自有行，账本行数量封顶钱包；
- 合约侧一行不动：交易所那份持仓本来就是唯一事实。
"""

from __future__ import annotations

import pytest

from btrader.core.storage import Database
from btrader.core.types import MarketType, Network
from btrader.engine.account import AccountService

from fakes import FakeVenue

SPOT = "BTC/USDT"
PRICE = 81_000.0
ENTRY = 80_000.0


def _venue(wallet_btc: float, last: float = PRICE) -> FakeVenue:
    venue = FakeVenue(balances={MarketType.SPOT: {"USDT": 0.0, "BTC": wallet_btc}})
    venue._last[SPOT] = last
    return venue


def _account(tmp_path, venue: FakeVenue) -> tuple[AccountService, Database]:
    db = Database(tmp_path / "rows.db")
    service = AccountService(
        rest_for=lambda market: venue,
        chart_rest_for=lambda market: venue,
        db=db,
        network=Network.TESTNET,
    )
    return service, db


def _seed_buy(db: Database, amount: float, price: float = ENTRY) -> None:
    db.save_trade(
        "binance:testnet",
        {
            "id": "fill-1",
            "symbol": SPOT,
            "side": "buy",
            "price": price,
            "amount": amount,
            "cost": amount * price,
            "fee": {"cost": 0.0, "currency": "USDT"},
            "timestamp": 1,
        },
        "live",
    )


def _rows(snap) -> list[dict]:
    return [item for item in snap.positions if item["symbol"] == SPOT]


def test_ledger_and_own_coins_show_up_as_two_rows(tmp_path):
    """钱包 1.0025、账本 0.0025：账本一行（平仓）+ 自有余额一行（卖出）。"""
    venue = _venue(1.0025)
    service, db = _account(tmp_path, venue)
    try:
        _seed_buy(db, 0.0025)
        snap = service.snapshot(MarketType.SPOT, SPOT)
        rows = _rows(snap)
        assert len(rows) == 2, rows

        ledger_row = rows[0]
        assert ledger_row["origin"] == "ledger"
        # 这一行的数量就是账本数量，不是钱包事实。
        assert ledger_row["contracts"] == pytest.approx(0.0025)
        assert ledger_row["ledger_size"] == pytest.approx(0.0025)
        assert ledger_row["entry_price"] == pytest.approx(ENTRY)
        assert ledger_row["unrealized_basis"] == "ledger"
        # (81000 - 80000) * 0.0025 = 2.5
        assert ledger_row["unrealized"] == pytest.approx(2.5)
        assert ledger_row["quote_value"] == pytest.approx(0.0025 * PRICE)

        own_row = rows[1]
        assert own_row["origin"] == "wallet"
        assert own_row["contracts"] == pytest.approx(1.0)
        assert own_row["entry_price"] == 0.0
        assert own_row["unrealized"] == 0.0
        assert own_row["unrealized_basis"] == ""
        assert own_row["ledger_size"] == 0.0
        assert own_row["quote_value"] == pytest.approx(1.0 * PRICE)

        # 两行加起来就是钱包事实，一分钱都不多不少。
        assert sum(row["contracts"] for row in rows) == pytest.approx(1.0025)
        assert sum(row["quote_value"] for row in rows) == pytest.approx(1.0025 * PRICE)
        # 钱包里「不是我们买的」那部分单独记账，仍然是自有的一整份。
        assert snap.wallet_value == pytest.approx(1.0 * PRICE)
    finally:
        db.close()


def test_only_the_ledger_means_no_wallet_row(tmp_path):
    """钱包 0.5、账本也是 0.5：只有账本行，没有第二行。"""
    venue = _venue(0.5)
    service, db = _account(tmp_path, venue)
    try:
        _seed_buy(db, 0.5)
        rows = _rows(service.snapshot(MarketType.SPOT, SPOT))
        assert len(rows) == 1, rows
        assert rows[0]["origin"] == "ledger"
        assert rows[0]["contracts"] == pytest.approx(0.5)
    finally:
        db.close()


def test_only_own_coins_stay_exactly_one_wallet_row(tmp_path):
    """没有成交账本：和以前一样，一行自有余额，按钮「卖出」。"""
    venue = _venue(1.0)
    service, db = _account(tmp_path, venue)
    try:
        rows = _rows(service.snapshot(MarketType.SPOT, SPOT))
        assert len(rows) == 1, rows
        assert rows[0]["origin"] == "wallet"
        assert rows[0]["contracts"] == pytest.approx(1.0)
        assert rows[0]["entry_price"] == 0.0
        assert rows[0]["unrealized"] == 0.0
    finally:
        db.close()


def test_wallet_below_the_ledger_caps_the_ledger_row(tmp_path):
    """账本说 2 BTC、钱包只有 0.5（币被提走了）：账本行封顶 0.5，也不出空的自有行。"""
    venue = _venue(0.5)
    service, db = _account(tmp_path, venue)
    try:
        _seed_buy(db, 2.0)
        rows = _rows(service.snapshot(MarketType.SPOT, SPOT))
        assert len(rows) == 1, rows
        assert rows[0]["origin"] == "ledger"
        assert rows[0]["contracts"] == pytest.approx(0.5)
        assert rows[0]["ledger_size"] == pytest.approx(0.5)
        # 盈亏也只按封顶后的数量算：1000 * 0.5 = 500
        assert rows[0]["unrealized"] == pytest.approx(500.0)
    finally:
        db.close()


def test_the_split_does_not_change_equity(tmp_path):
    """拆行只是把一行拆成两行，权益、现金、持仓市值都不许动。"""
    venue = _venue(1.0025)
    service, db = _account(tmp_path, venue)
    try:
        _seed_buy(db, 0.0025)
        snap = service.snapshot(MarketType.SPOT, SPOT)
        assert snap.equity == pytest.approx(1.0025 * PRICE)
        assert snap.cash == pytest.approx(0.0)
    finally:
        db.close()


def test_futures_rows_are_untouched(tmp_path):
    """合约侧一行不动：没有 origin 字段，数量就是交易所那份持仓。"""
    venue = _venue(0.0)
    venue._last["BTC/USDT:USDT"] = PRICE
    venue.create_order(MarketType.USDM, "BTC/USDT:USDT", "market", "buy", 0.01, PRICE)
    service, db = _account(tmp_path, venue)
    try:
        snap = service.snapshot(MarketType.USDM)
        rows = snap.positions
        assert len(rows) == 1, rows
        assert "origin" not in rows[0]
        assert rows[0]["contracts"] == pytest.approx(0.01)
    finally:
        db.close()