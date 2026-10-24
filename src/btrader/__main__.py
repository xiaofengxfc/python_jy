"""Application entry point: run the local backend and open it in a browser.

The packaged desktop build does not use this module — the Electron shell
launches ``btrader.web.__main__`` directly. This entry exists for source
runs::

    python -m btrader

命令行更新走同一个入口（服务器上更方便，不用记第二个脚本）::

    bitrader update check | apply | status | rollback
"""

from __future__ import annotations

import socket
import sys
import threading
import webbrowser

from .web.__main__ import main as backend_main


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "update":
        # 更新子命令不启动服务：它在服务器上要能在「服务坏了」的时候跑。
        from .update_cli import main as update_main

        return update_main(args[1:])
    port = _free_port()
    url = f"http://127.0.0.1:{port}/"
    # Give uvicorn a moment to bind before the browser hits the URL; a raced
    # request just shows the browser's retry page.
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    return backend_main(["--port", str(port)])


if __name__ == "__main__":
    raise SystemExit(main())
