"""Dark theme and shared stylesheet."""

from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

# Palette tokens (also used by custom widgets/painters).
BG = "#0f1115"
BG_ALT = "#151922"
BG_CARD = "#1a1f2b"
BG_HOVER = "#232936"
BORDER = "#2a3140"
TEXT = "#e6e9ef"
TEXT_DIM = "#8b93a7"
ACCENT = "#f0b90b"      # Binance yellow
UP = "#26a69a"
DOWN = "#ef5350"
BUY = "#26a69a"
SELL = "#ef5350"
GRID = "#232a37"

STYLESHEET = f"""
QWidget {{
    background-color: {BG};
    color: {TEXT};
    font-family: "Microsoft YaHei UI", "Segoe UI", "PingFang SC", sans-serif;
    font-size: 12px;
}}
QMainWindow, QDialog {{ background-color: {BG}; }}

QToolBar {{
    background: {BG_ALT};
    border: none;
    border-bottom: 1px solid {BORDER};
    spacing: 6px;
    padding: 4px 8px;
}}
QToolBar QLabel {{ color: {TEXT_DIM}; }}

QStatusBar {{
    background: {BG_ALT};
    border-top: 1px solid {BORDER};
    color: {TEXT_DIM};
}}
QStatusBar QLabel {{ padding: 0 6px; }}

QFrame#Card {{
    background: {BG_CARD};
    border: 1px solid {BORDER};
    border-radius: 6px;
}}
QFrame#Panel {{
    background: {BG_ALT};
    border: 1px solid {BORDER};
    border-radius: 6px;
}}
QLabel#Title {{ font-size: 15px; font-weight: 600; color: {TEXT}; }}
QLabel#Subtitle {{ color: {TEXT_DIM}; font-size: 11px; }}
QLabel#MetricValue {{ font-size: 17px; font-weight: 600; }}
QLabel#MetricLabel {{ color: {TEXT_DIM}; font-size: 11px; }}
QLabel#SymbolPrice {{ font-size: 22px; font-weight: 700; }}
QLabel#Muted {{ color: {TEXT_DIM}; }}

QPushButton {{
    background: {BG_HOVER};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 6px 14px;
    color: {TEXT};
}}
QPushButton:hover {{ background: #2b3344; border-color: #3a4356; }}
QPushButton:pressed {{ background: #333c50; }}
QPushButton:disabled {{ color: #5c6373; background: {BG_ALT}; }}
QPushButton#Buy {{ background: {BUY}; border-color: {BUY}; color: #04120f; font-weight: 700; }}
QPushButton#Buy:hover {{ background: #2fb8ad; }}
QPushButton#Sell {{ background: {SELL}; border-color: {SELL}; color: #1a0605; font-weight: 700; }}
QPushButton#Sell:hover {{ background: #f36a65; }}
QPushButton#Primary {{ background: {ACCENT}; border-color: {ACCENT}; color: #231a00; font-weight: 600; }}
QPushButton#Primary:hover {{ background: #ffcd2e; }}
QPushButton#Danger {{ background: #8b2c2c; border-color: #a33; color: #ffe9e9; }}
QPushButton#Flat {{ background: transparent; border: 1px solid {BORDER}; }}
QPushButton#Flat:hover {{ background: {BG_HOVER}; }}

QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QDateTimeEdit, QPlainTextEdit, QTextEdit {{
    background: {BG};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 4px 8px;
    selection-background-color: {ACCENT};
    selection-color: #000;
    min-height: 20px;
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus, QDateTimeEdit:focus {{
    border-color: {ACCENT};
}}
QComboBox::drop-down {{ border: none; width: 18px; }}
QComboBox QAbstractItemView {{
    background: {BG_CARD};
    border: 1px solid {BORDER};
    selection-background-color: {BG_HOVER};
}}
QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
    background: {BG_HOVER}; border: none; width: 14px;
}}

QTableView, QTableWidget, QTreeView, QListView {{
    background: {BG};
    alternate-background-color: {BG_ALT};
    border: 1px solid {BORDER};
    gridline-color: {BORDER};
    selection-background-color: {BG_HOVER};
    selection-color: {TEXT};
}}
QHeaderView::section {{
    background: {BG_ALT};
    color: {TEXT_DIM};
    border: none;
    border-bottom: 1px solid {BORDER};
    border-right: 1px solid {BORDER};
    padding: 5px 6px;
}}
QTableCornerButton::section {{ background: {BG_ALT}; border: none; }}

QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: 4px; top: -1px; }}
QTabBar::tab {{
    background: transparent;
    color: {TEXT_DIM};
    padding: 7px 16px;
    border: none;
    border-bottom: 2px solid transparent;
}}
QTabBar::tab:selected {{ color: {TEXT}; border-bottom: 2px solid {ACCENT}; }}
QTabBar::tab:hover {{ color: {TEXT}; }}

QScrollBar:vertical {{ background: {BG}; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: #333b4b; border-radius: 5px; min-height: 24px; }}
QScrollBar::handle:vertical:hover {{ background: #465167; }}
QScrollBar:horizontal {{ background: {BG}; height: 10px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: #333b4b; border-radius: 5px; min-width: 24px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}

QSplitter::handle {{ background: {BORDER}; }}
QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:vertical {{ height: 1px; }}

QGroupBox {{
    border: 1px solid {BORDER};
    border-radius: 6px;
    margin-top: 12px;
    padding-top: 8px;
    font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
    color: {TEXT_DIM};
}}

QCheckBox, QRadioButton {{ spacing: 6px; }}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 14px; height: 14px;
    border: 1px solid {BORDER};
    border-radius: 3px;
    background: {BG};
}}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
    background: {ACCENT}; border-color: {ACCENT};
}}
QRadioButton::indicator {{ border-radius: 7px; }}

QProgressBar {{
    background: {BG};
    border: 1px solid {BORDER};
    border-radius: 3px;
    text-align: center;
    max-height: 14px;
    font-size: 10px;
}}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 2px; }}

QMenu {{
    background: {BG_CARD};
    border: 1px solid {BORDER};
    padding: 4px;
}}
QMenu::item {{ padding: 6px 24px 6px 16px; border-radius: 3px; }}
QMenu::item:selected {{ background: {BG_HOVER}; }}
QToolTip {{
    background: {BG_CARD};
    color: {TEXT};
    border: 1px solid {BORDER};
    padding: 4px;
}}
"""


def apply_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(BG))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Base, QColor(BG))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(BG_ALT))
    palette.setColor(QPalette.ColorRole.Text, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Button, QColor(BG_HOVER))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(ACCENT))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#000000"))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(BG_CARD))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(TEXT))
    app.setPalette(palette)
    app.setStyleSheet(STYLESHEET)
