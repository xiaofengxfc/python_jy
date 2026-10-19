"""Small reusable widgets."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .. import theme


class Card(QFrame):
    """Flat surface: split by a hairline border instead of a shadow."""

    def __init__(self, parent: QWidget | None = None, padding: int = theme.CARD_PADDING) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(padding, padding, padding, padding)
        self._layout.setSpacing(10)

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
        self._layout.setSpacing(4)
        self._color: str | None = None
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_value(self, text: str, color: str | None = None) -> None:
        self.value.setText(text)
        self._color = color
        # Callers pass a palette colour; map it back to a semantic role so the
        # colour survives a theme switch instead of freezing the old hue.
        palette = theme.current()
        role = None
        if color is not None:
            role = {palette.up: "up", palette.down: "down"}.get(color, "dim")
        theme.set_role(self.value, role)

    def set_title(self, text: str) -> None:
        self.label.setText(text)


class PageHeader(QWidget):
    """Page title with an optional subtitle and a trailing action row."""

    def __init__(
        self, title: str, subtitle: str = "", parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(12)

        text = QVBoxLayout()
        text.setSpacing(3)
        self.title = QLabel(title)
        self.title.setObjectName("PageTitle")
        text.addWidget(self.title)
        self.subtitle = QLabel(subtitle)
        self.subtitle.setObjectName("Subtitle")
        self.subtitle.setVisible(bool(subtitle))
        text.addWidget(self.subtitle)
        layout.addLayout(text)
        layout.addStretch(1)
        self.actions = QHBoxLayout()
        self.actions.setSpacing(8)
        layout.addLayout(self.actions)

    def add_action(self, widget: QWidget) -> None:
        self.actions.addWidget(widget)


def pnl_color(value: float) -> str:
    if value > 0:
        return theme.current().up
    if value < 0:
        return theme.current().down
    return theme.current().text_dim


def percent_text(value: float, digits: int = 2) -> str:
    return f"{value * 100:+.{digits}f}%"


def money_text(value: float, digits: int = 2) -> str:
    return f"{value:,.{digits}f}"


class SectionTitle(QWidget):
    def __init__(self, title: str, subtitle: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 2)
        layout.setSpacing(2)
        self.title = QLabel(title)
        self.title.setObjectName("Title")
        layout.addWidget(self.title)
        if subtitle:
            sub = QLabel(subtitle)
            sub.setObjectName("Subtitle")
            layout.addWidget(sub)


def mono_font(size: int = 11, bold: bool = False) -> QFont:
    """Tabular figures; the stack is defined once in the theme module."""
    return theme.mono_font(size, bold)


class Badge(QLabel):
    """Pill-shaped status chip. Keeps its colour across theme switches."""

    def __init__(self, text: str = "", color: str | None = None, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._color = color or theme.current().text_dim
        self.set_color(self._color)

    def set_color(self, color: str) -> None:
        self._color = color
        c = QColor(color)
        fg = theme.current().text if color == theme.current().text_dim else color
        self.setStyleSheet(
            f"background: rgba({c.red()}, {c.green()}, {c.blue()}, 0.14);"
            f"color: {fg};"
            f"border: 1px solid rgba({c.red()}, {c.green()}, {c.blue()}, 0.30);"
            "border-radius: 10px; padding: 2px 10px; font-size: 11px; font-weight: 600;"
        )

    def refresh(self) -> None:
        self.set_color(self._color)


def divider(horizontal: bool = True) -> QFrame:
    """A 1px hairline used to separate blocks without adding boxes."""
    line = QFrame()
    line.setObjectName("Divider")
    if horizontal:
        line.setFixedHeight(1)
        line.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    else:
        line.setFixedWidth(1)
        line.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
    return line
