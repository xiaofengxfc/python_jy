"""CryptoPanic feed: parsing, token gating and caching.

The panel polls this endpoint, so the two things worth pinning down are that a
malformed upstream payload degrades instead of raising, and that a configured
token never hits the network more than once per TTL.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from btrader.core.credentials import CredentialStore
from btrader.news import NewsError, NewsService, parse_posts


PAYLOAD = {
    "results": [
        {
            "id": 101,
            "title": "Bitcoin breaks 80k",
            "url": "https://example.com/a",
            "published_at": "2026-10-19T09:00:00Z",
            "kind": "news",
            "source": {"title": "Example", "domain": "example.com"},
            "currencies": [{"code": "BTC"}],
            "votes": {"positive": 5, "negative": 1, "important": 0},
            "extra_field": "ignored",
        },
        {"id": 102, "title": "", "url": "https://example.com/b"},
        "not-a-row",
        {
            "id": 103,
            "title": "ETH upgrade scheduled",
            "url": "https://example.com/c",
            "currencies": [{"code": "eth"}, {}],
        },
    ]
}


def test_parse_posts_keeps_usable_rows_only() -> None:
    items = parse_posts(PAYLOAD)
    assert [item["id"] for item in items] == ["101", "103"]
    first = items[0]
    assert first["source"] == "Example"
    assert first["currencies"] == ["BTC"]
    assert first["positive"] == 5
    # The second usable row has no votes block and a lowercase ticker.
    assert items[1]["currencies"] == ["ETH"]
    assert items[1]["positive"] == 0


def test_parse_posts_survives_a_non_dict_payload() -> None:
    assert parse_posts(None) == []
    assert parse_posts({"results": "nope"}) == []


def test_service_without_a_token_does_not_call_the_network() -> None:
    calls: list[str] = []

    def fetch(url: str, timeout: float):
        calls.append(url)
        return PAYLOAD

    service = NewsService(lambda: "", fetcher=fetch)
    result = service.snapshot("BTC/USDT")
    assert result["configured"] is False
    assert result["items"] == []
    assert calls == []


def test_service_queries_the_base_asset_and_caches() -> None:
    calls: list[str] = []

    def fetch(url: str, timeout: float):
        calls.append(url)
        return PAYLOAD

    service = NewsService(lambda: "tok", fetcher=fetch)
    first = service.snapshot("BTC/USDT", limit=10)
    second = service.snapshot("BTC/USDT", limit=10)

    assert len(calls) == 1
    assert "currencies=BTC" in calls[0]
    assert "auth_token=tok" in calls[0]
    assert first["cached"] is False
    assert second["cached"] is True
    assert len(first["items"]) == 2


def test_upstream_failure_reports_error_and_keeps_last_good_items() -> None:
    state = {"fail": False}

    def fetch(url: str, timeout: float):
        if state["fail"]:
            raise NewsError("boom")
        return PAYLOAD

    service = NewsService(lambda: "tok", ttl=0, fetcher=fetch)
    service.snapshot("BTC/USDT")
    state["fail"] = True
    result = service.snapshot("BTC/USDT")
    assert result["error"] == "boom"
    assert result["cached"] is True
    assert len(result["items"]) == 2


def test_news_token_round_trips_through_the_keyring(tmp_path: Path) -> None:
    class FakeKeyring:
        def __init__(self) -> None:
            self.store: dict[tuple[str, str], str] = {}

        def get_password(self, service: str, name: str) -> str | None:
            return self.store.get((service, name))

        def set_password(self, service: str, name: str, value: str) -> None:
            self.store[(service, name)] = value

        def delete_password(self, service: str, name: str) -> None:
            self.store.pop((service, name), None)

    store = CredentialStore(backend=FakeKeyring())
    assert store.load_news() == ""
    assert store.save_news("abc123") is True
    assert store.load_news() == "abc123"
    store.delete_news()
    assert store.load_news() == ""
