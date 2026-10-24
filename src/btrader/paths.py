"""Filesystem locations used by the application."""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_DIR_NAME = "biTrader"

# 开发目录：源码跑起来（run_web.py、python -m btrader.web、electron dev）落这里，
# 冻结的桌面端落 APP_DIR_NAME。两套目录分开，自己调试时造的配置与成交不会进桌面端的库。
DEV_APP_DIR_NAME = "biTrader-dev"


def is_frozen_build() -> bool:
    """这个进程是不是冻结的桌面端（PyInstaller）。

    只认 sys.frozen。壳参数那套（--parent-pid / --watch-stdin）说明的是「谁拉起了它」，
    而 electron 的 dev 模式也是壳拉起来的、跑的还是仓库源码——它该落开发目录。
    """
    return bool(getattr(sys, "frozen", False))


def data_dir_label() -> str:
    """这批数据属于谁，一行字。日志与 /api/health 用，省得对着路径猜。"""
    return "desktop" if is_frozen_build() else "dev"


def _base_dir() -> Path:
    override = os.environ.get("BITRADER_DATA_DIR")
    if override:
        return Path(override).expanduser()
    name = APP_DIR_NAME if is_frozen_build() else DEV_APP_DIR_NAME
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home())
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    return base / name


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
    """AI 生成的候选策略目录。这个目录永远不参与 registry 的 import。"""
    path = user_strategies_dir() / "candidates"
    path.mkdir(parents=True, exist_ok=True)
    return path


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]
