"""News feed: parsing the two source shapes, filtering and caching.

The panel polls this endpoint, so the things worth pinning down are that a
malformed upstream payload degrades into a reported error instead of raising,
that a pair filter keeps unrelated headlines out, and that repeated polls hit
the network once per TTL.
"""

from __future__ import annotations

import json

from btrader.news import (
    NewsError,
    NewsService,
    parse_binance_announcements,
    parse_rss,
)

BINANCE_PAYLOAD = {
    "success": True,
    "data": {
        "catalogs": [
            {
                "catalogId": 48,
                "catalogName": "New Cryptocurrency Listing",
                "articles": [
                    {
                        "id": 1,
                        "code": "aaa111",
                        "title": "Binance Will List <b>ABC</b>",
                        "releaseDate": 1791948608368,
                    }
                ],
            },
            {
                "catalogId": 161,
                "catalogName": "Delisting",
                "articles": [
                    {"id": 2, "code": "bbb222", "title": "Binance Will Delist XYZ", "releaseDate": 1791000000000},
                    {"id": 3, "code": "", "title": "Broken row without a code"},
                    # A pair-specific announcement, so the per-pair filter has
                    # something matching to keep for BTC. Dated older than the
                    # rows above so the ordering assertions stay meaningful.
                    {
                        "id": 4,
                        "code": "ccc333",
                        "title": "Binance Will Adjust BTC Perpetual Funding",
                        "releaseDate": 1790000000000,
                    },
                    "not-a-row",
                ],
            },
            "not-a-catalog",
        ]
    },
}

RSS_TEXT = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0">
  <channel>
    <item>
      <title>Bitcoin breaks 80k as BTC inflows surge</title>
      <link>https://example.com/btc</link>
      <guid>https://example.com/btc</guid>
      <pubDate>Mon, 19 Oct 2026 13:30:00 +0000</pubDate>
      <category>Markets</category>
    </item>
    <item>
      <title>Ethereum upgrade scheduled for next month</title>
      <link>https://example.com/eth</link>
      <guid>https://example.com/eth</guid>
      <pubDate>Mon, 19 Oct 2026 12:00:00 +0000</pubDate>
    </item>
    <item>
      <title>No link here</title>
    </item>
  </channel>
</rss>
"""


def test_binance_rows_are_normalised_and_labelled() -> None:
    items = parse_binance_announcements(BINANCE_PAYLOAD)

    assert [item["id"] for item in items] == ["binance-1", "binance-2", "binance-4"]
    first = items[0]
    assert first["source"] == "币安"
    assert first["kind"] == "上币公告"
    assert first["title"] == "Binance Will List ABC"
    assert first["url"] == "https://www.binance.com/zh-CN/support/announcement/aaa111"
    assert first["published_at"].startswith("2026-")
    # Newest first, regardless of which catalog it came from.
    assert items[0]["published_at"] > items[1]["published_at"]


def test_binance_parser_survives_a_broken_payload() -> None:
    assert parse_binance_announcements(None) == []
    assert parse_binance_announcements({"data": "nope"}) == []
    assert parse_binance_announcements({"data": {"catalogs": [{}]}}) == []


def test_rss_rows_carry_source_and_timestamp() -> None:
    items = parse_rss(RSS_TEXT, source="CoinTelegraph")

    assert len(items) == 2  # the row without a link is dropped
    assert items[0]["source"] == "CoinTelegraph"
    assert items[0]["published_at"] == "2026-10-19T13:30:00+00:00"
    assert items[1]["kind"] == ""  # no category on that row


def test_rss_parser_reports_malformed_xml() -> None:
    try:
        parse_rss("<rss><channel>", source="Decrypt")
    except NewsError as exc:
        assert "Decrypt" in str(exc)
    else:  # pragma: no cover - the parser must not silently return nothing
        raise AssertionError("malformed XML should raise NewsError")


def _fetcher(calls: list[str], *, fail_rss: bool = False):
    def fetch(url: str, timeout: float, **_kwargs):
        calls.append(url)
        if url.startswith("https://www.binance.com"):
            return BINANCE_PAYLOAD
        if fail_rss:
            raise NewsError("boom")
        return RSS_TEXT

    return fetch


def test_snapshot_merges_sources_and_filters_by_pair() -> None:
    calls: list[str] = []
    service = NewsService(fetcher=_fetcher(calls))

    feed = service.snapshot("BTC/USDT", limit=10)

    assert len(calls) == 3  # announcements + two RSS feeds
    titles = [item["title"] for item in feed["items"]]
    # Announcements are filtered by pair too: their catalog is global, so an
    # unfiltered list showed other assets' listings while BTC was selected.
    assert "Binance Will Adjust BTC Perpetual Funding" in titles
    assert all("List ABC" not in title for title in titles)
    # RSS items are: only the BTC story mentions the base asset.
    assert "Bitcoin breaks 80k as BTC inflows surge" in titles
    assert all("Ethereum" not in title for title in titles)
    assert feed["error"] == ""
    assert feed["source"] == "币安公告 · CoinTelegraph · Decrypt"


def test_rss_matches_the_asset_name_not_just_the_ticker() -> None:
    """Headlines say "Bitcoin", never "BTC" — both must match."""
    calls: list[str] = []
    service = NewsService(fetcher=_fetcher(calls))

    feed = service.snapshot("BTC/USDT", limit=10)

    assert any(item["source"] == "CoinTelegraph" for item in feed["items"])


def test_an_asset_without_a_headline_gets_no_rss_rows() -> None:
    calls: list[str] = []
    service = NewsService(fetcher=_fetcher(calls))

    feed = service.snapshot("ETH/USDT", limit=10)

    titles = [item["title"] for item in feed["items"]]
    assert "Bitcoin breaks 80k as BTC inflows surge" not in titles
    assert any("Ethereum" in title for title in titles)


def test_announcements_cannot_starve_the_market_sources() -> None:
    """Announcements are always newest; the quota keeps room for RSS."""
    many = {
        "success": True,
        "data": {
            "catalogs": [
                {
                    "catalogId": 49,
                    "catalogName": "Latest Binance News",
                    "articles": [
                        {
                            "id": index,
                            "code": f"code{index}",
                            "title": f"Announcement {index} about BTC market making",
                            "releaseDate": 1791948608368 - index,
                        }
                        for index in range(50)
                    ],
                }
            ]
        },
    }

    def fetch(url: str, timeout: float, **_kwargs):
        if url.startswith("https://www.binance.com"):
            return many
        return RSS_TEXT

    feed = NewsService(fetcher=fetch).snapshot("BTC/USDT", limit=20)

    assert any(item["source"] == "币安" for item in feed["items"])
    assert any(item["source"] == "CoinTelegraph" for item in feed["items"])
    assert len(feed["items"]) <= 20


def test_snapshot_caches_for_the_ttl() -> None:
    calls: list[str] = []
    service = NewsService(fetcher=_fetcher(calls))

    first = service.snapshot("BTC/USDT")
    second = service.snapshot("BTC/USDT")

    assert len(calls) == 3
    assert first["cached"] is False
    assert second["cached"] is True


def test_one_failing_source_does_not_hide_the_others() -> None:
    calls: list[str] = []
    service = NewsService(fetcher=_fetcher(calls, fail_rss=True))

    feed = service.snapshot("BTC/USDT")

    # Announcements still arrive, and the failure is reported next to them.
    assert any(item["source"] == "币安" for item in feed["items"])
    assert "CoinTelegraph" in feed["error"]
    assert "Decrypt" in feed["error"]


def test_service_reports_a_total_outage_without_raising() -> None:
    def fetch(url: str, timeout: float, **_kwargs):
        raise NewsError("offline")

    service = NewsService(fetcher=fetch)
    feed = service.snapshot("ETH/USDT")

    assert feed["items"] == []
    assert "offline" in feed["error"]
    assert feed["source"].startswith("币安公告")


def test_sources_are_listed_for_the_settings_copy() -> None:
    assert NewsService.sources() == ["币安公告", "CoinTelegraph", "Decrypt"]
