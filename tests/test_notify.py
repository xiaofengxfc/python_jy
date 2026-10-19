"""WeChat push through the local OpenClaw gateway.

The notifier sits on the order path, so what matters here is that it can never
turn a filled order into an error: every failure mode (disabled, no target, no
CLI, a hanging or failing gateway) must be reported and swallowed. The CLI
arguments are pinned too, because ``openclaw message send`` is the contract with
the WeChat plugin.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from btrader.notify import NotifyError, NotifySettings, WeChatNotifier


class FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: str = "{}", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def captured(monkeypatch) -> dict:
    """Record the argv the notifier would execute, without running anything."""
    seen: dict = {}

    def fake_run(args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        return FakeCompleted()

    monkeypatch.setattr(subprocess, "run", fake_run)
    return seen


def _notifier(**overrides) -> WeChatNotifier:
    settings = NotifySettings(enabled=True, target="wxid-123", **overrides)
    return WeChatNotifier(settings, timeout=1.0)


def test_disabled_notifier_queues_nothing() -> None:
    notifier = WeChatNotifier(NotifySettings(enabled=False, target="wxid-123"))
    assert notifier.notify("hello") is False
    assert notifier.status()["pending"] == 0


def test_events_can_be_switched_off_individually() -> None:
    notifier = _notifier(on_fill=False)
    try:
        assert notifier.notify("filled", kind="fill") is False
        assert notifier.notify("halted", kind="risk") is True
    finally:
        # Without this the worker outlives the test and its send lands in the
        # next test's ``captured`` dict, which reads as a wrong-message bug.
        notifier.stop()


def test_empty_message_is_ignored() -> None:
    assert _notifier().notify("   ") is False


def test_send_now_requires_a_target() -> None:
    notifier = WeChatNotifier(NotifySettings(enabled=True, target=""))
    with pytest.raises(NotifyError, match="接收目标"):
        notifier.send_now("hello")


def test_send_now_reports_a_missing_cli(monkeypatch) -> None:
    monkeypatch.setattr("btrader.notify.shutil.which", lambda name: None)
    notifier = WeChatNotifier(
        NotifySettings(enabled=True, target="wxid-123", command="definitely-not-installed")
    )
    with pytest.raises(NotifyError, match="未找到"):
        notifier.send_now("hello")


def test_send_now_builds_the_openclaw_message_command(captured) -> None:
    notifier = WeChatNotifier(
        NotifySettings(enabled=True, target="wxid-123", command="openclaw")
    )
    # Resolve the command explicitly so the test does not depend on PATH.
    notifier._resolve_command = lambda: "openclaw"  # type: ignore[method-assign]
    result = notifier.send_now("成交了")
    assert result["ok"] is True
    args = captured["args"]
    assert args[:3] == ["openclaw", "message", "send"]
    assert "--channel" in args and args[args.index("--channel") + 1] == "openclaw-weixin"
    assert args[args.index("--target") + 1] == "wxid-123"
    assert args[args.index("--message") + 1] == "成交了"
    # The worker must never block the caller waiting on a shell.
    assert captured["kwargs"]["shell"] is False


def test_windows_cmd_shim_is_launched_through_comspec(monkeypatch) -> None:
    monkeypatch.setattr("btrader.notify.os.name", "nt")
    monkeypatch.setenv("COMSPEC", r"C:\Windows\System32\cmd.exe")
    argv = WeChatNotifier._wrap_launcher([r"C:\npm\openclaw.cmd", "message"])
    assert argv[:3] == [r"C:\Windows\System32\cmd.exe", "/c", r"C:\npm\openclaw.cmd"]


def test_plain_executable_is_not_wrapped(monkeypatch) -> None:
    monkeypatch.setattr("btrader.notify.os.name", "nt")
    assert WeChatNotifier._wrap_launcher(["/usr/bin/openclaw"]) == ["/usr/bin/openclaw"]


def test_non_zero_exit_becomes_a_readable_error(captured, monkeypatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda args, **kwargs: FakeCompleted(returncode=2, stderr="gateway not running"),
    )
    notifier = _notifier()
    notifier._resolve_command = lambda: "openclaw"  # type: ignore[method-assign]
    with pytest.raises(NotifyError, match="gateway not running"):
        notifier.send_now("hello")


def test_timeout_is_reported_without_hanging_the_caller(monkeypatch) -> None:
    def boom(args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args, timeout=1.0)

    monkeypatch.setattr(subprocess, "run", boom)
    notifier = _notifier()
    notifier._resolve_command = lambda: "openclaw"  # type: ignore[method-assign]
    with pytest.raises(NotifyError, match="没有响应"):
        notifier.send_now("hello")


def test_queued_message_is_sent_by_the_worker(captured, monkeypatch) -> None:
    notifier = _notifier()
    notifier._resolve_command = lambda: "openclaw"  # type: ignore[method-assign]
    try:
        assert notifier.notify("后台消息") is True
        # Poll the capture instead of sleeping a fixed time: the worker thread
        # starts on demand, and a fixed delay makes this test flaky.
        for _ in range(200):
            if "args" in captured:
                break
            time.sleep(0.01)
    finally:
        notifier.stop()
    assert "args" in captured, "后台线程没有发送排队的消息"
    assert captured["args"][captured["args"].index("--message") + 1] == "后台消息"


def test_status_reports_missing_cli(monkeypatch) -> None:
    monkeypatch.setattr("btrader.notify.shutil.which", lambda name: None)
    status = WeChatNotifier(NotifySettings(enabled=True, target="x")).status()
    assert status["available"] is False
    assert status["enabled"] is True


def test_status_finds_an_absolute_path_outside_path(monkeypatch, tmp_path: Path) -> None:
    exe = tmp_path / "openclaw"
    exe.write_text("", encoding="utf-8")
    monkeypatch.setattr("btrader.notify.shutil.which", lambda name: None)
    notifier = WeChatNotifier(
        NotifySettings(enabled=True, target="x", command=str(exe))
    )
    assert notifier.status()["available"] is True


def test_fill_format_names_the_market_and_pnl() -> None:
    text = WeChatNotifier.format_fill(
        symbol="BTC/USDT",
        side="sell",
        amount=0.5,
        price=82000.0,
        mode="paper",
        realized=12.5,
        market="usdm",
    )
    assert "纸面成交" in text and "BTC/USDT" in text
    assert "卖出" in text and "U 本位" in text
    assert "+12.5000" in text


def test_protection_format_labels_stop_loss() -> None:
    text = WeChatNotifier.format_protection(
        symbol="ETH/USDT", kind="stop_loss", price=3000.0, amount=1.0, mode="live"
    )
    assert "实盘止损触发" in text and "ETH/USDT" in text


class JsonCompleted:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_ok_false_json_is_not_reported_as_success(monkeypatch) -> None:
    """``--json`` failures can exit 0; ``ok: false`` must still surface."""
    monkeypatch.setattr(
        subprocess, "run", lambda args, **kw: JsonCompleted('{"ok": false, "error": "not logged in"}')
    )
    notifier = _notifier()
    notifier._resolve_command = lambda: "openclaw"  # type: ignore[method-assign]
    with pytest.raises(NotifyError, match="not logged in"):
        notifier.send_now("hello")


def test_json_payload_after_warnings_is_parsed(monkeypatch) -> None:
    """The CLI may print warnings before the payload line."""
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda args, **kw: JsonCompleted('warn: slow\ngateway\n{"ok": true, "messageId": "m1"}'),
    )
    notifier = _notifier()
    notifier._resolve_command = lambda: "openclaw"  # type: ignore[method-assign]
    assert notifier.send_now("hello")["ok"] is True


def test_send_now_overrides_target(monkeypatch, tmp_path: Path) -> None:
    """The test button may push at a target that is not saved yet."""
    seen: dict = {}

    def fake_run(args, **kwargs):
        seen["args"] = args
        return FakeCompleted()

    monkeypatch.setattr(subprocess, "run", fake_run)
    notifier = WeChatNotifier(
        NotifySettings(enabled=False, target="old", command="openclaw")
    )
    notifier._resolve_command = lambda: "openclaw"  # type: ignore[method-assign]
    # Disabled on purpose: testing the wiring is what justifies enabling it.
    notifier.send_now("hi")
    assert seen["args"][seen["args"].index("--target") + 1] == "old"


def test_long_message_is_clamped(captured) -> None:
    notifier = _notifier()
    notifier._resolve_command = lambda: "openclaw"  # type: ignore[method-assign]
    notifier.send_now("x" * 5000)
    sent = captured["args"][captured["args"].index("--message") + 1]
    assert len(sent) == 2000 and sent.endswith("…")


def test_failure_records_a_timestamp(monkeypatch) -> None:
    monkeypatch.setattr(
        subprocess, "run", lambda args, **kw: JsonCompleted("", returncode=1, stderr="boom")
    )
    notifier = _notifier()
    notifier._resolve_command = lambda: "openclaw"  # type: ignore[method-assign]
    with pytest.raises(NotifyError):
        notifier.send_now("hello")
    assert notifier.status()["last_error"] == "boom"
    assert notifier.status()["last_error_at"] > 0


def test_worker_sends_once_and_clears_the_error(captured) -> None:
    notifier = _notifier()
    notifier._resolve_command = lambda: "openclaw"  # type: ignore[method-assign]
    notifier.last_error = "old"
    try:
        notifier.notify("后台消息")
        for _ in range(200):
            if captured.get("args") and notifier.last_success_at:
                break
            time.sleep(0.01)
    finally:
        notifier.stop()
    assert notifier.last_error == ""
    assert notifier.status()["last_success_at"] > 0
