"""CryptoPanic news aggregation.

CryptoPanic merges many crypto outlets into one feed, which is what the panel
wants: a handful of recent headlines for the pair being traded, each linked to
its original article.

The token is a plain API token, not an exchange secret, so it lives in the OS
keyring next to the Binance keys (see :class:`CredentialStore`). Requests are
made with ``urllib``: the desktop build has no HTTP client dependency beyond
the standard library, and one GET per couple of minutes does not need one.

Every failure is raised as :class:`NewsError` with a message meant for the
panel. A missing token is not an error — it is a configured/not-configured
state the UI explains.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

log = logging.getLogger(__name__)

API_URL = "https://cryptopanic.com/api/v1/posts/"
DEFAULT_TTL_SECONDS = 120.0


class NewsError(RuntimeError):
    """A news request that failed in a way the panel should report."""


def parse_posts(payload: Any, *, limit: int = 20) -> list[dict[str, Any]]:
    """Normalise a CryptoPanic response into panel rows.

    Deliberately tolerant: the endpoint has gained and lost fields over time,
    and one malformed row must not hide the rest of the feed.
    """
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        return []
    items: list[dict[str, Any]] = []
    for row in results[:limit]:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        url = str(row.get("url") or "").strip()
        if not title or not url:
            continue
        source = row.get("source") or {}
        currencies = [
            str(item.get("code") or "").upper()
            for item in row.get("currencies") or []
            if isinstance(item, dict) and item.get("code")
        ]
        votes = row.get("votes") or {}
        items.append(
            {
                "id": str(row.get("id") or url),
                "title": title,
                "url": url,
                "source": str(source.get("title") or source.get("domain") or ""),
                "published_at": str(row.get("published_at") or row.get("created_at") or ""),
                "currencies": currencies,
                "kind": str(row.get("kind") or "news"),
                "positive": int(votes.get("positive") or 0),
                "negative": int(votes.get("negative") or 0),
                "important": int(votes.get("important") or 0),
            }
        )
    return items


def _http_get(url: str, timeout: float) -> Any:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "biTrader/0.5 (+desktop)",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise NewsError("CryptoPanic 拒绝了请求：请检查 API Token 是否正确") from exc
        if exc.code == 429:
            raise NewsError("CryptoPanic 请求过于频繁，请稍后再试") from exc
        raise NewsError(f"CryptoPanic 返回 HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise NewsError(f"无法连接 CryptoPanic：{exc.reason}") from exc
    except OSError as exc:
        raise NewsError(f"无法连接 CryptoPanic：{exc}") from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise NewsError("CryptoPanic 返回了无法解析的内容") from exc


class NewsService:
    """Token-gated CryptoPanic feed with a short cache.

    The free tier is rate limited, and the panel polls on a timer, so a reply
    is reused for ``ttl`` seconds. Rendering a cached answer never raises:
    a transient failure is reported alongside the last good items.
    """

    def __init__(
        self,
        token_loader: Callable[[], str],
        *,
        ttl: float = DEFAULT_TTL_SECONDS,
        timeout: float = 8.0,
        fetcher: Callable[[str, float], Any] = _http_get,
    ) -> None:
        self._token_loader = token_loader
        self._ttl = ttl
        self._timeout = timeout
        self._fetch = fetcher
        self._lock = threading.Lock()
        self._cache: dict[tuple[str, int], tuple[float, list[dict[str, Any]]]] = {}

    def _token(self) -> str:
        try:
            return (self._token_loader() or "").strip()
        except Exception:  # a broken keyring must not break the feed
            log.warning("读取新闻 Token 失败", exc_info=True)
            return ""

    def urls_for(self, symbol: str) -> list[str]:
        """``BTC/USDT`` -> ``["BTC"]``; the quote leg is not a news topic."""
        base = symbol.split("/")[0].strip().upper()
        return [base] if base else []

    def snapshot(self, symbol: str = "", limit: int = 20) -> dict[str, Any]:
        limit = max(1, min(limit, 50))
        token = self._token()
        if not token:
            return {
                "configured": False,
                "items": [],
                "cached": False,
                "error": "",
                "source": "CryptoPanic",
            }

        currencies = self.urls_for(symbol)
        key = (",".join(currencies), limit)
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None and now - cached[0] < self._ttl:
                return {
                    "configured": True,
                    "items": cached[1],
                    "cached": True,
                    "error": "",
                    "source": "CryptoPanic",
                }

        query = {
            "auth_token": token,
            "public": "true",
            "kind": "news",
            "filter": "hot",
        }
        if currencies:
            query["currencies"] = ",".join(currencies)
        url = f"{API_URL}?{urllib.parse.urlencode(query)}"
        try:
            payload = self._fetch(url, self._timeout)
            items = parse_posts(payload, limit=limit)
        except NewsError as exc:
            return {
                "configured": True,
                "items": cached[1] if cached else [],
                "cached": bool(cached),
                "error": str(exc),
                "source": "CryptoPanic",
            }

        with self._lock:
            self._cache[key] = (now, items)
        return {
            "configured": True,
            "items": items,
            "cached": False,
            "error": "",
            "source": "CryptoPanic",
        }

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()
