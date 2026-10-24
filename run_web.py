"""Local web UI entry point: python run_web.py

Development helper for iterating on the interface without rebuilding the
desktop executable. Serves http://127.0.0.1:8765.

Frontend files are read from disk on every request, so editing CSS/JS/HTML
only needs a browser refresh. Backend changes need a restart of this script.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def _ensure_stdio() -> None:
    """pythonw 起来的进程没有 stdout / stderr，兜到 <数据目录>/logs/console.log。

    pythonw.exe 是 GUI 子系统的解释器，压根不申请控制台：不重定向时 sys.stdout 与
    sys.stderr 都是 None。于是 uvicorn 那几行写不出去、print 是空操作、连 traceback
    都吐不出来——服务在跑，日志一个字都没有（量测就是这么发现 pythonw 起服务等于没有
    日志的）。这里把缺的那条流（两条都缺就都指向同一份文件）接到 console.log 上，
    追加 + 行缓冲：几个进程共用一份也不会互相截断。

    真实的终端里这两条流都在，这个函数什么都不做，也不会凭空多出一个文件。
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    from btrader import paths

    handle = open(
        paths.logs_dir() / "console.log",
        "a",
        buffering=1,
        encoding="utf-8",
        errors="replace",
    )
    if sys.stdout is None:
        sys.stdout = handle
    if sys.stderr is None:
        sys.stderr = handle


def main() -> int:
    parser = argparse.ArgumentParser(description="biTrader local web UI")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    # 先兜住 stdio，再配日志：pythonw 下这两条流是 None，不兜的话下面的 setup_logging
    # 会把一个 None 当控制台流装上去。
    _ensure_stdio()

    from btrader.logging_setup import setup_logging
    from btrader.web.app import create_app
    from btrader.web.__main__ import build_server, serve

    # 和 python -m btrader.web 那条路同一个口径：轮转文件日志
    # （<数据目录>/logs/bitrader.log）、error_buffer、excepthook 都在 setup_logging 里。
    # 这条路以前一次都没调过它，于是 pythonw 起的服务既没有文件日志，界面上的错误
    # 列表也读不到东西。
    setup_logging(logging.INFO, console_stream=sys.stderr)

    # 装配走 web/__main__.build_server，和 `python -m btrader.web` 共用同一份：
    # 在线更新最后一步要重启，updater 拿到的 should_exit 就是这里这个 Server 的。
    # 自己再拼一套 uvicorn.Config/Server，那个机制在真实进程里就等于不存在。
# 这里不开 reload：本脚本是给「改完自己重启一次」用的，没有热重载。
    server = build_server(create_app(), host=args.host, port=args.port, access_log=True)
    return serve(server)


if __name__ == "__main__":
    raise SystemExit(main())
