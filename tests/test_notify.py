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


def test_default_timeout_outlasts_a_real_send() -> None:
    """A real ``message send`` takes ~30 s: Node start plus a gateway trip.

    The old 20 s deadline fired first on every single send, so the notifier
    reported a timeout while the message actually reached WeChat — which is
    exactly what "扫码登录成功但无法通知" looked like from the page.
    """
    notifier = WeChatNotifier(NotifySettings(enabled=True, target="wxid-123"))
    assert notifier.timeout >= 60.0


def test_empty_target_falls_back_to_the_bound_account(captured, monkeypatch) -> None:
    """A completed scan already knows where to deliver, so nothing to type.

    The plugin stores the scanning account's WeChat id; with exactly one bound
    account that id is the recipient. Requiring the user to copy it by hand was
    the reason notifications stayed silent right after a successful login.
    """
    monkeypatch.setattr(
        "btrader.notify._bound_wechat_target",
        lambda: {"target": "abc@im.wechat", "account": "acc-1", "source": "account"},
    )
    notifier = WeChatNotifier(NotifySettings(enabled=True, target=""), timeout=1.0)
    notifier.send_now("hello")
    args = captured["args"]
    assert args[args.index("--target") + 1] == "abc@im.wechat"
    # The account travels with it: a send without ``--account`` fails once the
    # gateway has more than one binding.
    assert args[args.index("--account") + 1] == "acc-1"


def test_ambiguous_bindings_still_refuse_an_empty_target(captured, monkeypatch) -> None:
    """Two accounts means no safe default; guessing could deliver elsewhere."""
    monkeypatch.setattr("btrader.notify._bound_wechat_target", lambda: None)
    notifier = WeChatNotifier(NotifySettings(enabled=True, target=""), timeout=1.0)
    with pytest.raises(NotifyError):
        notifier.send_now("hello")
    assert "args" not in captured


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


def test_send_now_requires_a_target(monkeypatch) -> None:
    # Pin "nothing is bound": without this the test would read the real
    # ``~/.openclaw`` state and pass or fail depending on the developer's own
    # WeChat binding.
    monkeypatch.setattr("btrader.notify._bound_wechat_target", lambda: None)
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


def test_account_is_only_passed_when_set(captured) -> None:
    """One bound account needs no ``--account``; two do, or the plugin refuses."""
    notifier = WeChatNotifier(
        NotifySettings(enabled=True, target="wxid-123", command="openclaw")
    )
    notifier._resolve_command = lambda: "openclaw"  # type: ignore[method-assign]
    notifier.send_now("hello")
    assert "--account" not in captured["args"]

    notifier.settings.account = "abc-im-bot"
    notifier.send_now("hello")
    assert captured["args"][captured["args"].index("--account") + 1] == "abc-im-bot"


def test_target_warning_names_the_expected_suffix() -> None:
    from btrader.notify import normalize_target, target_warning

    assert target_warning("") == ""
    warning = target_warning("wxid-123")
    assert "@im.wechat" in warning
    assert target_warning("wxid-123@im.wechat") == ""
    # A pasted ``weixin:`` prefix is stripped rather than sent as part of the id.
    assert normalize_target("weixin: wxid-9 ") == "wxid-9"



def test_windows_cmd_shim_falls_back_to_comspec(monkeypatch) -> None:
    """An unreadable shim still has to reach ``cmd``, but safely.

    The fallback quotes every argument and flattens newlines, because ``cmd``
    would otherwise end the command at the first line break.
    """
    monkeypatch.setattr("btrader.notify.os.name", "nt")
    monkeypatch.setenv("COMSPEC", r"C:\Windows\System32\cmd.exe")
    argv = WeChatNotifier._wrap_launcher([r"C:\npm\openclaw.cmd", "message", "a\nb"])
    assert argv[:5] == [
        r"C:\Windows\System32\cmd.exe",
        "/d",
        "/s",
        "/c",
        argv[4],
    ]
    # One command string, every element quoted, and no raw newline left in it.
    assert '"C:\\npm\\openclaw.cmd"' in argv[4]
    assert '"a b"' in argv[4]
    assert "\n" not in argv[4]


def test_windows_shim_runs_the_node_entry_without_a_shell(monkeypatch, tmp_path) -> None:
    """The npm shim is parsed so ``node`` runs the script directly.

    Going through ``cmd.exe`` truncated multi-line notifications at the first
    newline (and let ``&`` start a second command), which is exactly what every
    formatted message contains. Running the embedded script keeps the text byte
    for byte.
    """
    monkeypatch.setattr("btrader.notify.os.name", "nt")
    script = tmp_path / "node_modules" / "openclaw" / "openclaw.mjs"
    script.parent.mkdir(parents=True)
    script.write_text("", encoding="utf-8")
    shim = tmp_path / "openclaw.cmd"
    shim.write_text(
        "@ECHO off\r\n"
        'IF EXIST "%dp0%\\node.exe" (\r\n'
        '  SET "_prog=%dp0%\\node.exe"\r\n'
        ") ELSE (\r\n"
        '  SET "_prog=node"\r\n'
        ")\r\n"
        'endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  '
        '"%dp0%\\node_modules\\openclaw\\openclaw.mjs" %*\r\n',
        encoding="utf-8",
    )
    node = tmp_path / "node.exe"
    node.write_text("", encoding="utf-8")
    message = "【纸面成交】BTC/USDT\n买入 0.5 @ 82,000.0000\nA&B 100% | x>y"
    argv = WeChatNotifier._wrap_launcher([str(shim), "message", "send", "--message", message])
    assert argv[0] == str(node)
    assert argv[1] == str(script)
    # The text survives intact: no shell to split on newlines or operators.
    assert argv[-1] == message



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


def test_small_price_is_not_rounded_away() -> None:
    """A 0.00001234 price used to print as 0.0000 — a useless fill message."""
    text = WeChatNotifier.format_fill(
        symbol="SHIB/USDT", side="buy", amount=1_000_000, price=0.00001234, mode="paper"
    )
    assert "0.00001234" in text
    assert "0.0000 " not in text


def test_large_price_keeps_cents_without_noise() -> None:
    text = WeChatNotifier.format_fill(
        symbol="BTC/USDT", side="buy", amount=0.5, price=82000.0, mode="paper"
    )
    assert "82,000.00" in text
    assert "82,000.0000" not in text


def test_dust_amount_does_not_use_scientific_notation() -> None:
    text = WeChatNotifier.format_protection(
        symbol="BTC/USDT", kind="take_profit", price=90000.0, amount=1e-7, mode="paper"
    )
    assert "0.0000001" in text
    assert "e-07" not in text.lower()


def test_unknown_side_does_not_claim_a_direction() -> None:
    """Reporting "卖出" for a blank side is worse than saying nothing."""
    text = WeChatNotifier.format_fill(
        symbol="BTC/USDT", side="", amount=0.1, price=82000.0, mode="paper"
    )
    assert "卖出" not in text
    assert "成交" in text



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
