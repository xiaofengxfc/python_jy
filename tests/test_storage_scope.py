"""存储作用域：主网/测试网、现货与合约各存各的。

这六条钉住的都是同一类事故：key 或主键少了一个维度，另一侧的数据被当成自己的。
修复前的表现是「切到测试网仍看到主网仓位」「成交列表少了记录」「K 线串台」，
而界面上没有任何一句话会说是共享存储造成的。
"""

from __future__ import annotations

import sqlite3

import pytest

from btrader.core.config import AppSettings, RiskSettings
from btrader.core.credentials import account_id
from btrader.core.storage import SCHEMA_VERSION, Database
from btrader.core.types import MarketType, Network
from btrader.engine.risk import RiskManager, risk_state_key
from btrader.engine.trading import TradingService, ledger_key
from btrader.exchange.klines import KlineStore

from fakes import FakeVenue

SPOT = "BTC/USDT"
FUTURES = "BTC/USDT:USDT"


def _service(db: Database, network: Network) -> TradingService:
    settings = AppSettings(network=network.value)
    venue = FakeVenue(network=network)
    return TradingService(
        settings,
        venue,
        RiskManager(settings.risk, db, scope=network.value),
        db,
        notifier=None,
    )


# --------------------------------------------------------------- 成交账本
def test_the_ledger_is_stored_per_network(tmp_path):
    """主网的 books 不能被测试网读到：切过去看到的是自己的空账本。"""
    db = Database(tmp_path / "ledger.db")
    try:
        mainnet = _service(db, Network.MAINNET)
        mainnet._save_live_ledger(
            {
                "seen": ["spot:1"],
                "books": {SPOT: {"contracts": 0.5, "entry": 100.0}},
            }
        )

        ledger = _service(db, Network.TESTNET)._live_ledger()
        assert ledger["books"] == {}
        assert ledger["seen"] == []
        assert ledger_key(Network.MAINNET) != ledger_key(Network.TESTNET)

        # 切回主网，那一份还在
        books = mainnet._live_ledger()["books"]
        assert books[SPOT]["contracts"] == pytest.approx(0.5)
    finally:
        db.close()


def test_one_venue_fill_id_is_tracked_per_market(tmp_path):
    """现货与合约的成交编号各自从 1 数起：撞号时两笔都要进账本。"""
    db = Database(tmp_path / "sync.db")
    venue = FakeVenue(network=Network.TESTNET)
    venue.fills = [
        {"id": "1", "symbol": SPOT, "side": "buy", "price": 100.0, "amount": 0.5,
         "fee": {"cost": 0.0}, "timestamp": 1},
        {"id": "1", "symbol": FUTURES, "side": "buy", "price": 100.0, "amount": 0.002,
         "fee": {"cost": 0.0}, "timestamp": 2},
    ]
    settings = AppSettings(network=Network.TESTNET.value)
    trading = TradingService(
        settings, venue, RiskManager(settings.risk, db), db, notifier=None
    )
    try:
        trading._sync_pairs([(MarketType.SPOT, SPOT), (MarketType.USDM, FUTURES)])
        books = trading._live_ledger()["books"]
        assert books[SPOT]["contracts"] == pytest.approx(0.5)
        assert books[FUTURES]["contracts"] == pytest.approx(0.002)
    finally:
        db.close()


# ----------------------------------------------------------------- 成交表
def test_one_fill_id_cannot_overwrite_another_market(tmp_path):
    """主键里有市场：同一个 id 的现货与合约成交是两行，不是一行彼此覆盖。"""
    db = Database(tmp_path / "trades.db")
    try:
        for symbol in (SPOT, FUTURES):
            db.save_trade(
                account_id(Network.TESTNET),
                {"id": "7", "symbol": symbol, "side": "buy", "price": 100.0,
                 "amount": 1.0, "timestamp": 1},
                "live",
            )
        rows = db.recent_trades(mode="live", account=account_id(Network.TESTNET))
        assert len(rows) == 2
        assert sorted(row["market_type"] for row in rows) == ["spot", "usdm"]
    finally:
        db.close()


def test_the_history_does_not_mix_networks(tmp_path):
    """读成交历史要带账户：测试网不该在主网的成交里看到自己。"""
    db = Database(tmp_path / "history.db")
    try:
        db.save_trade(
            account_id(Network.MAINNET),
            {"id": "1", "symbol": SPOT, "side": "buy", "price": 1.0, "amount": 1.0,
             "timestamp": 1},
            "live",
        )
        db.save_trade(
            account_id(Network.TESTNET),
            {"id": "2", "symbol": SPOT, "side": "buy", "price": 1.0, "amount": 1.0,
             "timestamp": 2},
            "live",
        )
        rows = db.recent_trades(mode="live", account=account_id(Network.TESTNET))
        assert [row["id"] for row in rows] == ["2"]
    finally:
        db.close()


# ------------------------------------------------------------------- 迁移
def test_the_migration_rebuilds_the_unscoped_tables(tmp_path):
    """v0 的库打开后自动重建：旧成交丢弃（口径已确认），配置和回测历史留着。"""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE orders (
            id TEXT PRIMARY KEY, account TEXT NOT NULL, market_type TEXT NOT NULL,
            symbol TEXT NOT NULL, side TEXT NOT NULL, type TEXT NOT NULL,
            price REAL, amount REAL, filled REAL, cost REAL, status TEXT, mode TEXT,
            created_at INTEGER, updated_at INTEGER, raw TEXT
        );
        CREATE TABLE trades (
            id TEXT PRIMARY KEY, account TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT,
            price REAL, amount REAL, cost REAL, fee REAL, fee_currency TEXT,
            timestamp INTEGER, mode TEXT, raw TEXT
        );
        CREATE TABLE settings_kv (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO trades (id, account, symbol) VALUES ('1', 'binance:mainnet', 'BTC/USDT');
        INSERT INTO settings_kv (key, value) VALUES ('keep', '"yes"');
        """
    )
    conn.commit()
    conn.close()

    db = Database(path)
    try:
        assert db.recent_trades() == []
        assert db.get_value("keep") == '"yes"'
        columns = {row["name"] for row in db.query("PRAGMA table_info(trades)")}
        assert "market_type" in columns
        assert db.query("PRAGMA user_version")[0][0] == SCHEMA_VERSION
        # 新表能正常写入，且按市场区分
        db.save_trade(
            account_id(Network.MAINNET),
            {"id": "9", "symbol": FUTURES, "side": "buy", "price": 1.0, "amount": 1.0,
             "timestamp": 1},
            "live",
        )
        assert [row["market_type"] for row in db.recent_trades()] == ["usdm"]
    finally:
        db.close()


# ------------------------------------------------------------------- 风控
def test_the_daily_state_is_stored_per_network(tmp_path):
    """熔断是账户级：测试网的亏损不该用掉主网的当日额度，反之亦然。"""
    db = Database(tmp_path / "risk.db")
    settings = RiskSettings(max_daily_loss=100, max_consecutive_losses=1)
    try:
        mainnet = RiskManager(settings, db, scope=Network.MAINNET.value)
        mainnet.record_trade(-150.0)
        assert not mainnet.check_order(10, 0, 10)

        testnet = RiskManager(settings, db, scope=Network.TESTNET.value)
        assert testnet.check_order(10, 0, 10), "测试网不该继承主网的熔断"

        restarted = RiskManager(settings, db, scope=Network.MAINNET.value)
        assert not restarted.check_order(10, 0, 10), "主网那一侧重启后仍要熔断"
        stored = db.get_json(risk_state_key(Network.MAINNET.value))
        assert stored["realized_pnl"] == pytest.approx(-150.0)
    finally:
        db.close()


# ----------------------------------------------------------------- K 线缓存
def test_the_kline_cache_is_partitioned_by_source(tmp_path):
    """缓存目录要跟着数据源走：换网络/换交易所不能读同一份 parquet。"""
    store = KlineStore(FakeVenue(), tmp_path / "klines", source="binance-mainnet")
    mainnet_path = store._path(MarketType.USDM, FUTURES, "1h")
    store.set_source("binance-testnet")
    testnet_path = store._path(MarketType.USDM, FUTURES, "1h")

    assert mainnet_path != testnet_path
    assert "binance-mainnet" in mainnet_path.parts
    assert "binance-testnet" in testnet_path.parts
    # 换源之后刷新时间表要清空，否则会拿旧的刷新时间当「刚刚拉过」
    assert store._refreshed == {}
