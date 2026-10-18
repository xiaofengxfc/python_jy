"""Candlestick chart built on pyqtgraph."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
import pyqtgraph as pg
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtWidgets import QVBoxLayout, QWidget

from ..theme import BG, BG_ALT, BORDER, DOWN, GRID, TEXT, TEXT_DIM, UP


class DateAxis(pg.AxisItem):
    """Maps bar indices to readable timestamps."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._timestamps: np.ndarray = np.array([], dtype=np.int64)

    def set_timestamps(self, ts: np.ndarray) -> None:
        self._timestamps = np.asarray(ts, dtype=np.int64)

    def tickStrings(self, values, scale, spacing):  # noqa: N802 - Qt override
        labels = []
        for value in values:
            index = int(round(value))
            if 0 <= index < len(self._timestamps):
                ts = pd.Timestamp(int(self._timestamps[index]), unit="ms", tz="UTC")
                span = spacing if spacing else 1
                labels.append(ts.strftime("%m-%d %H:%M") if span < 400 else ts.strftime("%Y-%m-%d"))
            else:
                labels.append("")
        return labels


@dataclass
class _LastState:
    candle: pg.GraphicsObject | None = None


class CandleChart(QWidget):
    """K line + volume + optional indicator overlays and markers."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        pg.setConfigOptions(antialias=False, background=BG, foreground=TEXT_DIM)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.date_axis = DateAxis(orientation="bottom")
        self.price_plot = pg.PlotWidget(axisItems={"bottom": self.date_axis})
        self.volume_plot = pg.PlotWidget()
        layout.addWidget(self.price_plot, 4)
        layout.addWidget(self.volume_plot, 1)

        self._setup_plot(self.price_plot)
        self._setup_plot(self.volume_plot)
        self.price_plot.setXLink(self.volume_plot)
        self.price_plot.showAxis("bottom", False)

        self.price_plot.addLegend(offset=(-10, 8), labelTextColor=TEXT_DIM)
        self.price_plot.getPlotItem().legend.setBrush(pg.mkBrush(20, 24, 32, 200))

        self._df = pd.DataFrame()
        self._candles: pg.GraphicsObject | None = None
        self._overlays: list[pg.PlotDataItem] = []
        self._markers: dict[str, list] = {"entry": [], "exit": []}
        self._volume_items: list[pg.GraphicsObject] = []
        self._show_volume = True
        self._last_price_line: pg.InfiniteLine | None = None
        self._label_text = ""

    def _setup_plot(self, widget: pg.PlotWidget) -> None:
        widget.setBackground(BG)
        widget.showGrid(x=True, y=True, alpha=0.18)
        item = widget.getPlotItem()
        item.setMenuEnabled(False)
        item.hideButtons()
        for axis in ("left", "right", "top", "bottom"):
            ax = item.getAxis(axis)
            ax.setPen(pg.mkPen(BORDER))
            ax.setTextPen(pg.mkPen(TEXT_DIM))
        item.getAxis("right").setStyle(showValues=False)
        widget.setMouseEnabled(x=True, y=False)

    # ------------------------------------------------------------------ plot
    def set_data(self, df: pd.DataFrame, reset_zoom: bool = False) -> None:
        if df is None or df.empty:
            self.clear()
            return
        self._df = df.reset_index(drop=True)
        n = len(self._df)
        x = np.arange(n, dtype=float)
        ts = self._df["timestamp"].to_numpy(dtype=np.int64)
        self.date_axis.set_timestamps(ts)

        if self._candles is not None:
            self.price_plot.removeItem(self._candles)
        opens = self._df["open"].to_numpy(dtype=float)
        highs = self._df["high"].to_numpy(dtype=float)
        lows = self._df["low"].to_numpy(dtype=float)
        closes = self._df["close"].to_numpy(dtype=float)
        self._candles = CandlestickItem(x, opens, highs, lows, closes)
        self.price_plot.addItem(self._candles)

        self._draw_volume(x, opens, closes)

        self.price_plot.setLimits(xMin=-2, xMax=n + 1)
        if reset_zoom:
            self.price_plot.setXRange(max(0, n - 200), n, padding=0)
        self._autoscale_y()

    def _draw_volume(self, x: np.ndarray, opens: np.ndarray, closes: np.ndarray) -> None:
        for item in self._volume_items:
            self.volume_plot.removeItem(item)
        self._volume_items.clear()
        if self._df.empty:
            return
        volume = self._df["volume"].to_numpy(dtype=float)
        colors = np.where(closes >= opens, UP, DOWN)
        brushes = [pg.mkBrush(c + "88") for c in colors]
        bars = pg.BarGraphItem(x=x, height=volume, width=0.7, brushes=brushes, pen=None)
        self.volume_plot.addItem(bars)
        self._volume_items.append(bars)
        self.volume_plot.setMouseEnabled(x=True, y=False)
        self.volume_plot.getPlotItem().hideButtons()
        self.volume_plot.setLimits(xMin=-2, xMax=len(x) + 1)

    def _autoscale_y(self, pad: float = 0.05) -> None:
        if self._df.empty:
            return
        low = float(self._df["low"].min())
        high = float(self._df["high"].max())
        span = max(high - low, 1e-9)
        self.price_plot.setYRange(low - span * pad, high + span * pad, padding=0)

    # --------------------------------------------------------------- overlays
    def set_overlays(self, series: dict[str, pd.Series]) -> None:
        for line in self._overlays:
            self.price_plot.removeItem(line)
        self._overlays.clear()
        if not series or self._df.empty:
            return
        colors = ["#4f9dff", "#ffb84d", "#b388ff", "#5ad1c7", "#ff8a80"]
        for i, (name, values) in enumerate(series.items()):
            y = np.asarray(values, dtype=float)
            if len(y) != len(self._df):
                continue
            pen = pg.mkPen(colors[i % len(colors)], width=1.3)
            curve = pg.PlotDataItem(np.arange(len(y)), y, pen=pen, name=name)
            self.price_plot.addItem(curve)
            self._overlays.append(curve)

    def set_price_line(self, price: float) -> None:
        if price <= 0:
            if self._last_price_line is not None:
                self.price_plot.removeItem(self._last_price_line)
                self._last_price_line = None
            return
        if self._last_price_line is None:
            self._last_price_line = pg.InfiniteLine(
                pos=price,
                angle=0,
                movable=False,
                pen=pg.mkPen("#f0b90b", width=1, style=Qt.PenStyle.DashLine),
                label=f"{price:,.4f}",
                labelOpts={"position": 0.98, "color": "#f0b90b", "fill": (30, 34, 44, 220)},
            )
            self.price_plot.addItem(self._last_price_line)
        else:
            self._last_price_line.setPos(price)
            self._last_price_line.label.setText(f"{price:,.4f}")

    def set_trade_markers(self, trades: pd.DataFrame) -> None:
        for marker in self._markers["entry"] + self._markers["exit"]:
            self.price_plot.removeItem(marker)
        self._markers = {"entry": [], "exit": []}
        if trades is None or trades.empty or self._df.empty:
            return
        ts_to_index = {int(t): i for i, t in enumerate(self._df["timestamp"])}
        for _, row in trades.iterrows():
            entry_idx = ts_to_index.get(int(row["entry_time"]))
            exit_idx = ts_to_index.get(int(row["exit_time"]))
            is_long = row["side"] == "long"
            if entry_idx is not None:
                symbol = "t1" if is_long else "t"
                marker = pg.ScatterPlotItem(
                    [entry_idx], [row["entry_price"]], symbol=symbol, size=11,
                    brush=pg.mkBrush(UP if is_long else DOWN), pen=pg.mkPen("#0f1115"),
                )
                self.price_plot.addItem(marker)
                self._markers["entry"].append(marker)
            if exit_idx is not None:
                symbol = "t" if is_long else "t1"
                pnl = float(row["pnl"])
                marker = pg.ScatterPlotItem(
                    [exit_idx], [row["exit_price"]], symbol=symbol, size=11,
                    brush=pg.mkBrush("#26a69a" if pnl > 0 else "#ef5350"),
                    pen=pg.mkPen("#0f1115"),
                )
                self.price_plot.addItem(marker)
                self._markers["exit"].append(marker)

    def clear(self) -> None:
        self.set_overlays({})
        self._df = pd.DataFrame()
        if self._candles is not None:
            self.price_plot.removeItem(self._candles)
            self._candles = None
        for item in self._volume_items:
            self.volume_plot.removeItem(item)
        self._volume_items.clear()
        self.set_trade_markers(pd.DataFrame())
        self.set_price_line(0)

    def zoom_to_last(self, count: int = 200) -> None:
        n = len(self._df)
        if n:
            self.price_plot.setXRange(max(0, n - count), n, padding=0)
            self._autoscale_y()

    @property
    def frame(self) -> pd.DataFrame:
        return self._df


class CandlestickItem(pg.GraphicsObject):
    """Efficient culling candle item."""

    def __init__(self, x, open_, high, low, close) -> None:
        super().__init__()
        self.x = np.asarray(x, dtype=float)
        self.open = np.asarray(open_, dtype=float)
        self.high = np.asarray(high, dtype=float)
        self.low = np.asarray(low, dtype=float)
        self.close = np.asarray(close, dtype=float)
        self._picture = None
        self._bounds = (
            QRectF(
                float(self.x.min()) - 0.5,
                float(self.low.min()),
                float(self.x.max() - self.x.min()) + 1.0,
                float(self.high.max() - self.low.min()) or 1.0,
            )
            if len(self.x)
            else QRectF()
        )
        self.generatePicture()

    def generatePicture(self) -> None:
        from PySide6.QtGui import QPainter, QPicture

        picture = QPicture()
        painter = QPainter(picture)
        width = 0.34
        up_pen = pg.mkPen(UP, width=1)
        down_pen = pg.mkPen(DOWN, width=1)
        up_brush = pg.mkBrush(UP)
        down_brush = pg.mkBrush(DOWN)
        for i in range(len(self.x)):
            o, h, l, c = self.open[i], self.high[i], self.low[i], self.close[i]
            rising = c >= o
            painter.setPen(up_pen if rising else down_pen)
            painter.drawLine(QPointF(self.x[i], l), QPointF(self.x[i], h))
            top, bottom = (o, c) if rising else (c, o)
            if abs(top - bottom) < 1e-12:
                painter.drawLine(
                    QPointF(self.x[i] - width, c), QPointF(self.x[i] + width, c)
                )
            else:
                painter.setBrush(up_brush if rising else down_brush)
                painter.drawRect(QRectF(self.x[i] - width, top, width * 2, bottom - top))
        painter.end()
        self._picture = picture
        self.prepareGeometryChange()

    def paint(self, painter, *args) -> None:
        if self._picture is not None:
            self._picture.play(painter)

    def boundingRect(self):
        return self._bounds
