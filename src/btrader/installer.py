"""Install OpenClaw (and its WeChat plugin) from the settings page.

The push feature only works once a CLI exists on this machine, so the settings
page offers to install it instead of telling the user to go run npm themselves.
That makes this module a *process launcher with a UI attached*, and three rules
follow from it:

* Nothing runs by itself. The page shows the exact command and waits for a
  click; installing software is not something a status poll may decide.
* The command is built from constants plus user-chosen options, and executed
  with ``shell=False``. Free-form input is never concatenated into a shell line.
* Installs are slow and can ask questions, so they run in a background thread
  whose output is streamed into a bounded log the page can read. The trading
  loop never waits on them.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# Verified against the npm registry: the gateway is published unscoped as
# ``openclaw``; the WeChat channel is a separate Tencent package, and
# ``openclaw-weixin-cli`` is only its installer (bin: ``weixin-installer``).
# Installing the gateway under a guessed scope is a 404, which is what this
# pointer looked like before it was checked.
GATEWAY_PACKAGE = "openclaw"
WECHAT_PACKAGE = "@tencent-weixin/openclaw-weixin"
WECHAT_INSTALLER = "@tencent-weixin/openclaw-weixin-cli"
NPM_REGISTRIES = ("https://registry.npmjs.org", "https://registry.npmmirror.com")

INSTALL_TIMEOUT = 900.0
LOG_LINES = 400


@dataclass(frozen=True)
class InstallOptions:
    """What the user chose on the page. Every field is an enumerated choice."""

    with_wechat: bool = True
    registry: str = ""
    node_path: str = ""
    npm_path: str = ""


@dataclass
class _Run:
    """One install attempt, kept so the page can read its tail after it ends."""

    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    state: str = "running"  # running | done | failed | canceled
    command: list[str] = field(default_factory=list)
    lines: deque[str] = field(default_factory=lambda: deque(maxlen=LOG_LINES))
    error: str = ""
    cancel: threading.Event = field(default_factory=threading.Event)


class OpenClawInstaller:
    """Runs ``npm install -g`` for OpenClaw in a background thread.

    At most one install runs at a time; a second click while one is in flight is
    refused rather than queued, because two npm processes writing the same global
    prefix is how a broken install happens.
    """

    def __init__(self, *, timeout: float = INSTALL_TIMEOUT) -> None:
        self.timeout = timeout
        self._lock = threading.Lock()
        self._run: _Run | None = None
        self._thread: threading.Thread | None = None
        self._process: subprocess.Popen | None = None

    # ------------------------------------------------------------------ public
    def status(self) -> dict:
        """What the page needs: what is installed, and what a run is doing."""
        env = self._environment()
        with self._lock:
            run = self._run
            return {
                # ``available`` mirrors the notifier's own test so the page can
                # tell "installed, just needs a target" from "nothing here yet".
                "available": bool(_which("openclaw")),
                "node": env["node"],
                "node_path": env["node_path"],
                "npm": env["npm"],
                "npm_path": env["npm_path"],
                "ready": bool(env["npm_path"]),
                "reason": env["reason"],
                "registries": list(NPM_REGISTRIES),
                "running": bool(run and run.state == "running"),
                "state": run.state if run else "idle",
                "command": list(run.command) if run else [],
                "started_at": run.started_at if run else 0.0,
                "finished_at": run.finished_at if run else 0.0,
                "error": run.error if run else "",
                "log": list(run.lines) if run else [],
            }

    def plan(self, options: InstallOptions) -> list[str]:
        """The argv this options set would run. Shown in the UI before starting."""
        env = self._environment(options)
        npm = options.npm_path.strip() or env["npm_path"] or "npm"
        registry = options.registry.strip()
        if registry and registry not in NPM_REGISTRIES:
            raise ValueError(f"不支持的镜像源：{registry}")
        args = [npm, "install", "--global", GATEWAY_PACKAGE]
        if options.with_wechat:
            # Both halves are needed: the channel package is what the gateway
            # loads, and its CLI is what registers the plugin with it.
            args.extend([WECHAT_PACKAGE, WECHAT_INSTALLER])
        # Keep the resolved registry explicit: a user who picked npmjs must not
        # silently land on a mirror configured in .npmrc, and vice versa.
        args.append(f"--registry={registry or NPM_REGISTRIES[0]}")
        args.extend(["--no-fund", "--no-audit"])
        return args

    def start(self, options: InstallOptions) -> dict:
        """Begin an install. Raises ``ValueError`` when it cannot start."""
        env = self._environment(options)
        if not env["npm_path"]:
            raise ValueError(env["reason"] or "未找到 npm，请先安装 Node.js。")
        args = self.plan(options)
        with self._lock:
            if self._run is not None and self._run.state == "running":
                raise ValueError("已有安装任务在进行中，请等待它结束。")
            run = _Run(command=args)
            self._run = run
            self._thread = threading.Thread(
                target=self._work, args=(run,), name="openclaw-install", daemon=True
            )
        self._thread.start()
        return self.status()

    def cancel(self) -> dict:
        """Ask the running install to stop.

        npm spawns its own children; terminating the process group on Windows is
        the only way to stop the tree, so the runner starts a new group and kills
        that group here.
        """
        with self._lock:
            run = self._run
            process = self._process
        if run is None or run.state != "running":
            return self.status()
        run.cancel.set()
        _terminate(process)
        return self.status()

    def shutdown(self) -> None:
        """Stop a running install during application shutdown."""
        with self._lock:
            run = self._run
            process = self._process
        if run is not None and run.state == "running":
            run.cancel.set()
            _terminate(process)

    # ---------------------------------------------------------------- internals
    def _environment(self, options: InstallOptions | None = None) -> dict:
        node_path = (options.node_path.strip() if options else "") or _which("node") or ""
        npm_path = (options.npm_path.strip() if options else "") or _which("npm") or ""
        reason = ""
        if not node_path:
            reason = "未找到 Node.js：请先安装 Node.js（推荐 20 及以上版本），再回来重试。"
        elif not npm_path:
            reason = "未找到 npm：它通常随 Node.js 一起安装，请检查安装是否完整。"
        elif options and options.npm_path.strip() and not os.path.isfile(options.npm_path.strip()):
            reason = f"指定的 npm 路径不存在：{options.npm_path.strip()}"
            npm_path = ""
        return {
            "node": _version(node_path),
            "node_path": node_path,
            "npm": _version(npm_path),
            "npm_path": npm_path,
            "reason": reason,
        }

    def _work(self, run: _Run) -> None:
        try:
            self._append(run, "$ " + " ".join(run.command))
            # ``start_new_session``/``CREATE_NEW_PROCESS_GROUP`` so cancel can
            # take down the whole npm tree rather than just the shim.
            kwargs: dict = {
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "stdin": subprocess.DEVNULL,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "shell": False,
                "bufsize": 1,
            }
            if os.name == "nt":
                kwargs["creationflags"] = getattr(
                    subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200
                )
            else:
                kwargs["start_new_session"] = True
            process = subprocess.Popen(_wrap_launcher(run.command), **kwargs)
            with self._lock:
                self._process = process
            deadline = time.time() + self.timeout
            assert process.stdout is not None
            for line in process.stdout:
                if run.cancel.is_set() or time.time() > deadline:
                    break
                self._append(run, line.rstrip())
            if run.cancel.is_set():
                _terminate(process)
                process.wait(timeout=5)
                run.state = "canceled"
                run.error = "安装已取消。"
                return
            if time.time() > deadline:
                _terminate(process)
                run.state = "failed"
                run.error = f"安装超过 {self.timeout:g} 秒仍未结束，已中止。"
                return
            code = process.wait(timeout=30)
            if code != 0:
                run.state = "failed"
                run.error = f"npm 返回退出码 {code}，详见下方日志。"
                return
            if not _which("openclaw"):
                # A zero exit with nothing on PATH means the global prefix is
                # not on PATH; that is a real, confusing failure, so name it.
                run.state = "failed"
                run.error = (
                    "安装命令成功，但仍找不到 openclaw；请确认 npm 全局目录在 PATH 中，"
                    "或直接把它的完整路径填到“OpenClaw 路径”。"
                )
                return
            run.state = "done"
            log.info("OpenClaw 安装完成：%s", " ".join(run.command))
        except FileNotFoundError as exc:
            run.state = "failed"
            run.error = f"无法启动 npm：{exc}"
        except Exception as exc:  # pragma: no cover - defensive
            run.state = "failed"
            run.error = f"安装过程出错：{exc}"
            log.warning("OpenClaw 安装失败", exc_info=True)
        finally:
            with self._lock:
                self._process = None
                run.finished_at = time.time()

    def _append(self, run: _Run, line: str) -> None:
        text = (line or "").rstrip()
        if not text:
            return
        with self._lock:
            run.lines.append(text)


def _which(name: str) -> str | None:
    """Locate an executable, honouring PATHEXT so Windows ``.cmd`` shims work."""
    return shutil.which(name)


def _wrap_launcher(args: list[str]) -> list[str]:
    """Run a Windows ``.cmd``/``.bat`` shim, which CreateProcess cannot exec."""
    first = args[0] if args else ""
    if os.name != "nt" or not first.lower().endswith((".cmd", ".bat")):
        return args
    return [os.environ.get("COMSPEC") or "cmd.exe", "/c", *args]


def _terminate(process: subprocess.Popen | None) -> None:
    """Stop a process and, on Windows, the children it spawned."""
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
    except Exception:  # pragma: no cover - best effort
        log.debug("终止安装进程失败", exc_info=True)


def _version(executable: str) -> str:
    """First line of ``--version``, or ``""`` when it cannot be run."""
    if not executable:
        return ""
    try:
        completed = subprocess.run(
            _wrap_launcher([executable, "--version"]),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            shell=False,
        )
    except Exception:
        return ""
    text = (completed.stdout or completed.stderr or "").strip()
    return text.splitlines()[0][:80] if text else ""
