"""The command-line pusher, for work that happens outside the app.

The point of this module is that a script or an agent can report "done" or
"broke" to the same WeChat channel without importing the whole application, so
what matters is: the settings it sends with, the exit code, and that a failed
send is reported rather than swallowed.
"""

from __future__ import annotations

import pytest

from btrader import notify_cli
from btrader.notify import NotifyError, NotifySettings, WeChatNotifier


class _Settings:
    def __init__(self, notify: NotifySettings) -> None:
        self.notify = notify


class _Store:
    """``ConfigStore()`` is read as ``.settings.notify``."""

    def __init__(self, notify: NotifySettings) -> None:
        self.settings = _Settings(notify)


@pytest.fixture
def sent(monkeypatch):
    """Capture the text and exit status without touching the real gateway."""
    seen: dict = {}

    class FakeNotifier:
        # ``main`` formats through the class it imported, so the fake has to
        # carry the real formatter rather than reimplement its layout.
        format_task = staticmethod(WeChatNotifier.format_task)

        def __init__(self, settings, **kwargs) -> None:
            seen["settings"] = settings
            seen["timeout"] = kwargs.get("timeout")

        def send_now(self, text: str) -> dict:
            seen["text"] = text
            if seen.get("fail"):
                raise NotifyError("网关没响应")
            return {"ok": True}

    monkeypatch.setattr(notify_cli, "WeChatNotifier", FakeNotifier)
    monkeypatch.setattr(
        notify_cli,
        "ConfigStore",
        lambda: _Store(NotifySettings(enabled=True, target="wxid-1")),
    )
    return seen


def test_main_sends_the_message_and_reports_success(sent, capsys) -> None:
    assert notify_cli.main(["--title", "任务完成", "回测跑完了"]) == 0
    assert "【任务完成】" in sent["text"]
    assert "回测跑完了" in sent["text"]
    assert "已发送" in capsys.readouterr().out


def test_main_without_a_title_sends_the_plain_text(sent) -> None:
    assert notify_cli.main(["部署失败"]) == 0
    assert sent["text"] == "部署失败"


def test_extra_lines_become_their_own_rows(sent) -> None:
    assert notify_cli.main(["--title", "优化完成", "--line", "胜率 54%"]) == 0
    assert sent["text"].splitlines() == ["【优化完成】", "胜率 54%"]


def test_usage_error_exits_2_without_sending(sent, capsys) -> None:
    assert notify_cli.main([]) == 2
    assert "text" not in sent
    assert "需要一条消息" in capsys.readouterr().err


def test_a_failed_send_exits_1(sent, capsys) -> None:
    sent["fail"] = True
    assert notify_cli.main(["回测完成"]) == 1
    assert "网关没响应" in capsys.readouterr().err


def test_a_failed_send_is_spooled_for_a_later_replay(sent, capsys, monkeypatch) -> None:
    """The CLI exits right after this call, so an unsent report must be kept.

    The app's worker replays the spool once the channel recovers — which is how
    a report sent while WeChat was asking for a new QR still arrives afterwards.
    """
    spooled: list[str] = []
    monkeypatch.setattr(
        notify_cli, "spool_message", lambda text: spooled.append(text) or 1
    )
    sent["fail"] = True

    assert notify_cli.main(["--title", "任务完成", "回测跑完了"]) == 1

    assert len(spooled) == 1
    assert "【任务完成】" in spooled[0]
    assert "已暂存" in capsys.readouterr().err
