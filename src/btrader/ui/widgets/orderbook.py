"""Order book ladder with depth bars."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from ...core.models import OrderBook
from ...exchange.symbols import format_amount
from .. import theme


class _DepthItem(QTableWidgetItem):
    def __init__(self, text: str, ratio: float, color: str) -> None:
        super().__init__(text)
        self._ratio = max(0.0, min(1.0, ratio))
        self._color = QColor(color)
        self.setTextAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )

    def set_ratio(self, ratio: float) -> None:
        self._ratio = max(0.0, min(1.0, ratio))

    def set_bar_color(self, color: str) -> None:
        self._color = QColor(color)

    def paint(self, painter, option, widget=None):  # noqa: D401 - Qt override
        if self._ratio > 0:
            fill = QColor(self._color)
            fill.setAlpha(30)
            painter.fillRect(
                option.rect.x(),
                option.rect.y(),
                int(option.rect.width() * self._ratio),
                option.rect.height(),
                fill,
            )
        super().paint(painter, option, widget)


class OrderBookWidget(QWidget):
    """Asks on top (red), bids below (green), with cumulative depth bars."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from PySide6.QtWidgets import QVBoxLayout

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["价格", "数量", "累计"])
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setFrameShape(QTableWidget.Shape.NoFrame)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        header.setFixedHeight(30)
        header.setDefaultAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.table.verticalHeader().setDefaultSectionSize(22)
        layout.addWidget(self.table)
        self._levels = 12
        self._last_price = 0.0
        theme.bus().changed.connect(self._repaint)

    def _repaint(self, *_args) -> None:
        palette = theme.current()
        for row in range(self.table.rowCount()):
            for col in range(3):
                item = self.table.item(row, col)
                if item is None:
                    continue
                if col == 0:
                    item.setForeground(QColor(palette.down if row < self._levels else palette.up))
                else:
                    item.setForeground(QColor(palette.text))
                if isinstance(item, _DepthItem):
                    item.set_bar_color(palette.down if row < self._levels else palette.up)
        self.table.viewport().update()

    def set_levels(self, levels: int) -> None:
        self._levels = max(5, levels)

    def update_book(self, book: OrderBook) -> None:
        palette = theme.current()
        asks = list(reversed(book.asks[: self._levels]))
        bids = book.bids[: self._levels]
        rows = len(asks) + len(bids)
        if self.table.rowCount() != rows:
            self.table.setRowCount(rows)
            self._build_rows()

        ask_max = max((a[1] for a in asks), default=1.0)
        bid_max = max((b[1] for b in bids), default=1.0)
        ask_cum = 0.0
        bid_cum = 0.0

        for row, (price, amount) in enumerate(asks):
            ask_cum += amount
            self._set_row(row, price, amount, ask_cum, ask_max, palette.down)
        offset = len(asks)
        for row, (price, amount) in enumerate(bids):
            bid_cum += amount
            self._set_row(offset + row, price, amount, bid_cum, bid_max, palette.up)

        self.table.setMinimumHeight(rows * 22 + 30)

    def _build_rows(self) -> None:
        for row in range(self.table.rowCount()):
            for col in range(3):
                item = QTableWidgetItem("")
                item.setTextAlignment(
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                )
                self.table.setItem(row, col, item)

    def _set_row(
        self,
        row: int,
        price: float,
        amount: float,
        cumulative: float,
        max_amount: float,
        color: str,
    ) -> None:
        palette = theme.current()
        ratio = cumulative / max_amount if max_amount else 0.0
        values = (f"{price:,.4f}", format_amount(amount), format_amount(cumulative))
        for col, text in enumerate(values):
            current = self.table.item(row, col)
            if current is None or not isinstance(current, _DepthItem):
                current = _DepthItem(text, ratio, color)
                self.table.setItem(row, col, current)
            else:
                current.setText(text)
                current.set_ratio(ratio)
                current.set_bar_color(color)
            current.setForeground(QColor(color if col == 0 else palette.text))

    def clear(self) -> None:
        self.table.setRowCount(0)
