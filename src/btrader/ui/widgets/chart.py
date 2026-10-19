"""Candlestick chart built on pyqtgraph."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
import pyqtgraph as pg
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QVBoxLayout, QWidget

from .. import theme


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


def _blend(first: str, second: str, ratio: float) -> str:
    """Mix two palette colours; ratio 0 returns ``first``, 1 returns ``second``."""
    a = QColor(first)
    b = QColor(second)
    mix = lambda x, y: round(x + (y - x) * ratio)  # noqa: E731
    return QColor(mix(a.red(), b.red()), mix(a.green(), b.green()), mix(a.blue(), b.blue())).name()


class CandleChart(QWidget):
    """K line + volume + optional indicator overlays and markers."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
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

        self.price_plot.addLegend(offset=(-10, 8))
        self._legend = self.price_plot.getPlotItem().legend

        self._df = pd.DataFrame()
        self._candles: pg.GraphicsObject | None = None
        self._overlays: list[pg.PlotDataItem] = []
        self._markers: dict[str, list] = {"entry": [], "exit": []}
        self._volume_items: list[pg.GraphicsObject] = []
        self._show_volume = True
        self._last_price_line: pg.InfiniteLine | None = None
        self._label_text = ""

        self._sync_theme()
        theme.bus().changed.connect(self._on_theme_changed)

    # ----------------------------------------------------------------- theme
    def _on_theme_changed(self, *_args) -> None:
        self._sync_theme()
        if not self._df.empty:
            self._redraw()

    def _sync_theme(self) -> None:
        palette = theme.current()
        for widget in (self.price_plot, self.volume_plot):
            widget.setBackground(palette.bg_card)
            item = widget.getPlotItem()
            for axis in ("left", "right", "top", "bottom"):
                ax = item.getAxis(axis)
                ax.setPen(pg.mkPen(palette.border))
                ax.setTextPen(pg.mkPen(palette.text_dim))
        if self._legend is not None:
            self._legend.setBrush(pg.mkBrush(palette.bg_card))
            self._legend.setPen(pg.mkPen(palette.border))
            self._legend.setLabelTextColor(palette.text_dim)
        if self._last_price_line is not None:
            self._last_price_line.setPen(
                pg.mkPen(palette.accent, width=1, style=Qt.PenStyle.DashLine)
            )

    def _redraw(self) -> None:
        """Rebuild cached graphics after a theme change."""
        df = self._df
        if df.empty:
            return
        overlays = {}
        for curve in self._overlays:
            if curve.name is not None:
                overlays[curve.name] = curve.getData()[1]
        self.set_data(df, reset_zoom=False)
        if overlays:
            self.set_overlays({name: pd.Series(values) for name, values in overlays.items()})

    def _setup_plot(self, widget: pg.PlotWidget) -> None:
        widget.showGrid(x=True, y=True, alpha=0.10)
        item = widget.getPlotItem()
        item.setMenuEnabled(False)
        item.hideButtons()
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
        palette = theme.current()
        volume = self._df["volume"].to_numpy(dtype=float)
        colors = np.where(closes >= opens, palette.up, palette.down)
        brushes = [pg.mkBrush(c + "66") for c in colors]
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
        palette = theme.current()
        # Overlay series reuse the palette so a theme switch never leaves a
        # clashing hue behind; the accent leads and the rest stay desaturated.
        colors = [
            palette.accent,
            _blend(palette.accent, palette.text, 0.35),
            _blend(palette.accent, palette.bg, 0.45),
            _blend(palette.text, palette.bg, 0.45),
            _blend(palette.accent, palette.text, 0.65),
        ]
        for i, (name, values) in enumerate(series.items()):
            y = np.asarray(values, dtype=float)
            if len(y) != len(self._df):
                continue
            pen = pg.mkPen(colors[i % len(colors)], width=1.4)
            curve = pg.PlotDataItem(np.arange(len(y)), y, pen=pen, name=name)
            self.price_plot.addItem(curve)
            self._overlays.append(curve)

    def set_price_line(self, price: float) -> None:
        palette = theme.current()
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
                pen=pg.mkPen(palette.accent, width=1, style=Qt.PenStyle.DashLine),
                label=f"{price:,.4f}",
                labelOpts={
                    "position": 0.98,
                    "color": palette.accent,
                    "fill": (palette.bg_card),
                },
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
        palette = theme.current()
        ts_to_index = {int(t): i for i, t in enumerate(self._df["timestamp"])}
        for _, row in trades.iterrows():
            entry_idx = ts_to_index.get(int(row["entry_time"]))
            exit_idx = ts_to_index.get(int(row["exit_time"]))
            is_long = row["side"] == "long"
            if entry_idx is not None:
                symbol = "t1" if is_long else "t"
                marker = pg.ScatterPlotItem(
                    [entry_idx], [row["entry_price"]], symbol=symbol, size=11,
                    brush=pg.mkBrush(palette.up if is_long else palette.down),
                    pen=pg.mkPen(palette.bg_card),
                )
                self.price_plot.addItem(marker)
                self._markers["entry"].append(marker)
            if exit_idx is not None:
                symbol = "t" if is_long else "t1"
                pnl = float(row["pnl"])
                marker = pg.ScatterPlotItem(
                    [exit_idx], [row["exit_price"]], symbol=symbol, size=11,
                    brush=pg.mkBrush(palette.up if pnl > 0 else palette.down),
                    pen=pg.mkPen(palette.bg_card),
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

        palette = theme.current()
        picture = QPicture()
        painter = QPainter(picture)
        width = 0.34
        up_pen = pg.mkPen(palette.up, width=1)
        down_pen = pg.mkPen(palette.down, width=1)
        up_brush = pg.mkBrush(palette.up)
        down_brush = pg.mkBrush(palette.down)
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
