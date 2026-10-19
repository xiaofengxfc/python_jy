"""Keep the local OpenClaw gateway running for as long as biTrader runs.

WeChat push goes through ``openclaw message send``, which needs the gateway's
WebSocket endpoint up. Installing OpenClaw per-user is not enough: if nothing
starts the gateway, every send fails with a connection error that looks like a
misconfiguration.

The gateway is therefore started here, as a child process, and stopped when the
app shuts down. Two rules keep that from becoming a liability:

* Best effort, never blocking. Startup proceeds whether or not the gateway
  comes up; the settings page reports the truth and trading never waits on it.
* Take over only what we started. A gateway the user already runs (or one
  installed as a scheduled task) is left alone, and we never kill it on exit.
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

GATEWAY_HOST = "127.0.0.1"
GATEWAY_PORT = 18789
# How long to wait for the port to open before reporting a failure. OpenClaw
# loads a lot of plugins on start; 25 s is generous without hanging shutdown.
START_TIMEOUT = 25.0
PROBE_TIMEOUT = 0.35


@dataclass
class _Gateway:
    state: str = "idle"  # idle | starting | running | failed | external
    command: list[str] | None = None
    error: str = ""
    started_at: float = 0.0


class GatewaySupervisor:
    """Owns one ``openclaw gateway`` child process, if we started it."""

    def __init__(self, *, port: int = GATEWAY_PORT, timeout: float = START_TIMEOUT) -> None:
        self.port = port
        self.timeout = timeout
        self._lock = threading.Lock()
        self._info = _Gateway()
        self._process: subprocess.Popen | None = None

    # ------------------------------------------------------------------ public
    def start(self) -> dict:
        """Start the gateway unless one is already listening.

        Called from the app's startup path in a background thread, so a missing
        CLI or a slow start delays nothing the user is waiting on.
        """
        if self._reachable():
            # Someone else's gateway: leave it running, and never stop it.
            with self._lock:
                self._info = _Gateway(state="external")
            log.info("检测到已在运行的 OpenClaw 网关，不再重复启动")
            return self.status()

        exe = shutil.which("openclaw")
        if not exe:
            with self._lock:
                self._info = _Gateway(
                    state="failed",
                    error="未找到 openclaw，微信推送不可用；可在设置页自动安装。",
                )
            return self.status()

        args = [exe, "gateway", "run"]
        with self._lock:
            self._info = _Gateway(state="starting", command=args, started_at=time.time())
        try:
            kwargs: dict = {
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
                "stdin": subprocess.DEVNULL,
                "shell": False,
            }
            if os.name == "nt":
                # A new group lets ``stop`` take the whole tree down; the gateway
                # is a Node process that may spawn workers of its own.
                kwargs["creationflags"] = getattr(
                    subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200
                )
            else:
                kwargs["start_new_session"] = True
            process = subprocess.Popen(_wrap(exe, args), **kwargs)
        except Exception as exc:
            with self._lock:
                self._info.state = "failed"
                self._info.error = f"启动 OpenClaw 网关失败：{exc}"
            log.warning("启动 OpenClaw 网关失败", exc_info=True)
            return self.status()

        with self._lock:
            self._process = process

        deadline = time.time() + self.timeout
        while time.time() < deadline:
            if self._reachable():
                with self._lock:
                    self._info.state = "running"
                log.info("OpenClaw 网关已就绪：%s:%s", GATEWAY_HOST, self.port)
                return self.status()
            if process.poll() is not None:
                break
            time.sleep(0.25)

        code = process.poll()
        with self._lock:
            self._info.state = "failed"
            self._info.error = (
                f"OpenClaw 网关在 {self.timeout:g} 秒内没有就绪"
                + (f"（进程已退出，退出码 {code}）" if code is not None else "")
                + "。可在终端手动运行一次 openclaw gateway run 查看原因。"
            )
        # Do not leave a half-started gateway behind for the next launch to fight.
        self.stop()
        return self.status()

    def stop(self) -> None:
        """Stop the gateway, but only the one we started."""
        with self._lock:
            process = self._process
            self._process = None
            owned = self._info.state == "running"
        if process is None or process.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                    capture_output=True,
                    shell=False,
                    timeout=10,
                )
            else:
                process.terminate()
                process.wait(timeout=5)
        except Exception:  # pragma: no cover - best effort on exit
            log.debug("停止 OpenClaw 网关失败", exc_info=True)
        if owned:
            with self._lock:
                self._info = _Gateway(state="idle")

    def status(self) -> dict:
        """What the settings page needs: is push actually able to reach a gateway."""
        with self._lock:
            info = self._info
            reachable = self._reachable()
            return {
                "state": info.state,
                "reachable": reachable,
                "managed": info.state in {"running", "starting"},
                "host": GATEWAY_HOST,
                "port": self.port,
                "command": list(info.command) if info.command else [],
                "error": info.error,
                "started_at": info.started_at,
            }

    # ---------------------------------------------------------------- internals
    def _reachable(self) -> bool:
        try:
            with socket.create_connection((GATEWAY_HOST, self.port), timeout=PROBE_TIMEOUT):
                return True
        except OSError:
            return False


def _wrap(exe: str, args: list[str]) -> list[str]:
    """Run a Windows ``.cmd`` shim, which CreateProcess cannot execute directly."""
    if os.name != "nt" or not exe.lower().endswith((".cmd", ".bat")):
        return args
    return [os.environ.get("COMSPEC") or "cmd.exe", "/c", *args]
