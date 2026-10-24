"""持仓的两件事：合约读不到凭证，现货把钱包余额当成交持仓。

两个都是现场看到的：切过市场之后合约页报「该市场尚未配置 API Key」，
现货持仓表里冒出几十个没买过的币，成本价 0、盈亏 0。
"""

from __future__ import annotations

import pytest

from btrader.core.credentials import CredentialStore, Credentials, account_id
from btrader.core.storage import Database
from btrader.core.types import MarketType, Network
from btrader.engine.account import AccountService

from fakes import FakeVenue


class MemoryKeyring:
    """替代系统密钥环：字典存，测试不落盘。"""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, name: str):
        return self.items.get((service, name))

    def set_password(self, service: str, name: str, value: str) -> None:
        self.items[(service, name)] = value

    def delete_password(self, service: str, name: str) -> None:
        self.items.pop((service, name), None)


@pytest.fixture
def app_ctx(monkeypatch, tmp_path):
    """真的 AppContext，数据目录和密钥环都换成临时的。"""
    monkeypatch.setenv("BITRADER_DATA_DIR", str(tmp_path))
    from btrader import context as context_module

    backend = MemoryKeyring()
    monkeypatch.setattr(
        context_module, "CredentialStore", lambda: CredentialStore(backend=backend)
    )
    ctx = context_module.AppContext()
    try:
        yield ctx
    finally:
        ctx.db.close()


def test_a_saved_key_reaches_the_client_the_account_reads(app_ctx) -> None:
    """保存 key 之后，账户用的那个客户端必须换上新 key。

    老实现切换市场时把客户端缓存清空，另建了一个实例存进去；保存 key 只更新
    了 self.rest，账户读的还是缓存那份空凭证，于是持仓永远是空的。
    """
    ctx = app_ctx
    ctx.set_network(Network.TESTNET, persist=False)
    ctx.set_market_type(MarketType.USDM, persist=False)
    assert ctx.rest_for(MarketType.USDM) is ctx.rest
    assert not ctx.rest.has_credentials

    assert ctx.save_credentials("key-1234567890", "secret-1234567890") is True

    assert ctx.rest_for(MarketType.USDM).has_credentials
    assert ctx.rest_for(MarketType.USDM) is ctx.rest


def test_switching_markets_keeps_each_slot_on_its_own_key(app_ctx) -> None:
    """切到现货再切回来，合约那一格还在，不要串。"""
    ctx = app_ctx
    ctx.set_network(Network.TESTNET, persist=False)

    ctx.set_market_type(MarketType.USDM, persist=False)
    ctx.save_credentials("futures-key-1", "futures-secret-1")
    ctx.set_market_type(MarketType.SPOT, persist=False)
    ctx.save_credentials("spot-key-1", "spot-secret-1")
    ctx.set_market_type(MarketType.USDM, persist=False)

    assert ctx.creds.api_key == "futures-key-1"
    assert ctx.rest.has_credentials


def _service(tmp_path, venue: FakeVenue) -> tuple[AccountService, Database]:
    db = Database(tmp_path / "account.db")
    service = AccountService(
        rest_for=lambda market: venue,
        chart_rest_for=lambda market: venue,
        db=db,
        network=Network.TESTNET,
    )
    return service, db


def _buy(db: Database, symbol: str, amount: float, price: float) -> None:
    db.save_trade(
        account_id(Network.TESTNET),
        {
            "id": f"t-{symbol}-{amount}",
            "symbol": symbol,
            "side": "buy",
            "price": price,
            "amount": amount,
            "cost": amount * price,
            "fee": 0.0,
            "timestamp": 1_700_000_000_000,
        },
        "live",
    )


def test_spot_coins_without_a_fill_are_marked_as_wallet(tmp_path) -> None:
    venue = FakeVenue()
    venue.balances[MarketType.SPOT] = {"USDT": 1_000.0, "BTC": 0.5, "USDC": 10_000.0}
    venue._last["BTC/USDT"] = 100.0
    venue._last["USDC/USDT"] = 1.0
    service, db = _service(tmp_path, venue)
    try:
        _buy(db, "BTC/USDT", 0.5, 90.0)

        snap = service.snapshot(MarketType.SPOT)
        by_symbol = {item["symbol"]: item for item in snap.positions}

        assert by_symbol["BTC/USDT"]["origin"] == "ledger"
        assert by_symbol["BTC/USDT"]["entry_price"] == pytest.approx(90.0)
        assert by_symbol["BTC/USDT"]["unrealized"] == pytest.approx(5.0)
        assert by_symbol["USDC/USDT"]["origin"] == "wallet"
        assert by_symbol["USDC/USDT"]["unrealized"] == pytest.approx(0.0)
        # 买来的排前面，钱包余额排后面。
        assert snap.positions[0]["symbol"] == "BTC/USDT"
        assert snap.wallet_value == pytest.approx(10_000.0)
    finally:
        db.close()


def test_wallet_coins_still_count_towards_equity(tmp_path) -> None:
    """钱包里的币不是持仓，但钱确实在账上，权益要算上。"""
    venue = FakeVenue()
    venue.balances[MarketType.SPOT] = {"USDT": 1_000.0, "USDC": 500.0}
    venue._last["USDC/USDT"] = 1.0
    service, db = _service(tmp_path, venue)
    try:
        snap = service.snapshot(MarketType.SPOT)

        assert snap.cash == pytest.approx(1_000.0)
        assert snap.wallet_value == pytest.approx(500.0)
        assert snap.equity == pytest.approx(1_500.0)
    finally:
        db.close()


def test_futures_positions_are_all_ledger(tmp_path) -> None:
    """合约持仓来自交易所，没有钱包那一类。"""
    venue = FakeVenue()
    venue._last["BTC/USDT:USDT"] = 100.0
    venue.create_order(MarketType.USDM, "BTC/USDT:USDT", "market", "buy", 1.0, 100.0)
    service, db = _service(tmp_path, venue)
    try:
        snap = service.snapshot(MarketType.USDM)

        assert snap.positions
        assert all("origin" not in item for item in snap.positions)
        assert snap.wallet_value == 0.0
    finally:
        db.close()
