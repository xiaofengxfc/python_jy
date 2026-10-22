"""企业微信通知：只发一条通用 Webhook，没有网关。

以前推送要靠本机 OpenClaw 网关转发：起一次 Node 大约 30 秒，网关没在跑还要等
50 秒超时，通道出问题时用户只看到「发送失败」。现在只做一件事：把消息 POST 到
用户自己填的地址。

参考 dsh-notifier 的「企业微信应用通知（通用 Webhook）」：

* 企业微信群机器人地址（``/cgi-bin/webhook/send?key=…``）按它自己的格式发，
  ``msgtype`` 选 text 或 markdown；
* 别的地址发 ``{"title": …, "content": …}``，或者按用户写的模板发。

三条规矩没变：

* 通知不能拖住下单。发送走后台队列，队列满了就丢最新一条并计数。
* 发不出去的消息写进 ``notify-spool.jsonl``，通道恢复后补发，不静默丢。
* 连续失败三次熔断五分钟，避免拿超时把队列堆满。
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from . import paths

log = logging.getLogger(__name__)

# 有的中转站会用 WAF 拦裸客户端（返回 403，正文是 error code: 1010），
# 带上自己的 UA 才放行。和 llm.py / news.py 用同一个。
USER_AGENT = "biTrader/0.5 (+desktop)"
# 企业微信群机器人单条 markdown 上限 4096 字节，中文一个字三字节，
# 2000 字留足余量，也够把锁屏那一屏说完。
MAX_MESSAGE_CHARS = 2000
# 一次 HTTP POST 的正常耗时是几百毫秒。10 秒还没回来就是网络有问题，
# 继续等只会让后台队列越堆越长。
WEBHOOK_TIMEOUT = 10.0
# 连续失败几次算通道坏了。三次足够排除偶发抖动。
BREAKER_THRESHOLD = 3
BREAKER_COOLDOWN = 300.0
# 失败的消息留在盘上，通道恢复后补发：
# 因为一次网络抖动就丢掉「熔断」告警，是最不该丢的那一条。
SPOOL_LIMIT = 200
SPOOL_MAX_AGE = 6 * 3600.0
SPOOL_BATCH = 5
SPOOL_FILE = "notify-spool.jsonl"
SPOOL_PREFIX = "【补发】"

# 消息格式。auto 按地址判断，用户一般不用管。
FORMATS = ("auto", "qyapi_markdown", "qyapi_text", "custom")
DEFAULT_FORMAT = "auto"
QYAPI_SEND_MARKER = "/cgi-bin/webhook/send"


class NotifyError(RuntimeError):
    """消息没发出去。文案要能直接告诉用户下一步做什么。"""


def check_webhook_url(url: str) -> str:
    """校验并归整 Webhook 地址。空串表示没配，交给调用方决定怎么说。"""
    text = (url or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    if not (lowered.startswith("http://") or lowered.startswith("https://")):
        raise NotifyError("Webhook 地址要以 http:// 或 https:// 开头。")
    parsed = urllib.parse.urlsplit(text)
    if not parsed.netloc:
        raise NotifyError("Webhook 地址不完整，缺少域名部分。")
    return text


def mask_url(url: str) -> str:
    """给界面看的形式：域名留着，key 只留后四位。"""
    text = (url or "").strip()
    if not text:
        return ""
    parsed = urllib.parse.urlsplit(text)
    query = parsed.query
    if "key=" in query:
        parts = []
        for item in query.split("&"):
            if item.startswith("key="):
                value = item[4:]
                parts.append("key=…" + value[-4:] if len(value) > 4 else "key=…")
            else:
                parts.append(item)
        query = "&".join(parts)
    base = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    return f"{base}?{query}" if query else base


def parse_headers(text: str) -> dict[str, str]:
    """把「每行一个 Key: Value」解析成字典。空行和没有冒号的行忽略。"""
    result: dict[str, str] = {}
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        if key:
            result[key] = value.strip()
    return result


def _post_json(
    url: str, body: dict, *, headers: dict[str, str] | None = None, timeout: float = WEBHOOK_TIMEOUT
) -> dict:
    """POST 一段 JSON，返回解析后的响应体（不是 JSON 就当空对象）。

    用标准库：桌面端没有额外的 HTTP 客户端，通知也不该为它引入依赖。
    """
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request_headers = {
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }
    request_headers.update(headers or {})
    request = urllib.request.Request(url, data=payload, headers=request_headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        raise NotifyError(_http_text(exc.code, detail)) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise NotifyError(f"连不上 Webhook 地址：{reason}。检查网络、代理或地址。") from exc
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _http_text(code: int, detail: str) -> str:
    hint = {
        400: "Webhook 地址不认这个请求体，检查消息格式选对了没有。",
        401: "Webhook 要求鉴权，把 token 放进地址或自定义请求头。",
        403: "Webhook 拒绝了这次请求，可能是 key 失效或来源被拦。",
        404: "Webhook 地址不存在，检查是不是复制少了字符。",
        413: "消息太长，超过 Webhook 的限制。",
        429: "发得太快被限流，稍后会重试。",
    }.get(code)
    if code >= 500:
        hint = "Webhook 服务端出错，稍后会自动重试。"
    message = hint or f"Webhook HTTP {code}。"
    return f"{message}（{detail}）" if detail else message


def _qyapi_error(payload: dict) -> str:
    """企业微信群机器人的错误码，翻成中文。"""
    try:
        code = int(payload.get("errcode") or 0)
    except (TypeError, ValueError):
        return "企业微信返回了无法识别的错误码。"
    if code == 0:
        return ""
    message = str(payload.get("errmsg") or "").strip()
    hint = {
        40058: "参数不对，检查消息格式。",
        45009: "触发频率限制（每分钟 20 条），消息已暂存，稍后补发。",
        93000: "Webhook key 无效，重新复制机器人地址。",
    }.get(code)
    text = hint or f"企业微信接口错误 {code}"
    return f"{text}（{message}）" if message else text


@dataclass
class NotifySettings:
    """发不发、发到哪、按什么格式发。

    ``webhook_url`` 就是企业微信群机器人的地址，或者任何接收 JSON 的自建地址。
    地址里已经带着 key，所以它等同于一个密码：不进日志，界面上只显示后四位。
    """

    enabled: bool = False
    webhook_url: str = ""
    msg_format: str = DEFAULT_FORMAT
    # custom 格式用的模板：两个占位符 {title} 和 {content}。
    webhook_template: str = ""
    # 自定义请求头，每行一个 "Key: Value"。
    webhook_headers: str = ""
    # 事件开关：一条策略忙起来时，手机不该被同一种消息刷屏。
    on_fill: bool = True
    on_protection: bool = True
    on_risk_halt: bool = True
    on_live_warning: bool = True
    # 自动交易启停、回测/优化结束这类长任务也报一声，手机不只知道成交。
    on_task: bool = True


@dataclass
class _Job:
    text: str
    title: str = ""
    queued_at: float = field(default_factory=time.time)


class Notifier:
    """队列 + 后台线程 + 一条 Webhook。

    ``settings`` 每次发送都重读，网页改完立刻生效，不用重建对象。
    """

    def __init__(
        self,
        settings: NotifySettings,
        *,
        timeout: float = WEBHOOK_TIMEOUT,
        queue_size: int = 50,
    ) -> None:
        self.settings = settings
        self.timeout = timeout
        self._queue: queue.Queue[_Job] = queue.Queue(maxsize=queue_size)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        # 最后一次结果，给设置页看。故意不落盘：上周的失败是噪音，不是状态。
        self.last_error: str = ""
        self.last_error_at: float = 0.0
        self.last_success_at: float = 0.0
        self.dropped: int = 0
        # 熔断：连续失败几次后先失败快一点，别让队列一条条等满 10 秒。
        self._failures: int = 0
        self._open_until: float = 0.0

    # ------------------------------------------------------------------- public
    def notify(self, text: str, *, kind: str = "") -> bool:
        """把一条消息放进队列。不接受时返回 ``False``。

        永不抛异常、永不阻塞：通知是尽力而为，成交处理器不能等它。
        """
        if not self.settings.enabled:
            return False
        if not self._event_enabled(kind):
            return False
        content = _prepare(text)
        if not content:
            return False
        self._ensure_worker()
        try:
            self._queue.put_nowait(_Job(content))
        except queue.Full:
            # 丢最新的一条是诚实的做法：通道已经堵了，让调用方去等更糟。
            with self._lock:
                self.dropped += 1
            log.warning("通知队列已满，丢掉一条消息")
            return False
        return True

    def send_now(self, text: str, *, title: str = "") -> dict:
        """同步发一条，给设置页的「测试」按钮用。

        失败抛 :class:`NotifyError`，把原因原样给用户。这里不看 ``enabled``：
        按钮就是用来在打开开关之前先验证通道的。也不看熔断：点一次就
        证明通道恢复了。
        """
        content = _prepare(text)
        if not content:
            raise NotifyError("消息内容是空的。")
        try:
            return self._deliver(content, title=title)
        except NotifyError as exc:
            self._fail(str(exc))
            raise

    def status(self) -> dict:
        """设置页要的一屏信息：配好没有、上次成功/失败、堆了多少。"""
        settings = self.settings
        url = (settings.webhook_url or "").strip()
        return {
            "enabled": settings.enabled,
            "format": self._format(),
            "url": mask_url(url),
            "available": bool(url),
            "last_error": self.last_error,
            "last_error_at": self.last_error_at,
            "last_success_at": self.last_success_at,
            "dropped": self.dropped,
            "pending": self._queue.qsize(),
            # 暂存和熔断必须露出来：不显示的话，通道看起来闲着一句话没发。
            "spooled": len(_spool_load()),
            "breaker_seconds": max(0, int(self._open_until - time.time())),
        }

    def stop(self) -> None:
        self._stop.set()
        # 叫醒工作线程，让它看到停止标记，而不是空等队列超时。
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
                self._deliver(job.text, title=job.title)
            except NotifyError as exc:
                self._fail(str(exc))
                self._note_failure()
                # 留着：通道不通的时候，熔断告警正是最该补发的那条。
                _spool_add(job.text)
                log.warning("通知发送失败：%s", exc)
            except Exception as exc:  # pragma: no cover - 兜底
                self._fail(f"发送异常：{exc}")
                self._note_failure()
                _spool_add(job.text)
                log.warning("通知发送异常", exc_info=True)
            else:
                self._note_success()
                # 通道刚证明能用，把堆着的补上。补发出意外也不能让工作线程死掉：
                # 线程一死，后面的通知就静默不发了。
                try:
                    self._replay_spool()
                except Exception:  # pragma: no cover - 防御
                    log.warning("补发暂存消息失败", exc_info=True)

    # -------------------------------------------------------------------- guard
    def _check_breaker(self) -> None:
        """通道已知坏着的时候，立刻失败，别让每条消息都等满 10 秒。"""
        remaining = self._open_until - time.time()
        if remaining > 0:
            raise NotifyError(
                f"连续发送失败，已暂停 {int(remaining)} 秒。检查 Webhook 地址后点测试。"
            )

    def _note_success(self) -> None:
        self._failures = 0
        self._open_until = 0.0

    def _note_failure(self) -> None:
        self._failures += 1
        if self._failures >= BREAKER_THRESHOLD:
            self._failures = 0
            self._open_until = time.time() + BREAKER_COOLDOWN
            log.warning("通知通道连续失败，暂停发送 %g 秒", BREAKER_COOLDOWN)

    def _fail(self, message: str) -> None:
        self.last_error = message
        self.last_error_at = time.time()

    def _replay_spool(self) -> None:
        """补发之前失败的，从旧的开始，一次几条。"""
        items = _spool_load()
        if not items:
            return
        batch, rest = items[:SPOOL_BATCH], items[SPOOL_BATCH:]
        _spool_save(rest)
        for index, item in enumerate(batch):
            try:
                self._deliver(_spool_text(item))
            except NotifyError as exc:
                # 这条和后面没动的按原顺序放回去。
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

    def _format(self) -> str:
        value = str(self.settings.msg_format or DEFAULT_FORMAT).strip().lower()
        return value if value in FORMATS else DEFAULT_FORMAT

    def _custom_body(self, title: str, text: str) -> dict:
        template = (self.settings.webhook_template or "").strip()
        if not template:
            return {"title": title, "content": text}
        filled = template.replace("{title}", title).replace("{content}", text)
        try:
            value = json.loads(filled)
        except ValueError as exc:
            raise NotifyError(
                "自定义模板不是合法 JSON。正文里带引号或换行时，模板里要用转义。"
            ) from exc
        if not isinstance(value, dict):
            raise NotifyError("自定义模板必须是一段 JSON 对象。")
        return value

    def _deliver(self, text: str, *, title: str = "") -> dict:
        """真正发出去的那一步。地址没填就是明确报错，不猜。"""
        url = check_webhook_url(self.settings.webhook_url)
        if not url:
            raise NotifyError("还没填 Webhook 地址。到设置页把企业微信群机器人的地址粘进来。")
        content = f"{title}\n{text}" if title else text
        fmt = self._format()
        if fmt == "custom":
            body = self._custom_body(title, text)
        elif fmt == "qyapi_text":
            body = {"msgtype": "text", "text": {"content": content}}
        elif fmt == "qyapi_markdown":
            body = {"msgtype": "markdown", "markdown": {"content": content}}
        elif QYAPI_SEND_MARKER in url:
            # auto：企业微信群机器人就按它的格式发，markdown 比纯文本好读。
            body = {"msgtype": "markdown", "markdown": {"content": content}}
        else:
            body = {"title": title, "content": text}
        payload = _post_json(
            url,
            body,
            headers=parse_headers(self.settings.webhook_headers),
            timeout=self.timeout,
        )
        if "errcode" in payload:
            error = _qyapi_error(payload)
            if error:
                raise NotifyError(error)
        self.last_error = ""
        self.last_error_at = 0.0
        self.last_success_at = time.time()
        return {"sent": True, "format": fmt, "url": mask_url(url)}

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
        """任务消息：开始了、停了、跑完了。

        和成交消息分开：这些不是交易事件。一次跑了一小时的优化，才是让人离开
        工位的原因，手机是它结束时唯一的告知方式。
        """
        body = [line for line in (lines or []) if line]
        return "\n".join([f"【{title}】", *body])

    @staticmethod
    def format_warning(*, title: str, detail: str = "") -> str:
        text = f"【风险提示】{title}"
        return f"{text}\n{detail}" if detail else text


# 兼容旧名字。外部代码和测试还这么叫，留着当别名比重命名一遍省事。
WeChatNotifier = Notifier


def _venue_label(mode: str) -> str:
    """成交发生在哪个场子。

    模拟盘也是真下单，只是里面的钱是假的，所以标签要说清楚钱是真的还是假的。
    """
    if mode == "testnet":
        return "测试网"
    return "实盘"


def _action_label(side: str) -> str:
    """说清楚方向，不猜。

    老代码对任何不是 ``"buy"`` 的方向都说「卖出」，方向字段缺失时会理直气壮地
    报错方向。现在认不出来就说「成交」，读着不别扭，也不误导。
    """
    key = str(side or "").strip().lower()
    if key in ("buy", "long"):
        return "买入"
    if key in ("sell", "short"):
        return "卖出"
    return "成交"


def _format_price(value: float) -> str:
    """按量级决定小数位：8 万的价格不需要 4 位，0.00001234 会被四舍五入成 0。"""
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
    """数量不用科学计数法：``1e-07`` 没人看得懂，交易所也不会这么显示。"""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "—"
    text = f"{amount:.8f}".rstrip("0").rstrip(".")
    return text or "0"


def _prepare(text: str) -> str:
    """去掉多余空白并截断。没内容可发的返回空串。"""
    cleaned = (text or "").replace("\x00", "").strip()
    if len(cleaned) > MAX_MESSAGE_CHARS:
        cleaned = cleaned[: MAX_MESSAGE_CHARS - 1] + "…"
    return cleaned


# ---------------------------------------------------------------------- spool
def _spool_path() -> Path:
    """发不出去的消息留在哪。"""
    return paths.data_dir() / SPOOL_FILE


def _spool_load() -> list[dict]:
    """读暂存，丢掉太老的。

    六小时前的成交已经不是新闻，补发反而更吵。文件坏了就当没有：
    一个坏文件绝不能挡住新消息。
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
    """整体替换，写一半的文件会丢消息。"""
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
    """补发时标一下，读起来像迟到的旧闻，不是刚发生的事。"""
    text = str(item.get("text") or "")
    return text if text.startswith(SPOOL_PREFIX) else f"{SPOOL_PREFIX}\n{text}"

