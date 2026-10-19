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

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
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
    raw TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_symbol ON orders(account, symbol, created_at DESC);

CREATE TABLE IF NOT EXISTS trades (
    id TEXT PRIMARY KEY,
    account TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT,
    price REAL,
    amount REAL,
    cost REAL,
    fee REAL,
    fee_currency TEXT,
    timestamp INTEGER,
    mode TEXT,
    raw TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(account, symbol, timestamp DESC);

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


class Database:
    """Thread-safe thin wrapper around sqlite3."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or paths.database_file()
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA)

    @contextmanager
    def cursor(self):
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
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
                   ON CONFLICT(id) DO UPDATE SET
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

    def save_trade(self, account: str, trade: dict, mode: str) -> None:
        raw = json.dumps(trade, default=str, ensure_ascii=False)
        with self.cursor() as cur:
            cur.execute(
                """INSERT OR REPLACE INTO trades
                   (id, account, symbol, side, price, amount, cost, fee, fee_currency,
                    timestamp, mode, raw)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(trade.get("id", "")),
                    account,
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

    def recent_orders(self, limit: int = 200) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM orders ORDER BY created_at DESC LIMIT ?", (limit,)
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
