"""Keep the local OpenClaw gateway running for as long as biTrader runs.

WeChat push goes through ``openclaw message send``, which needs the gateway's
WebSocket endpoint up. Installing OpenClaw per-user is not enough: if nothing
starts the gateway, every send fails with a connection error that looks like a
misconfiguration.

The gateway is therefore started here, as a child process, and stopped when the
app shuts down. Three rules keep that from becoming a liability:

* Best effort, never blocking. Startup proceeds whether or not the gateway
  comes up; the settings page reports the truth and trading never waits on it.
* Take over only what we started. A gateway the user already runs (or one
  installed as a scheduled task) is left alone, and we never kill it on exit.
* Explain a failure. The gateway writes its bootstrap progress to stderr and
  then exits, so its output is captured and quoted back: "exit code 78" on its
  own is not something a user can act on.
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass

from . import paths

log = logging.getLogger(__name__)

GATEWAY_HOST = "127.0.0.1"
GATEWAY_PORT = 18789
# How long to wait for the port to open before reporting a failure. Measured on
# a warm profile: bootstrap phases (plugin plan, payload verification, agent and
# workspace readiness) reach "starting HTTP server" at ~19 s, so the old 25 s
# budget killed the gateway on anything slower than this dev box. The wait runs
# in the startup thread and never blocks trading, so the cost of being generous
# is only how long a genuinely broken start takes to report.
START_TIMEOUT = 180.0
PROBE_TIMEOUT = 0.35
# ``openclaw config get/set`` is a Node process too, so it is not instant.
CONFIG_TIMEOUT = 30.0
# Child output kept for diagnosis. Bounded because the gateway logs every
# request once it is up, and nobody wants that in memory.
OUTPUT_LINES = 60


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
        self._lock = threading.RLock()
        self._info = _Gateway()
        self._process: subprocess.Popen | None = None
        # Tail of the child's stdout+stderr, drained by a reader thread.
        self._output: deque[str] = deque(maxlen=OUTPUT_LINES)
        # Guards against a second click spawning a second gateway while the
        # first is still probing for the port: the port is closed either way,
        # so ``_reachable`` cannot tell the two attempts apart.
        self._starting = False

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

        with self._lock:
            busy = self._starting
            self._starting = not busy
        if busy:
            log.info("网关启动已在进行中，忽略重复请求")
            return self.status()
        try:
            return self._spawn(exe, retried=False)
        finally:
            with self._lock:
                self._starting = False

    def detach(self) -> None:
        """Forget the child process *without* stopping it.

        Used on app shutdown. Killing the gateway costs the WeChat login: the
        ilink bot token belongs to the running plugin, and after a restart it
        answers ``errcode -14 session timeout`` until the user scans the QR
        again. Leaving the process alive keeps pushes working across app
        restarts, and the next ``start()`` simply adopts it (``_reachable``).
        """
        with self._lock:
            self._process = None
            self._info = _Gateway(state="external")

    def stop(self) -> None:
        """Stop the gateway, but only the one we started."""
        with self._lock:
            process = self._process
            self._process = None
            owned = self._info.state in {"running", "starting"}
            # A gateway that died on its own must not keep reporting "running":
            # the settings page reads that flag to decide whether push works.
            if owned and (process is None or process.poll() is not None):
                self._info = _Gateway(state="idle")
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
        # The socket (and the startup lock) outlive the process for a moment.
        # Returning immediately makes an immediate restart race that lock and
        # fail with "gateway-lock", so wait for the port to actually close.
        deadline = time.time() + 10.0
        while time.time() < deadline and self._reachable():
            time.sleep(0.2)
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
                "output": list(self._output),
            }

    def output(self) -> list[str]:
        """Recent child output, oldest first. Survives a failed attempt."""
        with self._lock:
            return list(self._output)

    # ----------------------------------------------------------------- spawn
    def _spawn(self, exe: str, *, retried: bool) -> dict:
        args = [exe, "gateway", "run"]
        with self._lock:
            self._output.clear()
            self._info = _Gateway(state="starting", command=args, started_at=time.time())

        process = self._launch(args)
        if process is None:
            return self.status()
        with self._lock:
            self._process = process

        if self._await_ready(process):
            return self.status()

        code = process.poll()
        # A gateway that was killed (app shutdown, a failed restart, a taskkill)
        # leaves its startup lock behind for a few seconds. The next launch then
        # exits 1 right after ``gateway-lock``, which reads like a permanent
        # failure; wait for the lock to clear and try once more.
        if not retried and self._died_on_gateway_lock():
            log.info("网关启动锁尚未释放，等待后重试")
            self._note("[biTrader] 网关启动锁未释放，3 秒后重试")
            time.sleep(3.0)
            return self._spawn(exe, retried=True)
        # A refusal to start because ``gateway.mode`` was never authored is the
        # one failure this app can fix by itself: it already installs OpenClaw
        # and registers the WeChat plugin, and ``openclaw doctor`` prescribes
        # exactly this value. Only an explicit "unset" is acted on — a mode the
        # user did author (e.g. remote) is never overwritten.
        if not retried and code is not None and self._mode_configured(exe) is False:
            log.info("gateway.mode 未配置，写入 local 后重试启动网关")
            self._note("[biTrader] gateway.mode 未配置，已设为 local 并重试启动")
            if self._set_mode_local(exe):
                return self._spawn(exe, retried=True)

        return self._report_failure(process, code, args)

    def _died_on_gateway_lock(self) -> bool:
        """The last launch ended at the gateway-lock startup phase.

        Checked on output rather than exit code alone: every early failure exits
        1, and only this one is transient (the lock belongs to a process that is
        still dying).
        """
        with self._lock:
            tail = list(self._output)[-8:]
        return any("gateway-lock" in line for line in tail)

    def _launch(self, args: list[str]) -> subprocess.Popen | None:
        """Spawn ``openclaw gateway run`` fully detached, logging to a file.

        Not a pipe: a pipe ties the gateway's life to this process. When the app
        restarts, the reader closes, the child's next write fails, and the
        gateway dies — which in turn invalidates the WeChat bot session and
        forces a new QR scan. A log file has no such coupling, and the tail of
        it still feeds the failure messages the settings page shows.
        """
        handle = None
        kwargs: dict = {
            "stdin": subprocess.DEVNULL,
            "shell": False,
        }
        try:
            handle = open(self._log_path(), "ab", buffering=0)
            try:
                handle.write(
                    f"\n[biTrader {time.strftime('%Y-%m-%d %H:%M:%S')}] "
                    f"启动网关：{' '.join(args)}\n".encode("utf-8")
                )
            except OSError:
                pass
            kwargs["stdout"] = handle
            kwargs["stderr"] = subprocess.STDOUT
        except OSError:  # pragma: no cover - unwritable data dir
            kwargs["stdout"] = subprocess.DEVNULL
            kwargs["stderr"] = subprocess.DEVNULL
        if os.name == "nt":
            # A new group lets ``stop`` take the whole tree down; DETACHED_PROCESS
            # keeps it alive when the console that started it goes away.
            kwargs["creationflags"] = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
                | getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            )
        else:
            kwargs["start_new_session"] = True
        try:
            process = subprocess.Popen(_wrap(args[0], args), **kwargs)
        except Exception as exc:
            with self._lock:
                self._info = _Gateway(
                    state="failed",
                    command=list(args),
                    error=f"启动 OpenClaw 网关失败：{exc}",
                )
            log.warning("启动 OpenClaw 网关失败", exc_info=True)
            return None
        finally:
            if handle is not None:
                handle.close()
        # The reader is what makes a failed bootstrap readable; it tails the log
        # file instead of a pipe.
        threading.Thread(
            target=self._pump, args=(process,), name="openclaw-gateway", daemon=True
        ).start()
        return process

    def _log_path(self):
        return paths.logs_dir() / "openclaw-gateway.log"

    def _pump(self, process: subprocess.Popen) -> None:
        """Feed the child's diagnostics into the bounded buffer.

        Production writes to a log file (see ``_launch``) and this tails it. A
        plain iterable stdout is still honoured so a caller — or a test — can
        hand over a pipe instead.
        """
        stream = getattr(process, "stdout", None)
        if stream is not None:
            try:
                for line in stream:
                    self._note(line)
            except Exception:  # pragma: no cover - closed pipe
                log.debug("读取网关输出失败", exc_info=True)
            return
        path = self._log_path()
        offset = 0
        try:
            offset = max(0, path.stat().st_size - 64_000)
        except OSError:
            offset = 0
        try:
            while True:
                try:
                    with open(path, "rb") as handle:
                        handle.seek(offset)
                        chunk = handle.read()
                        offset = handle.tell()
                except OSError:
                    chunk = b""
                if chunk:
                    for line in chunk.decode("utf-8", "replace").splitlines():
                        self._note(line)
                if process.poll() is not None:
                    # One last read after exit catches the final diagnostic.
                    try:
                        with open(path, "rb") as handle:
                            handle.seek(offset)
                            rest = handle.read().decode("utf-8", "replace")
                    except OSError:
                        rest = ""
                    for line in rest.splitlines():
                        self._note(line)
                    return
                time.sleep(0.25)
        except Exception:  # pragma: no cover - shutdown race
            log.debug("读取网关日志失败", exc_info=True)

    def _await_ready(self, process: subprocess.Popen) -> bool:
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            if self._reachable():
                with self._lock:
                    self._info.state = "running"
                log.info("OpenClaw 网关已就绪：%s:%s", GATEWAY_HOST, self.port)
                return True
            if process.poll() is not None:
                return False
            time.sleep(0.25)
        return False

    def _report_failure(
        self, process: subprocess.Popen, code: int | None, args: list[str]
    ) -> dict:
        """Kill a half-started gateway and say why it failed.

        The tail of the child's output is the only actionable part: OpenClaw
        reports a rejected bootstrap as a phase name and an exit code.
        """
        # Do not leave a half-started gateway behind for the next launch to fight.
        self.stop()
        if code is not None:
            message = f"OpenClaw 网关启动失败（退出码 {code}）"
        else:
            message = f"OpenClaw 网关在 {self.timeout:g} 秒内没有就绪"
        tail = " | ".join(self.output()[-5:])
        message += f"：{tail[:400]}" if tail else "，且没有任何输出"
        message += "。可在终端手动运行一次 openclaw gateway run 查看原因。"
        with self._lock:
            self._info = _Gateway(state="failed", command=list(args), error=message)
        log.warning("OpenClaw 网关启动失败：%s", message)
        return self.status()

    def _note(self, line: str) -> None:
        text = (line or "").strip()
        if not text:
            return
        with self._lock:
            self._output.append(text)

    # ----------------------------------------------------------- config repair
    @staticmethod
    def _mode_configured(exe: str) -> bool | None:
        """Whether ``gateway.mode`` is authored in OpenClaw's config.

        ``openclaw config get`` exits non-zero and prints "unset" when the key
        was never written; ``gateway run`` then refuses to start. ``None``
        means the check itself failed, and a guess must not touch the user's
        configuration.
        """
        try:
            completed = subprocess.run(
                _wrap(exe, [exe, "config", "get", "gateway.mode"]),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=CONFIG_TIMEOUT,
                shell=False,
                stdin=subprocess.DEVNULL,
            )
            text = ((completed.stdout or "") + (completed.stderr or "")).strip()
            code = completed.returncode
        except Exception:
            log.debug("检查 gateway.mode 失败", exc_info=True)
            return None
        if code != 0 or "unset" in text.lower():
            return False
        return bool(text)

    @staticmethod
    def _set_mode_local(exe: str) -> bool:
        """Author ``gateway.mode=local``. Never called for a user-set mode."""
        try:
            completed = subprocess.run(
                _wrap(exe, [exe, "config", "set", "gateway.mode", "local"]),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=CONFIG_TIMEOUT,
                shell=False,
                stdin=subprocess.DEVNULL,
            )
            code = completed.returncode
            output = ((completed.stderr or "") + (completed.stdout or "")).strip()
        except Exception:
            log.warning("写入 gateway.mode 失败", exc_info=True)
            return False
        if code != 0:
            log.warning("写入 gateway.mode 失败：%s", output[:200])
            return False
        return True

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
