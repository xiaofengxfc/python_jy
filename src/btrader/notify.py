"""Outbound WeChat notifications through a local OpenClaw gateway.

The WeChat ClawBot plugin (``@tencent-weixin/openclaw-weixin-cli``) binds a
personal WeChat account to an OpenClaw Gateway running on this machine. Once the
plugin is installed and the account has been bound by scanning a QR code, the
gateway exposes the bound conversation as a regular OpenClaw channel, so pushes
go out through OpenClaw's own message CLI::

    openclaw message send --channel <channel> --target <id> --message <text> --json

That CLI is the stable, documented interface (``docs.openclaw.ai/cli/message``);
there is no public per-message HTTP endpoint for a channel send, so this module
shells out instead of inventing one. A missing CLI, a logged-out plugin or a
gateway that is not running are normal, recoverable conditions: they are
logged, surfaced to the settings page, and never allowed to interrupt trading.

Sends go through a bounded background queue. A hanging gateway must not stall an
order submission or the paper reconciler, which run on critical paths.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# Both forms are used in the wild: the plugin's manifest id carries no scope
# prefix, while older guides spell the channel after the npm package. The
# settings page shows this value so a user whose gateway names it differently
# can correct it instead of being told "send failed" with no context.
DEFAULT_CHANNEL = "openclaw-weixin"
DEFAULT_COMMAND = "openclaw"
# Chat text is cheap for us and a wall of it is unreadable on a lock screen.
MAX_MESSAGE_CHARS = 2000


class NotifyError(RuntimeError):
    """A message could not be handed to the WeChat channel."""


@dataclass
class NotifySettings:
    """Where and whether to push notifications.

    ``target`` is the WeChat conversation OpenClaw should deliver to. Leaving it
    empty makes the CLI refuse the send, which is reported rather than guessed.
    """

    enabled: bool = False
    channel: str = DEFAULT_CHANNEL
    target: str = ""
    command: str = DEFAULT_COMMAND
    # Events are opt-in individually so an active strategy cannot spam the phone.
    on_fill: bool = True
    on_protection: bool = True
    on_risk_halt: bool = True
    on_live_warning: bool = True


@dataclass
class _Job:
    text: str
    queued_at: float = field(default_factory=time.time)


class WeChatNotifier:
    """Queue + worker around ``openclaw message send``.

    ``settings`` is read on every send, so the web layer can change it at runtime
    without rebuilding the notifier.
    """

    def __init__(
        self,
        settings: NotifySettings,
        *,
        timeout: float = 20.0,
        queue_size: int = 50,
    ) -> None:
        self.settings = settings
        self.timeout = timeout
        self._queue: queue.Queue[_Job] = queue.Queue(maxsize=queue_size)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        # Last outcome, shown in the settings page. Deliberately not persisted:
        # a stale failure from last week is noise, not status. ``*_at`` values
        # are wall-clock seconds, so the page can say how long ago it happened.
        self.last_error: str = ""
        self.last_error_at: float = 0.0
        self.last_success_at: float = 0.0
        self.dropped: int = 0

    # ------------------------------------------------------------------- public
    def notify(self, text: str, *, kind: str = "") -> bool:
        """Queue one message. Returns ``False`` when it was not accepted.

        Never raises and never blocks: a notification is best-effort by design.
        """
        if not self.settings.enabled:
            return False
        if not self._event_enabled(kind):
            return False
        text = _prepare(text)
        if not text:
            return False
        self._ensure_worker()
        try:
            self._queue.put_nowait(_Job(text))
        except queue.Full:
            # Dropping the newest is honest: the channel is wedged, and blocking
            # the caller (a fill handler) to wait for it would be worse.
            self.dropped += 1
            log.warning("微信通知队列已满，丢弃一条消息")
            return False
        return True

    def send_now(self, text: str) -> dict:
        """Send synchronously, for the settings page's “测试” button.

        Raises :class:`NotifyError` with the CLI's own message so the user can
        fix the cause (未安装 / 未登录 / target 为空).

        This checks the target and the CLI but deliberately *not*
        ``settings.enabled``: the button exists to verify the wiring before the
        user trusts the stream with real fills, and a successful test is what
        justifies turning the stream on.
        """
        text = _prepare(text)
        if not text:
            raise NotifyError("消息内容为空")
        if not str(self.settings.target or "").strip():
            raise NotifyError("尚未填写微信接收目标，无法发送。")
        try:
            return self._run(text)
        except NotifyError as exc:
            self._record_failure(str(exc))
            raise

    def status(self) -> dict:
        return {
            "enabled": self.settings.enabled,
            "channel": self.settings.channel,
            "target": self.settings.target,
            "command": self.settings.command,
            "available": bool(self._resolve_command()),
            "last_error": self.last_error,
            "last_error_at": self.last_error_at,
            "last_success_at": self.last_success_at,
            "dropped": self.dropped,
            "pending": self._queue.qsize(),
        }

    def stop(self) -> None:
        self._stop.set()
        # Wake the worker so it can observe the flag instead of waiting out the
        # queue-timeout on an empty queue.
        try:
            self._queue.put_nowait(_Job(""))
        except queue.Full:
            pass
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=2.0)

    # ------------------------------------------------------------------ worker
    def _ensure_worker(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._work, name="wechat-notify", daemon=True
            )
            self._thread.start()

    def _work(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if not job.text:
                continue
            try:
                if not str(self.settings.target or "").strip():
                    # Enqueued while a target was set, target cleared before we
                    # got here. Retrying would only repeat the same refusal.
                    continue
                self._run(job.text, from_queue=True)
            except NotifyError as exc:
                self._record_failure(str(exc))
                log.warning("微信通知发送失败：%s", exc)
            except Exception as exc:  # pragma: no cover - defensive
                self._record_failure(f"发送异常：{exc}")
                log.warning("微信通知异常", exc_info=True)

    # -------------------------------------------------------------------- send
    def _event_enabled(self, kind: str) -> bool:
        s = self.settings
        return {
            "fill": s.on_fill,
            "protection": s.on_protection,
            "risk": s.on_risk_halt,
            "warning": s.on_live_warning,
            "manual": True,
        }.get(kind, True)

    def _resolve_command(self) -> str | None:
        command = (self.settings.command or DEFAULT_COMMAND).strip()
        if not command:
            return None
        # ``shutil.which`` on Windows appends PATHEXT, so a bare "openclaw" finds
        # the npm ``openclaw.cmd`` shim as well as a real .exe.
        found = shutil.which(command)
        if found:
            return found
        # An absolute path that exists is usable even when it is not on PATH.
        try:
            if os.path.isfile(command):
                return command
        except OSError:
            pass
        return None

    def _run(self, text: str, *, from_queue: bool = False) -> dict:
        command = self._resolve_command()
        if not command:
            raise NotifyError(
                f"未找到 {self.settings.command or DEFAULT_COMMAND}，请安装 OpenClaw，"
                "或在设置中填写可执行文件的完整路径。"
            )
        channel = (self.settings.channel or DEFAULT_CHANNEL).strip() or DEFAULT_CHANNEL
        target = str(self.settings.target or "").strip()
        if not target:
            raise NotifyError("尚未填写微信接收目标，无法发送。")
        args = [
            command,
            "message",
            "send",
            "--channel",
            channel,
            "--target",
            target,
            "--message",
            text,
            "--json",
        ]
        try:
            completed = subprocess.run(
                self._wrap_launcher(args),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                shell=False,
            )
        except FileNotFoundError as exc:
            raise NotifyError(f"未找到 {command}：{exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise NotifyError(
                f"OpenClaw 在 {self.timeout:g} 秒内没有响应，请检查网关是否在运行。"
            ) from exc
        except OSError as exc:
            raise NotifyError(f"调用 OpenClaw 失败：{exc}") from exc

        stdout = (completed.stdout or "").strip()
        if completed.returncode != 0:
            detail = (completed.stderr or stdout or "").strip()
            raise NotifyError(detail[:500] or f"OpenClaw 返回退出码 {completed.returncode}")
        # A non-zero exit is not the only failure mode: a suppressed or partially
        # delivered send exits nonzero too, but a gateway that answers with
        # ``{"ok": false}`` and status 0 must not be reported as success.
        payload = _parse_json(stdout)
        if isinstance(payload, dict) and payload.get("ok") is False:
            reason = str(payload.get("error") or "OpenClaw 报告发送失败")
            raise NotifyError(reason[:500])
        if from_queue:
            log.info("微信通知已发出（%s）", str(payload.get("messageId") or "") if payload else "")
        self.last_error = ""
        self.last_error_at = 0.0
        self.last_success_at = time.time()
        return {"ok": True, "stdout": stdout[:2000]}

    def _record_failure(self, message: str) -> None:
        self.last_error = message
        self.last_error_at = time.time()

    @staticmethod
    def _wrap_launcher(args: list[str]) -> list[str]:
        """Make a Windows ``.cmd``/``.bat`` shim executable by CreateProcess.

        npm installs the CLI as ``openclaw.cmd``; Windows cannot exec that
        directly with ``shell=False``. Routing through ``cmd.exe /c`` keeps the
        argument list intact (``list2cmdline`` quotes it) instead of handing a
        single command string to a shell.
        """
        first = args[0] if args else ""
        if os.name != "nt" or not first.lower().endswith((".cmd", ".bat")):
            return args
        comspec = os.environ.get("COMSPEC") or "cmd.exe"
        return [comspec, "/c", *args]

    # --------------------------------------------------------------- formatting
    @staticmethod
    def format_fill(
        *,
        symbol: str,
        side: str,
        amount: float,
        price: float,
        mode: str,
        realized: float | None = None,
        market: str = "",
    ) -> str:
        action = "买入" if side == "buy" else "卖出"
        where = "实盘" if mode == "live" else "纸面"
        venue = {"spot": "现货", "usdm": "U 本位", "coinm": "币本位"}.get(market, market)
        lines = [
            f"【{where}成交】{symbol}",
            f"{action} {amount:.8g} @ {price:,.4f}",
        ]
        if venue:
            lines.append(f"市场：{venue}")
        if realized is not None and abs(realized) > 1e-12:
            lines.append(f"已实现盈亏：{realized:+,.4f} USDT")
        return "\n".join(lines)

    @staticmethod
    def format_protection(
        *, symbol: str, kind: str, price: float, amount: float, mode: str
    ) -> str:
        label = {"stop_loss": "止损", "take_profit": "止盈"}.get(kind, "保护单")
        where = "实盘" if mode == "live" else "纸面"
        return (
            f"【{where}{label}触发】{symbol}\n"
            f"平仓 {amount:.8g} @ {price:,.4f}"
        )

    @staticmethod
    def format_halt(*, reason: str, day_pnl: float) -> str:
        return (
            "【风控熔断】已停止开新仓\n"
            f"原因：{reason}\n"
            f"当日盈亏：{day_pnl:+,.4f} USDT\n"
            "平仓与撤单不受影响。"
        )

    @staticmethod
    def format_warning(*, title: str, detail: str = "") -> str:
        text = f"【风险提示】{title}"
        return f"{text}\n{detail}" if detail else text


def _prepare(text: str) -> str:
    """Trim and clamp a message. Returns ``""`` for anything not worth sending."""
    cleaned = (text or "").strip()
    if len(cleaned) > MAX_MESSAGE_CHARS:
        cleaned = cleaned[: MAX_MESSAGE_CHARS - 1] + "…"
    return cleaned


def _parse_json(stdout: str) -> dict | None:
    """Best-effort decode of ``--json`` output; non-JSON output is not an error.

    The CLI is allowed to print warnings before the payload, so the last line is
    tried as well as the whole body.
    """
    if not stdout:
        return None
    try:
        value = json.loads(stdout)
        return value if isinstance(value, dict) else None
    except ValueError:
        pass
    for line in reversed(stdout.splitlines()):
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
