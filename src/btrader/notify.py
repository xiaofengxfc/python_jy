"""Outbound 企业微信 notifications.

Two transports are supported:

* **Direct self-built-app API** — ``wecom_api`` mode calls
  ``qyapi.weixin.qq.com`` with Corpid / AgentId / a keyring-stored Secret.
* **OpenClaw smart bot** — ``openclaw`` mode uses the local gateway's outbound
  WebSocket and its message CLI.

The direct API is useful when the user has a 企业微信 administrator-created app
but does not want to install OpenClaw. The openclaw bridge remains the default
for compatibility with existing installations.

The 企业微信 smart-bot plugin (``@wecom/wecom-openclaw-plugin``) keeps an
outbound WebSocket (long connection) from the gateway on this machine to
``wss://openws.work.weixin.qq.com``. Because the connection is outbound and
authenticated with a Bot ID + Secret, there is no callback URL, no public IP and
no QR code that can expire.

Once the channel is configured, pushes go out through OpenClaw's own message
CLI::

    openclaw message send --channel wecom --target <userid> --message <text> --json

That CLI is the stable, documented interface (``docs.openclaw.ai/cli/message``);
there is no public per-message HTTP endpoint for a channel send, so this module
shells out instead of inventing one. A missing CLI, a plugin that was never
registered, or a gateway that is not running are normal, recoverable conditions:
they are logged, surfaced to the settings page, and never allowed to interrupt
trading.

Sends go through a bounded background queue. A hanging gateway must not stall an
order submission or the fill reconciler, which run on critical paths.
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
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from . import paths

log = logging.getLogger(__name__)

# Direct 企业微信 self-built-app API. The secret is kept in the OS keyring, not
# in config.json, for the same reason the AI key is.
WECOM_API_BASE = "https://qyapi.weixin.qq.com"
WECOM_KEYRING_SERVICE = "biTrader-wecom"
WECOM_KEYRING_USER = "corp_secret"


# Both forms are used in the wild: the plugin's manifest id carries no scope
# prefix, while older guides spell the channel after the npm package. The
# settings page shows this value so a user whose gateway names it differently
# can correct it instead of being told "send failed" with no context.
# 企业微信智能机器人（长连接）的渠道名。由官方插件 `@wecom/wecom-openclaw-plugin`
# 注册，连接方式为出站 WebSocket —— 不需要公网回调地址，也不会像个人微信 bot
# 那样隔几小时就掉登录。
DEFAULT_CHANNEL = "wecom"
DEFAULT_COMMAND = "openclaw"
# Chat text is cheap for us and a wall of it is unreadable on a lock screen.
MAX_MESSAGE_CHARS = 2000
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


def load_wecom_secret() -> str:
    """Read the self-built-app Secret from the OS keyring, never config.json."""
    try:
        import keyring

        return keyring.get_password(WECOM_KEYRING_SERVICE, WECOM_KEYRING_USER) or ""
    except Exception as exc:  # a broken keyring must not break the page
        log.warning("读取企业微信 Secret 失败: %s", exc)
        return ""


def save_wecom_secret(value: str) -> bool:
    """Store (or clear) the self-built-app Secret in the OS keyring."""
    try:
        import keyring

        if value:
            keyring.set_password(WECOM_KEYRING_SERVICE, WECOM_KEYRING_USER, value)
        else:
            try:
                keyring.delete_password(WECOM_KEYRING_SERVICE, WECOM_KEYRING_USER)
            except Exception:
                pass
        return True
    except Exception as exc:
        log.warning("保存企业微信 Secret 失败: %s", exc)
        return False


def _wecom_http_json(
    url: str,
    *,
    params: dict | None = None,
    body: dict | None = None,
    timeout: float = 15.0,
) -> dict:
    """Minimal JSON client for qyapi.weixin.qq.com.

    ``urllib`` is used deliberately: it is part of the standard library and
    honours the same HTTPS proxy environment as the rest of the process, so a
    desktop proxy setup does not need an extra dependency just for notifications.
    """
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    data = None
    headers = {"User-Agent": "biTrader/0.5"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        raise NotifyError(f"企业微信接口 HTTP {exc.code}：{detail[:300]}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise NotifyError(f"连接企业微信接口失败：{exc}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise NotifyError(f"企业微信接口返回了非 JSON 内容：{raw[:300]}") from exc
    if not isinstance(payload, dict):
        raise NotifyError("企业微信接口返回格式不正确")
    return payload


def _wecom_error(payload: dict) -> str:
    code = str(payload.get("errcode") or "")
    message = str(payload.get("errmsg") or "").strip()
    if code in {"40001", "40014", "42001"}:
        return "企业微信 Secret 无效或 access_token 已过期，请重新保存 Secret。"
    if code == "60020":
        return "企业微信接口提示 IP 不在可信 IP 范围，请在应用后台配置可信 IP。"
    if code == "81013":
        return "企业微信接收人不存在或不在应用可见范围内。"
    return f"企业微信接口错误 {code or '未知'}：{message or '未返回原因'}"


def _wecom_errcode(payload: dict) -> int:
    try:
        return int(payload.get("errcode") or 0)
    except (TypeError, ValueError) as exc:
        raise NotifyError("企业微信接口返回了无法识别的错误码") from exc


@dataclass
class NotifySettings:
    """Where and whether to push notifications.

    Two transports are supported:

    * ``wecom_api`` — direct 企业微信 self-built-app HTTP API using
      Corpid / AgentId / Secret. The secret lives in the OS keyring.
    * ``openclaw`` — the existing smart-bot long connection through the local
      OpenClaw gateway, addressed by a conversation/userid.

    ``target`` is the 企业微信 userid in either mode. An empty value makes the
    send refuse with an explanation instead of guessing a recipient.
    """

    enabled: bool = False
    mode: str = "openclaw"
    channel: str = DEFAULT_CHANNEL
    target: str = ""
    # Direct self-built-app credentials. The secret is not a dataclass field:
    # it is stored in the OS keyring (see load_wecom_secret/save_wecom_secret).
    corp_id: str = ""
    agent_id: str = ""
    api_base: str = WECOM_API_BASE
    # The plugin's own account id inside the gateway. Empty means "the default
    # account", which is what a single-bot setup should leave alone.
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
        # Cached self-built-app access token. WeCom tokens last ~2 hours; a
        # notification should not pay a gettoken round trip every time.
        self._wecom_token: str = ""
        self._wecom_token_until: float = 0.0

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
            log.warning("企业微信通知队列已满，丢弃一条消息")
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
            raise NotifyError("尚未填写企业微信接收人（userid），无法发送。")
        try:
            return self._run(text, respect_breaker=False)
        except NotifyError as exc:
            self._record_failure(str(exc))
            self._note_failure()
            raise

    def status(self) -> dict:
        mode = self._mode()
        secret_saved = bool(load_wecom_secret()) if mode == "wecom_api" else False
        if mode == "wecom_api":
            available = bool(
                str(self.settings.corp_id or "").strip()
                and str(self.settings.agent_id or "").strip()
                and secret_saved
            )
        else:
            available = bool(self._resolve_command())
        return {
            "enabled": self.settings.enabled,
            "mode": mode,
            "channel": self.settings.channel,
            "target": self.settings.target,
            "account": self.settings.account,
            "command": self.settings.command,
            "corp_id": self.settings.corp_id,
            "agent_id": self.settings.agent_id,
            "api_base": self.settings.api_base,
            "secret_saved": secret_saved,
            "available": available,
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

    def session_state(self, *, refresh: bool = False) -> dict:
        """Kept for API compatibility: the 企业微信 bot has no expiring login.

        The personal-WeChat bot needed a QR scan every few hours; the 企业微信
        intelligent bot authenticates once with Bot ID + Secret and holds an
        outbound WebSocket, so there is nothing to re-scan. Returning a fixed
        verdict keeps older callers (and saved JSON) working.
        """
        return {"state": "ok", "detail": "", "account": self.settings.account}

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
                log.warning("企业微信通知发送失败：%s", exc)
            except Exception as exc:  # pragma: no cover - defensive
                self._record_failure(f"发送异常：{exc}")
                self._note_failure()
                _spool_add(job.text)
                log.warning("企业微信通知异常", exc_info=True)
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
            log.warning("企业微信通道连续失败，暂停发送 %g 秒", BREAKER_COOLDOWN)

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

    def _mode(self) -> str:
        mode = str(self.settings.mode or "").strip().lower()
        return "wecom_api" if mode == "wecom_api" else "openclaw"

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
        """The recipient (WeCom userid) and account to send to.

        The aibot API addresses a conversation: for a single chat the chatid is
        the member's userid. It only exists after that member has written to the
        bot once, so an empty target is reported rather than guessed.
        """
        return normalize_target(self.settings.target), str(
            self.settings.account or ""
        ).strip()

    def _run(
        self, text: str, *, from_queue: bool = False, respect_breaker: bool = True
    ) -> dict:
        if respect_breaker:
            self._check_breaker()
        if self._mode() == "wecom_api":
            payload = self._run_wecom_api(text)
            if from_queue:
                log.info("企业微信 API 通知已发出")
            self.last_error = ""
            self.last_error_at = 0.0
            self.last_success_at = time.time()
            self._note_success()
            return payload
        command = self._resolve_command()
        if not command:
            raise NotifyError(
                f"未找到 {self.settings.command or DEFAULT_COMMAND}，请安装 OpenClaw，"
                "或在设置中填写可执行文件的完整路径。"
            )
        channel = (self.settings.channel or DEFAULT_CHANNEL).strip() or DEFAULT_CHANNEL
        target, account = self._resolve_target()
        if not target:
            raise NotifyError("尚未填写企业微信接收人（userid），无法发送。")
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
            log.info("企业微信通知已发出（%s）", str(payload.get("messageId") or "") if payload else "")
        self.last_error = ""
        self.last_error_at = 0.0
        self.last_success_at = time.time()
        self._note_success()
        return {"ok": True, "stdout": stdout[:2000]}

    def _run_wecom_api(self, text: str) -> dict:
        """Send one text message through the 企业微信 self-built-app API."""
        settings = self.settings
        target, _ = self._resolve_target()
        if not target:
            raise NotifyError("尚未填写企业微信接收人（userid），无法发送。")
        corp_id = str(settings.corp_id or "").strip()
        if not corp_id:
            raise NotifyError("尚未填写企业微信 CorpID。")
        agent_raw = str(settings.agent_id or "").strip()
        if not agent_raw:
            raise NotifyError("尚未填写企业微信 AgentId。")
        try:
            agent_id = int(agent_raw)
        except ValueError as exc:
            raise NotifyError("企业微信 AgentId 必须是数字。") from exc
        if agent_id <= 0:
            raise NotifyError("企业微信 AgentId 必须是正整数。")
        secret = load_wecom_secret()
        if not secret:
            raise NotifyError("尚未保存企业微信应用 Secret。")

        base = (str(settings.api_base or "").strip() or WECOM_API_BASE).rstrip("/")
        token = self._wecom_access_token(base, corp_id, secret)
        url = f"{base}/cgi-bin/message/send"
        payload = _wecom_http_json(
            url,
            params={"access_token": token},
            body={
                "touser": target,
                "msgtype": "text",
                "agentid": agent_id,
                "text": {"content": text},
                "safe": 0,
            },
            timeout=min(self.timeout, 15.0),
        )
        if _wecom_errcode(payload) != 0:
            raise NotifyError(_wecom_error(payload))
        log.info("企业微信 API 通知已发出（agentid=%s）", agent_id)
        return {
            "ok": True,
            "stdout": json.dumps(
                {"errcode": 0, "errmsg": payload.get("errmsg") or "ok"},
                ensure_ascii=False,
            )[:2000],
        }

    def _wecom_access_token(self, base: str, corp_id: str, secret: str) -> str:
        now = time.time()
        key = f"{base}|{corp_id}|{secret}"
        if self._wecom_token and now < self._wecom_token_until and getattr(
            self, "_wecom_token_key", ""
        ) == key:
            return self._wecom_token
        payload = _wecom_http_json(
            f"{base}/cgi-bin/gettoken",
            params={"corpid": corp_id, "corpsecret": secret},
            timeout=min(self.timeout, 15.0),
        )
        token = str(payload.get("access_token") or "").strip()
        if _wecom_errcode(payload) != 0 or not token:
            raise NotifyError(_wecom_error(payload))
        try:
            expires = float(payload.get("expires_in") or 7200)
        except (TypeError, ValueError):
            expires = 7200.0
        self._wecom_token = token
        self._wecom_token_key = key
        # Refresh two minutes early; never cache a token for longer than the
        # server said it is valid.
        self._wecom_token_until = now + max(60.0, expires - 120.0)
        return token

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
        where = _venue_label(mode)
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
        where = _venue_label(mode)
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


def _venue_label(mode: str) -> str:
    """Where a fill happened.

    The testnet is a real venue with fake money, so its fills must not be
    announced as paper: the label tells the reader whether the money was real.
    """
    if mode == "testnet":
        return "测试网"
    return "实盘"

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
    """Trim a recipient id, tolerating a pasted label or channel prefix."""
    text = str(raw or "").strip().strip("\"'").strip()
    lowered = text.lower()
    for prefix in ("wecom:", "qywx:", "wework:", "wechatwork:"):
        if lowered.startswith(prefix):
            text = text.split(":", 1)[1].strip()
            break
    return text


def _explain_failure(detail: str, code: int) -> str:
    """Turn the CLI's generic failure into something actionable."""
    text = (detail or "").strip()
    lowered = text.lower()
    # WeCom's aibot API addresses conversations, not people: the chatid for a
    # single chat is the member's userid, and it only exists after that member
    # has written to the bot once. Naming the fix beats "invalid chatid".
    if "93006" in lowered or "invalid chatid" in lowered:
        return (
            "企业微信返回 93006（invalid chatid）：接收人 userid 不存在或还没和机器人说过话。"
            "请先在企业微信里给这个机器人发一条消息，再把你的 userid 填进设置。"
        )
    if "45009" in lowered or "api freq" in lowered:
        return "企业微信接口触发频率限制（45009），消息已暂存，几分钟后会自动补发。"
    return text[:500] or f"OpenClaw 返回退出码 {code}"


def target_warning(target: str) -> str:
    """Warn about a target the WeCom bot cannot address yet."""
    text = normalize_target(target)
    if not text:
        return ""
    if "@" in text or text.endswith(".wechat"):
        return (
            "这看起来是个人微信的 ID。企业微信智能机器人要用成员 userid"
            "（如 XiaoJiaJian），不是你微信的 wxid。"
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


def spool_message(text: str) -> int:
    """Keep a message for later delivery; returns how many are waiting.

    Used by the one-shot CLI (``btrader.notify_cli``): that process dies right
    after the send, so a failure would otherwise lose the report entirely. The
    app's worker replays whatever is here once the channel recovers.
    """
    try:
        _spool_add(_prepare(text))
    except Exception:  # pragma: no cover - a broken data dir must not raise
        log.debug("暂存消息失败", exc_info=True)
        return 0
    try:
        return len(_spool_load())
    except Exception:  # pragma: no cover
        return 0


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
