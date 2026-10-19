"""Global error log viewer.

Every warning and error the application logs is also kept in an in-memory
ring buffer (see :mod:`btrader.logging_setup`). This dialog surfaces that
buffer so a failure in a background worker can be read without hunting for
``%APPDATA%\\biTrader\\logs\\bitrader.log``.
"""

from __future__ import annotations

import logging

import qtawesome as qta
from PySide6.QtCore import QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ... import paths
from ...logging_setup import error_buffer
from .. import theme
from .common import mono_font


class LogViewDialog(QDialog):
    """Non-modal viewer for the captured warnings and errors."""

    cleared = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("错误日志")
        self.setModal(False)
        self.resize(880, 560)
        self.setMinimumSize(560, 360)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        header = QHBoxLayout()
        header.setSpacing(10)
        text = QVBoxLayout()
        text.setSpacing(2)
        self.title = QLabel("错误日志")
        self.title.setObjectName("PageTitle")
        text.addWidget(self.title)
        self.hint = QLabel("记录最近 1000 条警告与错误，最新的在最上面。")
        self.hint.setObjectName("Subtitle")
        text.addWidget(self.hint)
        header.addLayout(text)
        header.addStretch(1)

        self.errors_only = QCheckBox("仅显示错误")
        self.errors_only.toggled.connect(self._toggle_filter)
        header.addWidget(self.errors_only)
        layout.addLayout(header)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        self.count_label = QLabel("")
        self.count_label.setObjectName("Subtitle")
        toolbar.addWidget(self.count_label)
        toolbar.addStretch(1)

        self.refresh_btn = QPushButton("刷新")
        self.refresh_btn.setObjectName("Flat")
        self.refresh_btn.clicked.connect(self.refresh)
        toolbar.addWidget(self.refresh_btn)

        self.copy_btn = QPushButton("复制全部")
        self.copy_btn.setObjectName("Flat")
        self.copy_btn.setToolTip("把当前显示的内容复制到剪贴板")
        self.copy_btn.clicked.connect(self._copy)
        toolbar.addWidget(self.copy_btn)

        self.clear_btn = QPushButton("清空")
        self.clear_btn.setObjectName("Flat")
        self.clear_btn.setToolTip("清空本次运行的内存日志，不影响磁盘日志文件")
        self.clear_btn.clicked.connect(self._clear)
        toolbar.addWidget(self.clear_btn)

        self.open_btn = QPushButton("打开日志文件")
        self.open_btn.setObjectName("Flat")
        self.open_btn.clicked.connect(self._open_folder)
        toolbar.addWidget(self.open_btn)
        layout.addLayout(toolbar)

        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setFont(mono_font(11))
        self.view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.view.setPlaceholderText("暂无警告或错误。")
        layout.addWidget(self.view, 1)

        footer = QHBoxLayout()
        footer.addStretch(1)
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.close)
        footer.addWidget(close_btn)
        layout.addLayout(footer)

        self._timer = QTimer(self)
        self._timer.setInterval(1500)
        self._timer.timeout.connect(self._poll)
        self._timer.start()

        # Last buffer revision rendered; a timer tick with no new record must
        # not rebuild the text, or the scroll position jumps back to the top.
        self._rendered_revision = -1

        self.refresh()

    # ------------------------------------------------------------------ data
    def refresh(self, force: bool = False) -> None:
        buffer = error_buffer()
        errors, warnings = buffer.counts()
        revision = buffer.revision

        if force or revision != self._rendered_revision:
            self._rendered_revision = revision
            records = buffer.records()
            if self.errors_only.isChecked():
                records = [item for item in records if item.level >= logging.ERROR]
            shown = len(records)
            bar = self.view.verticalScrollBar()
            at_top = bar.value() == 0
            position = bar.value()
            # Newest first: the failure that just happened is what you want.
            self.view.setPlainText(
                "".join(item.as_text() for item in reversed(records))
            )
            if not at_top:
                bar.setValue(position)
        else:
            shown = errors + warnings

        total = errors + warnings
        self.count_label.setText(
            f"共 {total} 条 · 错误 {errors} · 警告 {warnings}"
            + (f" · 当前显示 {shown} 条" if shown != total else "")
        )

    def _poll(self) -> None:
        # Only repaint while visible; a hidden window still costs layout work.
        if self.isVisible():
            self.refresh()

    def _copy(self) -> None:
        QApplication.clipboard().setText(self.view.toPlainText())

    def _clear(self) -> None:
        error_buffer().clear()
        self.refresh(force=True)
        self.cleared.emit()

    def _toggle_filter(self) -> None:
        self.refresh(force=True)

    def _open_folder(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(paths.logs_dir())))

    # ---------------------------------------------------------------- theme
    def refresh_theme(self) -> None:
        palette = theme.current()
        self.refresh_btn.setIcon(qta.icon("fa6s.rotate", color=palette.text_dim))
        self.refresh_btn.setIconSize(QSize(11, 11))
        self.open_btn.setIcon(qta.icon("fa6s.folder-open", color=palette.text_dim))
        self.open_btn.setIconSize(QSize(11, 11))

    def showEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        self.refresh_theme()
        self.refresh()
