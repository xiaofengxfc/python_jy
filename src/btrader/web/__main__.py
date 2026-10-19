"""Standalone backend process, launched by the Electron shell.

The shell starts this module as a child process and keeps it alive for the
lifetime of the window:

* the port is chosen by the OS (``--port 0``), so two copies of the app never
  fight over a fixed port;
* the chosen port is announced on stdout as ``BITRADER_READY {json}`` — the
  only line the shell parses, everything else goes to stderr/log file;
* the process exits on its own once the shell is gone, so a crash in the shell
  cannot leave an orphaned server holding a port.

Run it directly for development::

    python -m btrader.web --port 8765
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sys
import threading
import time

from .. import __version__
from ..logging_setup import setup_logging

READY_PREFIX = "BITRADER_READY "
log = logging.getLogger(__name__)

# uvicorn writes its banner and access log to stdout by default, which would
# interleave with the handshake line. Send it to stderr instead.
LOG_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "()": "uvicorn.logging.DefaultFormatter",
            "fmt": "%(levelprefix)s %(message)s",
            "use_colors": False,
        },
        "access": {
            "()": "uvicorn.logging.AccessFormatter",
            "fmt": '%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s',
            "use_colors": False,
        },
    },
    "handlers": {
        "default": {
            "formatter": "default",
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stderr",
        },
        "access": {
            "formatter": "access",
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stderr",
        },
    },
    "loggers": {
        "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": False},
        "uvicorn.error": {"level": "INFO"},
        "uvicorn.access": {
            "handlers": ["access"],
            "level": "WARNING",
            "propagate": False,
        },
    },
}


def bind_socket(host: str, port: int) -> socket.socket:
    """Bind now so the port is known before the server starts serving."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(128)
    sock.setblocking(False)
    return sock


def _watch_parent(pid: int, server) -> None:
    """Stop the server once the launching shell exits."""
    if sys.platform == "win32":
        import ctypes

        SYNCHRONIZE = 0x00100000
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not handle:
            # Parent is already gone.
            os._exit(0)
        try:
            kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)
        finally:
            kernel32.CloseHandle(handle)
        log.info("父进程 %s 已退出，后端随之关闭", pid)
        os._exit(0)

    while os.getppid() == pid:
        time.sleep(1.0)
    log.info("父进程 %s 已退出，后端随之关闭", pid)
    os._exit(0)


def _watch_stdin(server) -> None:
    """Treat EOF on stdin as "the shell is gone".

    Electron closes the pipe on quit; this catches the case where the parent
    process outlives the window (e.g. a detached launcher).
    """
    try:
        sys.stdin.buffer.read(1)
    except Exception:
        pass
    log.info("标准输入已关闭，后端退出")
    server.should_exit = True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="biTrader local backend")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0, help="0 picks a free port")
    parser.add_argument(
        "--parent-pid", type=int, default=0, help="exit when this process exits"
    )
    parser.add_argument(
        "--watch-stdin", action="store_true", help="exit on stdin EOF"
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    setup_logging(
        getattr(logging, str(args.log_level).upper(), logging.INFO),
        console_stream=sys.stderr,
    )

    try:
        sock = bind_socket(args.host, args.port)
    except OSError as exc:
        print(
            f"无法绑定 {args.host}:{args.port} — {exc}",
            file=sys.stderr,
            flush=True,
        )
        return 2

    port = sock.getsockname()[1]

    import uvicorn

    from .app import create_app

    config = uvicorn.Config(
        create_app(),
        host=args.host,
        port=port,
        log_config=LOG_CONFIG,
        log_level="info",
        access_log=False,
        lifespan="on",
    )
    server = uvicorn.Server(config)

    if args.parent_pid:
        threading.Thread(
            target=_watch_parent,
            args=(args.parent_pid, server),
            name="parent-watch",
            daemon=True,
        ).start()
    if args.watch_stdin:
        threading.Thread(
            target=_watch_stdin, args=(server,), name="stdin-watch", daemon=True
        ).start()

    def _announce() -> None:
        """Print the handshake line once uvicorn is actually serving.

        The socket is already listening, so an early connection would queue
        safely — but reporting readiness before the lifespan completed would
        make a failed startup look like a hang on the shell side.
        """
        deadline = time.time() + 30.0
        while time.time() < deadline:
            if server.started:
                print(
                    READY_PREFIX
                    + json.dumps(
                        {"port": port, "pid": os.getpid(), "version": __version__}
                    ),
                    flush=True,
                )
                log.info("biTrader 后端已启动 http://%s:%s", args.host, port)
                return
            if server.should_exit:
                break
            time.sleep(0.02)
        print(
            "BITRADER_ERROR 后端在 30 秒内未能完成启动",
            file=sys.stderr,
            flush=True,
        )

    threading.Thread(target=_announce, name="ready-announce", daemon=True).start()

    try:
        server.run(sockets=[sock])
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        pass
    finally:
        try:
            sock.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
