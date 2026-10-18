"""Application entry point."""

from __future__ import annotations

import logging
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QIcon, QPixmap, QPainter, QColor
from PySide6.QtWidgets import QApplication

from . import APP_NAME, __version__
from .logging_setup import setup_logging

log = logging.getLogger(__name__)


def _make_icon() -> QIcon:
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
    app.setFont(QFont("Microsoft YaHei UI", 9))

    from .ui.theme import apply_theme

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
