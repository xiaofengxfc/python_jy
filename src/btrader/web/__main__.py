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
from ..core import updater as _updater
from ..logging_setup import setup_logging

READY_PREFIX = "BITRADER_READY "

# 优雅退出的宽限：重启时老进程得能自己退出去。一个卡住的请求（大回测、慢导出）没有上限
# 就会把老进程挂在那儿——Windows 上没人接手端口，systemd 那边也是干等到 SIGKILL。
GRACEFUL_TIMEOUT = 30
log = logging.getLogger(__name__)

# uvicorn 的日志走 root 的处理器，不自己挂一个非传播的 StreamHandler。
#
# 两条原因：
#   1. root 上挂着 <数据目录>/logs/bitrader.log 那份文件处理器——uvicorn 的
#      「Started server process / Uvicorn running / 异常」必须落进文件。以前它自己带
#      一个 stderr 处理器且 propagate=False，于是 pythonw / 桌面壳这类没有控制台的
#      进程里，这些行写到 None 上，日志里一个字都没有（量测就是这么发现的）；
#   2. 控制台流由 setup_logging 统一指向 stderr，不会和 stdout 上的
#      BITRADER_READY 握手行搅在一起。
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
        # 交给 root：文件日志（bitrader.log）与控制台是同一套出口，
        # 无控制台的进程里才留得下 uvicorn 的启动行。
        "uvicorn": {"handlers": [], "propagate": True},
        "uvicorn.error": {"propagate": True},
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


def build_server(
    app,
    *,
    host: str,
    port: int,
    log_config: dict | None = None,
    access_log: bool = False,
):
    """装配 uvicorn 的 Config + Server，并把退出开关交给 updater。

    两条启动路径（``python -m btrader.web`` 和仓库根的 ``run_web.py``）都走这里。
    在线更新最后一步要重启，靠的就是 ``updater.set_shutdown_hook`` 拿到的
    ``should_exit``；各写一份装配代码的话，换条路径跑就等于悄悄失去优雅退出。
    端口也在这里交给 updater（``set_web_port``）：更新完的垫子要等的是这个端口，
    不是写死的 8765。
    """
    import uvicorn

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_config=LOG_CONFIG if log_config is None else log_config,
        log_level="info",
        access_log=access_log,
        lifespan="on",
        # 重启时老进程得能自己退出去：一个卡住的请求（大回测、慢导出）没有上限就会把
        # 老进程挂在那儿，Windows 上没人接手端口，systemd 那边也干等到 SIGKILL。
        timeout_graceful_shutdown=GRACEFUL_TIMEOUT,
    )
    server = uvicorn.Server(config)

    _updater.set_shutdown_hook(lambda: setattr(server, "should_exit", True))
    # 重启要跟着真实地址走：垫片拉的就是这一个地址、盯的就是这一个端口。写死
    # 127.0.0.1:8765 的话，任何不在那儿的实例（自带端口、--port 0 临时端口、量测用的
    # 8799、--host 0.0.0.0 的服务器）更新完重启都必然出问题：要么 Errno 10048，
    # 要么服务只剩下回环、外面全连不上。
    _updater.set_web_bind(host, port)
    return server


def serve(server, *, sockets=None) -> int:
    """跑起来，Ctrl+C 不算失败。

    uvicorn 自己会接 SIGINT / SIGTERM 把它变成 ``should_exit``；这里再兜一层
    ``KeyboardInterrupt``，是为了在信号处理器装不上（非主线程、某些终端）时
    也不至于在用户眼前抛一段 traceback。返回 0 表示「按正常方式停了」。
    """
    try:
        if sockets:
            server.run(sockets=list(sockets))
        else:
            server.run()
    except KeyboardInterrupt:  # pragma: no cover - 交互式才走到
        pass
    return 0


def desktop_flags(*, parent_pid: int, watch_stdin: bool) -> list[str]:
    """桌面壳独有的那两个参数（空列表 = 不是壳拉起来的）。

    只有 `--parent-pid` / `--watch-stdin` 算壳的证据：壳盯着这个 pid 与 stdin，进程一换
    就失联，更新最后一步拉起的是裸 run_web.py，这两样一个都不带。

    **`--port 0` 不算**：那是 `python -m btrader.web` 的默认值（让系统挑个空闲端口），
    普通的服务端启动也长这样。把它当成壳的证据，一个 `python -m btrader.web` 起的仓库版
    就会被判成桌面态、整张更新卡换成「已是最新 0.6.x」，而那份 checkout 可能落后好几个提交。
    `--port 0` 的信息不丢：它走 `ephemeral_port()`，只管 apply / rollback 的拒绝文案。
    """
    flags: list[str] = []
    if parent_pid:
        flags.append(f"--parent-pid {parent_pid}")
    if watch_stdin:
        flags.append("--watch-stdin")
    return flags


def ephemeral_port(port: int) -> bool:
    """`--port 0` = 让系统挑临时端口。和「是不是桌面壳」是两件事，别混。"""
    return port == 0


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

    # 先告诉 updater 这个进程是怎么起来的，两件事分开说：
    #   1) 壳参数（--parent-pid / --watch-stdin）——这条路径不许在线更新，也不会换成桌面卡；
    #   2) --port 0 只是「端口由系统临时分配」，照样拦 apply，但不算桌面壳。
    _updater.set_desktop_shell(
        "、".join(
            desktop_flags(parent_pid=args.parent_pid, watch_stdin=args.watch_stdin)
        )
    )
    _updater.set_ephemeral_port(ephemeral_port(args.port))

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

    from .app import create_app

    # 配置与 Server 的装配在 build_server 里，和 run_web.py 共用同一份：
    # 在线更新要重启时，updater 拿到的就是这里的 should_exit。
    server = build_server(create_app(), host=args.host, port=port)

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
        return serve(server, sockets=[sock])
    finally:
        try:
            sock.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
