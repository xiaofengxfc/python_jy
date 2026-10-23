"""Local web UI entry point: python run_web.py

Development helper for iterating on the interface without rebuilding the
desktop executable. Serves http://127.0.0.1:8765.

Frontend files are read from disk on every request, so editing CSS/JS/HTML
only needs a browser refresh. Backend changes need a restart of this script.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def main() -> int:
    parser = argparse.ArgumentParser(description="biTrader local web UI")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    from btrader.web.app import create_app
    from btrader.web.__main__ import build_server, serve

    # 装配走 web/__main__.build_server，和 `python -m btrader.web` 共用同一份：
    # 在线更新最后一步要重启，updater 拿到的 should_exit 就是这里这个 Server 的。
    # 自己再拼一套 uvicorn.Config/Server，那个机制在真实进程里就等于不存在。
# 这里不开 reload：本脚本是给「改完自己重启一次」用的，没有热重载。
    server = build_server(create_app(), host=args.host, port=args.port, access_log=True)
    return serve(server)


if __name__ == "__main__":
    raise SystemExit(main())
