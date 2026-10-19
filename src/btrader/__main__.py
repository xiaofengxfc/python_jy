"""Application entry point: run the local backend and open it in a browser.

The packaged desktop build does not use this module — the Electron shell
launches ``btrader.web.__main__`` directly. This entry exists for source
runs::

    python -m btrader
"""

from __future__ import annotations

import socket
import threading
import webbrowser

from .web.__main__ import main as backend_main


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def main() -> int:
    port = _free_port()
    url = f"http://127.0.0.1:{port}/"
    # Give uvicorn a moment to bind before the browser hits the URL; a raced
    # request just shows the browser's retry page.
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    return backend_main(["--port", str(port)])


if __name__ == "__main__":
    raise SystemExit(main())
