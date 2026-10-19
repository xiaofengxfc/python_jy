"""Outbound WeChat notifications through a local OpenClaw gateway.

The WeChat ClawBot plugin binds a personal WeChat account to an OpenClaw Gateway
running on this machine. OpenClaw's own CLI is the stable way to push a message
*out* through that channel::

    openclaw message send --channel openclaw-weixin --target <id> --message ...

There is no public per-message HTTP endpoint for a channel send, so this module
shells out to the CLI instead of inventing one. A missing CLI or a logged-out
plugin is a normal, recoverable condition: it is logged, surfaced to the UI, and
never allowed to interrupt trading.

Sends go through a bounded background queue. A hanging gateway must not stall an
order submission or the paper reconciler, which run on critical paths.
"""

from __future__ import annotations

import logging
import os
import queue
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_CHANNEL = "openclaw-weixin"
DEFAULT_COMMAND = "openclaw"


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
        # a stale failure from last week is noise, not status.
        self.last_error: str = ""
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
        text = (text or "").strip()
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
        """
        text = (text or "").strip()
        if not text:
            raise NotifyError("消息内容为空")
        if not self.settings.target:
            raise NotifyError("尚未填写微信接收目标，无法发送。")
        return self._run(text)

    def status(self) -> dict:
        return {
            "enabled": self.settings.enabled,
            "channel": self.settings.channel,
            "target": self.settings.target,
            "command": self.settings.command,
            "available": bool(self._resolve_command()),
            "last_error": self.last_error,
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
            try:
                if job.text:
                    self._run(job.text)
            except NotifyError as exc:
                self.last_error = str(exc)
                log.warning("微信通知发送失败：%s", exc)
            except Exception as exc:  # pragma: no cover - defensive
                self.last_error = f"发送异常：{exc}"
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
        if shutil.which(command):
            return command
        # An absolute path that exists is usable even when it is not on PATH.
        try:
            from pathlib import Path

            if Path(command).is_file():
                return command
        except OSError:
            pass
        return None

    def _run(self, text: str) -> dict:
        command = self._resolve_command()
        if not command:
            raise NotifyError(
                f"未找到 {self.settings.command}，请先在设置中填写 OpenClaw 可执行文件路径"
                "（安装微信 ClawBot 后会一并安装）。"
            )
        channel = (self.settings.channel or DEFAULT_CHANNEL).strip() or DEFAULT_CHANNEL
        args = [
            command,
            "message",
            "send",
            "--channel",
            channel,
            "--target",
            self.settings.target,
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

        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise NotifyError(detail[:500] or f"OpenClaw 返回退出码 {completed.returncode}")
        self.last_error = ""
        self.last_success_at = time.time()
        return {"ok": True, "stdout": (completed.stdout or "").strip()[:2000]}

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
