"""Filesystem locations used by the application."""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_DIR_NAME = "biTrader"


def _base_dir() -> Path:
    override = os.environ.get("BITRADER_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home())
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    return base / APP_DIR_NAME


def data_dir() -> Path:
    path = _base_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def cache_dir() -> Path:
    path = data_dir() / "cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def kline_cache_dir() -> Path:
    path = cache_dir() / "klines"
    path.mkdir(parents=True, exist_ok=True)
    return path


def logs_dir() -> Path:
    path = data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_file() -> Path:
    return data_dir() / "config.json"


def database_file() -> Path:
    return data_dir() / "bitrader.db"


def user_strategies_dir() -> Path:
    path = data_dir() / "strategies"
    path.mkdir(parents=True, exist_ok=True)
    return path


def strategy_candidates_dir() -> Path:
    """AI 生成的候选策略目录；永远不参与 registry 的 import。"""
    path = user_strategies_dir() / "candidates"
    path.mkdir(parents=True, exist_ok=True)
    return path


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]
