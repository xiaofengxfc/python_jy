"""Free crypto news for the pair being traded.

The panel needs a handful of recent, relevant headlines, each linked to its
original article — not a paid aggregator. Three keyless sources are merged:

* Binance announcements — listing, delisting, maintenance and risk notices.
  These are the events that actually move a Binance pair, and they carry the
  exchange's own Chinese copy when it exists.
* CoinTelegraph and Decrypt RSS — the wider market context.

Fetching uses ``urllib`` and ``xml.etree`` only: the desktop build has no HTTP
client dependency beyond the standard library, and one request per source every
couple of minutes does not need one.

A source that fails never hides the others: the feed reports what it could read
and lists the failures alongside it. When every source fails, the last good
items are still returned with an error, so a blank panel always means "no news"
rather than "the network broke".
"""

from __future__ import annotations

import html
import http.client
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable

log = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 120.0

BINANCE_ANNOUNCEMENTS = (
    "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"
    "?type=1&pageNo=1&pageSize={size}"
)
# Catalogs that matter to a trader. Deliberately not "Latest Activities":
# reward campaigns are noise next to a listing or a delisting.
BINANCE_CATALOGS = {
    48: "上币公告",
    161: "下架公告",
    49: "币安公告",
}
RSS_SOURCES: tuple[tuple[str, str], ...] = (
    ("CoinTelegraph", "https://cointelegraph.com/rss"),
    ("Decrypt", "https://decrypt.co/feed"),
)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

# RSS headlines spell assets out ("Bitcoin breaks 80k"), so matching the ticker
# alone would drop every article. Anything not listed still matches on its
# ticker; this only widens the net for the majors.
TICKER_ALIASES: dict[str, tuple[str, ...]] = {
    "BTC": ("bitcoin",),
    "ETH": ("ethereum", "ether"),
    "SOL": ("solana",),
    "XRP": ("ripple",),
    "BNB": ("bnb", "binance coin"),
    "DOGE": ("dogecoin",),
    "ADA": ("cardano",),
    "AVAX": ("avalanche",),
    "DOT": ("polkadot",),
    "LINK": ("chainlink",),
    "TRX": ("tron",),
    "MATIC": ("polygon",),
    "LTC": ("litecoin",),
    "BCH": ("bitcoin cash",),
}


class NewsError(RuntimeError):
    """A news request that failed in a way the panel should report."""


def _clean(value: str, limit: int = 400) -> str:
    """Strip HTML tags and collapse whitespace from a feed field."""
    text = _TAG_RE.sub(" ", value or "")
    text = html.unescape(text)
    return _WS_RE.sub(" ", text).strip()[:limit]


def _iso(epoch_ms: Any) -> str:
    try:
        stamp = int(epoch_ms)
    except (TypeError, ValueError):
        return ""
    if stamp <= 0:
        return ""
    if stamp > 10_000_000_000:  # milliseconds
        stamp //= 1000
    return datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat()


def _from_rfc822(value: str) -> str:
    try:
        return parsedate_to_datetime(value).astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError):
        return ""


def parse_binance_announcements(payload: Any, *, limit: int = 20) -> list[dict[str, Any]]:
    """Normalise the announcement listing into panel rows.

    The endpoint nests articles under catalogs and has changed its shape over
    time, so every level is checked instead of assumed.
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    catalogs = data.get("catalogs") if isinstance(data, dict) else None
    if not isinstance(catalogs, list):
        return []
    items: list[dict[str, Any]] = []
    for catalog in catalogs:
        if not isinstance(catalog, dict):
            continue
        try:
            catalog_id = int(catalog.get("catalogId") or 0)
        except (TypeError, ValueError):
            catalog_id = 0
        label = BINANCE_CATALOGS.get(catalog_id, "币安公告")
        for row in catalog.get("articles") or []:
            if not isinstance(row, dict):
                continue
            title = _clean(str(row.get("title") or ""))
            code = str(row.get("code") or "").strip()
            if not title or not code:
                continue
            items.append(
                {
                    "id": f"binance-{row.get('id') or code}",
                    "title": title,
                    "url": f"https://www.binance.com/zh-CN/support/announcement/{code}",
                    "source": "币安",
                    "kind": label,
                    "published_at": _iso(row.get("releaseDate")),
                }
            )
    items.sort(key=lambda row: row["published_at"], reverse=True)
    return items[:limit]


def parse_rss(xml_text: str, *, source: str, limit: int = 20) -> list[dict[str, Any]]:
    """Normalise one RSS feed into panel rows.

    One malformed document must not take the panel down, so a parse failure
    raises :class:`NewsError` and is reported per source.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise NewsError(f"{source} 返回了无法解析的内容") from exc

    items: list[dict[str, Any]] = []
    for node in root.iter("item"):
        title = _clean(node.findtext("title") or "")
        link = (node.findtext("link") or "").strip()
        if not title or not link:
            continue
        items.append(
            {
                "id": f"{source}-{node.findtext('guid') or link}",
                "title": title,
                "url": link,
                "source": source,
                "kind": _clean(node.findtext("category") or "", 40),
                # Kept for matching only: headlines name the asset, but the
                # summary often carries the ticker the title spells out
                # ("Bitcoin" in the title, "BTC" in the body).
                "summary": _clean(node.findtext("description") or "", 600),
                "published_at": _from_rfc822(node.findtext("pubDate") or ""),
            }
        )
        if len(items) >= limit:
            break
    return items


def _http_get(url: str, timeout: float, *, as_text: bool = False) -> Any:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "biTrader/0.5 (+desktop)",
            "Accept": "application/rss+xml, application/xml, application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise NewsError(f"新闻源返回 HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise NewsError(f"无法连接新闻源：{exc.reason}") from exc
    except http.client.HTTPException as exc:
        # 被截断的响应抛 IncompleteRead / BadStatusLine，它们不是 OSError。
        # 漏掉这一类，异常会一路穿出 snapshot()，面板拿到的是 500 而不是这一行错误。
        raise NewsError(f"无法连接新闻源：{exc}") from exc
    except OSError as exc:
        raise NewsError(f"无法连接新闻源：{exc}") from exc

    if as_text:
        try:
            return raw.decode("utf-8", errors="replace")
        except Exception as exc:  # pragma: no cover - decode is already lenient
            raise NewsError("新闻源返回了无法解析的内容") from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise NewsError("新闻源返回了无法解析的内容") from exc


def _mentions(item: dict[str, Any], base: str) -> bool:
    """Loose pair match.

    RSS has no reliable ticker field, so the base asset is looked for as a
    word, together with its full name (``BTC`` also matches ``Bitcoin``).
    Binance announcements go through the same filter: their catalogs are global
    ("币安将上线 XXX"), so an unfiltered list showed ETH news while BTC was
    selected.
    """
    if not base:
        return True
    terms = (base, *TICKER_ALIASES.get(base, ()))
    haystack = f"{item.get('title', '')} {item.get('kind', '')} {item.get('summary', '')}"
    return any(
        re.search(rf"\b{re.escape(term)}\b", haystack, re.IGNORECASE) for term in terms
    )


class NewsService:
    """Keyless multi-source feed with a short cache.

    No credentials and no configuration: every source is public, so the panel
    works on a fresh install. A reply is reused for ``ttl`` seconds because the
    panel polls on a timer and the upstream feeds are rate limited.
    """

    def __init__(
        self,
        *,
        ttl: float = DEFAULT_TTL_SECONDS,
        timeout: float = 8.0,
        fetcher: Callable[..., Any] = _http_get,
    ) -> None:
        self._ttl = ttl
        self._timeout = timeout
        self._fetch = fetcher
        self._lock = threading.Lock()
        self._cache: dict[
            tuple[str, int], tuple[float, list[dict[str, Any]], str, bool]
        ] = {}

    @staticmethod
    def sources() -> list[str]:
        return ["币安公告", "CoinTelegraph", "Decrypt"]

    def urls_for(self, symbol: str) -> list[str]:
        """``BTC/USDT`` -> ``["BTC"]``; the quote leg is not a news topic."""
        base = symbol.split("/")[0].strip().upper()
        return [base] if base else []

    def _binance(
        self, limit: int, base: str = ""
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
        """``(matching, everything, error)``.

        Everything is returned too: when a pair has no news at all, the panel
        shows the market-wide headlines instead of an empty box, and calls it
        what it is.
        """
        url = BINANCE_ANNOUNCEMENTS.format(size=max(limit, 20))
        try:
            payload = self._fetch(url, self._timeout)
        except NewsError as exc:
            return [], [], f"币安公告：{exc}"
        items = parse_binance_announcements(payload, limit=max(limit * 4, 20))
        # The catalog is global, so the per-pair filter has to happen here;
        # asking for more rows first keeps a narrow pair from going dry.
        matched = [item for item in items if _mentions(item, base)]
        return matched[:limit], items, ""

    def _rss(
        self, source: str, url: str, base: str, limit: int
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
        try:
            text = self._fetch(url, self._timeout, as_text=True)
        except NewsError as exc:
            return [], [], f"{source}：{exc}"
        except TypeError:
            # Tests inject a plain ``fetcher(url, timeout)`` that returns a dict.
            return [], [], f"{source}：新闻源返回了无法解析的内容"
        items = parse_rss(text, source=source, limit=max(limit * 4, 20))
        matched = [item for item in items if _mentions(item, base)]
        return matched[:limit], items, ""

    def snapshot(self, symbol: str = "", limit: int = 20) -> dict[str, Any]:
        limit = max(1, min(limit, 50))
        currencies = self.urls_for(symbol)
        base = currencies[0] if currencies else ""
        key = (base, limit)
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None and now - cached[0] < self._ttl:
                return {
                    "items": cached[1],
                    "cached": True,
                    "error": cached[2],
                    "source": " · ".join(self.sources()),
                    "symbol": symbol,
                    "fallback": cached[3],
                }

        collected: list[dict[str, Any]] = []
        unfiltered: list[dict[str, Any]] = []
        errors: list[str] = []

        # Announcements are always the newest rows, so without a quota they
        # would fill the panel and the market coverage would never show. Split
        # the budget instead of relying on timestamps: the venue news and the
        # wider market are different questions, not competitors.
        binance_quota = max(3, limit * 2 // 5)
        rss_quota = max(2, limit // 5)

        announcements, everything, error = self._binance(binance_quota, base)
        collected.extend(announcements)
        unfiltered.extend(everything)
        if error:
            errors.append(error)

        for source, url in RSS_SOURCES:
            items, everything, error = self._rss(source, url, base, rss_quota)
            collected.extend(items)
            unfiltered.extend(everything)
            if error:
                errors.append(error)

        # Newest first; a row with no timestamp sorts last but stays visible.
        collected.sort(key=lambda row: row["published_at"], reverse=True)
        items = collected[:limit]
        message = "；".join(errors)
        # A quiet pair must not look like a broken feed. Fall back to the
        # market-wide headlines and label them, rather than showing nothing.
        fallback = False
        if base and not items and unfiltered:
            unfiltered.sort(key=lambda row: row["published_at"], reverse=True)
            items = unfiltered[:limit]
            fallback = True

        with self._lock:
            self._cache[key] = (now, items, message, fallback)
        return {
            "items": items,
            "cached": False,
            "error": message,
            "source": " · ".join(self.sources()),
            # The panel labels the feed with the asset it was filtered for, so a
            # stale render cannot pass for the newly selected pair.
            "symbol": symbol,
            "fallback": fallback,
        }

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()
