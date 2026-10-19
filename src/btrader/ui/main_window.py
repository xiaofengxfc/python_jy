"""Main window: frameless shell, collapsible sidebar and status strip."""

from __future__ import annotations

import logging

import qtawesome as qta
from PySide6.QtCore import QEvent, QPoint, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSizeGrip,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import APP_NAME, __version__
from ..context import AppContext
from ..core.types import Network, RunMode
from . import theme
from .pages.auto import AutoPage
from .pages.backtest import BacktestPage
from .pages.portfolio import PortfolioPage
from .pages.settings import SettingsPage
from .pages.trading import TradingPage

log = logging.getLogger(__name__)

SIDEBAR_WIDE = 204
SIDEBAR_NARROW = 68
TITLEBAR_H = 40
RESIZE_MARGIN = 5

NAV_ITEMS = [
    ("交易", "fa6s.chart-line", "行情与手动下单"),
    ("资产", "fa6s.wallet", "余额与持仓"),
    ("回测", "fa6s.flask-vial", "策略回测与优化"),
    ("自动交易", "fa6s.robot", "策略自动执行"),
    ("设置", "fa6s.gear", "API、风控与插件"),
]


def _logo(size: int = 28) -> QIcon:
    """Small accent-coloured mark used in the sidebar header."""
    palette = theme.current()
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(palette.accent))
    painter.drawRoundedRect(0, 0, size, size, size * 0.28, size * 0.28)
    painter.setBrush(QColor(theme.current().accent_text))
    bar_w = max(2.0, size * 0.10)
    for x, top, height in ((0.28, 0.46, 0.26), (0.47, 0.34, 0.40), (0.66, 0.24, 0.50)):
        painter.drawRoundedRect(
            int(size * x), int(size * top), int(bar_w), int(size * height), 1, 1
        )
    painter.end()
    return QIcon(pixmap)


class TitleBar(QFrame):
    """Draggable strip carrying only the window controls."""

    def __init__(self, window: "MainWindow") -> None:
        super().__init__(window)
        self._window = window
        self.setObjectName("TitleBar")
        self.setFixedHeight(TITLEBAR_H)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 0, 10, 0)
        layout.setSpacing(6)

        # The page title lives in the page itself, so the strip stays empty
        # and works purely as a drag handle.
        layout.addStretch(1)

        self.min_btn = self._button("fa6s.window-minimize", "最小化", "WindowButton")
        self.min_btn.clicked.connect(window.showMinimized)
        layout.addWidget(self.min_btn)

        self.max_btn = self._button("fa6s.window-maximize", "最大化", "WindowButton")
        self.max_btn.clicked.connect(window.toggle_maximized)
        layout.addWidget(self.max_btn)

        self.close_btn = self._button("fa6s.xmark", "关闭", "CloseButton")
        self.close_btn.clicked.connect(window.close)
        layout.addWidget(self.close_btn)

    def _button(self, icon_name: str, tip: str, object_name: str) -> QPushButton:
        button = QPushButton()
        button.setObjectName(object_name)
        button.setFixedSize(30, 26)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setToolTip(tip)
        button.setIcon(qta.icon(icon_name, color=theme.current().text_dim))
        button.setIconSize(QSize(12, 12))
        return button

    def refresh_theme(self) -> None:
        palette = theme.current()
        self.min_btn.setIcon(qta.icon("fa6s.window-minimize", color=palette.text_dim))
        self.max_btn.setIcon(
            qta.icon(
                "fa6s.window-restore" if self._window.isMaximized() else "fa6s.window-maximize",
                color=palette.text_dim,
            )
        )
        self.close_btn.setIcon(qta.icon("fa6s.xmark", color=palette.text_dim))

    # --------------------------------------------------------------- dragging
    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.LeftButton:
            handle = self.window().windowHandle()
            if handle is not None:
                handle.startSystemMove()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.LeftButton:
            self._window.toggle_maximized()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class SidebarAccount(QFrame):
    """Compact account snapshot pinned to the bottom of the sidebar."""

    def __init__(self, window: "MainWindow") -> None:
        super().__init__(window)
        self._window = window
        self.setObjectName("SidebarAccount")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("查看资产与持仓")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 9, 10, 9)
        layout.setSpacing(3)

        self.mode_label = QLabel("纸面交易")
        self.mode_label.setObjectName("SidebarAccountMode")
        layout.addWidget(self.mode_label)

        self.equity_label = QLabel("--")
        self.equity_label.setObjectName("SidebarAccountValue")
        layout.addWidget(self.equity_label)

        self.api_label = QLabel("未配置 API")
        self.api_label.setObjectName("SidebarAccountHint")
        layout.addWidget(self.api_label)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.LeftButton:
            self._window.navigate(1)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def update_state(self, mode: str, network: str, equity: str, api: str, api_ok: bool) -> None:
        self.mode_label.setText(f"{mode} · {network}")
        theme.set_role(self.mode_label, "accent" if "实盘" in mode else None)
        self.equity_label.setText(equity)
        self.api_label.setText(api)
        theme.set_role(self.api_label, None if api_ok else "down")


class Sidebar(QFrame):
    """Vertical navigation; collapses to icons only."""

    def __init__(self, window: "MainWindow") -> None:
        super().__init__(window)
        self._window = window
        self.setObjectName("Sidebar")
        self.setFixedWidth(SIDEBAR_WIDE)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(4)

        header = QHBoxLayout()
        header.setContentsMargins(6, 6, 6, 14)
        header.setSpacing(10)
        self.logo = QLabel()
        self.logo.setPixmap(_logo(26).pixmap(26, 26))
        self.logo.setFixedSize(26, 26)
        header.addWidget(self.logo)
        self.brand = QLabel(APP_NAME)
        self.brand.setObjectName("Brand")
        header.addWidget(self.brand)
        header.addStretch(1)
        layout.addLayout(header)
        self._header = header

        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons: list[QPushButton] = []
        for index, (label, icon_name, hint) in enumerate(NAV_ITEMS):
            button = QPushButton(label)
            button.setObjectName("NavItem")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setToolTip(hint)
            button.setIconSize(QSize(17, 17))
            button.setFixedHeight(40)
            button.clicked.connect(lambda _=False, i=index: self._window.navigate(i))
            self.group.addButton(button, index)
            self.buttons.append(button)
            layout.addWidget(button)
            button.setProperty("icon_name", icon_name)
        self.buttons[0].setChecked(True)

        layout.addStretch(1)

        self.account = SidebarAccount(window)
        layout.addWidget(self.account)

        self.collapse_btn = QPushButton()
        self.collapse_btn.setObjectName("IconButton")
        self.collapse_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.collapse_btn.setFixedSize(36, 36)
        self.collapse_btn.setIconSize(QSize(15, 15))
        self.collapse_btn.setToolTip("收起侧边栏")
        self.collapse_btn.clicked.connect(window.toggle_sidebar)
        layout.addWidget(self.collapse_btn, 0, Qt.AlignmentFlag.AlignLeft)

        self.collapsed = False
        self.refresh_theme()

    # ---------------------------------------------------------------- visuals
    def refresh_theme(self) -> None:
        palette = theme.current()
        self.logo.setPixmap(_logo(26).pixmap(26, 26))
        self.collapse_btn.setIcon(
            qta.icon(
                "fa6s.chevron-right" if self.collapsed else "fa6s.chevron-left",
                color=palette.text_dim,
            )
        )
        for index, button in enumerate(self.buttons):
            self._paint_button(index)

    def _paint_button(self, index: int) -> None:
        palette = theme.current()
        button = self.buttons[index]
        label = NAV_ITEMS[index][0]
        icon_name = NAV_ITEMS[index][1]
        active = button.isChecked()
        color = palette.accent if active else palette.text_dim
        button.setIcon(qta.icon(icon_name, color=color))
        button.setText("" if self.collapsed else label)
        # Collapsed state drives alignment through QSS, not an inline
        # stylesheet: an inline ``padding`` would silently override the
        # NavItem rule (12px) and drift out of sync with the rest of the UI.
        button.setProperty("compact", self.collapsed)
        style = button.style()
        style.unpolish(button)
        style.polish(button)
        button.update()

    def set_collapsed(self, collapsed: bool) -> None:
        self.collapsed = collapsed
        self.setFixedWidth(SIDEBAR_NARROW if collapsed else SIDEBAR_WIDE)
        self.logo.setVisible(not collapsed)
        self.brand.setVisible(not collapsed)
        self.account.setVisible(not collapsed)
        self.collapse_btn.setToolTip("展开侧边栏" if collapsed else "收起侧边栏")
        for index in range(len(self.buttons)):
            self._paint_button(index)
        self.refresh_theme()

    def select(self, index: int) -> None:
        self.buttons[index].setChecked(True)
        self._paint_button(index)


class MainWindow(QMainWindow):
    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self.setWindowTitle(f"{APP_NAME} · 币安交易工作台 v{__version__}")
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowSystemMenuHint
            | Qt.WindowType.WindowMinMaxButtonsHint
        )
        self.setMinimumSize(1080, 700)
        self.resize(1440, 900)
        self._resize_edge = Qt.Edge(0)
        self._build_ui()
        self._build_status_strip()
        theme.bus().changed.connect(self._on_theme_changed)
        self._refresh_status()

        self.status_timer = QTimer(self)
        self.status_timer.setInterval(5000)
        self.status_timer.timeout.connect(self._refresh_status)
        self.status_timer.start()

    # -------------------------------------------------------------------- ui
    def _build_ui(self) -> None:
        root = QFrame()
        root.setObjectName("WindowRoot")
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.sidebar = Sidebar(self)
        layout.addWidget(self.sidebar)

        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)

        self.title_bar = TitleBar(self)
        right.addWidget(self.title_bar)

        self.stack = QStackedWidget()
        self.trading_page = TradingPage(self.ctx)
        self.portfolio_page = PortfolioPage(self.ctx)
        self.backtest_page = BacktestPage(self.ctx)
        self.auto_page = AutoPage(self.ctx)
        self.settings_page = SettingsPage(self.ctx)
        for page in (
            self.trading_page,
            self.portfolio_page,
            self.backtest_page,
            self.auto_page,
            self.settings_page,
        ):
            self.stack.addWidget(page)

        for page in (
            self.trading_page,
            self.portfolio_page,
            self.auto_page,
            self.settings_page,
            self.backtest_page,
        ):
            page.status.connect(self._set_status)

        self.backtest_page.promote.connect(self._promote_backtest)
        self.settings_page.network_changed.connect(self._on_network_changed)
        self.settings_page.theme_changed.connect(self._on_theme_setting_changed)

        right.addWidget(self.stack, 1)
        self._right_layout = right
        layout.addLayout(right, 1)
        self.setCentralWidget(root)
        self._sync_window_state()

    # ------------------------------------------------------------ status strip
    def _build_status_strip(self) -> None:
        strip = QFrame()
        strip.setObjectName("StatusStrip")
        strip.setFixedHeight(30)
        layout = QHBoxLayout(strip)
        layout.setContentsMargins(14, 0, 10, 0)
        layout.setSpacing(12)

        self.status_label = QLabel("就绪")
        self.status_label.setObjectName("StatusLabel")
        layout.addWidget(self.status_label, 1)

        self.network_label = QLabel("")
        self.network_label.setObjectName("StatusLabel")
        layout.addWidget(self.network_label)

        self.api_label = QLabel("")
        self.api_label.setObjectName("StatusLabel")
        layout.addWidget(self.api_label)

        self.risk_label = QLabel("")
        self.risk_label.setObjectName("StatusLabel")
        layout.addWidget(self.risk_label)

        grip = QSizeGrip(strip)
        grip.setFixedSize(12, 12)
        layout.addWidget(grip, 0, Qt.AlignmentFlag.AlignRight)

        # Frameless windows have no native status bar, so the strip is appended
        # below the page stack in the right-hand column.
        self._right_layout.addWidget(strip)
        self.status_strip = strip

    def _refresh_status(self) -> None:
        state = self.ctx.risk.state
        settings = self.ctx.settings.risk
        parts = []
        if settings.kill_switch:
            parts.append("急停已开启")
        if state.halted_reason:
            parts.append(f"熔断：{state.halted_reason}")
        parts.append(f"今日成交 {state.trades} 笔")
        if state.realized_pnl:
            parts.append(f"今日盈亏 {state.realized_pnl:+.2f}")
        self.risk_label.setText(" · ".join(parts))
        danger = bool(settings.kill_switch or state.halted_reason)
        theme.set_role(self.risk_label, "down" if danger else None)

        network = self.ctx.network
        self.network_label.setText(
            f"{network.label} · {RunMode.PAPER.label}"
        )
        theme.set_role(
            self.network_label, "accent" if network is Network.MAINNET else None
        )
        if self.ctx.rest.has_credentials:
            self.api_label.setText(self.ctx.creds.masked_key)
            theme.set_role(self.api_label, None)
            api_text, api_ok = self.ctx.creds.masked_key, True
        else:
            self.api_label.setText("未配置 API")
            theme.set_role(self.api_label, "down")
            api_text, api_ok = "未配置 API", False

        self.sidebar.account.update_state(
            RunMode.PAPER.label,
            network.label,
            self._paper_equity_text(),
            api_text,
            api_ok,
        )

    def _paper_equity_text(self) -> str:
        """Sidebar snapshot: paper equity, since the strip reports paper mode."""
        paper = self.ctx.paper
        holdings = sum(
            pos.contracts * pos.entry_price
            for pos in paper.account.positions.values()
            if pos.contracts
        )
        return f"{paper.account.cash + holdings:,.2f} USDT"

    # -------------------------------------------------------------- navigation
    def navigate(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        self.sidebar.select(index)

    def toggle_sidebar(self) -> None:
        self.sidebar.set_collapsed(not self.sidebar.collapsed)

    # ------------------------------------------------------------ window chrome
    def toggle_maximized(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()
        self._sync_window_state()

    def _sync_window_state(self) -> None:
        self.title_bar.max_btn.setToolTip("还原" if self.isMaximized() else "最大化")
        self.title_bar.refresh_theme()

    def changeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            self._sync_window_state()

    # --------------------------------------------------------------- resizing
    def _edge_at(self, pos: QPoint) -> Qt.Edge:
        rect = self.rect()
        edges = Qt.Edge(0)
        if pos.x() <= RESIZE_MARGIN:
            edges |= Qt.Edge.LeftEdge
        if pos.x() >= rect.width() - RESIZE_MARGIN:
            edges |= Qt.Edge.RightEdge
        if pos.y() <= RESIZE_MARGIN:
            edges |= Qt.Edge.TopEdge
        if pos.y() >= rect.height() - RESIZE_MARGIN:
            edges |= Qt.Edge.BottomEdge
        return edges

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self.isMaximized():
            self.setCursor(Qt.CursorShape.ArrowCursor)
            return super().mouseMoveEvent(event)
        edges = self._edge_at(event.position().toPoint())
        if edges:
            cursor = Qt.CursorShape.ArrowCursor
            if edges in (
                Qt.Edge.LeftEdge | Qt.Edge.TopEdge,
                Qt.Edge.RightEdge | Qt.Edge.BottomEdge,
            ):
                cursor = Qt.CursorShape.SizeFDiagCursor
            elif edges in (
                Qt.Edge.RightEdge | Qt.Edge.TopEdge,
                Qt.Edge.LeftEdge | Qt.Edge.BottomEdge,
            ):
                cursor = Qt.CursorShape.SizeBDiagCursor
            elif edges & (Qt.Edge.LeftEdge | Qt.Edge.RightEdge):
                cursor = Qt.CursorShape.SizeHorCursor
            else:
                cursor = Qt.CursorShape.SizeVerCursor
            self.setCursor(cursor)
        else:
            self.unsetCursor()
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.LeftButton and not self.isMaximized():
            edges = self._edge_at(event.position().toPoint())
            if edges:
                handle = self.windowHandle()
                if handle is not None:
                    handle.startSystemResize(edges)
                    event.accept()
                    return
        super().mousePressEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt override
        self.unsetCursor()
        super().leaveEvent(event)

    # ------------------------------------------------------------------ theme
    def _on_theme_setting_changed(self, name: str) -> None:
        app = QApplication.instance()
        if app is not None:
            theme.apply_theme(app, name)
        self.ctx.settings.theme = name
        self.ctx.save_settings()

    def _on_theme_changed(self, *_args) -> None:
        for label in (self.status_label, self.api_label):
            theme.set_role(label, None)
        self.sidebar.refresh_theme()
        self.title_bar.refresh_theme()
        self._refresh_status()

    # ---------------------------------------------------------------- routing
    def _promote_backtest(self, result, params: dict) -> None:
        self.navigate(3)
        self.auto_page.symbol_edit.setCurrentText(self.backtest_page.symbol_edit.text().strip())
        self.auto_page.timeframe_combo.setCurrentText(
            self.backtest_page.timeframe_combo.currentText()
        )
        market_index = self.auto_page.market_combo.findData(
            self.backtest_page.market_combo.currentData()
        )
        if market_index >= 0:
            self.auto_page.market_combo.setCurrentIndex(market_index)
        strategy_index = self.auto_page.strategy_combo.findData(
            self.backtest_page.strategy_combo.currentData()
        )
        if strategy_index >= 0:
            self.auto_page.strategy_combo.setCurrentIndex(strategy_index)
        for name, value in params.items():
            widget = self.auto_page._param_widgets.get(name)
            if widget is None:
                continue
            from PySide6.QtWidgets import QCheckBox, QComboBox, QDoubleSpinBox, QSpinBox

            if isinstance(widget, QCheckBox):
                widget.setChecked(bool(value))
            elif isinstance(widget, QComboBox):
                index = widget.findData(value)
                if index >= 0:
                    widget.setCurrentIndex(index)
            elif isinstance(widget, (QSpinBox, QDoubleSpinBox)):
                widget.setValue(value)
        self.auto_page.mode_combo.setCurrentIndex(0)  # always paper first
        self._set_status("已把回测参数带入自动交易，请确认后以纸面模式启动")

    def _on_network_changed(self) -> None:
        self._refresh_status()
        self._set_status(f"环境已切换：{self.ctx.network.label}")

    def _set_status(self, message: str) -> None:
        self.status_label.setText(message)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        try:
            self.auto_page.close()
            for page in (
                self.trading_page,
                self.portfolio_page,
                self.backtest_page,
                self.settings_page,
            ):
                runner = getattr(page, "runner", None)
                if runner is not None:
                    runner.shutdown(2000)
            worker = getattr(self.backtest_page, "_worker", None)
            if worker is not None and worker.isRunning():
                worker.wait(2000)
            self.ctx.shutdown()
        finally:
            super().closeEvent(event)
