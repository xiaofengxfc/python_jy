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

import io
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# Verified against the npm registry: the gateway is published unscoped as
# ``openclaw``; the企业微信 channel is Tencent's own plugin package, registered
# through the gateway (`openclaw plugins install`) rather than a global npm -g.
GATEWAY_PACKAGE = "openclaw"
WECOM_PACKAGE = "@wecom/wecom-openclaw-plugin"
# The channel id the gateway knows the plugin by (package.json openclaw.channel.id).
WECOM_CHANNEL = "wecom"
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


@dataclass
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
        # The 企业微信 plugin is deliberately *not* installed globally here: the
        # gateway keeps its own plugin ledger, and a package that only sits in
        # the npm prefix is invisible to it. ``channel_plan`` registers it.
        # Keep the resolved registry explicit: a user who picked npmjs must not
        # silently land on a mirror configured in .npmrc, and vice versa.
        args.append(f"--registry={registry or NPM_REGISTRIES[0]}")
        args.extend(["--no-fund", "--no-audit"])
        return args

    def channel_plan(self) -> list[str]:
        """The argv that registers the 企业微信 plugin with the gateway.

        ``npm install -g`` only puts files on disk. The gateway has its own
        plugin ledger, and a channel it has never heard of makes ``message send``
        fail — so this step is what turns "npm is done" into "the gateway knows
        about 企业微信".
        """
        exe = _openclaw_executable()
        if not exe:
            raise ValueError("未找到 openclaw，请先完成上面的自动安装。")
        return [
            exe,
            "plugins",
            "install",
            WECOM_PACKAGE,
            "--force",
            "--accept-capabilities",
        ]

    def channel_status(self) -> dict:
        """企业微信渠道（长连接）的实时状态。

        读 ``channels status --json``：插件是否注册、是否已填 Bot ID/Secret、
        长连接是否在跑。个人微信那套「扫码 → 绑定 → 会话上下文」已经删除：
        企微机器人用 BotID + Secret 认证一次，并保持一条出站 WebSocket，
        没有会过期的登录态，也没有需要重新扫的二维码。
        """
        exe = _openclaw_executable()
        info = {
            "channel": WECOM_CHANNEL,
            "cli": bool(exe),
            "plugin": False,
            "configured": False,
            "running": False,
            "enabled": True,
            "lifecycle": "",
            "account": "",
            "state": "unknown",
            "detail": "",
        }
        if not exe:
            info["state"] = "missing"
            info["detail"] = "未安装 openclaw"
            return info

        live = _channels_status_json(exe, WECOM_CHANNEL)
        if live is not None:
            info.update(live)
        else:
            # Older CLIs have no JSON status; the table still says whether the
            # channel is registered, configured and enabled.
            text = _run_capture([exe, "channels", "list", "--all"])
            for line in text.splitlines():
                stripped = line.strip().lstrip("-* ").strip()
                if not stripped.lower().startswith(WECOM_CHANNEL):
                    continue
                info["plugin"] = True
                lowered = stripped.lower()
                info["configured"] = (
                    "not configured" not in lowered and "unconfigured" not in lowered
                )
                info["enabled"] = "disabled" not in lowered
                info["detail"] = stripped[:200]
                break

        if not info["plugin"]:
            info["state"] = "missing"
            info["detail"] = info["detail"] or "网关中未注册企业微信渠道"
        elif info["configured"]:
            info["state"] = "ready"
            info["detail"] = _channel_detail(info)
        else:
            info["state"] = "installed"
            info["detail"] = info["detail"] or "插件已装，但还没填 Bot ID / Secret"
        return info

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

    def register_channel(self) -> dict:
        """Register the 企业微信 plugin with the gateway, streamed to the log.

        A separate run from the npm install: the gateway keeps its own plugin
        ledger, so a package that only sits in the global npm prefix is invisible
        to it and ``message send`` fails with an unknown channel.
        """
        args = self.channel_plan()
        with self._lock:
            if self._run is not None and self._run.state == "running":
                raise ValueError("已有安装任务在进行中，请等待它结束。")
            run = _Run(command=args)
            self._run = run
            self._thread = threading.Thread(
                target=self._work_register,
                args=(run,),
                name="openclaw-channel",
                daemon=True,
            )
        self._thread.start()
        return self.status()

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

    def _work_register(self, run: _Run) -> None:
        """Run ``plugins install <wecom package>`` and check the ledger after."""
        try:
            self._append(run, "$ " + " ".join(run.command))
            code = _stream_process(run, self._set_process, run.command)
            if run.cancel.is_set():
                run.state = "canceled"
                run.error = "插件注册已取消。"
                return
            if code != 0:
                run.state = "failed"
                run.error = f"openclaw 返回退出码 {code}，详见下方日志。"
                return
            status = self.channel_status()
            if not status["plugin"]:
                # exit 0 but the gateway still cannot see it: usually a pinned
                # build the running gateway refuses to load.
                run.state = "failed"
                run.error = (
                    "插件已下载，但网关未能加载它。请查看日志中的加载错误；"
                    "通常是插件版本与 OpenClaw 版本不兼容。"
                )
                return
            run.state = "done"
            log.info("企业微信插件已注册：%s", WECOM_CHANNEL)
        except FileNotFoundError as exc:
            run.state = "failed"
            run.error = f"无法启动 openclaw：{exc}"
        except Exception as exc:  # pragma: no cover - defensive
            run.state = "failed"
            run.error = f"插件注册出错：{exc}"
            log.warning("注册企业微信插件失败", exc_info=True)
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

    @staticmethod
    def _set_process(self, process: subprocess.Popen | None) -> None:
        with self._lock:
            self._process = process


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


def _openclaw_executable() -> str:
    """Full path of the gateway CLI, or ``""``. A bare name breaks on Windows."""
    return _which("openclaw") or ""


def _run_capture(args: list[str], timeout: float = 30.0) -> str:
    """Run a short read-only command and return its combined output."""
    try:
        completed = subprocess.run(
            _wrap_launcher(args),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
            stdin=subprocess.DEVNULL,
        )
    except Exception:
        log.debug("执行 %s 失败", args, exc_info=True)
        return ""
    return (completed.stdout or "") + (completed.stderr or "")


# --------------------------------------------------------------------- wechat
def _state_dir() -> Path:
    """Where the gateway keeps its per-user state.

    Mirrors the plugin's own resolution (``OPENCLAW_STATE_DIR``, then the legacy
    ``CLAWDBOT_STATE_DIR``, then ``~/.openclaw``) so a machine that relocated the
    state directory still finds its bound accounts.
    """
    for name in ("OPENCLAW_STATE_DIR", "CLAWDBOT_STATE_DIR"):
        value = os.environ.get(name, "").strip()
        if value:
            return Path(value).expanduser()
    return Path.home() / ".openclaw"


def _forget_account(account: str) -> list[str]:
    """Delete one account's files and drop it from the plugin's index.

    Returns the names removed, so the caller can report precisely what changed.
    """
    if not account:
        return []
    root = _state_dir() / "openclaw-weixin"
    folder = root / "accounts"
    removed: list[str] = []
    for suffix in (".json", ".context-tokens.json", ".sync.json"):
        path = folder / f"{account}{suffix}"
        try:
            if path.is_file():
                path.unlink()
                removed.append(path.name)
        except OSError:
            log.debug("无法删除 %s", path, exc_info=True)
    index = root / "accounts.json"
    try:
        listed = json.loads(index.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        listed = None
    if isinstance(listed, list):
        kept = [item for item in listed if item != account]
        try:
            if kept:
                index.write_text(
                    json.dumps(kept, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
            else:
                index.unlink()
            removed.append(index.name)
        except OSError:
            log.debug("无法更新 %s", index, exc_info=True)
    return removed


def restart_gateway_process() -> tuple[int, str]:
    """Ask the CLI to restart a gateway this app does not own.

    ``GatewaySupervisor.stop`` deliberately only kills its own child: a gateway
    started from a terminal (which is what the login wizard leaves behind) keeps
    running, so "重启网关" would silently do nothing. The CLI's own restart is
    the one command that works for both.
    """
    exe = _openclaw_executable()
    if not exe:
        return 1, "未找到 openclaw"
    return _run_command([exe, "gateway", "restart"], timeout=120.0)


def _run_command(args: list[str], timeout: float = 60.0) -> tuple[int, str]:
    """Run a short command and return ``(exit_code, combined output)``."""
    try:
        completed = subprocess.run(
            _wrap_launcher(args),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
            stdin=subprocess.DEVNULL,
        )
    except Exception as exc:
        log.debug("执行 %s 失败", args, exc_info=True)
        return 1, str(exc)
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


def _channels_status_json(exe: str, channel: str) -> dict | None:
    """Live channel state from ``channels status --json``, or ``None``.

    Returns the subset the settings page needs, or ``None`` when the CLI is too
    old to answer in JSON (the caller then falls back to parsing the table).
    """
    text = _run_capture([exe, "channels", "status", "--json"])
    payload = _last_json_object(text)
    if payload is None:
        return None
    channels = payload.get("channels")
    if channels is None:
        # A gateway that is still booting answers with an error envelope that
        # may still parse as JSON. Treating that as "no channel" made a bound,
        # working account flip to 未注册插件 for a page refresh; fall back to
        # the local inventory instead.
        return None
    entry: dict = {}
    if isinstance(channels, dict):
        candidate = channels.get(channel)
        if isinstance(candidate, dict):
            entry = candidate
    elif isinstance(channels, list):
        for candidate in channels:
            if isinstance(candidate, dict) and candidate.get("channel") == channel:
                entry = candidate
                break
    if not entry:
        # The channel is known to the gateway only when it appears at all; an
        # absent entry is authoritative only when the payload actually carries
        # an ordering, otherwise the shape is not one we can read.
        order = payload.get("channelOrder")
        if not isinstance(order, list):
            return None
        if channel in order:
            return {"plugin": True, "configured": False, "running": False}
        return {"plugin": False, "configured": False, "running": False}
    return {
        "plugin": True,
        "configured": bool(entry.get("configured")),
        "running": bool(entry.get("running")),
        "enabled": bool(entry.get("enabled", True)),
        "lifecycle": str(entry.get("lifecycle") or ""),
        "account": str(entry.get("accountId") or ""),
        "error": str(entry.get("lastError") or ""),
    }


def _last_json_object(text: str) -> dict | None:
    """Decode the last JSON object in ``text``; log noise may precede it."""
    if not text:
        return None
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except ValueError:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _channel_detail(info: dict) -> str:
    """One-line summary the settings page shows under the channel card."""
    if not info.get("plugin"):
        return "网关中未注册企业微信渠道，请先安装插件。"
    if not info.get("configured"):
        return "插件已安装，但还没填 Bot ID / Secret。"
    state = "长连接运行中" if info.get("running") else (
        f"已配置，但长连接未运行（{info.get('lifecycle') or 'stopped'}）"
    )
    account = info.get("account") or ""
    return f"{state}，账号 {account}。" if account else state


def _stream_process(
    run: _Run, store, args: list[str], timeout: float = INSTALL_TIMEOUT
) -> int:
    """Run a command, appending its output to ``run`` until it exits.

    Shared by every step that reports progress to the page, so cancel and the
    log behave the same whether we are installing or registering a plugin.
    """
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
        # A new process group is what lets ``_terminate`` kill the whole tree.
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen(_wrap_launcher(args), **kwargs)
    store(process)
    deadline = time.time() + timeout
    try:
        assert process.stdout is not None
        for line in process.stdout:
            if run.cancel.is_set() or time.time() > deadline:
                break
            text = (line or "").rstrip()
            if text:
                run.lines.append(text)
        if run.cancel.is_set():
            _terminate(process)
            process.wait(timeout=5)
            return -1
        if time.time() > deadline:
            _terminate(process)
            process.wait(timeout=5)
            return -1
        return process.wait(timeout=30)
    finally:
        store(None)


def _spawn_terminal(args: list[str]) -> None:
    """Open a visible terminal window and run ``args`` in it.

    The login wizard draws a QR code and waits for a scan. That only works in a
    real console, so this deliberately detaches: the window outlives the server
    request, and the user finishes the flow there.
    """
    if os.name == "nt":
        # ``start`` needs a title argument first, otherwise it eats the command.
        subprocess.Popen(
            ["cmd", "/c", "start", "OpenClaw 微信扫码登录", "cmd", "/k", *args],
            shell=False,
            close_fds=True,
        )
        return
    subprocess.Popen(args, shell=False, start_new_session=True)

