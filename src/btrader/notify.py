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
import re
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import paths

log = logging.getLogger(__name__)

# Both forms are used in the wild: the plugin's manifest id carries no scope
# prefix, while older guides spell the channel after the npm package. The
# settings page shows this value so a user whose gateway names it differently
# can correct it instead of being told "send failed" with no context.
DEFAULT_CHANNEL = "openclaw-weixin"
DEFAULT_COMMAND = "openclaw"
# Chat text is cheap for us and a wall of it is unreadable on a lock screen.
# The WeChat plugin chunks outbound text at 4000 characters, so staying well
# below that keeps every message in a single bubble.
MAX_MESSAGE_CHARS = 2000
# The plugin resolves the recipient against its per-account context-token store,
# which is keyed by the raw Weixin user id. Those ids always end with this
# suffix, so a truncated paste (a bare wxid) silently sends without a context
# token — worth naming in the settings page instead of only in the gateway log.
WECHAT_ID_SUFFIX = "@im.wechat"
# npm's Windows shim embeds the real script path in a quoted string, e.g.
# ``"%dp0%\node_modules\openclaw\openclaw.mjs" %*``. Lifting that out lets the
# notifier call node directly instead of handing the text to cmd.exe.
_SHIM_SCRIPT_RE = re.compile(r'"([^"]+\.(?:js|mjs|cjs))"', re.IGNORECASE)

# A dead gateway costs one CLI round trip per message (~30 s) because the CLI
# waits for a gateway that is not there. Three failures in a row open the
# breaker for a few minutes, so the queue drains instead of stacking timeouts —
# which used to mean ten notifications took five minutes to all fail.
BREAKER_THRESHOLD = 3
BREAKER_COOLDOWN = 300.0
# The CLI's own failure takes ~30 s; a TCP probe answers "is anything listening"
# in a fraction of that, so a down gateway is reported before the wait starts.
PREFLIGHT_TIMEOUT = 0.4
# Failed messages are kept on disk and replayed once the channel is back: losing
# a 熔断 warning because the gateway restarted mid-send is not acceptable.
SPOOL_LIMIT = 200
SPOOL_MAX_AGE = 6 * 3600.0
SPOOL_BATCH = 5
SPOOL_FILE = "notify-spool.jsonl"
SPOOL_PREFIX = "【补发】"


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
    # Multiple WeChat accounts can be bound to one gateway. Empty means "the
    # default account", which is what a single-account setup should leave alone;
    # with two bound accounts the plugin cannot guess and the send fails, so the
    # page offers this field instead of forcing a CLI flag by hand.
    account: str = ""
    command: str = DEFAULT_COMMAND
    # Events are opt-in individually so an active strategy cannot spam the phone.
    on_fill: bool = True
    on_protection: bool = True
    on_risk_halt: bool = True
    on_live_warning: bool = True
    # Long-running work (自动交易启停、回测/优化完成) reports progress here too,
    # so the phone is not only about fills.
    on_task: bool = True
    # Probe the gateway port before spawning the CLI. Turn off only for a
    # gateway on a non-default port, where the probe would be a false negative.
    preflight: bool = True


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
        # A single ``message send`` is a full Node start plus a gateway round
        # trip; measured at ~30 s on a warm machine. The old 20 s deadline
        # fired first every time, so every notification "failed" while the
        # message actually went through.
        timeout: float = 90.0,
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
        # Circuit breaker state: consecutive failures and the wall-clock time
        # until which sends fail fast instead of waiting out the CLI.
        self._failures: int = 0
        self._open_until: float = 0.0

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
            with self._lock:
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
        justifies turning the stream on. It also ignores an open breaker: an
        explicit click is how the user proves the channel recovered.
        """
        text = _prepare(text)
        if not text:
            raise NotifyError("消息内容为空")
        if not self._resolve_target()[0]:
            raise NotifyError("尚未填写微信接收目标，无法发送。")
        try:
            return self._run(text, respect_breaker=False)
        except NotifyError as exc:
            self._record_failure(str(exc))
            self._note_failure()
            raise

    def status(self) -> dict:
        return {
            "enabled": self.settings.enabled,
            "channel": self.settings.channel,
            "target": self.settings.target,
            "account": self.settings.account,
            "command": self.settings.command,
            "available": bool(self._resolve_command()),
            # Surface a target the plugin cannot match before the user saves it;
            # an empty string means "no complaint".
            "target_warning": target_warning(self.settings.target) if self.settings.enabled else "",
            "last_error": self.last_error,
            "last_error_at": self.last_error_at,
            "last_success_at": self.last_success_at,
            "dropped": self.dropped,
            "pending": self._queue.qsize(),
            # Spooled + paused are states the page must show: without them the
            # channel looks idle while messages are stacking up off-screen.
            "spooled": len(_spool_load()),
            "breaker_seconds": max(0, int(self._open_until - time.time())),
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
                if not self._resolve_target()[0]:
                    # Enqueued while a target was set, target cleared before we
                    # got here. Retrying would only repeat the same refusal.
                    continue
                self._run(job.text, from_queue=True)
            except NotifyError as exc:
                self._record_failure(str(exc))
                self._note_failure()
                # Keep the message: the gateway being down is exactly when a
                # 熔断 warning matters, and it is worth replaying later.
                _spool_add(job.text)
                log.warning("微信通知发送失败：%s", exc)
            except Exception as exc:  # pragma: no cover - defensive
                self._record_failure(f"发送异常：{exc}")
                self._note_failure()
                _spool_add(job.text)
                log.warning("微信通知异常", exc_info=True)
            else:
                # Proof the channel works again: drain what piled up meanwhile.
                self._replay_spool()

    # -------------------------------------------------------------------- guard
    def _check_breaker(self) -> None:
        """Refuse instantly while the channel is known to be broken."""
        remaining = self._open_until - time.time()
        if remaining > 0:
            raise NotifyError(
                f"通道连续发送失败，已暂停 {int(remaining)} 秒。请检查网关或 OpenClaw 后重试。"
            )

    def _note_success(self) -> None:
        self._failures = 0
        self._open_until = 0.0

    def _note_failure(self) -> None:
        self._failures += 1
        if self._failures >= BREAKER_THRESHOLD:
            self._failures = 0
            self._open_until = time.time() + BREAKER_COOLDOWN
            log.warning("微信通道连续失败，暂停发送 %g 秒", BREAKER_COOLDOWN)

    def _preflight(self) -> None:
        """Fail in 0.4 s when nothing is listening on the gateway port.

        The CLI cannot tell us quickly: it starts Node, then waits for a gateway
        that is not there, then exits — about 30 s per message. A TCP probe on
        the port the app itself starts the gateway on answers the same question
        immediately, so the user gets "先启动网关" instead of a 30 s stall.
        """
        if not self.settings.preflight:
            return
        _probe_gateway()

    def _replay_spool(self) -> None:
        """Send what failed earlier, oldest first, a few at a time."""
        items = _spool_load()
        if not items:
            return
        batch, rest = items[:SPOOL_BATCH], items[SPOOL_BATCH:]
        _spool_save(rest)
        for index, item in enumerate(batch):
            try:
                self._run(_spool_text(item), from_queue=True)
            except NotifyError as exc:
                # Put this one and the untouched remainder back, in order.
                _spool_save([item, *batch[index + 1:], *rest])
                log.warning("补发失败：%s", exc)
                return

    # -------------------------------------------------------------------- send
    def _event_enabled(self, kind: str) -> bool:
        s = self.settings
        return {
            "fill": s.on_fill,
            "protection": s.on_protection,
            "risk": s.on_risk_halt,
            "warning": s.on_live_warning,
            "task": s.on_task,
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

    def _resolve_target(self) -> tuple[str, str]:
        """The recipient and account to send to.

        An empty target falls back to the one bound WeChat account. The QR scan
        writes the scanner's own WeChat id into the plugin's account file, and
        that id is exactly what ``--target`` expects — requiring the user to
        copy it out of a log is why "已经扫码登录但仍然发不出去" happened.
        Ambiguous bindings (two accounts) still refuse, rather than guessing.
        """
        target = normalize_target(self.settings.target)
        account = str(self.settings.account or "").strip()
        if target:
            return target, account
        bound = _bound_wechat_target()
        if bound is None:
            return "", account
        return bound["target"], account or bound["account"]

    def _run(
        self, text: str, *, from_queue: bool = False, respect_breaker: bool = True
    ) -> dict:
        if respect_breaker:
            self._check_breaker()
        command = self._resolve_command()
        if not command:
            raise NotifyError(
                f"未找到 {self.settings.command or DEFAULT_COMMAND}，请安装 OpenClaw，"
                "或在设置中填写可执行文件的完整路径。"
            )
        channel = (self.settings.channel or DEFAULT_CHANNEL).strip() or DEFAULT_CHANNEL
        target, account = self._resolve_target()
        if not target:
            raise NotifyError("尚未填写微信接收目标，无法发送。")
        self._preflight()
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
        if account:
            # Only when the user named one: an explicit ``--account`` selects that
            # account, while omitting it lets the plugin pick its default (and
            # disambiguate by context token when several are bound).
            args.extend(["--account", account])
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
            raise NotifyError(_explain_failure(detail, completed.returncode))
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
        self._note_success()
        return {"ok": True, "stdout": stdout[:2000]}

    def _record_failure(self, message: str) -> None:
        self.last_error = message
        self.last_error_at = time.time()

    @staticmethod
    def _wrap_launcher(args: list[str]) -> list[str]:
        """Make a Windows ``.cmd``/``.bat`` shim executable without a shell.

        npm installs the CLI as ``openclaw.cmd``, and Windows cannot exec that
        directly with ``shell=False``. The obvious workaround — ``cmd.exe /c`` —
        is wrong for this payload: ``cmd`` treats a newline as a command
        separator and ``&``, ``|``, ``>``, ``<``, ``^`` as operators, so a
        multi-line notification arrived truncated at the first line and a
        message containing ``&`` could run part of itself as a command.

        The shim is a tiny batch file whose real work is one quoted script path,
        so that path is lifted out and run through ``node`` directly. Only when
        the shim cannot be read does this fall back to ``cmd.exe``, with the
        text flattened onto one line so the argument at least survives intact.
        """
        first = args[0] if args else ""
        if os.name != "nt" or not first.lower().endswith((".cmd", ".bat")):
            return args
        direct = _node_entry_argv(first, args[1:])
        if direct:
            return direct
        comspec = os.environ.get("COMSPEC") or "cmd.exe"
        # ``/d`` skips AutoRun scripts and ``/s`` keeps the quoting we build here
        # instead of letting cmd re-parse it.
        command_line = " ".join(_quote_for_cmd(arg) for arg in args)
        return [comspec, "/d", "/s", "/c", command_line]

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
        action = _action_label(side)
        where = "实盘" if mode == "live" else "纸面"
        venue = {"spot": "现货", "usdm": "U 本位", "coinm": "币本位"}.get(market, market)
        lines = [
            f"【{where}成交】{symbol}",
            f"{action} {_format_amount(amount)} @ {_format_price(price)}",
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
            f"平仓 {_format_amount(amount)} @ {_format_price(price)}"
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
    def format_task(title: str, lines: list[str] | None = None) -> str:
        """A 任务 message: what started, stopped, or finished.

        Kept separate from the trading formatters because these are not trading
        events: an optimisation that took an hour is the reason someone walks
        away from the desk, and the phone is how they learn it ended.
        """
        body = [line for line in (lines or []) if line]
        return "\n".join([f"【{title}】", *body])

    @staticmethod
    def format_warning(*, title: str, detail: str = "") -> str:
        text = f"【风险提示】{title}"
        return f"{text}\n{detail}" if detail else text


def _action_label(side: str) -> str:
    """Name the trade direction without inventing one.

    The old code said "卖出" for anything that was not exactly ``"buy"``, so a
    missing or unexpected side produced a confidently wrong message. An unknown
    side now says so, which is honest and still readable on a lock screen.
    """
    key = str(side or "").strip().lower()
    if key in ("buy", "long"):
        return "买入"
    if key in ("sell", "short"):
        return "卖出"
    return "成交"


def _format_price(value: float) -> str:
    """Render a price with enough precision to be meaningful.

    A fixed four decimals is wrong for both ends of the market: a 82,000 USDT
    price gained two noise digits, and a 0.00001234 alt price was printed as
    ``0.0000`` — a fill message that told the user nothing. Decimals therefore
    scale with magnitude, and thousands separators are kept.
    """
    try:
        price = float(value)
    except (TypeError, ValueError):
        return "—"
    magnitude = abs(price)
    if magnitude >= 1000:
        decimals = 2
    elif magnitude >= 100:
        decimals = 3
    elif magnitude >= 1:
        decimals = 4
    elif magnitude >= 0.01:
        decimals = 5
    else:
        decimals = 8
    return f"{price:,.{decimals}f}"


def _format_amount(value: float) -> str:
    """Render a base-asset quantity without scientific notation.

    ``"{:.8g}"`` turned a dust-sized exit into ``1e-07``; a chat message should
    show the number the exchange does.
    """
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "—"
    text = f"{amount:.8f}".rstrip("0").rstrip(".")
    return text or "0"


def _prepare(text: str) -> str:
    """Trim and clamp a message. Returns ``""`` for anything not worth sending."""
    # A NUL byte cannot cross CreateProcess at all; dropping it is better than
    # failing the whole send on a stray control character.
    cleaned = (text or "").replace("\x00", "").strip()
    if len(cleaned) > MAX_MESSAGE_CHARS:
        cleaned = cleaned[: MAX_MESSAGE_CHARS - 1] + "…"
    return cleaned


def normalize_target(raw: str) -> str:
    """Trim a recipient id, tolerating a pasted chat label around it."""
    text = str(raw or "").strip().strip("\"'").strip()
    if text.lower().startswith("weixin:"):
        text = text.split(":", 1)[1].strip()
    return text


def _explain_failure(detail: str, code: int) -> str:
    """Turn the CLI's generic failure into something actionable.

    ``The CLI command failed. For help, run openclaw doctor`` is what the plugin
    prints for every early failure — including "no account is bound", which is
    by far the most common cause after an unbind. Saying so here is the
    difference between a dead end and "重新扫码".
    """
    text = (detail or "").strip()
    generic = not text or "CLI command failed" in text
    if generic:
        try:
            from .installer import wechat_accounts

            if not wechat_accounts():
                return (
                    "当前没有已绑定的微信账号（可能已解绑或尚未扫码）："
                    "请到「设置 → 微信通道」重新扫码绑定后再发送。"
                )
        except Exception:  # pragma: no cover - best effort
            pass
    return text[:500] or f"OpenClaw 返回退出码 {code}"


def _bound_wechat_target() -> dict | None:
    """The single bound WeChat account's own recipient id, if unambiguous.

    Imported lazily: the installer pulls in the whole gateway/CLI surface, and
    the notifier is constructed during application start-up.
    """
    try:
        from .installer import wechat_targets
    except Exception:  # pragma: no cover - import cycle guard
        return None
    try:
        targets = wechat_targets()
    except Exception:  # pragma: no cover - best effort
        return None
    return targets[0] if len(targets) == 1 else None


def target_warning(target: str) -> str:
    """Explain a target the plugin cannot match to a conversation token.

    The channel resolves the recipient against its own context-token store,
    keyed by the raw Weixin id (``<wxid>@im.wechat``). A bare wxid still sends,
    but without the token the plugin warns and the reply can land in the wrong
    conversation, so the settings page says so before the user saves it.
    """
    text = normalize_target(target)
    if not text:
        return ""
    if not text.lower().endswith(WECHAT_ID_SUFFIX):
        return (
            f"接收目标通常以 {WECHAT_ID_SUFFIX} 结尾：先给 ClawBot 发一条消息，"
            "再从网关日志里复制完整的用户 ID。"
        )
    return ""


def _node_entry_argv(shim: str, args: list[str]) -> list[str] | None:
    """Return ``[node, script, *args]`` for an npm shim, or ``None``.

    Works from the shim's own text rather than from a guessed package layout, so
    it keeps working when npm changes its global prefix or the package is
    installed under a scope. ``%dp0%`` is the shim's directory.
    """
    try:
        with open(shim, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return None
    script = ""
    for match in _SHIM_SCRIPT_RE.finditer(text):
        script = match.group(1)
    if not script:
        return None
    folder = os.path.dirname(os.path.abspath(shim))
    script = re.sub(r"%dp0%", lambda _m: folder, script, flags=re.IGNORECASE)
    script = re.sub(r"%~dp0", lambda _m: folder + os.sep, script, flags=re.IGNORECASE)
    if not os.path.isabs(script):
        script = os.path.join(folder, script)
    if not os.path.isfile(script):
        return None
    # npm ships a private node.exe beside the shim when it manages the runtime;
    # otherwise the PATH lookup that found the shim will find node too.
    local_node = os.path.join(folder, "node.exe")
    node = local_node if os.path.isfile(local_node) else (shutil.which("node") or "node")
    return [node, script, *args]


def _quote_for_cmd(arg: str) -> str:
    """Quote one argv element for the ``cmd.exe`` fallback path.

    Newlines are flattened because ``cmd`` would otherwise end the command
    there, which is exactly the truncation this fallback exists to survive.
    """
    clean = str(arg).replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    return '"' + clean.replace('"', '""') + '"'



def _probe_gateway() -> None:
    """Raise :class:`NotifyError` when nothing listens on the gateway port.

    Module-level so the check can be exercised directly (and disabled wholesale
    in tests) without constructing a notifier around it.
    """
    try:
        # Imported lazily: ``gateway`` pulls in the supervisor machinery.
        from .gateway import GATEWAY_HOST, GATEWAY_PORT
    except Exception:  # pragma: no cover - defensive
        return
    try:
        with socket.create_connection((GATEWAY_HOST, GATEWAY_PORT), PREFLIGHT_TIMEOUT):
            return
    except OSError:
        pass
    raise NotifyError(
        f"网关没有在 {GATEWAY_HOST}:{GATEWAY_PORT} 监听，请到设置页启动网关后再试"
        "（若你用自定义端口，可关闭「发送前检查网关」）。"
    )


# ---------------------------------------------------------------------- spool
def _spool_path() -> Path:
    """Where undeliverable messages are kept between attempts."""
    return paths.data_dir() / SPOOL_FILE


def _spool_load() -> list[dict]:
    """Read the spool, dropping entries too old to be worth sending.

    A fill from six hours ago is not news; replaying it would be worse than the
    silence that preceded it. Corruption is treated as "nothing to replay" —
    a bad spool must never block a new send.
    """
    try:
        raw = _spool_path().read_text(encoding="utf-8")
    except (OSError, ValueError):
        return []
    now = time.time()
    items: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if not isinstance(item, dict) or not str(item.get("text") or "").strip():
            continue
        try:
            at = float(item.get("at") or 0.0)
        except (TypeError, ValueError):
            at = 0.0
        if now - at > SPOOL_MAX_AGE:
            continue
        items.append({"text": str(item["text"]), "at": at})
    return items[-SPOOL_LIMIT:]


def _spool_save(items: list[dict]) -> None:
    """Replace the spool atomically: a half-written file loses messages."""
    path = _spool_path()
    trimmed = items[-SPOOL_LIMIT:]
    if not trimmed:
        try:
            path.unlink()
        except OSError:
            pass
        return
    body = "".join(
        json.dumps({"text": item["text"], "at": item.get("at", time.time())}, ensure_ascii=False)
        + "\n"
        for item in trimmed
    )
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        log.debug("写入通知暂存失败", exc_info=True)
        try:
            tmp.unlink()
        except OSError:
            pass


def _spool_add(text: str) -> None:
    items = _spool_load()
    if any(item["text"] == text for item in items):
        return
    items.append({"text": text, "at": time.time()})
    _spool_save(items)


def _spool_text(item: dict) -> str:
    """Mark a replayed message so it reads as late news, not a fresh event."""
    text = str(item.get("text") or "")
    return text if text.startswith(SPOOL_PREFIX) else f"{SPOOL_PREFIX}\n{text}"


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
