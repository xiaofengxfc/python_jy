"""Fill backfill: exchange trades must reach the risk manager once.

The daily-loss and losing-streak breakers can only see an exit that is reported
to them, and a venue-side stop is never seen by the submitting process. These
tests pin down that path: the cost book derives a closing PnL from the fills,
and the persisted ledger keeps repeated polling (and a restart) from charging
the same exit twice.
"""

from __future__ import annotations

import pytest

from btrader.core.config import AppSettings, RiskSettings
from btrader.core.storage import Database
from btrader.core.types import MarketType
from btrader.engine.risk import RiskManager
from btrader.engine.trading import TradingService
from btrader.notify import NotifySettings, WeChatNotifier


class FakeREST:
    """Stands in for BinanceREST: only the endpoints the sync touches."""

    def __init__(self, trades: dict[str, list[dict]]) -> None:
        self.has_credentials = True
        self.trades = trades
        self.calls = 0

    def fetch_open_orders(self, market, symbol=None):  # noqa: ARG002
        return []

    def fetch_positions(self, market, symbols=None):  # noqa: ARG002
        return []

    def fetch_my_trades(self, market, symbol, limit=50):  # noqa: ARG002
        self.calls += 1
        return list(self.trades.get(symbol, []))


def _service(tmp_path, rest: FakeREST, db: Database | None = None) -> tuple[TradingService, Database]:
    settings = AppSettings(risk=RiskSettings(max_order_quote=0, max_position_quote=0))
    db = db or Database(tmp_path / "live.db")
    service = TradingService(
        settings,
        rest,
        RiskManager(settings.risk),
        db,
    )
    return service, db


def _trade(trade_id: str, side: str, amount: float, price: float) -> dict:
    return {
        "id": trade_id,
        "symbol": "BTC/USDT",
        "side": side,
        "amount": amount,
        "price": price,
        "timestamp": int(trade_id) * 1000,
        "fee": {"cost": 0.0, "currency": "USDT"},
    }


def test_live_round_trip_feeds_risk(tmp_path):
    """A buy then a sell at a higher price must register the profit."""
    rest = FakeREST(
        {
            "BTC/USDT": [
                _trade("1", "buy", 1.0, 100.0),
                _trade("2", "sell", 1.0, 110.0),
            ]
        }
    )
    service, db = _service(tmp_path, rest)

    booked = service.sync_live_fills([__import__("btrader.core.types", fromlist=["MarketType"]).MarketType.SPOT])

    assert len(booked) == 1
    assert booked[0]["realized"] == pytest.approx(10.0)
    assert service.risk.state.realized_pnl == pytest.approx(10.0)
    assert service.risk.state.consecutive_losses == 0

    # Both fills were persisted, so the history survives a restart too.
    rows = db.query("SELECT id FROM trades ORDER BY id")
    assert [row["id"] for row in rows] == ["1", "2"]
    db.close()


def test_live_exit_feeds_daily_loss_breaker(tmp_path):
    """The point of the whole feature: a live loss can trip the breaker."""
    rest = FakeREST(
        {
            "BTC/USDT": [
                _trade("1", "buy", 1.0, 100.0),
                _trade("2", "sell", 1.0, 90.0),
            ]
        }
    )
    service, db = _service(tmp_path, rest)
    service.risk.settings.max_daily_loss = 5.0
    from btrader.core.types import MarketType

    service.sync_live_fills([MarketType.SPOT])

    assert service.risk.state.realized_pnl == pytest.approx(-10.0)
    assert service.risk.snapshot()["halted"] is True
    assert not service.risk.check_order(1, 0, 1).allowed
    db.close()


def test_live_sync_is_idempotent_and_survives_restart(tmp_path):
    """Polling twice, or restarting, must not book the same exit again."""
    from btrader.core.types import MarketType

    rest = FakeREST(
        {
            "BTC/USDT": [
                _trade("1", "buy", 1.0, 100.0),
                _trade("2", "sell", 1.0, 110.0),
            ]
        }
    )
    service, db = _service(tmp_path, rest)
    service.sync_live_fills([MarketType.SPOT])
    service.sync_live_fills([MarketType.SPOT])
    assert service.risk.state.realized_pnl == pytest.approx(10.0)

    # A fresh process re-reads the ledger from SQLite.
    restarted, _ = _service(tmp_path, rest, db)
    assert restarted.sync_live_fills([MarketType.SPOT]) == []
    assert restarted.risk.state.realized_pnl == pytest.approx(0.0)
    db.close()


def test_live_cost_book_handles_add_and_partial_exit(tmp_path):
    """Average cost across two buys, then a half-size exit."""
    from btrader.core.types import MarketType

    rest = FakeREST(
        {
            "BTC/USDT": [
                _trade("1", "buy", 1.0, 100.0),
                _trade("2", "buy", 1.0, 120.0),
                _trade("3", "sell", 1.0, 130.0),
            ]
        }
    )
    service, db = _service(tmp_path, rest)
    booked = service.sync_live_fills([MarketType.SPOT])

    assert len(booked) == 1
    # average entry 110 (with fee 0), half the position closed at 130
    assert booked[0]["closedQty"] == pytest.approx(1.0)
    assert booked[0]["realized"] == pytest.approx(20.0)
    assert service.risk.state.realized_pnl == pytest.approx(20.0)
    db.close()


def test_slices_of_one_order_produce_a_single_message(tmp_path):
    """A swept stop is many fills of one order, not many orders."""
    from btrader.core.types import MarketType

    class Recording(WeChatNotifier):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.sent: list[tuple[str, str]] = []

        def notify(self, text, *, kind=""):
            self.sent.append((text, kind))
            return True

    rest = FakeREST(
        {
            "BTC/USDT": [
                _trade("1", "buy", 1.0, 100.0),
                {**_trade("2", "sell", 0.4, 110.0), "orderId": "ord-1"},
                {**_trade("3", "sell", 0.6, 120.0), "orderId": "ord-1"},
            ]
        }
    )
    notifier = Recording(NotifySettings(enabled=True, webhook_url="https://example.com/hook"))
    service, db = _service(tmp_path, rest)
    service.notifier = notifier

    booked = service.sync_live_fills([MarketType.SPOT])

    assert len(booked) == 2
    assert len(notifier.sent) == 1
    text, kind = notifier.sent[0]
    assert kind == "fill"
    # merged size, share-weighted price: (0.4*110 + 0.6*120) / 1.0
    assert "平仓" not in text and "卖出 1 @ 116.000" in text
    db.close()


def test_daily_loss_breaker_is_announced_without_a_halt_reason(tmp_path):
    """The daily-loss cap has no ``halted_reason``; the push must still fire."""
    from btrader.core.types import MarketType

    class Recording(WeChatNotifier):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.sent: list[str] = []

        def notify(self, text, *, kind=""):
            self.sent.append(kind)
            return True

    rest = FakeREST(
        {
            "BTC/USDT": [
                _trade("1", "buy", 1.0, 100.0),
                _trade("2", "sell", 1.0, 90.0),
            ]
        }
    )
    notifier = Recording(NotifySettings(enabled=True, webhook_url="https://example.com/hook"))
    service, db = _service(tmp_path, rest)
    service.notifier = notifier
    service.risk.settings.max_daily_loss = 5.0

    service.sync_live_fills([MarketType.SPOT])
    service.notify_risk_halt()
    service.notify_risk_halt()

    assert service.risk.state.halted_reason == ""
    assert notifier.sent.count("risk") == 1
    db.close()
