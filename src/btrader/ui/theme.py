"""Theme system: dark/light palettes, stylesheet and change notifications.

Everything visual reads colours through :func:`current` at paint time, so a
theme switch needs no restart. Widgets that cache colours (pyqtgraph items,
custom painters) listen to :data:`bus` and repaint themselves.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette
from PySide6.QtWidgets import QApplication, QWidget


@dataclass(frozen=True)
class Palette:
    name: str
    is_dark: bool
    bg: str            # window background
    bg_alt: str        # sidebar / inputs / headers
    bg_card: str       # raised surfaces
    bg_hover: str
    bg_pressed: str
    border: str
    text: str
    text_dim: str
    accent: str
    accent_hover: str
    accent_text: str
    up: str
    down: str
    up_hover: str
    down_hover: str
    grid: str
    sidebar: str
    shadow_alpha: int


# ---------------------------------------------------------------- typography
FONT_FAMILY = "Inter"
FONT_FALLBACK = '"Microsoft YaHei UI", "Segoe UI", sans-serif'
FONT_STACK = f'"{FONT_FAMILY}", {FONT_FALLBACK}'

# --------------------------------------------------------------- layout scale
# Clean density: 13px base text, 8px card radius, 16px gutters.
BASE_FONT_PX = 13
CARD_RADIUS = 8
CONTROL_RADIUS = 6
CONTROL_HEIGHT = 36
CONTROL_GAP = 10
PAGE_MARGIN = (24, 20, 24, 20)
PAGE_GAP = 16
CARD_PADDING = 16


def font_dirs() -> list[Path]:
    """Where bundled font files live, in source and in frozen builds."""
    roots = []
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", "")
        if base:
            roots.append(Path(base))
        roots.append(Path(sys.executable).resolve().parent)
    roots.append(Path(__file__).resolve().parents[1])  # the btrader package
    return [root / "assets" / "fonts" for root in roots]


def load_fonts() -> bool:
    """Register the bundled Inter faces. Safe to call more than once."""
    loaded = False
    seen: set[str] = set()
    for folder in font_dirs():
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.ttf")):
            if path.name in seen:
                continue
            seen.add(path.name)
            if QFontDatabase.addApplicationFont(str(path)) >= 0:
                loaded = True
    return loaded


def display_font(size: int = BASE_FONT_PX, weight: int = 400) -> QFont:
    """A font that uses Inter first and falls back to the CJK system face."""
    font = QFont(FONT_FAMILY)
    font.setFamilies([FONT_FAMILY, "Microsoft YaHei UI", "Segoe UI"])
    font.setPixelSize(size)
    font.setWeight(QFont.Weight(weight))
    return font


def mono_font(size: int = BASE_FONT_PX, bold: bool = False) -> QFont:
    """Tabular figures for prices and quantities."""
    font = QFont("Consolas")
    font.setFamilies(["Consolas", "Cascadia Mono", "Courier New"])
    font.setStyleHint(QFont.StyleHint.Monospace)
    font.setPixelSize(size)
    font.setBold(bold)
    return font


DARK = Palette(
    name="dark",
    is_dark=True,
    bg="#0e0f11",
    bg_alt="#121316",
    bg_card="#16181b",
    bg_hover="#1e2024",
    bg_pressed="#2a2d33",
    border="#282b30",
    text="#e8e9eb",
    text_dim="#8b9096",
    accent="#9fb0c9",
    accent_hover="#b6c4d8",
    accent_text="#14161a",
    up="#2eb86a",
    down="#e05b5b",
    up_hover="#39c878",
    down_hover="#ea6d6d",
    grid="#1c1f23",
    sidebar="#121316",
    shadow_alpha=0,
)

LIGHT = Palette(
    name="light",
    is_dark=False,
    bg="#ffffff",
    bg_alt="#ffffff",
    bg_card="#ffffff",
    bg_hover="#f2f3f5",
    bg_pressed="#dcdfe3",
    border="#e4e6e9",
    text="#16181d",
    text_dim="#71767d",
    accent="#3f4a5c",
    accent_hover="#333c4a",
    accent_text="#ffffff",
    up="#1a9c5b",
    down="#d94848",
    up_hover="#178a50",
    down_hover="#c33e3e",
    grid="#eef0f2",
    sidebar="#ffffff",
    shadow_alpha=0,
)

PALETTES = {"dark": DARK, "light": LIGHT}


class _ThemeBus(QObject):
    """Emits the active palette whenever the theme changes."""

    changed = Signal(object)


_bus: _ThemeBus | None = None
_current: Palette = DARK


def bus() -> _ThemeBus:
    global _bus
    if _bus is None:
        _bus = _ThemeBus()
    return _bus


def current() -> Palette:
    return _current


def system_theme() -> str:
    """Best-effort read of the OS light/dark preference."""
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            ) as key:
                value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return "light" if value else "dark"
        except OSError:
            return "dark"
    return "dark"


def resolve(name: str | None) -> str:
    """Map a stored preference (including ``system``) to a palette name."""
    value = (name or "dark").lower()
    if value == "system":
        return system_theme()
    return value if value in PALETTES else "dark"


def set_theme(name: str) -> Palette:
    """Activate a palette. Returns it; also mirrors it onto module globals."""
    global _current
    _current = PALETTES.get(name, DARK)
    for field in _current.__dataclass_fields__:
        globals()[field.upper()] = getattr(_current, field)
    return _current


def _rgba(color: str, alpha: float) -> str:
    c = QColor(color)
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {alpha:.3f})"


def set_role(widget: QWidget, role: str | None) -> None:
    """Apply a semantic colour role (``up`` / ``down`` / ``dim`` / ``accent``).

    The colour itself lives in the stylesheet, so switching themes repaints
    the widget instead of leaving the previous palette baked into the widget.
    """
    if widget.property("role") == role:
        return
    widget.setProperty("role", role)
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def stylesheet(p: Palette) -> str:
    soft = _rgba(p.accent, 0.14)
    sell_soft = _rgba(p.down, 0.16)
    return f"""
QWidget {{
    color: {p.text};
    font-family: {FONT_STACK};
    font-size: {BASE_FONT_PX}px;
}}
/* Only the window paints the page colour. Every other container stays
   transparent, otherwise nested widgets stack their own fill on top of the
   card behind them and the layout reads as stacked colour blocks. */
QMainWindow, QDialog {{ background-color: {p.bg}; }}
QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent; }}
QStackedWidget {{ background: transparent; }}
QAbstractScrollArea {{ background: transparent; }}

/* ---------------------------------------------------------- surfaces */
QFrame#Sidebar {{ background: {p.sidebar}; border-right: 1px solid {p.border}; }}
QFrame#SidebarAccount {{
    background: {p.bg_card};
    border: 1px solid {p.border};
    border-radius: {CARD_RADIUS}px;
}}
QFrame#SidebarAccount:hover {{ background: {p.bg_hover}; }}
QLabel#SidebarAccountMode {{ font-size: 11px; font-weight: 600; color: {p.text_dim}; }}
QLabel#SidebarAccountValue {{ font-size: 14px; font-weight: 650; color: {p.text}; }}
QLabel#SidebarAccountHint {{ font-size: 11px; }}
QFrame#TitleBar {{ background: transparent; border: none; }}
QFrame#StatusStrip {{ background: {p.bg_alt}; border-top: 1px solid {p.border}; }}
QFrame#Card {{
    background: {p.bg_card};
    border: 1px solid {p.border};
    border-radius: {CARD_RADIUS}px;
}}
QFrame#Divider {{ background: {p.border}; border: none; }}
QFrame#Drawer {{
    background: {p.bg_card};
    border: 1px solid {p.border};
    border-radius: {CARD_RADIUS}px;
}}
QPushButton#DrawerHeader {{
    background: transparent;
    border: none;
    border-radius: {CONTROL_RADIUS}px;
    color: {p.text};
    font-size: 14px;
    font-weight: 600;
    text-align: left;
    padding: 0;
    min-height: {CONTROL_HEIGHT}px;
    max-height: {CONTROL_HEIGHT}px;
}}
QPushButton#DrawerHeader:hover {{ background: {p.bg_hover}; }}

QListWidget#SettingsNav {{
    background: transparent;
    border: none;
    outline: none;
    padding: 0;
}}
QListWidget#SettingsNav::item {{
    background: transparent;
    border-radius: {CONTROL_RADIUS}px;
    color: {p.text_dim};
    font-size: 13px;
    padding: 8px 10px;
}}
QListWidget#SettingsNav::item:hover {{ background: {p.bg_hover}; color: {p.text}; }}
/* bg_card equals the page colour in the light palette, so a card-coloured
   selection would be invisible; a tinted fill reads in both themes. */
QListWidget#SettingsNav::item:selected {{
    background: {soft};
    color: {p.text};
    border: 1px solid transparent;
}}

QLabel {{ background: transparent; }}
QLabel#Title {{ font-size: 14px; font-weight: 600; color: {p.text}; }}
QLabel#PageTitle {{ font-size: 19px; font-weight: 650; color: {p.text}; }}
QLabel#Subtitle {{ color: {p.text_dim}; font-size: 12px; }}
QLabel#MetricValue {{ font-size: 22px; font-weight: 650; color: {p.text}; }}
QLabel#MetricLabel {{ color: {p.text_dim}; font-size: 12px; }}
QLabel#SymbolPrice {{ font-size: 26px; font-weight: 700; }}
QLabel#ChangePct {{ font-weight: 600; }}
QLabel#Brand {{ color: {p.text}; font-size: 15px; font-weight: 700; }}
QLabel#StatValue {{ font-size: 13px; font-weight: 600; color: {p.text}; }}
QLabel#StatusLabel {{ color: {p.text_dim}; font-size: 11px; }}
QLabel#Eyebrow {{
    color: {p.text_dim}; font-size: 11px; font-weight: 600;
    letter-spacing: 0.6px;
}}
/* Semantic colour roles. Colours belong in the stylesheet: an inline
   ``setStyleSheet(f"color: ...")`` freezes whichever palette was active when
   it ran, so a theme switch would leave the old hue behind.
   Every ID below carries a fixed ``color`` of its own; an ID selector (100)
   outranks a bare attribute match (10), so each one is repeated here. Any new
   label with its own colour must be added to these lists or roles silently
   stop working on it. */
QLabel[role="up"], QLabel#Title[role="up"], QLabel#Subtitle[role="up"],
QLabel#StatusLabel[role="up"], QLabel#SidebarAccountHint[role="up"],
QLabel#SidebarAccountMode[role="up"], QLabel#SidebarAccountValue[role="up"],
QLabel#MetricValue[role="up"], QLabel#StatValue[role="up"],
QLabel#Brand[role="up"] {{
    color: {p.up};
}}
QLabel[role="down"], QLabel#Title[role="down"], QLabel#Subtitle[role="down"],
QLabel#StatusLabel[role="down"], QLabel#SidebarAccountHint[role="down"],
QLabel#SidebarAccountMode[role="down"], QLabel#SidebarAccountValue[role="down"],
QLabel#MetricValue[role="down"], QLabel#StatValue[role="down"],
QLabel#Brand[role="down"] {{
    color: {p.down};
}}
QLabel[role="dim"], QLabel#Title[role="dim"], QLabel#Subtitle[role="dim"],
QLabel#StatusLabel[role="dim"], QLabel#SidebarAccountHint[role="dim"],
QLabel#SidebarAccountMode[role="dim"], QLabel#SidebarAccountValue[role="dim"],
QLabel#MetricValue[role="dim"], QLabel#StatValue[role="dim"],
QLabel#Brand[role="dim"] {{
    color: {p.text_dim};
}}
QLabel[role="accent"], QLabel#Title[role="accent"], QLabel#Subtitle[role="accent"],
QLabel#StatusLabel[role="accent"], QLabel#SidebarAccountHint[role="accent"],
QLabel#SidebarAccountMode[role="accent"], QLabel#SidebarAccountValue[role="accent"],
QLabel#MetricValue[role="accent"], QLabel#StatValue[role="accent"],
QLabel#Brand[role="accent"] {{
    color: {p.accent};
}}
QLabel#FieldLabel {{ color: {p.text_dim}; font-size: 12px; font-weight: 500; }}

/* ---------------------------------------------------------- sidebar nav */
QPushButton#NavItem {{
    background: transparent;
    border: 1px solid transparent;
    border-radius: 9px;
    padding: 0 12px;
    color: {p.text_dim};
    font-size: 13px;
    text-align: left;
    min-height: 38px;
    max-height: 38px;
}}
QPushButton#NavItem:hover {{ background: {p.bg_hover}; color: {p.text}; }}
QPushButton#NavItem[compact="true"] {{ text-align: center; padding: 0; }}
QPushButton#NavItem:checked {{
    background: {soft};
    color: {p.accent};
    font-weight: 600;
}}
/* Bare icon buttons take their size from the widget, not the 36px control
   scale; state the height explicitly so QSS does not override setFixedSize. */
QPushButton#IconButton {{
    background: transparent;
    border: none;
    border-radius: 7px;
    padding: 0;
    color: {p.text_dim};
    min-height: 36px;
    max-height: 36px;
}}
QPushButton#IconButton:hover {{ background: {p.bg_hover}; color: {p.text}; }}
QPushButton#WindowButton, QPushButton#CloseButton {{
    background: transparent;
    border: none;
    border-radius: 6px;
    padding: 0;
    color: {p.text_dim};
    min-height: 26px;
    max-height: 26px;
}}
QPushButton#WindowButton:hover {{ background: {p.bg_hover}; color: {p.text}; }}
QPushButton#CloseButton:hover {{ background: {p.down}; color: #ffffff; }}

/* ---------------------------------------------------------- buttons */
QPushButton {{
    background: {p.bg_hover};
    border: 1px solid transparent;
    border-radius: {CONTROL_RADIUS}px;
    padding: 0 14px;
    min-height: {CONTROL_HEIGHT - 2}px;
    color: {p.text};
    font-size: 13px;
}}
QPushButton:hover {{ background: {p.border}; }}
QPushButton:pressed {{ background: {p.bg_pressed}; }}
QPushButton:disabled {{
    color: {p.text_dim};
    background: {p.bg_alt};
    border-color: {p.border};
}}
QPushButton#Buy {{
    background: {p.up}; border: 1px solid transparent; color: #ffffff; font-weight: 600;
}}
QPushButton#Buy:hover {{ background: {p.up_hover}; }}
QPushButton#Sell {{
    background: {p.down}; border: 1px solid transparent; color: #ffffff; font-weight: 600;
}}
QPushButton#Sell:hover {{ background: {p.down_hover}; }}
/* A solid fill overrides the generic :disabled rule, so the coloured buttons
   need their own — otherwise a disabled buy button still reads as armed. */
QPushButton#Buy:disabled, QPushButton#Sell:disabled,
QPushButton#Primary:disabled, QPushButton#Danger:disabled {{
    background: {p.bg_alt};
    border-color: {p.border};
    color: {p.text_dim};
}}
QPushButton#Primary {{
    background: {p.accent}; border: 1px solid transparent; color: {p.accent_text};
    font-weight: 600;
}}
QPushButton#Primary:hover {{ background: {p.accent_hover}; }}
QPushButton#Danger {{
    background: {sell_soft}; border: 1px solid transparent; color: {p.down}; font-weight: 600;
}}
QPushButton#Danger:hover {{ background: {sell_soft}; border-color: {p.down}; }}
QPushButton#Flat {{
    background: transparent; border: 1px solid {p.border}; color: {p.text};
}}
QPushButton#Flat:hover {{ background: {p.bg_hover}; border-color: {p.border}; }}

/* ---------------------------------------------------- segmented controls */
QPushButton#SegmentLeft, QPushButton#SegmentRight {{
    background: transparent;
    border: 1px solid {p.border};
    color: {p.text_dim};
    padding: 0 12px;
    min-height: {CONTROL_HEIGHT - 2}px;
    font-size: 13px;
}}
QPushButton#SegmentLeft {{
    border-top-left-radius: {CONTROL_RADIUS}px;
    border-bottom-left-radius: {CONTROL_RADIUS}px;
    border-right: none;
}}
QPushButton#SegmentRight {{
    border-top-right-radius: {CONTROL_RADIUS}px;
    border-bottom-right-radius: {CONTROL_RADIUS}px;
}}
QPushButton#SegmentLeft:hover, QPushButton#SegmentRight:hover {{
    background: {p.bg_hover};
    color: {p.text};
}}
QPushButton#SegmentLeft:checked, QPushButton#SegmentRight:checked {{
    background: {p.accent};
    border-color: {p.accent};
    color: {p.accent_text};
    font-weight: 600;
}}
QPushButton#MiniSegmentLeft, QPushButton#MiniSegmentRight {{
    background: transparent;
    border: 1px solid {p.border};
    color: {p.text_dim};
    padding: 0 9px;
    min-height: {CONTROL_HEIGHT - 2}px;
    font-size: 12px;
    max-width: 44px;
}}
QPushButton#MiniSegmentLeft {{
    border-top-left-radius: {CONTROL_RADIUS}px;
    border-bottom-left-radius: {CONTROL_RADIUS}px;
    border-right: none;
}}
QPushButton#MiniSegmentRight {{
    border-top-right-radius: {CONTROL_RADIUS}px;
    border-bottom-right-radius: {CONTROL_RADIUS}px;
}}
QPushButton#MiniSegmentLeft:hover, QPushButton#MiniSegmentRight:hover {{
    background: {p.bg_hover};
    color: {p.text};
}}
QPushButton#MiniSegmentLeft:checked, QPushButton#MiniSegmentRight:checked {{
    background: {p.accent};
    border-color: {p.accent};
    color: {p.accent_text};
}}
QPushButton#Percent {{
    background: transparent;
    border: 1px solid {p.border};
    border-radius: {CONTROL_RADIUS}px;
    color: {p.text_dim};
    padding: 0;
    min-height: {CONTROL_HEIGHT - 2}px;
    font-size: 12px;
}}
QPushButton#Percent:hover {{
    background: {p.bg_hover};
    border-color: {p.text_dim};
    color: {p.text};
}}

/* ---------------------------------------------------------- inputs */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QDateTimeEdit,
QPlainTextEdit, QTextEdit {{
    background: {p.bg_alt};
    border: 1px solid {p.border};
    border-radius: {CONTROL_RADIUS}px;
    padding: 0 10px;
    min-height: {CONTROL_HEIGHT - 2}px;
    max-height: {CONTROL_HEIGHT - 2}px;
    color: {p.text};
    selection-background-color: {p.accent};
    selection-color: {p.accent_text};
}}
QPlainTextEdit, QTextEdit {{ padding: 8px 10px; min-height: 0; max-height: 16777215px; }}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus,
QDateTimeEdit:focus, QPlainTextEdit:focus, QTextEdit:focus {{
    border-color: {p.accent};
}}
QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled {{
    color: {p.text_dim};
    background: {p.bg_hover};
    border-color: {p.border};
}}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox::down-arrow {{
    image: none; border-left: 4px solid transparent; border-right: 4px solid transparent;
    border-top: 5px solid {p.text_dim}; margin-right: 8px;
}}
/* Popup windows are not clipped to a rounded rect unless the window is
   translucent, which Qt does not do for combo views (the square corner then
   shows the border colour). Keep popups square. */
QComboBox QAbstractItemView {{
    background: {p.bg_card};
    border: 1px solid {p.border};
    border-radius: 0;
    padding: 4px;
    selection-background-color: {p.bg_hover};
    outline: none;
}}
QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
    background: transparent; border: none; width: 14px;
}}

/* ---------------------------------------------------------- tables */
QTableView, QTableWidget, QTreeView, QListView {{
    background: transparent;
    alternate-background-color: transparent;
    border: none;
    gridline-color: transparent;
    selection-background-color: {soft};
    selection-color: {p.text};
    outline: none;
}}
QTableView::item, QTableWidget::item {{ padding: 2px 4px; border: none; }}
QHeaderView::section {{
    background: transparent;
    color: {p.text_dim};
    border: none;
    border-bottom: 1px solid {p.border};
    padding: 7px 6px;
    font-size: 12px;
    font-weight: 600;
}}
QTableCornerButton::section {{ background: transparent; border: none; }}

/* ---------------------------------------------------------- tabs */
QTabWidget::pane {{ border: none; background: transparent; }}
QTabBar::tab {{
    background: transparent;
    color: {p.text_dim};
    padding: 0 16px;
    min-height: 34px;
    border: none;
    border-bottom: 2px solid transparent;
    font-size: 13px;
}}
QTabBar::tab:selected {{ color: {p.text}; border-bottom: 2px solid {p.accent}; }}
QTabBar::tab:hover {{ color: {p.text}; }}

/* ---------------------------------------------------------- scrollbars */
QScrollBar:vertical {{ background: transparent; width: 9px; margin: 2px; }}
QScrollBar::handle:vertical {{
    background: {p.border}; border-radius: 4px; min-height: 28px;
}}
QScrollBar::handle:vertical:hover {{ background: {p.text_dim}; }}
QScrollBar:horizontal {{ background: transparent; height: 9px; margin: 2px; }}
QScrollBar::handle:horizontal {{
    background: {p.border}; border-radius: 4px; min-width: 28px;
}}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}

/* ---------------------------------------------------------- misc */
QSplitter::handle {{ background: transparent; }}
QSplitter::handle:horizontal {{ width: 6px; }}
QSplitter::handle:vertical {{ height: 6px; }}

QGroupBox {{
    border: 1px solid {p.border};
    border-radius: {CARD_RADIUS}px;
    margin-top: 10px;
    padding-top: 10px;
    font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 4px;
    color: {p.text_dim};
}}

QCheckBox, QRadioButton {{ spacing: 7px; background: transparent; }}
QCheckBox::indicator {{
    width: 15px; height: 15px;
    border: 1px solid {p.border};
    border-radius: 4px;
    background: {p.bg_alt};
    padding: 0;
}}
QCheckBox::indicator:checked {{
    /* The bundled tick glyph is black, so the checked box keeps a light fill:
       painting the accent here would leave the tick at roughly 2:1 contrast. */
    background: {p.bg_alt};
    border-color: {p.accent};
    image: url(:/qt-project.org/styles/commonstyle/images/standardbutton-apply-16.png);
}}
QCheckBox::indicator:disabled {{ background: {p.bg_hover}; border-color: {p.border}; }}
QRadioButton::indicator {{
    width: 15px; height: 15px;
    border: 1px solid {p.border};
    border-radius: 8px;
    background: {p.bg_alt};
}}
QRadioButton::indicator:checked {{
    background: {p.accent}; border: 4px solid {p.bg_alt};
    outline: 1px solid {p.accent};
}}

QProgressBar {{
    background: {p.bg_hover};
    border: none;
    border-radius: 3px;
    text-align: center;
    max-height: 12px;
    font-size: 10px;
}}
QProgressBar::chunk {{ background: {p.accent}; border-radius: 3px; }}

QMenu {{
    background: {p.bg_card};
    border: 1px solid {p.border};
    border-radius: 0;
    padding: 6px;
}}
QMenu::item {{ padding: 7px 22px 7px 14px; border-radius: 6px; }}
QMenu::item:selected {{ background: {p.bg_hover}; }}

QToolTip {{
    background: {p.bg_card};
    color: {p.text};
    border: 1px solid {p.border};
    padding: 5px 7px;
    border-radius: 0;
}}

QStatusBar {{
    background: {p.bg_alt};
    border-top: 1px solid {p.border};
    color: {p.text_dim};
}}
QStatusBar QLabel {{ padding: 0 6px; background: transparent; }}
QToolBar {{
    background: {p.bg_alt};
    border: none;
    border-bottom: 1px solid {p.border};
    spacing: 6px;
    padding: 4px 8px;
}}
"""


def _build_palette(p: Palette) -> QPalette:
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(p.bg))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(p.text))
    palette.setColor(QPalette.ColorRole.Base, QColor(p.bg_alt))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(p.bg_card))
    palette.setColor(QPalette.ColorRole.Text, QColor(p.text))
    palette.setColor(QPalette.ColorRole.Button, QColor(p.bg_hover))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(p.text))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(p.accent))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(p.accent_text))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(p.bg_card))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(p.text))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(p.text_dim))
    return palette


def apply_theme(app: QApplication, name: str | None = None) -> Palette:
    """Activate ``name`` (``dark`` / ``light`` / ``system``) and restyle the app."""
    load_fonts()
    app.setFont(display_font(BASE_FONT_PX))
    palette = set_theme(resolve(name or _current.name))
    app.setStyle("Fusion")
    app.setPalette(_build_palette(palette))
    app.setStyleSheet(stylesheet(palette))
    bus().changed.emit(palette)
    return palette


# Initialise the module globals so `theme.TEXT` etc. are always defined.
set_theme("dark")
