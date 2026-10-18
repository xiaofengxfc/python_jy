"""Small reusable widgets."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from ..theme import DOWN, TEXT_DIM, UP


class Card(QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(12, 10, 12, 10)
        self._layout.setSpacing(6)

    def body(self) -> QVBoxLayout:
        return self._layout

    def add_widget(self, widget: QWidget) -> None:
        self._layout.addWidget(widget)


class MetricCard(Card):
    """Label + value, used for account / backtest summaries."""

    def __init__(self, label: str, value: str = "--", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.label = QLabel(label)
        self.label.setObjectName("MetricLabel")
        self.value = QLabel(value)
        self.value.setObjectName("MetricValue")
        self._layout.addWidget(self.label)
        self._layout.addWidget(self.value)
        self._layout.setSpacing(2)

    def set_value(self, text: str, color: str | None = None) -> None:
        self.value.setText(text)
        if color:
            self.value.setStyleSheet(f"color: {color};")
        else:
            self.value.setStyleSheet("")

    def set_title(self, text: str) -> None:
        self.label.setText(text)


def pnl_color(value: float) -> str:
    if value > 0:
        return UP
    if value < 0:
        return DOWN
    return TEXT_DIM


def percent_text(value: float, digits: int = 2) -> str:
    return f"{value * 100:+.{digits}f}%"


def money_text(value: float, digits: int = 2) -> str:
    return f"{value:,.{digits}f}"


class SectionTitle(QWidget):
    def __init__(self, title: str, subtitle: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 4)
        layout.setSpacing(1)
        self.title = QLabel(title)
        self.title.setObjectName("Title")
        layout.addWidget(self.title)
        if subtitle:
            sub = QLabel(subtitle)
            sub.setObjectName("Subtitle")
            layout.addWidget(sub)


def mono_font(size: int = 11, bold: bool = False) -> QFont:
    font = QFont("Consolas")
    font.setStyleHint(QFont.StyleHint.Monospace)
    font.setPointSize(size)
    font.setBold(bold)
    return font


class Badge(QLabel):
    def __init__(self, text: str = "", color: str = TEXT_DIM, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.set_color(color)
        font = self.font()
        font.setPointSize(9)
        self.setFont(font)

    def set_color(self, color: str) -> None:
        self.setStyleSheet(
            f"background: {QColor(color).name()}22; color: {color};"
            f"border: 1px solid {color}55; border-radius: 3px; padding: 1px 6px;"
        )
