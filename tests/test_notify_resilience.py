"""Channel resilience: 预检、熔断、失败消息补发.

Each of these exists because of one concrete failure mode:

* a gateway that is not running turned every message into a ~30 s CLI stall;
* a wedged channel stacked those stalls, so ten messages took five minutes;
* a message that failed during a gateway restart was simply lost.
"""

from __future__ import annotations

import json
import socket
import subprocess
import time

import pytest

from btrader import notify
from btrader.notify import NotifyError, NotifySettings, WeChatNotifier


class FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: str = "{}", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _notifier(**overrides) -> WeChatNotifier:
    values = {"enabled": True, "target": "wxid-123", **overrides}
    return WeChatNotifier(NotifySettings(**values), timeout=1.0)


# ------------------------------------------------------------------ preflight
def test_preflight_reports_a_gateway_that_is_not_listening(monkeypatch) -> None:
    def refuse(address, timeout=None):
        raise ConnectionRefusedError(10061, "refused")

    monkeypatch.setattr(notify.socket, "create_connection", refuse)
    with pytest.raises(NotifyError, match="网关没有在"):
        notify._probe_gateway()


def test_preflight_is_silent_when_the_port_answers(monkeypatch) -> None:
    class Sock:
        def __enter__(self):
            return self

        def __exit__(self, *exc) -> None:
            return None

    monkeypatch.setattr(notify.socket, "create_connection", lambda *a, **k: Sock())
    notify._probe_gateway()  # no exception is the assertion


def test_preflight_can_be_switched_off(monkeypatch) -> None:
    """A gateway on a custom port would be a false negative, so it is optional."""
    calls: list = []
    monkeypatch.setattr(notify, "_probe_gateway", lambda: calls.append(1))
    notifier = _notifier(preflight=False)
    notifier.settings.preflight = False
    notifier._preflight()
    assert calls == []


def test_a_dead_gateway_does_not_spawn_the_cli(monkeypatch) -> None:
    """The whole point: fail in milliseconds, not after a Node start."""
    spawned: list = []

    def refuse(address, timeout=None):
        raise ConnectionRefusedError(10061, "refused")

    # conftest disables the probe for the whole suite, so this test re-arms it
    # explicitly rather than reading the (already patched) class attribute.
    monkeypatch.setattr(
        notify.WeChatNotifier,
        "_preflight",
        lambda self: notify._probe_gateway() if self.settings.preflight else None,
    )
    monkeypatch.setattr(notify.socket, "create_connection", refuse)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: spawned.append(a) or FakeCompleted())
    notifier = _notifier()
    with pytest.raises(NotifyError, match="网关没有在"):
        notifier.send_now("hello")
    assert spawned == []


# -------------------------------------------------------------------- breaker
def test_three_failures_open_the_breaker(monkeypatch) -> None:
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: FakeCompleted(returncode=1, stderr="boom")
    )
    notifier = _notifier()
    for _ in range(notify.BREAKER_THRESHOLD):
        with pytest.raises(NotifyError):
            notifier.send_now("hello")
    assert notifier.status()["breaker_seconds"] > 0


def test_an_open_breaker_fails_without_calling_the_cli(monkeypatch) -> None:
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: FakeCompleted(returncode=1, stderr="boom")
    )
    notifier = _notifier()
    notifier._open_until = time.time() + 120
    calls: list = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a) or FakeCompleted())
    with pytest.raises(NotifyError, match="暂停"):
        notifier._run("hello")
    assert calls == []


def test_a_manual_test_ignores_the_breaker(monkeypatch) -> None:
    """The click is how the user proves the channel recovered."""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: FakeCompleted())
    notifier = _notifier()
    notifier._open_until = time.time() + 300
    assert notifier.send_now("hello")["ok"] is True
    assert notifier.status()["breaker_seconds"] == 0


def test_success_resets_the_failure_streak(monkeypatch) -> None:
    notifier = _notifier()
    notifier._failures = notify.BREAKER_THRESHOLD - 1
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: FakeCompleted())
    notifier.send_now("hello")
    assert notifier._failures == 0
    assert notifier._open_until == 0.0


# ---------------------------------------------------------------------- spool
@pytest.fixture
def spool(tmp_path, monkeypatch):
    path = tmp_path / notify.SPOOL_FILE
    monkeypatch.setattr(notify, "_spool_path", lambda: path)
    return path


def test_failed_messages_are_kept_and_replayed_with_a_prefix(spool) -> None:
    notify._spool_add("【风控熔断】已停止开新仓")
    items = notify._spool_load()
    assert [item["text"] for item in items] == ["【风控熔断】已停止开新仓"]
    assert notify._spool_text(items[0]).splitlines() == [
        notify.SPOOL_PREFIX,
        "【风控熔断】已停止开新仓",
    ]


def test_the_same_message_is_not_spooled_twice(spool) -> None:
    notify._spool_add("same")
    notify._spool_add("same")
    assert len(notify._spool_load()) == 1


def test_stale_messages_are_dropped_instead_of_replayed(spool) -> None:
    """A fill from hours ago is not news."""
    old = time.time() - notify.SPOOL_MAX_AGE - 60
    spool.write_text(
        json.dumps({"text": "old", "at": old}) + "\n"
        + json.dumps({"text": "fresh", "at": time.time()}) + "\n",
        encoding="utf-8",
    )
    assert [item["text"] for item in notify._spool_load()] == ["fresh"]


def test_a_corrupt_spool_does_not_block_sending(spool) -> None:
    spool.write_text("{not json\n", encoding="utf-8")
    assert notify._spool_load() == []
    notify._spool_add("after corruption")
    assert len(notify._spool_load()) == 1


def test_an_empty_spool_removes_the_file(spool) -> None:
    notify._spool_add("x")
    assert spool.exists()
    notify._spool_save([])
    assert not spool.exists()


def test_replay_sends_old_messages_after_a_success(spool, monkeypatch) -> None:
    notify._spool_add("第一条")
    notify._spool_add("第二条")
    sent: list[str] = []

    def fake_run(args, **kwargs) -> FakeCompleted:
        # argv ends with --message <text> --json
        sent.append(args[args.index("--message") + 1])
        return FakeCompleted()

    monkeypatch.setattr(subprocess, "run", fake_run)
    notifier = _notifier()
    notifier._replay_spool()
    assert [text.splitlines()[-1] for text in sent] == ["第一条", "第二条"]
    assert notify._spool_load() == []


def test_replay_stops_at_the_first_failure_and_keeps_the_rest(spool, monkeypatch) -> None:
    """No half-drained spool: the unsent remainder must stay in order."""
    notify._spool_add("第一条")
    notify._spool_add("第二条")
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: FakeCompleted(returncode=1, stderr="boom")
    )
    notifier = _notifier()
    notifier._replay_spool()
    assert [item["text"] for item in notify._spool_load()] == ["第一条", "第二条"]
