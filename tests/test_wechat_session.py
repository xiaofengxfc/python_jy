"""The WeChat bot login expires on its own — the app must notice and say so.

``ilink/bot/getupdates`` answers ``{"errcode": -14, "errmsg": "session timeout"}``
once the token is dead, and only a fresh QR scan revives it. These tests pin the
probe that turns that into "重新扫码" in the UI, and the notifier's caching of it.
"""

from __future__ import annotations

import json

import pytest

from btrader import installer
from btrader.notify import NotifySettings, WeChatNotifier

# Captured at import time, before the autouse fixture stubs the module
# attribute: these tests are *about* the probe itself.
real_session_state = installer.wechat_session_state


class _Response:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *exc) -> None:
        return None


@pytest.fixture
def account(tmp_path, monkeypatch):
    """Point the plugin state at a fake account with a token."""
    folder = tmp_path / "openclaw-weixin" / "accounts"
    folder.mkdir(parents=True)
    (folder / "acct-im-bot.json").write_text(
        json.dumps(
            {
                "token": "acct@im.bot:secret",
                "userId": "peer@im.wechat",
                "baseUrl": "https://ilinkai.weixin.qq.com",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(installer, "_state_dir", lambda: tmp_path)
    return "acct-im-bot"


def test_an_expired_login_is_reported_as_expired(account, monkeypatch):
    monkeypatch.setattr(
        installer.urllib.request,
        "urlopen",
        lambda *a, **k: _Response({"errcode": -14, "errmsg": "session timeout"}),
    )

    state = real_session_state(account)

    assert state["state"] == "expired"
    assert "session timeout" in state["detail"]
    assert state["account"] == account


def test_a_live_login_is_ok(account, monkeypatch):
    monkeypatch.setattr(
        installer.urllib.request, "urlopen", lambda *a, **k: _Response({"errcode": 0})
    )

    assert real_session_state(account)["state"] == "ok"


def test_a_network_failure_never_asks_for_a_rescan(account, monkeypatch):
    """A flaky connection must not be mistaken for a dead token."""

    def boom(*_args, **_kwargs):
        raise OSError("network down")

    monkeypatch.setattr(installer.urllib.request, "urlopen", boom)

    state = real_session_state(account)

    assert state["state"] == "unknown"
    assert "network down" in state["detail"]


def test_an_unknown_error_code_is_not_called_expired(account, monkeypatch):
    monkeypatch.setattr(
        installer.urllib.request,
        "urlopen",
        lambda *a, **k: _Response({"errcode": -1, "errmsg": "busy"}),
    )

    assert real_session_state(account)["state"] == "unknown"


def test_no_account_is_reported_separately(tmp_path, monkeypatch):
    monkeypatch.setattr(installer, "_state_dir", lambda: tmp_path)

    assert real_session_state()["state"] == "missing"


def test_status_carries_the_session_verdict_and_caches_it(monkeypatch):
    """Opening the page must not hit WeChat's API on every poll."""
    calls: list[int] = []

    def probe(account: str = "") -> dict:
        calls.append(1)
        return {"state": "expired", "detail": "需要重新扫码", "account": "a"}

    monkeypatch.setattr(installer, "wechat_session_state", probe)
    notifier = WeChatNotifier(NotifySettings(enabled=True))

    first = notifier.status()
    second = notifier.status()

    assert first["session"]["state"] == "expired"
    assert second["session"]["state"] == "expired"
    assert len(calls) == 1


def test_the_probe_is_skipped_while_pushes_are_disabled():
    """A disabled channel must not do network I/O from a status read."""
    notifier = WeChatNotifier(NotifySettings(enabled=False))

    assert notifier.session_state()["state"] == "unknown"


def test_the_session_verdict_is_cached_for_a_minute(monkeypatch):
    probes: list[int] = []

    def slow_probe(account: str = "") -> dict:
        probes.append(1)
        return {"state": "ok", "detail": "", "account": "a"}

    monkeypatch.setattr(installer, "wechat_session_state", slow_probe)
    notifier = WeChatNotifier(NotifySettings(enabled=True))

    notifier.session_state()
    notifier.session_state()
    assert len(probes) == 1

    notifier.session_state(refresh=True)
    assert len(probes) == 2
