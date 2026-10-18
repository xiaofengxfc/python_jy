"""Application logging configuration."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from . import paths

_CONFIGURED = False


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

    # ccxt / urllib3 are chatty at INFO.
    logging.getLogger("ccxt").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
