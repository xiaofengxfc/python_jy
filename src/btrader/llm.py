"""Transport for the optional LLM assistant.

Only *transport* lives here: provider selection, the API key, the response
cache, the daily budget and the audit trail. What to ask the model lives in
``btrader.ai``.

Three rules shape this module:

* **The key never touches ``config.json``.** It goes into the OS keyring, next
  to the exchange credentials, because a JSON file in ``%APPDATA%`` is read by
  every process the user runs.
* **No new dependency.** The desktop build ships without an HTTP client beyond
  the standard library (``news.py`` already talks to the network with
  ``urllib``), so this does too.
* **A failure is not an error.** Every caller is an optional convenience —
  analysis, a briefing, a review. :meth:`LLMService.ask` returns a result dict
  with ``ok=False`` and a readable ``error`` instead of raising, so a slow or
  misconfigured model can never take down a page or a trading loop.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field, replace
from datetime import date
from typing import Any

from . import paths

log = logging.getLogger(__name__)

KEYRING_SERVICE = "biTrader-ai"
KEYRING_USER = "api_key"

# The endpoints are spelled out rather than discovered: an OpenAI-compatible
# provider differs only by base URL, so one code path covers DeepSeek, Moonshot,
# 通义, SiliconFlow and anything else that speaks /chat/completions.
PROVIDERS: dict[str, dict[str, Any]] = {
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "needs_key": True,
        "style": "openai",
        "help": "便宜、中文好，注册后在控制台创建 API Key",
    },
    "openai": {
        "label": "OpenAI 兼容接口",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "needs_key": True,
        "style": "openai",
        "help": "任何兼容 /chat/completions 的服务，改下面的地址和模型即可",
    },
    "anthropic": {
        "label": "Anthropic Claude",
        "base_url": "https://api.anthropic.com",
        "model": "claude-3-5-haiku-latest",
        "needs_key": True,
        "style": "anthropic",
        "help": "长文本分析较强，价格高于 DeepSeek",
    },
    "ollama": {
        "label": "本地 Ollama",
        "base_url": "http://127.0.0.1:11434",
        "model": "qwen2.5:7b",
        "needs_key": False,
        "style": "ollama",
        "help": "数据完全不出本机，但需要显卡且明显更慢",
    },
}

DEFAULT_PROVIDER = "deepseek"

# Some OpenAI-compatible relays sit behind a Cloudflare-style WAF that rejects
# urllib's default ``Python-urllib/3.x`` User-Agent with a bare HTTP 403 whose
# body reads "error code: 1010". The request never reaches the model, so the
# key looks unauthorised for no reason. An explicit honest UA — the same one
# ``news.py`` sends — is all those endpoints need to accept the call.
USER_AGENT = "biTrader/0.5 (+desktop)"
ACCEPT = "application/json"

# 输出额度不开放给用户调。改额度只动下面这两个常量，别在调用点写数字。
# 推理模型先把额度花在思考上：3000 时正文偶尔被截成空，4000 留够余量。
# 问答、分析、简报这类普通回答用它。
OUTPUT_TOKENS = 4000
# 写代码这类长回答（策略实验室生成一个完整模块）用它。推理模型先花掉一部分
# 额度思考，源码又长，2400 那种小额度会让 JSON 从中间断掉；DeepSeek 和
# Claude 3.5 Haiku 的上限都是 8192，8000 各家都吃得下，别再往下砍。
OUTPUT_TOKENS_CODE = 8000
# 同一根已收盘 K 线重复提问不该计费两次。缓存窗口固定，不给调。
CACHE_SECONDS = 300.0
CACHE_LIMIT = 500
AUDIT_LIMIT_BYTES = 2 * 1024 * 1024

# 「被截断」在两家协议里叫法不同：OpenAI 风格回 finish_reason="length"，
# Anthropic 回 stop_reason="max_tokens"。两条都要认，否则 Anthropic 那边被截断
# 的回答会被当成「模型答完了但答得不好」，用户又不知道是额度问题。
TRUNCATED_FINISH = ("length", "max_tokens")


def is_truncated(finish: str) -> bool:
    """这一步回答是不是被输出上限截断了。"""
    return str(finish or "") in TRUNCATED_FINISH


@dataclass
class AISettings:
    """What the user chose in 设置 → AI 助手."""

    enabled: bool = False
    provider: str = DEFAULT_PROVIDER
    model: str = ""
    base_url: str = ""
    temperature: float = 0.2
    timeout: float = 60.0
    # Positions, balances and P/L are private. Sending them to a third-party
    # API is opt-in, and the settings page says so next to the switch.
    share_position: bool = False
    # Auto-trader hook: write a briefing after every closed candle.
    auto_brief: bool = False

    def resolved(self) -> "AISettings":
        """Fill in the provider defaults so callers never see an empty model.

        An unknown provider (an old config, a typo) resolves to the default spec
        instead of raising: this runs while rendering the settings page, and a
        bad enum value there must not take the page down.
        """
        spec = PROVIDERS.get(self.provider) or PROVIDERS[DEFAULT_PROVIDER]
        provider = self.provider if self.provider in PROVIDERS else DEFAULT_PROVIDER
        return replace(
            self,
            provider=provider,
            model=(self.model or "").strip() or str(spec["model"]),
            base_url=(self.base_url or "").strip() or str(spec["base_url"]),
        )


class AIError(RuntimeError):
    """A call could not be completed; the message is shown to the user.

    ``finish`` is the provider's ``finish_reason`` when the request actually
    reached the model (empty otherwise). It rides on the exception so
    :meth:`ask` can still report "被截断" on the failure path, where the text
    itself is empty and the finish reason would otherwise be lost.
    """

    def __init__(self, message: str, *, finish: str = "") -> None:
        super().__init__(message)
        self.finish = finish


# --------------------------------------------------------------------- keyring
def load_key() -> str:
    try:
        import keyring

        return keyring.get_password(KEYRING_SERVICE, KEYRING_USER) or ""
    except Exception as exc:  # a broken keyring must not break the page
        log.warning("读取 AI 密钥失败: %s", exc)
        return ""


def save_key(value: str) -> bool:
    try:
        import keyring

        if value:
            keyring.set_password(KEYRING_SERVICE, KEYRING_USER, value)
        else:
            try:
                keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)
            except Exception:
                pass
        return True
    except Exception as exc:
        log.warning("保存 AI 密钥失败: %s", exc)
        return False


def mask_key(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 10:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


# ----------------------------------------------------------------------- files
def _cache_file():
    return paths.cache_dir() / "ai-cache.json"


def _audit_file():
    return paths.logs_dir() / "ai-audit.jsonl"


def _read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path, value) -> None:
    try:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:  # pragma: no cover - disk trouble
        log.warning("写入 %s 失败: %s", path.name, exc)


class _Cache:
    """Tiny TTL cache on disk, so a restart does not re-bill the same candle."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        raw = _read_json(_cache_file(), {})
        self._entries: dict[str, dict] = raw.get("entries") or {}
        self._calls: dict[str, int] = raw.get("calls") or {}

    def _flush(self) -> None:
        if len(self._entries) > CACHE_LIMIT:
            # Oldest first: the newest answers are the ones worth keeping.
            ordered = sorted(self._entries.items(), key=lambda kv: kv[1].get("at", 0))
            for key, _ in ordered[: len(self._entries) - CACHE_LIMIT]:
                self._entries.pop(key, None)
        _write_json(_cache_file(), {"entries": self._entries, "calls": self._calls})

    def get(self, key: str, ttl: float) -> dict | None:
        """A cached answer, or None when it is missing or older than ``ttl``.

        ``ttl <= 0`` means the user turned caching off, so nothing is ever
        served from here — "缓存 0 秒" must mean "每次重新问", not "永久缓存".
        """
        if ttl <= 0:
            return None
        with self._lock:
            entry = self._entries.get(key)
            if not entry:
                return None
            if time.time() - float(entry.get("at") or 0) > ttl:
                self._entries.pop(key, None)
                return None
            return entry

    def put(self, key: str, text: str, tokens: int, finish: str = "") -> None:
        with self._lock:
            self._entries[key] = {
                "at": time.time(),
                "text": text,
                "tokens": int(tokens),
                # finish 存下来，缓存命中时才能照样报出 truncated。
                "finish": str(finish or ""),
            }
            self._flush()

    def calls_today(self) -> int:
        with self._lock:
            return int(self._calls.get(date.today().isoformat()) or 0)

    def count_call(self) -> None:
        with self._lock:
            today = date.today().isoformat()
            # A fresh day starts from zero; the old keys are dropped rather than
            # kept, so the file cannot grow one entry per day forever.
            self._calls = {today: int(self._calls.get(today) or 0) + 1}
            self._flush()

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._flush()


_CACHE: _Cache | None = None
_CACHE_LOCK = threading.Lock()


def cache() -> _Cache:
    global _CACHE
    with _CACHE_LOCK:
        if _CACHE is None:
            _CACHE = _Cache()
        return _CACHE


def reset_cache() -> None:
    """Test hook: forget the on-disk cache so a suite never reads real data."""
    global _CACHE
    with _CACHE_LOCK:
        _CACHE = None


# ----------------------------------------------------------------------- audit
def _append_audit(record: dict[str, Any]) -> None:
    path = _audit_file()
    try:
        if path.exists() and path.stat().st_size > AUDIT_LIMIT_BYTES:
            path.replace(path.with_suffix(".1.jsonl"))
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:  # pragma: no cover - disk trouble
        log.warning("写入 AI 审计日志失败: %s", exc)


def audit_tail(limit: int = 50) -> list[dict[str, Any]]:
    """Newest audit records first. Used by the settings page and hit-rate page."""
    path = _audit_file()
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in reversed(lines[-max(1, limit * 4) :]):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------- service
@dataclass
class LLMService:
    """One place that knows how to talk to the configured model.

    ``settings`` is read on every call, so the settings page can change the
    provider without rebuilding anything.
    """

    settings: AISettings = field(default_factory=AISettings)
    _key: str = ""

    # ---------------------------------------------------------------- status
    def key(self) -> str:
        if not self._key:
            self._key = load_key()
        return self._key

    def reload_key(self) -> str:
        self._key = load_key()
        return self._key

    def set_key(self, value: str) -> bool:
        ok = save_key(value.strip())
        if ok:
            self._key = value.strip()
        return ok

    def status(self) -> dict[str, Any]:
        settings = self.settings.resolved()
        spec = PROVIDERS.get(self.settings.provider) or PROVIDERS[DEFAULT_PROVIDER]
        key = self.key()
        return {
            **asdict(settings),
            "provider_label": spec["label"],
            "provider_help": spec["help"],
            "needs_key": bool(spec["needs_key"]),
            "has_key": bool(key),
            "key_hint": mask_key(key),
            "configured": bool(key) or not spec["needs_key"],
            "providers": [
                {
                    "key": name,
                    "label": item["label"],
                    "model": item["model"],
                    "base_url": item["base_url"],
                    "needs_key": bool(item["needs_key"]),
                    "help": item["help"],
                }
                for name, item in PROVIDERS.items()
            ],
            "calls_today": cache().calls_today(),
            "audit": audit_tail(20),
        }

    # ------------------------------------------------------------------- ask
    def ask(
        self,
        prompt: str,
        *,
        system: str = "",
        cache_key: str = "",
        kind: str = "",
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        """One completion. Never raises; failures come back as ``ok=False``.

        ``cache_key`` is the identity of the *question*: callers pass something
        like ``kline|BTC/USDT|1h|<last closed ts>`` so the same candle is paid
        for once. An empty key disables the cache (used by "发送测试").
        """
        started = time.time()
        settings = self.settings.resolved()
        result: dict[str, Any] = {
            "ok": False,
            "text": "",
            "cached": False,
            "tokens": 0,
            "model": settings.model,
            "provider": settings.provider,
            "elapsed": 0.0,
            "error": "",
            "kind": kind,
            # finish / truncated 是新增字段：老调用方只读 ok/text/error/tokens，
            # 那几个字段的含义一个都没改。
            "finish": "",
            "truncated": False,
        }
        if not self.settings.enabled:
            result["error"] = "AI 助手未启用，请在设置里打开开关。"
            return result

        if cache_key:
            hit = cache().get(cache_key, CACHE_SECONDS)
            if hit:
                cached_finish = str(hit.get("finish") or "")
                result.update(
                    ok=True,
                    text=str(hit.get("text") or ""),
                    cached=True,
                    tokens=int(hit.get("tokens") or 0),
                    # 命中缓存也要如实报 finish / truncated：调用方按它决定
                    # 「截断」还是「回答不合格」，缓存不该改变这个判断。
                    finish=cached_finish,
                    truncated=is_truncated(cached_finish),
                    elapsed=round(time.time() - started, 3),
                )
                return result

        spec = PROVIDERS.get(self.settings.provider) or PROVIDERS[DEFAULT_PROVIDER]
        key = self.key()
        if spec["needs_key"] and not key:
            result["error"] = "还没有填写 API Key。"
            return result
        finish = ""
        try:
            text, tokens, finish = self._request(
                spec, settings, key, system, prompt, max_tokens=max_tokens
            )
        except AIError as exc:
            finish = str(getattr(exc, "finish", "") or "")
            result["finish"] = finish
            result["truncated"] = is_truncated(finish)
            result["error"] = str(exc)
            result["elapsed"] = round(time.time() - started, 3)
            _append_audit(
                {
                    "at": time.time(),
                    "kind": kind,
                    "provider": settings.provider,
                    "model": settings.model,
                    "ok": False,
                    "error": str(exc),
                    "cache_key": cache_key,
                }
            )
            return result
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("AI 调用异常", exc_info=True)
            result["error"] = f"调用失败：{exc}"
            result["elapsed"] = round(time.time() - started, 3)
            return result

        cache().count_call()
        truncated = is_truncated(finish)
        if cache_key and not truncated:
            # 被截断的回答不进缓存：它是一次「没写完」的结果，缓存下来只会
            # 让同样的半截答案在 5 分钟里反复命中。
            cache().put(cache_key, text, tokens, finish)
        result.update(
            ok=True,
            text=text,
            tokens=tokens,
            finish=finish,
            # 被截断的回答也可能有正文——JSON 停在字符串中间。routes_lab 靠这个
            # 把「截断」和「模型答非所问」分开报，不能在这里丢掉。
            truncated=truncated,
            elapsed=round(time.time() - started, 3),
        )
        _append_audit(
            {
                "at": time.time(),
                "kind": kind,
                "provider": settings.provider,
                "model": settings.model,
                "ok": True,
                "tokens": tokens,
                "elapsed": result["elapsed"],
                "cache_key": cache_key,
                "prompt_chars": len(prompt),
                "answer_chars": len(text),
                # The answer itself is kept: the hit-rate page replays it.
                "text": text[:4000],
            }
        )
        return result

    # --------------------------------------------------------------- request
    def _request(
        self,
        spec: dict[str, Any],
        settings: AISettings,
        key: str,
        system: str,
        prompt: str,
        *,
        max_tokens: int | None = None,
    ) -> tuple[str, int, str]:
        """Returns ``(text, tokens, finish)``.

        ``finish`` is the provider's finish reason, or "" when it does not say.
        Callers need it to tell "模型写完了但 JSON 坏了" from "被截断了".
        """
        style = spec["style"]
        base = settings.base_url.rstrip("/")
        output_limit = int(max_tokens or OUTPUT_TOKENS)
        if style == "anthropic":
            url = f"{base}/v1/messages"
            payload = {
                "model": settings.model,
                "max_tokens": output_limit,
                "temperature": float(settings.temperature),
                "messages": [{"role": "user", "content": prompt}],
            }
            if system:
                payload["system"] = system
            headers = {
                "content-type": "application/json",
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
            }
        elif style == "ollama":
            url = f"{base}/api/chat"
            messages = ([{"role": "system", "content": system}] if system else []) + [
                {"role": "user", "content": prompt}
            ]
            payload = {
                "model": settings.model,
                "messages": messages,
                "stream": False,
                "options": {
                    "temperature": float(settings.temperature),
                    "num_predict": output_limit,
                },
            }
            headers = {"content-type": "application/json"}
        else:
            url = f"{base}/chat/completions"
            messages = ([{"role": "system", "content": system}] if system else []) + [
                {"role": "user", "content": prompt}
            ]
            payload = {
                "model": settings.model,
                "messages": messages,
                "temperature": float(settings.temperature),
                "max_tokens": output_limit,
                "stream": False,
            }
            headers = {
                "content-type": "application/json",
                "authorization": f"Bearer {key}",
            }

        # Every provider gets these two: a bare urllib client is rejected by
        # some relays' WAF before the request ever reaches the model (a 403
        # whose body says "error code: 1010"), which the user would otherwise
        # read as "this key may not access that model".
        headers.setdefault("user-agent", USER_AGENT)
        headers.setdefault("accept", ACCEPT)

        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=float(settings.timeout)) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            raise AIError(_http_error_text(exc.code, detail, settings.provider)) from exc
        except urllib.error.URLError as exc:
            raise AIError(f"连不上模型服务（{exc.reason}）：检查网络或代理设置。") from exc
        except TimeoutError as exc:
            raise AIError(f"模型超过 {settings.timeout:g} 秒没有响应。") from exc

        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise AIError("模型返回的不是 JSON，可能被代理或防火墙拦截了。") from exc

        text, tokens = _extract(style, data)
        finish = _finish_reason(style, data)
        if not text:
            raise AIError(_empty_answer_text(style, data, output_limit), finish=finish)
        return text, tokens, finish


# A 403 from the edge is not a 403 from the model: a WAF in front of an
# OpenAI-compatible relay rejects clients by signature with a bare
# "error code: 1010" page, and blaming the key sends the user to the wrong fix.
_WAF_MARKERS = ("cloudflare", "error code: 1010", "error code: 1015", "error code: 1020")


def _looks_like_waf_block(detail: str) -> bool:
    lowered = (detail or "").lower()
    return any(marker in lowered for marker in _WAF_MARKERS)


def _http_error_text(code: int, detail: str, provider: str) -> str:
    """Turn the usual HTTP codes into something the user can act on."""
    if code == 403 and _looks_like_waf_block(detail):
        hint = (
            "请求被服务商前面的防火墙拦截了（Cloudflare 1010），"
            "不是 Key 或模型权限的问题。检查网络和代理，或者联系服务商放行客户端。"
        )
    else:
        hint = {
            401: "API Key 无效或已过期。",
            402: "账户余额不足。",
            403: "这个 Key 没有访问该模型的权限。",
            404: "模型名或接口地址不对。",
            429: "触发频率限制，稍后再试。",
        }.get(code)
        if code >= 500:
            hint = "模型服务端错误，稍后重试。"
    message = hint or f"HTTP {code}。"
    if detail:
        message += f" {detail}"
    return f"{PROVIDERS.get(provider, {}).get('label', provider)}：{message}"


def _finish_reason(style: str, data: Any) -> str:
    """The provider's own "why did it stop" field, or "" when it is missing."""
    if not isinstance(data, dict):
        return ""
    if style == "openai":
        choices = data.get("choices") or []
        if choices and isinstance(choices[0], dict):
            return str(choices[0].get("finish_reason") or "")
        return ""
    if style == "anthropic":
        return str(data.get("stop_reason") or "")
    if style == "ollama":
        return str(data.get("done_reason") or "")
    return ""


def _reasoning_text(style: str, data: Any) -> str:
    """Hidden reasoning a model emitted instead of an answer, if any."""
    if not isinstance(data, dict):
        return ""
    if style == "openai":
        choices = data.get("choices") or []
        if choices and isinstance(choices[0], dict):
            message = choices[0].get("message") or {}
            return str(message.get("reasoning_content") or message.get("reasoning") or "")
        return ""
    if style == "ollama":
        return str((data.get("message") or {}).get("thinking") or "")
    return ""


def _empty_answer_text(style: str, data: Any, limit: int) -> str:
    """正文为空时说清楚原因：是被截断，还是模型只给了思考过程。

    推理模型把 ``max_tokens`` 先花在隐藏的思考上。额度不够时整段预算都被
    思考吃掉，``content`` 是空串、``finish_reason`` 是 length，用户看到的
    “模型返回了空内容”完全指不到问题在哪。

    额度是常量，用户改不了，所以文案只说用户能动手的事：重试、把要求写短。
    别再把用户指到设置页去——那里没有「输出上限」这一项。
    """
    if _finish_reason(style, data) == "length":
        return (
            f"回答被输出上限截断了（上限 {limit} tokens）。"
            "推理模型先花额度思考，正文就没额度了。"
            "重试一次，或把要求写短一点。"
        )
    if _reasoning_text(style, data).strip():
        return "模型只返回了思考过程，没有正文。重试一次，或把要求写短一点。"
    return "模型返回了空内容。"


def _extract(style: str, data: Any) -> tuple[str, int]:
    if not isinstance(data, dict):
        return "", 0
    if style == "anthropic":
        blocks = data.get("content") or []
        text = "".join(
            str(block.get("text") or "")
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        )
        usage = data.get("usage") or {}
        tokens = int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
        return text.strip(), tokens
    if style == "ollama":
        message = data.get("message") or {}
        text = str(message.get("content") or "")
        tokens = int(data.get("prompt_eval_count") or 0) + int(data.get("eval_count") or 0)
        return text.strip(), tokens
    choices = data.get("choices") or []
    if choices and isinstance(choices[0], dict):
        message = choices[0].get("message") or {}
        text = str(message.get("content") or "")
        usage = data.get("usage") or {}
        tokens = int(usage.get("total_tokens") or 0)
        return text.strip(), tokens
    return "", 0
