"""Application logging configuration.

Besides the rotating file log, warnings and errors are kept in a small
in-memory ring buffer so the UI can show them without the user digging
through ``%APPDATA%\\biTrader\\logs``. Background workers log from their own
threads, so the buffer is lock-guarded and the UI snapshots it from a timer.
"""

from __future__ import annotations

import logging
import sys
import threading
import traceback
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from logging.handlers import RotatingFileHandler

from . import paths

_CONFIGURED = False
MAX_RECORDS = 1000


@dataclass(frozen=True)
class LogRecord:
    """One captured warning/error, already formatted for display."""

    level: int
    logger: str
    message: str
    detail: str
    when: str

    @property
    def level_name(self) -> str:
        return logging.getLevelName(self.level)

    def as_text(self) -> str:
        head = f"{self.when}  {self.level_name:<8} {self.logger}"
        body = f"    {self.message}"
        if self.detail:
            return f"{head}\n{body}\n{self.detail}\n"
        return f"{head}\n{body}\n"


class ErrorLogBuffer(logging.Handler):
    """Ring buffer of recent warnings and errors, safe to read cross-thread."""

    def __init__(self, capacity: int = MAX_RECORDS, level: int = logging.WARNING) -> None:
        super().__init__(level)
        self._records: deque[LogRecord] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._revision = 0

    @property
    def capacity(self) -> int:
        return self._records.maxlen or 0

    @property
    def revision(self) -> int:
        """Bumped on every emit/clear so viewers can skip redundant redraws."""
        with self._lock:
            return self._revision

    def emit(self, record: logging.LogRecord) -> None:
        try:
            detail = ""
            if record.exc_info:
                detail = "".join(traceback.format_exception(*record.exc_info)).rstrip()
            entry = LogRecord(
                level=record.levelno,
                logger=record.name,
                message=record.getMessage(),
                detail=detail,
                when=datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S"),
            )
        except Exception:  # a broken log call must never break the application
            return
        with self._lock:
            self._records.append(entry)
            self._revision += 1

    def records(self) -> list[LogRecord]:
        with self._lock:
            return list(self._records)

    def clear(self) -> None:
        with self._lock:
            self._records.clear()
            self._revision += 1

    def counts(self) -> tuple[int, int]:
        """``(errors, warnings)`` — errors are level ERROR and above."""
        with self._lock:
            errors = sum(1 for item in self._records if item.level >= logging.ERROR)
            warnings = sum(1 for item in self._records if item.level == logging.WARNING)
        return errors, warnings


_buffer: ErrorLogBuffer | None = None
_buffer_lock = threading.Lock()


def error_buffer() -> ErrorLogBuffer:
    """The process-wide buffer (created on first use)."""
    global _buffer
    with _buffer_lock:
        if _buffer is None:
            _buffer = ErrorLogBuffer()
        return _buffer


def install_error_buffer() -> ErrorLogBuffer:
    """Attach the buffer to the root logger. Idempotent."""
    buffer = error_buffer()
    root = logging.getLogger()
    if buffer not in root.handlers:
        root.addHandler(buffer)
        # The buffer is a WARNING+ collector; without this a root logger left
        # at ERROR would silently drop warnings.
        if root.level > logging.WARNING:
            root.setLevel(logging.INFO)
    return buffer


def install_excepthook() -> None:
    """Route uncaught exceptions into the log instead of a silent crash."""
    previous = sys.excepthook

    def _hook(exc_type, exc, tb):  # noqa: ANN001 - sys.excepthook signature
        if issubclass(exc_type, KeyboardInterrupt):
            previous(exc_type, exc, tb)
            return
        logging.getLogger("btrader.unhandled").critical(
            "未捕获异常: %s", exc, exc_info=(exc_type, exc, tb)
        )
        try:
            previous(exc_type, exc, tb)
        except Exception:  # windowed builds have no usable stderr
            pass

    sys.excepthook = _hook


def setup_logging(level: int = logging.INFO) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    _CONFIGURED = True

    root = logging.getLogger()
    root.setLevel(level)

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)-22s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        paths.logs_dir() / "bitrader.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    install_error_buffer()
    install_excepthook()

    # ccxt / urllib3 are chatty at INFO.
    logging.getLogger("ccxt").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
