"""Smoke tests for the Qt UI layer (offscreen)."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from btrader.context import AppContext  # noqa: E402
from btrader.ui.main_window import MainWindow  # noqa: E402
from btrader.ui.theme import apply_theme  # noqa: E402
from btrader.ui.widgets.chart import CandleChart  # noqa: E402

from helpers import make_candles  # noqa: E402


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    os.environ.setdefault("BITRADER_DATA_DIR", str(tmp_path_factory.mktemp("bitrader")))
    application = QApplication.instance() or QApplication([])
    apply_theme(application)
    yield application


@pytest.fixture
def ctx(app, tmp_path):
    os.environ["BITRADER_DATA_DIR"] = str(tmp_path)
    context = AppContext()
    context.settings.risk.kill_switch = False
    yield context
    context.shutdown()


def test_main_window_builds_all_pages(app, ctx):
    window = MainWindow(ctx)
    assert window.stack.count() == 5
    assert window.trading_page.current_symbol()
    window.close()


def test_chart_accepts_data(app):
    chart = CandleChart()
    df = make_candles(120)
    chart.set_data(df, reset_zoom=True)
    assert len(chart.frame) == 120
    chart.set_overlays({"ma": df["close"].rolling(5).mean()})
    chart.set_price_line(float(df["close"].iloc[-1]))
    chart.clear()
    assert chart.frame.empty


def test_theme_stylesheet_contains_tokens(app):
    from btrader.ui import theme

    assert theme.ACCENT in theme.STYLESHEET
    assert "#f0b90b" in theme.STYLESHEET


def test_paper_limit_order_fills_when_price_crosses(app, ctx):
    """A resting paper limit order fills on the next ticker that crosses it."""
    from btrader.core.types import MarketType, RunMode
    from btrader.engine.trading import OrderRequest

    window = MainWindow(ctx)
    page = window.trading_page
    assert page.mode_combo.currentData() == RunMode.PAPER.value

    ctx.trading.submit(
        OrderRequest(
            symbol="BTC/USDT",
            side="buy",
            order_type="limit",
            amount=0.01,
            price=500.0,
            market_type=MarketType.SPOT,
            mode=RunMode.PAPER,
        )
    )
    page._refresh_orders()
    assert page.open_orders_table.rowCount() == 1

    # Price still above the limit: the order must keep resting.
    page._apply_ticker("BTC/USDT", {"last": 600.0, "high": 620.0, "low": 590.0})
    assert page.open_orders_table.rowCount() == 1

    # Price crosses the limit: the order fills at the limit price.
    page._apply_ticker("BTC/USDT", {"last": 400.0, "high": 610.0, "low": 390.0})
    assert page.open_orders_table.rowCount() == 0
    assert ctx.paper.account.position_for("BTC/USDT").contracts == pytest.approx(0.01)
    window.close()
