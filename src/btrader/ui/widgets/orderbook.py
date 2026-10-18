"""Binance-style order book ladder with depth bars."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from ...core.models import OrderBook
from ...exchange.symbols import format_amount
from ..theme import BG, DOWN, TEXT, TEXT_DIM, UP


class _DepthItem(QTableWidgetItem):
    def __init__(self, text: str, ratio: float, color: str, side: str) -> None:
        super().__init__(text)
        self._ratio = max(0.0, min(1.0, ratio))
        self._color = QColor(color)
        self._side = side
        self.setTextAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )

    def set_ratio(self, ratio: float) -> None:
        self._ratio = max(0.0, min(1.0, ratio))

    def paint(self, painter, option, widget=None):  # noqa: D401 - Qt override
        if self._ratio > 0:
            fill = QColor(self._color)
            fill.setAlpha(38)
            painter.fillRect(
                option.rect.x(),
                option.rect.y(),
                int(option.rect.width() * self._ratio),
                option.rect.height(),
                fill,
            )
        super().paint(painter, option, widget)


class OrderBookWidget(QWidget):
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
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        header.setFixedHeight(26)
        self.table.verticalHeader().setDefaultSectionSize(19)
        layout.addWidget(self.table)
        self._levels = 15
        self._last_price = 0.0

    def set_levels(self, levels: int) -> None:
        self._levels = max(5, levels)

    def update_book(self, book: OrderBook) -> None:
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
            self._set_row(row, price, amount, ask_cum, ask_max, UP, DOWN, is_ask=True)
            continue
        offset = len(asks)
        for row, (price, amount) in enumerate(bids):
            bid_cum += amount
            self._set_row(offset + row, price, amount, bid_cum, bid_max, DOWN, UP, is_ask=False)

        self.table.setMinimumHeight(rows * 19 + 26)

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
        price_color: str,
        bar_color: str,
        is_ask: bool,
    ) -> None:
        ratio = cumulative / max_amount if max_amount else 0.0
        values = (f"{price:,.4f}", format_amount(amount), format_amount(cumulative))
        for col, text in enumerate(values):
            current = self.table.item(row, col)
            if current is None:
                current = _DepthItem(text, ratio, bar_color, "ask" if is_ask else "bid")
                self.table.setItem(row, col, current)
            else:
                current.setText(text)
                if isinstance(current, _DepthItem):
                    current.set_ratio(ratio)
            current.setForeground(QColor(price_color if col == 0 else TEXT))

    def clear(self) -> None:
        self.table.setRowCount(0)
