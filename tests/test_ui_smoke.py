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
