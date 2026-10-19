"""Application entry point."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication

from . import APP_NAME, __version__
from .logging_setup import setup_logging

log = logging.getLogger(__name__)


def _asset(*parts: str) -> Path | None:
    """Locate a bundled resource in both source and frozen builds."""
    roots = []
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", "")
        if base:
            roots.append(Path(base))
        roots.append(Path(sys.executable).resolve().parent)
    roots.append(Path(__file__).resolve().parents[2])
    for root in roots:
        candidate = root.joinpath(*parts)
        if candidate.exists():
            return candidate
    return None


def _make_icon() -> QIcon:
    bundled = _asset("packaging", "bitrader.ico")
    if bundled is not None:
        return QIcon(str(bundled))

    pixmap = QPixmap(64, 64)
    pixmap.fill(QColor("#0f1115"))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor("#f0b90b"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(10, 10, 44, 44, 10, 10)
    painter.setBrush(QColor("#0f1115"))
    painter.drawRect(24, 18, 6, 28)
    painter.drawRect(34, 22, 6, 20)
    painter.end()
    return QIcon(pixmap)


def main() -> None:
    setup_logging()
    log.info("%s v%s 启动", APP_NAME, __version__)

    QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setOrganizationName(APP_NAME)
    app.setWindowIcon(_make_icon())

    from .ui.theme import apply_theme

    # `apply_theme` registers the bundled Inter faces and sets the app font.
    apply_theme(app)

    from .context import AppContext

    ctx = AppContext()

    from .ui.main_window import MainWindow

    window = MainWindow(ctx)
    window.show()
    exit_code = app.exec()
    ctx.shutdown()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
