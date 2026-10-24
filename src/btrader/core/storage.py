"""SQLite metadata store for orders, trades, runs and account snapshots."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

from .. import paths
from .types import market_for_symbol

log = logging.getLogger(__name__)

# v1：orders / trades 的主键从 (id) 改成 (account, market_type, id)。
# 币安现货、U 本位、币本位各有一套成交编号，撞号是常态而不是意外：同一个 id 的
# 两笔成交在 (id) 这个主键上互相覆盖，剩下的那一行属于哪个市场已经无从得知。
# 旧行的 market_type 从来没记过，靠 symbol 写法反推也只是猜，所以整表丢弃。
SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id TEXT NOT NULL,
    account TEXT NOT NULL,
    market_type TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    type TEXT NOT NULL,
    price REAL,
    amount REAL,
    filled REAL,
    cost REAL,
    status TEXT,
    mode TEXT,
    created_at INTEGER,
    updated_at INTEGER,
    raw TEXT,
    PRIMARY KEY (account, market_type, id)
);
CREATE INDEX IF NOT EXISTS idx_orders_symbol ON orders(account, market_type, symbol, created_at DESC);

CREATE TABLE IF NOT EXISTS trades (
    id TEXT NOT NULL,
    account TEXT NOT NULL,
    market_type TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT,
    price REAL,
    amount REAL,
    cost REAL,
    fee REAL,
    fee_currency TEXT,
    timestamp INTEGER,
    mode TEXT,
    raw TEXT,
    PRIMARY KEY (account, market_type, id)
);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(account, market_type, symbol, timestamp DESC);

CREATE TABLE IF NOT EXISTS backtest_runs (
    id TEXT PRIMARY KEY,
    created_at INTEGER,
    symbol TEXT,
    market_type TEXT,
    timeframe TEXT,
    strategy TEXT,
    params TEXT,
    start TEXT,
    end TEXT,
    metrics TEXT
);

CREATE TABLE IF NOT EXISTS settings_kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def _scope_where(account: str | None, market_type: str | None) -> tuple[str, list[str]]:
    """WHERE 片段 + 参数，供所有历史查询共用。

    主网与测试网、现货与合约的成交都写在同一张表里，要读出「这个账户这个市场的
    那几条」只能靠这两个条件。缺一个就会把另一侧的行混进列表——面板上看到的就是
    「测试网的成交里出现了主网的数量」，而列表本身不说哪个数属于谁。
    """
    clauses: list[str] = []
    params: list[str] = []
    if account:
        clauses.append("account = ?")
        params.append(account)
    if market_type:
        clauses.append("market_type = ?")
        params.append(market_type)
    return ("WHERE " + " AND ".join(clauses)) if clauses else "", params


class Database:
    """Thread-safe thin wrapper around sqlite3."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or paths.database_file()
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            # 先迁移再建表：v0 的 trades 没有 market_type，直接跑 SCHEMA 里的
            # CREATE INDEX 会撞上 "no such column"，整个库打不开。
            self._migrate()
            self._conn.executescript(SCHEMA)

    def _migrate(self) -> None:
        """Bring an existing file up to SCHEMA_VERSION.

        v0 的 orders / trades 主键只有 id，trades 连 market_type 都没有。改主键
        在 SQLite 里只能重建表，而重建时没有任何一列能说清旧行属于哪个市场——
        撞号的那一行早就被覆盖掉了。

        旧表**改名留档**，不 DROP：对外发布之后，用户库里那份成交历史就算读不出来，
        也不该被一次启动悄悄删掉。新表由紧随其后的 SCHEMA 建出来，backtest_runs 与
        settings_kv 一个字节不动。
        """
        version = int(self._conn.execute("PRAGMA user_version").fetchone()[0] or 0)
        if version >= SCHEMA_VERSION:
            return
        statements = []
        for table in ("orders", "trades"):
            if self._table_exists(table):
                statements.append(
                    "ALTER TABLE %s RENAME TO %s;" % (table, self._backup_name(table))
                )
        statements.append("PRAGMA user_version = %d;" % SCHEMA_VERSION)
        self._conn.executescript("\n".join(statements))
        self._conn.commit()
        log.info(
            "数据库升级到 v%d：orders / trades 重建，旧表留档为 *_v0_backup", SCHEMA_VERSION
        )

    def _table_exists(self, name: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        return row is not None

    def _backup_name(self, table: str) -> str:
        """给旧表找一个没被占的备份名：搬到一半断电、再来一次时不会撞名。"""
        candidate = "%s_v0_backup" % table
        suffix = 2
        while self._table_exists(candidate):
            candidate = "%s_v0_backup_%d" % (table, suffix)
            suffix += 1
        return candidate

    @contextmanager
    def cursor(self):
        """Run a statement block in one transaction.

        A failure rolls the transaction back before re-raising. Without this,
        the connection kept the partially applied writes open and the next
        commit() — from an unrelated save — flushed them to disk.
        """
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            finally:
                cur.close()

    def executemany(self, sql: str, rows: Iterable[tuple]) -> None:
        with self.cursor() as cur:
            cur.executemany(sql, rows)

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(sql, params)
            try:
                return cur.fetchall()
            finally:
                cur.close()

    def save_order(self, account: str, market_type: str, order: dict, mode: str) -> None:
        raw = json.dumps(order, default=str, ensure_ascii=False)
        with self.cursor() as cur:
            cur.execute(
                """INSERT INTO orders (id, account, market_type, symbol, side, type,
                       price, amount, filled, cost, status, mode, created_at, updated_at, raw)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(account, market_type, id) DO UPDATE SET
                       filled=excluded.filled, cost=excluded.cost, status=excluded.status,
                       updated_at=excluded.updated_at, raw=excluded.raw""",
                (
                    str(order.get("id", "")),
                    account,
                    market_type,
                    order.get("symbol", ""),
                    order.get("side", ""),
                    order.get("type", ""),
                    order.get("price"),
                    order.get("amount"),
                    order.get("filled"),
                    order.get("cost"),
                    order.get("status"),
                    mode,
                    order.get("timestamp") or 0,
                    order.get("lastTradeTimestamp") or order.get("timestamp") or 0,
                    raw,
                ),
            )

    def save_trade(
        self,
        account: str,
        trade: dict,
        mode: str,
        market_type: str | None = None,
    ) -> None:
        """Store one fill, scoped by account and market.

        market_type 缺省时按 symbol 写法推（BTC/USDT 现货、BTC/USDT:USDT U 本位）：
        主键里就有它，留空等于让另一侧的同号成交顶掉这一条，所以宁可从 symbol 现推。
        """
        market = market_type or market_for_symbol(str(trade.get("symbol") or "")).value
        raw = json.dumps(trade, default=str, ensure_ascii=False)
        with self.cursor() as cur:
            cur.execute(
                """INSERT OR REPLACE INTO trades
                   (id, account, market_type, symbol, side, price, amount, cost, fee,
                    fee_currency, timestamp, mode, raw)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(trade.get("id", "")),
                    account,
                    market,
                    trade.get("symbol", ""),
                    trade.get("side", ""),
                    trade.get("price"),
                    trade.get("amount"),
                    trade.get("cost"),
                    (trade.get("fee") or {}).get("cost") if isinstance(trade.get("fee"), dict) else trade.get("fee"),
                    (trade.get("fee") or {}).get("currency") if isinstance(trade.get("fee"), dict) else trade.get("feeCurrency"),
                    trade.get("timestamp") or 0,
                    mode,
                    raw,
                ),
            )

    def save_backtest(self, run: dict[str, Any]) -> None:
        with self.cursor() as cur:
            cur.execute(
                """INSERT OR REPLACE INTO backtest_runs
                   (id, created_at, symbol, market_type, timeframe, strategy, params,
                    start, end, metrics)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    run["id"],
                    run["created_at"],
                    run["symbol"],
                    run["market_type"],
                    run["timeframe"],
                    run["strategy"],
                    json.dumps(run.get("params", {}), ensure_ascii=False),
                    run.get("start", ""),
                    run.get("end", ""),
                    json.dumps(run.get("metrics", {}), ensure_ascii=False),
                ),
            )

    def recent_orders(
        self,
        limit: int = 200,
        *,
        account: str | None = None,
        market_type: str | None = None,
    ) -> list[sqlite3.Row]:
        where, params = _scope_where(account, market_type)
        return self.query(
            f"SELECT * FROM orders {where} ORDER BY created_at DESC LIMIT ?",
            (*params, limit),
        )

    def recent_trades(
        self,
        limit: int = 100,
        *,
        mode: str | None = None,
        account: str | None = None,
        market_type: str | None = None,
    ) -> list[sqlite3.Row]:
        """Stored fills, newest first.

        The mode filter picks the account a fill came from: the venue ledger
        ("live") is what the app shows now, while rows left over from the
        removed paper account stay readable for comparison. account and
        market_type are the scope the caller is looking at: without them a
        testnet session reads the mainnet fills as its own history.
        """
        where, params = _scope_where(account, market_type)
        if mode:
            where = (where + " AND mode = ?") if where else "WHERE mode = ?"
            params.append(mode)
        return self.query(
            f"SELECT * FROM trades {where} ORDER BY timestamp DESC LIMIT ?",
            (*params, limit),
        )

    def recent_backtests(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM backtest_runs ORDER BY created_at DESC LIMIT ?", (limit,)
        )

    # ------------------------------------------------------------- key/value
    def get_value(self, key: str) -> str | None:
        rows = self.query("SELECT value FROM settings_kv WHERE key = ?", (key,))
        return rows[0]["value"] if rows else None

    def set_value(self, key: str, value: str) -> None:
        with self.cursor() as cur:
            cur.execute(
                "INSERT INTO settings_kv (key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def delete_value(self, key: str) -> None:
        with self.cursor() as cur:
            cur.execute("DELETE FROM settings_kv WHERE key = ?", (key,))

    def adopt_legacy_key(self, legacy: str, scoped: str) -> bool:
        """把旧版没有作用域的 key 搬进现在按作用域命名的 key。

        0.6.3 的成交账本与风控状态各只存一份（live.ledger.v1 / risk.state.v1），
        0.6.4 起按网络分开。升级之后不搬，老用户第一眼就是「账本空了」——现货仓位
        归零、平仓接口 400、当日额度也跟着复位。只在目标 key 不存在时搬，旧 key
        原样留着：那是证据，也是万一 key 命名再变时的退路。
        """
        if self.get_value(scoped) or not self.get_value(legacy):
            return False
        with self.cursor() as cur:
            cur.execute(
                "INSERT OR IGNORE INTO settings_kv (key, value) "
                "SELECT ?, value FROM settings_kv WHERE key = ?",
                (scoped, legacy),
            )
        log.info("已把旧存储项 %s 搬到 %s", legacy, scoped)
        return True

    def get_json(self, key: str) -> Any:
        raw = self.get_value(key)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            log.warning("设置项 %s 不是合法 JSON，已忽略", key)
            return None

    def set_json(self, key: str, value: Any) -> None:
        self.set_value(key, json.dumps(value, ensure_ascii=False, default=str))

    def close(self) -> None:
        with self._lock:
            self._conn.close()
