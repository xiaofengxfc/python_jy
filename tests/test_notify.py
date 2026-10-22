"""企业微信通知：一条 Webhook 发出去。

通知站在下单路径上，所以这里最要紧的是：任何失败都不能把一次成交变成报错。
其次是请求体本身——地址决定格式，body 发错就等于把功能发丢了。
"""

from __future__ import annotations

import threading
import time

import pytest

from btrader import notify
from btrader.notify import Notifier, NotifyError, NotifySettings

QYAPI = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abcd1234efgh5678"


@pytest.fixture
def posted(monkeypatch) -> dict:
    """记下发出去的请求，不真的联网。"""
    seen: dict = {"calls": []}

    def fake(url, body, *, headers=None, timeout=None):
        seen["calls"].append(
            {"url": url, "body": body, "headers": headers or {}, "timeout": timeout}
        )
        seen["body"] = body
        return {"errcode": 0, "errmsg": "ok"}

    monkeypatch.setattr(notify, "_post_json", fake)
    return seen


def _notifier(**overrides) -> Notifier:
    values = {"enabled": True, "webhook_url": QYAPI, **overrides}
    return Notifier(NotifySettings(**values), timeout=1.0)


def _wait_idle(notifier: Notifier, timeout: float = 3.0) -> None:
    """等后台线程把队列发完。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if notifier.status()["pending"] == 0:
            return
        time.sleep(0.02)


# ------------------------------------------------------------------ 开关与队列
def test_disabled_notifier_queues_nothing() -> None:
    notifier = Notifier(NotifySettings(enabled=False, webhook_url=QYAPI))
    assert notifier.notify("hello") is False
    assert notifier.status()["pending"] == 0


def test_default_timeout_is_short_enough_to_not_stack_up() -> None:
    """十秒还没回来就是网络有问题，继续等只会让队列变长。"""
    assert Notifier(NotifySettings()).timeout == 10.0


def test_event_switch_off_drops_that_kind_only() -> None:
    notifier = _notifier(on_task=False)
    assert notifier.notify("回测完成", kind="task") is False
    assert notifier.notify("成交", kind="fill") is True
    _wait_idle(notifier)


def test_unknown_kind_is_sent() -> None:
    notifier = _notifier()
    assert notifier.notify("随便一条") is True
    _wait_idle(notifier)


def test_a_full_queue_drops_and_counts(monkeypatch) -> None:
    """通道堵住时丢消息并计数，而不是让调用方等着。"""
    release = threading.Event()

    def slow(url, body, *, headers=None, timeout=None):
        release.wait(2.0)
        return {"errcode": 0}

    monkeypatch.setattr(notify, "_post_json", slow)
    notifier = Notifier(NotifySettings(enabled=True, webhook_url=QYAPI), queue_size=1)
    try:
        accepted = [notifier.notify(f"m{i}") for i in range(20)]
        assert False in accepted
        assert notifier.status()["dropped"] > 0
    finally:
        release.set()


def test_notify_never_raises_when_the_send_explodes(monkeypatch) -> None:
    def boom(url, body, **kwargs):
        raise RuntimeError("网络没了")

    monkeypatch.setattr(notify, "_post_json", boom)
    notifier = _notifier()
    assert notifier.notify("成交") is True
    deadline = time.time() + 3.0
    while time.time() < deadline and not notifier.last_error:
        time.sleep(0.02)
    assert "网络没了" in notifier.last_error


# ------------------------------------------------------------------- 请求体
def test_group_robot_address_gets_its_own_format(posted) -> None:
    notifier = _notifier()
    notifier.send_now("【实盘成交】BTC/USDT")
    body = posted["body"]
    assert body["msgtype"] == "markdown"
    assert "BTC/USDT" in body["markdown"]["content"]


def test_plain_text_format_sends_text(posted) -> None:
    notifier = _notifier(msg_format="qyapi_text")
    notifier.send_now("干活了")
    assert posted["body"]["msgtype"] == "text"
    assert posted["body"]["text"]["content"] == "干活了"


def test_any_other_address_gets_title_and_content(posted) -> None:
    notifier = _notifier(webhook_url="https://example.com/hook")
    notifier.send_now("正文", title="标题")
    assert posted["body"] == {"title": "标题", "content": "正文"}


def test_custom_template_fills_the_placeholders(posted) -> None:
    notifier = _notifier(
        msg_format="custom",
        webhook_template='{"text": {"content": "{title}\\n{content}"}}',
    )
    notifier.send_now("正文", title="标题")
    assert posted["body"] == {"text": {"content": "标题\n正文"}}


def test_a_broken_template_says_so(posted) -> None:
    notifier = _notifier(msg_format="custom", webhook_template="{不是 JSON}")
    with pytest.raises(NotifyError, match="模板"):
        notifier.send_now("正文")


def test_headers_are_parsed_from_text(posted) -> None:
    notifier = _notifier(webhook_headers="Authorization: Bearer abc\nX-Trace: 1")
    notifier.send_now("正文")
    assert posted["calls"][-1]["headers"]["Authorization"] == "Bearer abc"
    assert posted["calls"][-1]["headers"]["X-Trace"] == "1"


# ------------------------------------------------------------------- 失败原因
def test_missing_url_is_explained_in_chinese() -> None:
    notifier = Notifier(NotifySettings(enabled=True, webhook_url=""), timeout=1.0)
    with pytest.raises(NotifyError, match="还没填 Webhook 地址"):
        notifier.send_now("正文")


def test_empty_message_is_rejected() -> None:
    with pytest.raises(NotifyError, match="空的"):
        _notifier().send_now("   ")


def test_rate_limit_error_code_is_translated(monkeypatch) -> None:
    monkeypatch.setattr(
        notify, "_post_json", lambda *a, **k: {"errcode": 45009, "errmsg": "api freq"}
    )
    with pytest.raises(NotifyError, match="频率限制"):
        _notifier().send_now("正文")


def test_a_bad_key_is_named(monkeypatch) -> None:
    monkeypatch.setattr(notify, "_post_json", lambda *a, **k: {"errcode": 93000})
    with pytest.raises(NotifyError, match="key"):
        _notifier().send_now("正文")


def test_a_successful_send_clears_the_last_error(posted) -> None:
    notifier = _notifier()
    notifier.last_error = "上次失败了"
    notifier.send_now("正文")
    assert notifier.last_error == ""
    assert notifier.last_success_at > 0


# --------------------------------------------------------------------- 地址处理
def test_url_must_be_http() -> None:
    with pytest.raises(NotifyError, match="http"):
        notify.check_webhook_url("qyapi.weixin.qq.com/send")


def test_url_without_a_host_is_rejected() -> None:
    with pytest.raises(NotifyError, match="域名"):
        notify.check_webhook_url("https://")


def test_mask_hides_the_key_but_keeps_the_tail() -> None:
    masked = notify.mask_url(QYAPI)
    assert "abcd1234efgh5678" not in masked
    assert masked.endswith("5678")
    assert "qyapi.weixin.qq.com" in masked


def test_long_messages_are_clamped() -> None:
    text = notify._prepare("字" * (notify.MAX_MESSAGE_CHARS + 50))
    assert len(text) == notify.MAX_MESSAGE_CHARS
    assert text.endswith("…")


# --------------------------------------------------------------------- 文案格式
def test_fill_on_the_testnet_says_testnet() -> None:
    text = Notifier.format_fill(
        symbol="BTC/USDT", side="buy", amount=0.01, price=67000.5, mode="testnet", market="usdm"
    )
    assert "测试网成交" in text
    assert "买入" in text


def test_fill_on_the_live_venue_says_live() -> None:
    text = Notifier.format_fill(symbol="BTC/USDT", side="sell", amount=1, price=2, mode="mainnet")
    assert "实盘成交" in text
    assert "卖出" in text


def test_an_unknown_side_does_not_invent_a_direction() -> None:
    text = Notifier.format_fill(symbol="BTC/USDT", side="", amount=1, price=2, mode="mainnet")
    assert "成交" in text
    assert "卖出" not in text


def test_halt_message_names_the_reason_and_the_day() -> None:
    text = Notifier.format_halt(reason="连续亏损", day_pnl=-12.5)
    assert "连续亏损" in text
    assert "-12.5000" in text
    assert "平仓与撤单不受影响" in text


def test_task_message_keeps_its_title_line() -> None:
    text = Notifier.format_task("回测完成", ["BTC/USDT 15m", "收益 +3.2%"])
    assert text.startswith("【回测完成】")
    assert "收益 +3.2%" in text


def test_warning_message_can_be_a_single_line() -> None:
    assert Notifier.format_warning(title="余额不足") == "【风险提示】余额不足"


def test_price_precision_follows_magnitude() -> None:
    assert notify._format_price(67000.5) == "67,000.50"
    assert notify._format_price(0.00001234) == "0.00001234"


def test_amount_has_no_scientific_notation() -> None:
    assert notify._format_amount(0.0000001) == "0.0000001"


def test_status_exposes_what_the_page_shows(posted) -> None:
    notifier = _notifier()
    state = notifier.status()
    assert state["available"] is True
    assert state["format"] == "auto"
    assert state["url"].endswith("5678")
    assert state["spooled"] == 0

