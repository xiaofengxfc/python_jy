"""通道韧性：熔断和失败消息补发。

每条都对应一个具体的坏情况：

* 地址填错时，不能让每条消息都等满 10 秒；
* 通道连着坏，队列会被超时堆满，成交通知反而更慢；
* 通道恢复的那一刻，之前失败的那条（往往正是熔断告警）不能丢。
"""

from __future__ import annotations

import pytest

from btrader import notify
from btrader.notify import Notifier, NotifyError, NotifySettings

URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abcd1234efgh5678"


@pytest.fixture(autouse=True)
def clean_spool(tmp_path, monkeypatch):
    """暂存文件写进临时目录，别碰真实数据。"""
    monkeypatch.setattr(notify, "_spool_path", lambda: tmp_path / "notify-spool.jsonl")
    return tmp_path / "notify-spool.jsonl"


def _notifier(**overrides) -> Notifier:
    values = {"enabled": True, "webhook_url": URL, **overrides}
    return Notifier(NotifySettings(**values), timeout=1.0)


# ---------------------------------------------------------------------- 熔断
def test_three_failures_open_the_breaker() -> None:
    notifier = _notifier()
    for _ in range(notify.BREAKER_THRESHOLD):
        notifier._note_failure()
    assert notifier.status()["breaker_seconds"] > 0


def test_a_failed_send_through_the_worker_feeds_the_breaker(monkeypatch) -> None:
    def boom(url, body, **kwargs):
        raise NotifyError("连不上")

    monkeypatch.setattr(notify, "_post_json", boom)
    notifier = _notifier()
    with pytest.raises(NotifyError, match="连不上"):
        notifier.send_now("正文")
    assert notifier.last_error == "连不上"


def test_an_open_breaker_fails_fast_without_sending(monkeypatch) -> None:
    calls: list = []

    def fake(url, body, **kwargs):
        calls.append(url)
        return {"errcode": 0}

    monkeypatch.setattr(notify, "_post_json", fake)
    notifier = _notifier()
    notifier._open_until = 9999999999.0
    with pytest.raises(NotifyError, match="已暂停"):
        notifier._check_breaker()
    assert calls == []


def test_a_success_resets_the_failure_count(monkeypatch) -> None:
    monkeypatch.setattr(notify, "_post_json", lambda *a, **k: {"errcode": 0})
    notifier = _notifier()
    notifier._note_failure()
    notifier._note_success()
    assert notifier._failures == 0
    assert notifier.status()["breaker_seconds"] == 0


# ---------------------------------------------------------------------- 补发
def test_a_failed_message_is_kept_for_later(monkeypatch) -> None:
    def boom(url, body, **kwargs):
        raise NotifyError("连不上")

    monkeypatch.setattr(notify, "_post_json", boom)
    notifier = _notifier()
    notifier.notify("【实盘成交】BTC/USDT", kind="fill")
    import time

    deadline = time.time() + 3.0
    while time.time() < deadline and not notify._spool_load():
        time.sleep(0.02)
    assert [item["text"] for item in notify._spool_load()] == ["【实盘成交】BTC/USDT"]


def test_replayed_messages_are_marked_as_late(monkeypatch) -> None:
    sent: list = []

    def fake(url, body, **kwargs):
        sent.append(body)
        return {"errcode": 0}

    monkeypatch.setattr(notify, "_post_json", fake)
    notify._spool_add("旧消息")
    notifier = _notifier()
    notifier._replay_spool()
    assert notify._spool_load() == []
    assert notify.SPOOL_PREFIX in sent[-1]["markdown"]["content"]


def test_a_failed_replay_keeps_the_message_and_its_order(monkeypatch) -> None:
    notify._spool_add("第一条")
    notify._spool_add("第二条")

    def boom(url, body, **kwargs):
        raise NotifyError("还是连不上")

    monkeypatch.setattr(notify, "_post_json", boom)
    notifier = _notifier()
    notifier._replay_spool()
    assert [item["text"] for item in notify._spool_load()] == ["第一条", "第二条"]


def test_the_same_message_is_not_stored_twice() -> None:
    notify._spool_add("重复的一条")
    notify._spool_add("重复的一条")
    assert len(notify._spool_load()) == 1


def test_a_stale_message_is_dropped_instead_of_replayed(monkeypatch) -> None:
    """六小时前的成交不是新闻，补发只会更吵。"""
    import json
    import time as _time

    path = notify._spool_path()
    old = {"text": "很旧的一条", "at": _time.time() - notify.SPOOL_MAX_AGE - 60}
    path.write_text(json.dumps(old, ensure_ascii=False) + "\n", encoding="utf-8")
    assert notify._spool_load() == []


def test_a_corrupt_spool_does_not_block_a_new_send(monkeypatch) -> None:
    monkeypatch.setattr(notify, "_post_json", lambda *a, **k: {"errcode": 0})
    notify._spool_path().write_text("这不是 JSON\n", encoding="utf-8")
    assert notify._spool_load() == []
    notifier = _notifier()
    notifier.send_now("新消息")  # 不抛异常就是通过


def test_the_spool_is_capped(monkeypatch) -> None:
    monkeypatch.setattr(notify, "SPOOL_LIMIT", 5)
    for index in range(9):
        notify._spool_add(f"m{index}")
    texts = [item["text"] for item in notify._spool_load()]
    assert texts == [f"m{i}" for i in range(4, 9)]

