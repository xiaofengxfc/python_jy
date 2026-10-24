"""成交账本是一份 JSON：读-改-写必须串起来。

两个写者都是真实存在的：5 秒一轮的同步线程（``sync_live_fills``），和「下单/平仓
之后立刻补记一次」的请求线程（``sync_symbol_fills`` → ``_book_the_fill``）。
没有锁时后写者会把先写者刚记下的 ``seen`` 与 ``books`` 整份覆盖掉，那几笔成交等于
没记过，下一轮重新 book 一遍并再调一次 ``risk.record_trade`` —— 当日亏损与连亏
熔断会把同一笔平仓算两次。
"""

from __future__ import annotations

import threading

import pytest

from btrader.core.config import AppSettings, RiskSettings
from btrader.core.storage import Database
from btrader.core.types import MarketType
from btrader.engine import trading as trading_module
from btrader.engine.risk import RiskManager
from btrader.engine.trading import TradingService

from fakes import FakeVenue

SPOT = "BTC/USDT"


def _trading(tmp_path):
    settings = AppSettings(
        risk=RiskSettings(
            max_order_quote=0,
            max_position_quote=0,
            max_leverage=20,
            max_daily_trades=0,
            max_daily_loss=0,
            max_consecutive_losses=0,
        )
    )
    venue = FakeVenue()
    venue._last[SPOT] = 100.0
    db = Database(tmp_path / "ledger.db")
    return TradingService(settings, venue, RiskManager(settings.risk), db), db


def _fill(trade_id: str, side: str, amount: float) -> dict:
    return {
        "id": trade_id,
        "symbol": SPOT,
        "side": side,
        "amount": amount,
        "price": 100.0,
        "timestamp": 1_700_000_000_000,
        "fee": {"cost": 0.0, "currency": "USDT"},
    }


def test_the_ledger_write_happens_under_the_ledger_lock(tmp_path):
    """写盘那一刻必须已经握着账本锁，否则「先读后写」之间就能被插进来。"""
    trading, db = _trading(tmp_path)
    try:
        trading.db.set_json(trading._ledger_key(), {"seen": [], "books": {}})
        owned: list[bool] = []
        original = trading._save_live_ledger
        owned_probe = getattr(trading._ledger_lock, "_is_owned", None)
        if owned_probe is None:  # pragma: no cover - 只在非 CPython 上发生
            pytest.skip("这个解释器的锁不支持 _is_owned 探针")

        def spy(ledger: dict) -> None:
            owned.append(bool(owned_probe()))
            original(ledger)

        trading._save_live_ledger = spy  # type: ignore[method-assign]
        trading.rest.fetch_my_trades = lambda market, symbol, limit=50: [_fill("t1", "buy", 0.5)]
        trading._sync_pairs([(MarketType.SPOT, SPOT)])

        assert owned == [True], "保存账本时还没拿到 _ledger_lock"
    finally:
        db.close()


def test_concurrent_syncs_never_lose_a_fill(tmp_path):
    """8 个线程各同步一笔成交：8 笔都要留在 ``seen`` 里，谁也别覆盖谁。"""
    trading, db = _trading(tmp_path)
    try:
        trading.db.set_json(trading._ledger_key(), {"seen": [], "books": {}})
        count = 8
        barrier = threading.Barrier(count)
        errors: list[BaseException] = []

        def worker(index: int) -> None:
            def fetch(market, symbol, limit=50):
                # 让所有人都在「已经取回成交、准备记账」这一步对齐，最大化重叠
                barrier.wait(timeout=10)
                return [_fill(f"t{index}", "buy", 0.1)]

            try:
                trading.rest.fetch_my_trades = fetch  # 同实例共用，取回来的内容由闭包决定
                trading._sync_pairs([(MarketType.SPOT, SPOT)])
            except BaseException as exc:  # noqa: BLE001 - 线程里的异常要带回主线程
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        assert not errors, errors
        seen = trading._live_ledger()["seen"]
        assert len(seen) == count, f"账本丢了成交：{sorted(seen)}"
        # 账本数量也要对上：8 × 0.1
        assert trading._live_ledger()["books"][SPOT]["contracts"] == pytest.approx(0.8)
    finally:
        db.close()


def test_the_seen_window_keeps_the_newest_ids(tmp_path, monkeypatch):
    """去重键是一份「最近 N 条」的窗口，截断要截掉旧的。

    以前那句是 list(set(...))：顺序任意，还跟着 PYTHONHASHSEED 变。超过上限之后
    可能把较新的键截掉、留下更旧的，下一轮同步就把那笔成交重新记一次账，
    风控的当日亏损与连亏跟着多数一次。
    """
    trading, db = _trading(tmp_path)
    try:
        trading.db.set_json(trading._ledger_key(), {"seen": [], "books": {}})
        monkeypatch.setattr(trading_module, "LIVE_TRADE_LIMIT", 3)
        for index in range(5):
            trading.rest.fetch_my_trades = (
                lambda market, symbol, limit=50, i=index: [_fill(f"t{i}", "buy", 0.1)]
            )
            trading._sync_pairs([(MarketType.SPOT, SPOT)])

        assert trading._live_ledger()["seen"] == ["spot:t2", "spot:t3", "spot:t4"]
    finally:
        db.close()
