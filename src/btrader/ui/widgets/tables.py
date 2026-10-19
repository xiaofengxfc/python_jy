"""Shared table helpers."""

from __future__ import annotations

import pandas as pd
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QTableWidget,
    QTableWidgetItem,
)

from .. import theme


class DataTable(QTableWidget):
    """Read-only table with sensible defaults and a DataFrame loader."""

    def __init__(self, headers: list[str], parent=None) -> None:
        super().__init__(0, len(headers), parent)
        self.setHorizontalHeaderLabels(headers)
        self.verticalHeader().setVisible(False)
        self.setShowGrid(False)
        self.setAlternatingRowColors(False)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setFrameShape(QTableWidget.Shape.NoFrame)
        self.verticalHeader().setDefaultSectionSize(30)
        header = self.horizontalHeader()
        header.setHighlightSections(False)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        header.setDefaultAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )

    def set_stretch_column(self, index: int) -> None:
        header = self.horizontalHeader()
        header.setSectionResizeMode(index, QHeaderView.ResizeMode.ResizeToContents)

    def set_row(self, row: int, values: list[str], colors: list[str | None] | None = None) -> None:
        if row >= self.rowCount():
            self.setRowCount(row + 1)
        for col, text in enumerate(values):
            item = self.item(row, col)
            if item is None:
                item = QTableWidgetItem("")
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.setItem(row, col, item)
            item.setText(text)
            color = colors[col] if colors and col < len(colors) else None
            item.setForeground(QColor(color or theme.current().text))

    def load_dataframe(
        self,
        frame: pd.DataFrame,
        formatters: dict[str, callable] | None = None,
    ) -> None:
        """Populate the table from a DataFrame; column order follows the frame."""
        formatters = formatters or {}
        self.setColumnCount(len(frame.columns))
        self.setHorizontalHeaderLabels([str(c) for c in frame.columns])
        self.setRowCount(len(frame))
        for row, (_, record) in enumerate(frame.iterrows()):
            for col, name in enumerate(frame.columns):
                value = record[name]
                fmt = formatters.get(name)
                text = fmt(value) if fmt else str(value)
                item = self.item(row, col)
                if item is None:
                    item = QTableWidgetItem("")
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                    self.setItem(row, col, item)
                item.setText(text)
                item.setForeground(QColor(theme.current().text))

    def clear_rows(self) -> None:
        self.setRowCount(0)
