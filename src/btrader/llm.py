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

# 输出上限不再是「我们替模型定一个额度」。OpenAI 兼容协议里 max_tokens 是可选
# 字段，省略就等于「由模型自己决定什么时候停」，所以那一路现在根本不发这个字段
# （以前固定 4000：分析类回答常从中间断掉，界面上只剩半截内容，用户看不出是我
# 们截的）。ollama 的 num_predict 同理，只有调用方显式要求时才写。

# 下面两个只在「供应商必须填一个数」时用：Anthropic 的 max_tokens 是必填字段，
# 省略直接 400。给一个够大的兜底值，模型自己会在写完时停。
#
# 历史原因（别删）：推理模型会先把输出额度花在思考上，额度给小了正文会被截成
# 空——3000 时偶尔整段没有正文，这也是当初调到 4000 的原因；写策略代码那一路
# 更甚，源码长、思考也长，2400 那种小额度会让 JSON 从中间断掉。
FALLBACK_MAX_TOKENS = 32000
# 模型的真上限比兜底值小时（Claude 3.5 Haiku、DeepSeek 都是 8192），供应商会回
# 400「max_tokens 超过上限」。这种情况下自动降到这个数重试一次，别把英文报错
# 甩给用户，也别让用户自己去猜该填多少。
FALLBACK_MAX_TOKENS_RETRY = 8192
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

    def __init__(self, message: str, *, finish: str = "", detail: str = "") -> None:
        super().__init__(message)
        self.finish = finish
        # 供应商返回的原文。用来判断「是不是输出上限超了」，不进给用户看的文案。
        self.detail = detail


# ----------------------------------------------------------------- 密钥的落点
# 和交易所的 API Key 走同一个 store（core/secret_store）：桌面有系统密钥环就用
# 密钥环；Debian/Ubuntu 服务器上没有 SecretService/DBus，就落到数据目录里的加密
# 文件。以前这里直接 import keyring 就用，服务器上「AI 助手的 Key 存不进去」，
# 报错还是一句英文——和交易所 Key 同一个坑，只是换了个地方。
#
# service 名保持 biTrader-ai：改了等于把已经存进去的 Key 弄丢。旧的系统密钥环里
# 那一份不用迁移：探到密钥环可用时读的就是同一个 service，第一次读就拿回来了。
_STORE: Any = None
_STORE_LOCK = threading.Lock()


def _store():
    """当前生效的密钥存储（探一次，之后进程内复用）。"""
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            from .core import secret_store

            _STORE = secret_store.open_store()
        return _STORE


def reset_store() -> None:
    """Test hook: 忘掉缓存的存储后端，下次调用重新探测。"""
    global _STORE
    with _STORE_LOCK:
        _STORE = None


def storage_info() -> dict[str, str]:
    """AI 的 Key 存在哪儿，给设置页看（和凭据卡同一个口径）。"""
    try:
        from .core import secret_store

        return secret_store.describe(_store())
    except Exception as exc:  # 说明信息不该把状态接口弄挂
        log.warning("读取 AI 密钥存储信息失败: %s", exc)
        return {"kind": "none", "label": "不可用", "path": "", "detail": "读不到存储信息。"}


def load_key() -> str:
    try:
        return _store().get_password(KEYRING_SERVICE, KEYRING_USER) or ""
    except Exception as exc:  # 坏存储不能把页面弄挂
        log.warning("读取 AI 密钥失败: %s", exc)
        return ""


def save_key(value: str) -> bool:
    try:
        store = _store()
        if value:
            store.set_password(KEYRING_SERVICE, KEYRING_USER, value)
        else:
            try:
                store.delete_password(KEYRING_SERVICE, KEYRING_USER)
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
        try:
            # 缓存目录建不出来（只读盘、权限）时降级成「永不命中」，
            # 让调用照样发出去，而不是在 ask 的开头就抛异常。
            raw = _read_json(_cache_file(), {})
        except OSError as exc:  # pragma: no cover - disk trouble
            log.warning("读取 AI 缓存失败: %s", exc)
            raw = {}
        self._entries: dict[str, dict] = raw.get("entries") or {}
        self._calls: dict[str, int] = raw.get("calls") or {}

    def _flush(self) -> None:
        if len(self._entries) > CACHE_LIMIT:
            # Oldest first: the newest answers are the ones worth keeping.
            ordered = sorted(self._entries.items(), key=lambda kv: kv[1].get("at", 0))
            for key, _ in ordered[: len(self._entries) - CACHE_LIMIT]:
                self._entries.pop(key, None)
        try:
            _write_json(_cache_file(), {"entries": self._entries, "calls": self._calls})
        except OSError as exc:  # pragma: no cover - disk trouble
            log.warning("写入 AI 缓存失败: %s", exc)

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

    def put(
        self,
        key: str,
        text: str,
        tokens: int,
        finish: str = "",
        fingerprint: str = "",
        usage: dict[str, int] | None = None,
    ) -> None:
        with self._lock:
            self._entries[key] = {
                "at": time.time(),
                "text": text,
                "tokens": int(tokens),
                # finish 存下来，缓存命中时才能照样报出 truncated。
                "finish": str(finish or ""),
                # 供应商/模型/温度/额度。同一句问题换了这些就是另一个问题，
                # 命中时要靠它判断这条答案还能不能用。
                "fingerprint": str(fingerprint or ""),
                # 命中也要能报「本次多少 tokens」，所以用量跟着答案一起存。
                "usage": dict(usage or {}),
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
    # 路径解析（logs 目录建不出来）也算写盘失败的一种：审计是旁路，写不进去
    # 只能少一条记录，不能把一次已经成功的模型调用变成异常。
    try:
        path = _audit_file()
        if path.exists() and path.stat().st_size > AUDIT_LIMIT_BYTES:
            path.replace(path.with_suffix(".1.jsonl"))
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:  # pragma: no cover - disk trouble
        log.warning("写入 AI 审计日志失败: %s", exc)


def audit_tail(limit: int = 50) -> list[dict[str, Any]]:
    """Newest audit records first. Used by the settings page and hit-rate page."""
    try:
        path = _audit_file()
        if not path.exists():
            return []
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
            # Key 存在哪儿：桌面上是系统密钥环，服务器上退化成数据目录里的加密
            # 文件。设置页那张 AI 卡据此说明，和「API 凭据」卡同一口径。
            "storage": storage_info(),
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
        # 影响输出的四件事。缓存 key 由调用方给（「哪一根 K 线」），这里补上
        # 「问的是谁、用多大额度」，否则换个模型后 5 分钟内还拿旧模型的答案。
        fingerprint = (
            f"{settings.provider}|{settings.model}"
            f"|{float(settings.temperature):g}|{int(max_tokens or 0)}"
        )
        result: dict[str, Any] = {
            "ok": False,
            "text": "",
            "cached": False,
            "tokens": 0,
            # 实际用量：input / output 分开，total 是两者之和（= tokens）。
            # 界面靠它显示「本次 N tokens」，也靠它算成本。
            "usage": {"input": 0, "output": 0, "total": 0},
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
            stored = str(hit.get("fingerprint") or "") if hit else ""
            # 老版本写下的条目没有指纹字段，空串照旧命中（几分钟内自然过期）。
            if hit and stored and stored != fingerprint:
                hit = None
            if hit:
                cached_finish = str(hit.get("finish") or "")
                hit_tokens = int(hit.get("tokens") or 0)
                result.update(
                    ok=True,
                    text=str(hit.get("text") or ""),
                    cached=True,
                    tokens=hit_tokens,
                    usage=_usage_from_stored(hit.get("usage"), hit_tokens),
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
            text, usage, finish = self._request(
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
        tokens = int(usage.get("total") or 0)
        truncated = is_truncated(finish)
        if cache_key and not truncated:
            # 被截断的回答不进缓存：它是一次「没写完」的结果，缓存下来只会
            # 让同样的半截答案在 5 分钟里反复命中。
            cache().put(cache_key, text, tokens, finish, fingerprint, usage)
        result.update(
            ok=True,
            text=text,
            tokens=tokens,
            usage=usage,
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
    ) -> tuple[str, dict[str, int], str]:
        """Returns ``(text, usage, finish)``.

        ``usage`` 是 ``{"input", "output", "total"}``，给界面报「本次 N tokens」。
        ``finish`` is the provider's finish reason, or "" when it does not say.
        Callers need it to tell "模型写完了但 JSON 坏了" from "被截断了".

        输出上限：调用方显式传了 ``max_tokens`` 才发这个字段。没传时，能省的供应商
        （OpenAI 兼容、ollama）干脆不发，让模型自己决定什么时候停；Anthropic 的
        max_tokens 是必填字段，给 ``FALLBACK_MAX_TOKENS`` 兜底，被供应商以「超过
        模型上限」拒掉时降到 ``FALLBACK_MAX_TOKENS_RETRY`` 重试一次。
        """
        style = spec["style"]
        base = settings.base_url.rstrip("/")
        explicit = int(max_tokens or 0)
        if style == "anthropic":
            url = f"{base}/v1/messages"
            payload = {
                "model": settings.model,
                # 必填字段，不能省。
                "max_tokens": explicit or FALLBACK_MAX_TOKENS,
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
            options: dict[str, Any] = {"temperature": float(settings.temperature)}
            if explicit:
                # 不写就是「让模型自己停」（num_predict 默认无限）。
                options["num_predict"] = explicit
            payload = {
                "model": settings.model,
                "messages": messages,
                "stream": False,
                "options": options,
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
                "stream": False,
            }
            if explicit:
                payload["max_tokens"] = explicit
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

        try:
            data = self._post(url, payload, headers, settings)
        except AIError as exc:
            retry_limit = int(payload.get("max_tokens") or 0)
            if not (
                style == "anthropic"
                and retry_limit > FALLBACK_MAX_TOKENS_RETRY
                and _rejects_output_limit(getattr(exc, "detail", ""))
            ):
                raise
            # 模型的真上限比兜底值小。降一档重试一次，用户不该为这件事看到英文原文。
            log.warning(
                "%s 说输出上限超了（%s），降到 %s 重试一次：%s",
                settings.provider,
                retry_limit,
                FALLBACK_MAX_TOKENS_RETRY,
                getattr(exc, "detail", "")[:200],
            )
            payload["max_tokens"] = FALLBACK_MAX_TOKENS_RETRY
            try:
                data = self._post(url, payload, headers, settings)
            except AIError as again:
                if _rejects_output_limit(getattr(again, "detail", "")):
                    raise AIError(
                        f"{spec.get('label') or settings.provider}：这个模型允许的输出上限比 "
                        f"{FALLBACK_MAX_TOKENS} 小，已经自动降到 "
                        f"{FALLBACK_MAX_TOKENS_RETRY} 重试还是被拒。"
                        "换个模型，或把要求写短一点。"
                    ) from again
                raise

        text, usage = _extract(style, data)
        finish = _finish_reason(style, data)
        if not text:
            # ollama 的上限在 options.num_predict 里，别只认 max_tokens。
            ceiling = int(
                payload.get("max_tokens")
                or (payload.get("options") or {}).get("num_predict")
                or 0
            )
            raise AIError(_empty_answer_text(style, data, ceiling), finish=finish)
        return text, usage, finish

    def _post(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        settings: AISettings,
    ) -> Any:
        """一次 HTTP 往返。失败的 ``AIError`` 带上供应商原文（``detail``）。"""
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
            raise AIError(
                _http_error_text(exc.code, detail, settings.provider), detail=detail
            ) from exc
        except urllib.error.URLError as exc:
            raise AIError(f"连不上模型服务（{exc.reason}）：检查网络或代理设置。") from exc
        except TimeoutError as exc:
            raise AIError(f"模型超过 {settings.timeout:g} 秒没有响应。") from exc

        try:
            return json.loads(raw)
        except ValueError as exc:
            raise AIError("模型返回的不是 JSON，可能被代理或防火墙拦截了。") from exc


# A 403 from the edge is not a 403 from the model: a WAF in front of an
# OpenAI-compatible relay rejects clients by signature with a bare
# "error code: 1010" page, and blaming the key sends the user to the wrong fix.
_WAF_MARKERS = ("cloudflare", "error code: 1010", "error code: 1015", "error code: 1020")

# 「max_tokens 超过模型上限」的各种说法（Anthropic 的原文是
# "max_tokens: 32000 > 8192, which is the maximum allowed number of output
# tokens for claude-3-5-haiku"）。只看这个判断要不要降一档重试，不拿它当文案。
_LIMIT_MARKERS = ("max_tokens", "max output tokens", "output tokens")
_LIMIT_WORDS = ("exceed", "too large", "greater", "maximum", ">", "larger")


def _rejects_output_limit(detail: str) -> bool:
    lowered = (detail or "").lower()
    if not any(marker in lowered for marker in _LIMIT_MARKERS):
        return False
    return any(word in lowered for word in _LIMIT_WORDS)


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
    if is_truncated(_finish_reason(style, data)):
        ceiling = f"（这次的上限 {limit} tokens）" if limit else "（供应商自身设定的上限）"
        return (
            f"回答被输出上限截断了{ceiling}。"
            "推理模型先花额度思考，正文就没额度了。"
            "重试一次，或把要求写短一点。"
        )
    if _reasoning_text(style, data).strip():
        return "模型只返回了思考过程，没有正文。重试一次，或把要求写短一点。"
    return "模型返回了空内容。"


def _usage_row(prompt_tokens: int, completion_tokens: int) -> dict[str, int]:
    """用量统一成一种形状：输入、输出分开，total 是两者之和。"""
    prompt_tokens = int(prompt_tokens or 0)
    completion_tokens = int(completion_tokens or 0)
    return {
        "input": prompt_tokens,
        "output": completion_tokens,
        "total": prompt_tokens + completion_tokens,
    }


def _usage_from_stored(stored: Any, total: int) -> dict[str, int]:
    """缓存里的用量。老条目没有 usage 字段，只能把 total 摊在合计上。"""
    if isinstance(stored, dict) and ("input" in stored or "output" in stored):
        return _usage_row(int(stored.get("input") or 0), int(stored.get("output") or 0))
    return {"input": 0, "output": 0, "total": int(total or 0)}


def _extract(style: str, data: Any) -> tuple[str, dict[str, int]]:
    """正文 + 用量。用量按家的字段名各不相同，这里统一成 ``_usage_row``。"""
    if not isinstance(data, dict):
        return "", _usage_row(0, 0)
    if style == "anthropic":
        blocks = data.get("content") or []
        text = "".join(
            str(block.get("text") or "")
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        )
        usage = data.get("usage") or {}
        return text.strip(), _usage_row(
            int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)
        )
    if style == "ollama":
        message = data.get("message") or {}
        text = str(message.get("content") or "")
        return text.strip(), _usage_row(
            int(data.get("prompt_eval_count") or 0), int(data.get("eval_count") or 0)
        )
    choices = data.get("choices") or []
    if choices and isinstance(choices[0], dict):
        message = choices[0].get("message") or {}
        text = str(message.get("content") or "")
        usage = data.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        completion_tokens = int(
            usage.get("completion_tokens") or usage.get("output_tokens") or 0
        )
        total = int(usage.get("total_tokens") or 0)
        row = _usage_row(prompt_tokens, completion_tokens)
        if total and not (prompt_tokens or completion_tokens):
            # 只给合计的供应商（老接口常见）：分开的两项留空，别瞎猜比例。
            row = {"input": 0, "output": 0, "total": total}
        return text.strip(), row
    return "", _usage_row(0, 0)
