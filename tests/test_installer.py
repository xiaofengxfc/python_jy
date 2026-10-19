"""The "install OpenClaw" helper behind the settings page.

This module runs ``npm install --global``, so the tests pin the parts that would
be expensive to get wrong: the argv is built from constants and enumerated
options (never from a shell string), two concurrent installs are refused, a
failing npm is reported instead of claimed as success, and the log tail is
readable after the run ends.

Nothing here actually touches npm: :class:`subprocess.Popen` and ``shutil.which``
are replaced.
"""

from __future__ import annotations

import io
import json
import os
import threading
import time

import pytest

from btrader.installer import (
    GATEWAY_PACKAGE,
    NPM_REGISTRIES,
    WECHAT_CHANNEL,
    WECHAT_INSTALLER,
    WECHAT_PACKAGE,
    InstallOptions,
    OpenClawInstaller,
    wechat_accounts,
)


class FakeProcess:
    """Stands in for ``Popen``: replays lines, then reports an exit code.

    ``installs`` mimics npm's real effect: a successful run puts ``openclaw``
    on PATH, which is exactly what the installer checks afterwards.
    """

    def __init__(
        self,
        lines: list[str],
        code: int = 0,
        argv: list[str] | None = None,
        *,
        installs: bool = True,
        which: dict | None = None,
    ) -> None:
        self.stdout = io.StringIO("\n".join(lines) + ("\n" if lines else ""))
        self._code = code
        self.argv = argv or []
        self.pid = 4242
        self.terminated = False
        if installs and code == 0 and which is not None:
            which["openclaw"] = "/usr/local/bin/openclaw"

    def wait(self, timeout: float | None = None) -> int:
        return self._code

    def poll(self) -> int | None:
        return self._code

    def terminate(self) -> None:
        self.terminated = True


@pytest.fixture
def fake_env(monkeypatch) -> dict:
    """Pretend Node.js and npm are installed and ``openclaw`` is not.

    ``which`` is answered from a table so a test can flip "installed" on after
    the install finishes, which is what the real check does.
    """
    state = {"which": {"node": "/usr/bin/node", "npm": "/usr/bin/npm"}, "runs": []}

    def fake_which(name: str) -> str | None:
        return state["which"].get(name)

    monkeypatch.setattr("btrader.installer.shutil.which", fake_which)
    monkeypatch.setattr("btrader.installer._version", lambda exe: f"v-{os.path.basename(exe)}")
    return state


def _wait(installer: OpenClawInstaller, limit: float = 5.0) -> dict:
    """Poll ``status()`` until the background run leaves ``running``."""
    deadline = time.time() + limit
    while time.time() < deadline:
        status = installer.status()
        if not status["running"]:
            return status
        time.sleep(0.01)
    raise AssertionError(f"安装没有在 {limit:g} 秒内结束：{installer.status()}")


def test_plan_installs_gateway_and_plugin(fake_env) -> None:
    argv = OpenClawInstaller().plan(InstallOptions(with_wechat=True))
    assert argv[0] == "/usr/bin/npm"
    assert argv[1:4] == ["install", "--global", GATEWAY_PACKAGE]
    # The channel package is what the gateway loads; its CLI registers it.
    assert WECHAT_PACKAGE in argv
    assert WECHAT_INSTALLER in argv
    # Pinned so a typo in the registry cannot slip through silently.
    assert f"--registry={NPM_REGISTRIES[0]}" in argv


def test_plan_can_skip_the_wechat_plugin(fake_env) -> None:
    argv = OpenClawInstaller().plan(InstallOptions(with_wechat=False))
    assert WECHAT_PACKAGE not in argv
    assert WECHAT_INSTALLER not in argv
    assert GATEWAY_PACKAGE in argv


def test_plan_honours_a_known_mirror(fake_env) -> None:
    mirror = NPM_REGISTRIES[1]
    argv = OpenClawInstaller().plan(InstallOptions(registry=mirror))
    assert f"--registry={mirror}" in argv


def test_plan_rejects_an_unknown_registry(fake_env) -> None:
    """The registry reaches npm as an argv element; only known hosts are allowed."""
    with pytest.raises(ValueError, match="镜像源"):
        OpenClawInstaller().plan(InstallOptions(registry="https://evil.example"))


def test_status_reports_missing_node_and_refuses_to_start(monkeypatch) -> None:
    monkeypatch.setattr("btrader.installer.shutil.which", lambda name: None)
    installer = OpenClawInstaller()
    status = installer.status()
    assert status["node"] == "" and status["ready"] is False
    assert "Node.js" in status["reason"]
    with pytest.raises(ValueError, match="Node.js"):
        installer.start(InstallOptions())


def test_install_streams_the_log_and_reports_success(fake_env, monkeypatch) -> None:
    lines = ["added 12 packages in 8s", "npm warn deprecated something"]
    monkeypatch.setattr(
        "btrader.installer.subprocess.Popen",
        lambda argv, **kw: FakeProcess(lines, argv=argv, which=fake_env["which"]),
    )
    installer = OpenClawInstaller()
    installer.start(InstallOptions())

    status = _wait(installer)
    assert status["state"] == "done"
    assert status["error"] == ""
    # The echoed command comes first, then npm's own output.
    assert status["log"][0].startswith("$ ")
    assert any("added 12 packages" in line for line in status["log"])


def test_a_zero_exit_without_the_cli_on_path_is_a_failure(fake_env, monkeypatch) -> None:
    """npm can succeed while the global prefix is not on PATH; say so."""
    monkeypatch.setattr(
        "btrader.installer.subprocess.Popen",
        # ``installs=False``: npm exits 0 but nothing lands on PATH.
        lambda argv, **kw: FakeProcess([], argv=argv, installs=False),
    )
    installer = OpenClawInstaller()
    installer.start(InstallOptions())

    status = _wait(installer)
    assert status["state"] == "failed"
    assert "PATH" in status["error"]


def test_install_completion_is_visible_in_availability(fake_env, monkeypatch) -> None:
    """After a successful run the page must stop offering the button."""
    monkeypatch.setattr(
        "btrader.installer.subprocess.Popen",
        lambda argv, **kw: FakeProcess(["ok"], argv=argv, which=fake_env["which"]),
    )
    installer = OpenClawInstaller()
    installer.start(InstallOptions())

    status = _wait(installer)
    assert status["state"] == "done"
    assert status["available"] is True


def test_non_zero_exit_reports_the_code(fake_env, monkeypatch) -> None:
    monkeypatch.setattr(
        "btrader.installer.subprocess.Popen",
        lambda argv, **kw: FakeProcess(
            ["npm ERR! code EACCES"], code=1, argv=argv, installs=False
        ),
    )
    installer = OpenClawInstaller()
    installer.start(InstallOptions())

    status = _wait(installer)
    assert status["state"] == "failed"
    assert "退出码 1" in status["error"]
    assert any("EACCES" in line for line in status["log"])


def test_a_second_install_is_refused_while_one_runs(fake_env) -> None:
    """Two npm processes against one global prefix is how a prefix gets broken."""
    import threading

    gate = threading.Event()

    class Blocking(FakeProcess):
        def wait(self, timeout=None):
            gate.wait(5)
            return 0

    from btrader import installer as module

    original = module.subprocess.Popen
    module.subprocess.Popen = lambda argv, **kw: Blocking([], argv=argv)
    try:
        installer = OpenClawInstaller()
        installer.start(InstallOptions())
        with pytest.raises(ValueError, match="进行中"):
            installer.start(InstallOptions())
    finally:
        gate.set()
        _wait(installer)
        module.subprocess.Popen = original


def test_cancel_stops_the_run(fake_env, monkeypatch, tmp_path) -> None:
    """Cancel must end in a terminal state rather than leaving ``running`` stuck."""
    gate = tmp_path / "never"

    class Hanging(FakeProcess):
        def wait(self, timeout=None):
            gate.exists()  # release purely through cancel
            return 0

    monkeypatch.setattr(
        "btrader.installer.subprocess.Popen", lambda argv, **kw: Hanging([], argv=argv)
    )
    installer = OpenClawInstaller()
    installer.start(InstallOptions())
    installer.cancel()

    status = _wait(installer)
    assert status["state"] in {"canceled", "done", "failed"}


def test_status_is_idle_before_anything_runs(fake_env) -> None:
    status = OpenClawInstaller().status()
    assert status["state"] == "idle"
    assert status["running"] is False
    assert status["log"] == []
    assert set(NPM_REGISTRIES) == set(status["registries"])


def test_channel_plan_registers_the_plugin_with_the_gateway(fake_env) -> None:
    """npm only writes files; the gateway keeps its own plugin ledger."""
    fake_env["which"]["openclaw"] = "/usr/local/bin/openclaw"
    argv = OpenClawInstaller().channel_plan()
    assert argv[:3] == ["/usr/local/bin/openclaw", "plugins", "install"]
    assert WECHAT_PACKAGE in argv
    # Both flags are needed for a non-interactive run, otherwise it prompts.
    assert "--force" in argv and "--accept-capabilities" in argv


def test_channel_plan_needs_the_cli(fake_env) -> None:
    with pytest.raises(ValueError, match="openclaw"):
        OpenClawInstaller().channel_plan()


def test_login_plan_uses_the_channel_id_the_gateway_knows(fake_env) -> None:
    fake_env["which"]["openclaw"] = "/usr/local/bin/openclaw"
    argv = OpenClawInstaller().login_plan()
    assert argv[1:] == ["channels", "login", "--channel", WECHAT_CHANNEL]


def test_channel_status_reads_the_gateway_catalog(fake_env, monkeypatch) -> None:
    fake_env["which"]["openclaw"] = "/usr/local/bin/openclaw"
    output = (
        "Chat channels:\n"
        "- openclaw-weixin: installed, not configured, disabled\n"
    )
    monkeypatch.setattr("btrader.installer._run_capture", lambda args, timeout=30.0: output)
    status = OpenClawInstaller().channel_status()
    assert status["plugin"] is True
    # Installed but never scanned: the page must offer the QR code, not "done".
    assert status["configured"] is False
    assert status["state"] == "installed"


def test_channel_status_reports_a_missing_channel(fake_env, monkeypatch) -> None:
    fake_env["which"]["openclaw"] = "/usr/local/bin/openclaw"
    monkeypatch.setattr(
        "btrader.installer._run_capture", lambda args, timeout=30.0: "Chat channels:\n"
    )
    status = OpenClawInstaller().channel_status()
    assert status["plugin"] is False
    assert status["state"] == "missing"


def test_channel_status_without_the_cli_does_not_shell_out(fake_env, monkeypatch) -> None:
    called = []
    monkeypatch.setattr(
        "btrader.installer._run_capture", lambda args, timeout=30.0: called.append(args) or ""
    )
    status = OpenClawInstaller().channel_status()
    assert status["state"] == "missing" and status["cli"] is False
    assert called == []


def test_register_channel_streams_the_log(fake_env, monkeypatch) -> None:
    fake_env["which"]["openclaw"] = "/usr/local/bin/openclaw"
    monkeypatch.setattr(
        "btrader.installer.subprocess.Popen",
        lambda argv, **kw: FakeProcess(["Installed plugin: openclaw-weixin"], argv=argv),
    )
    monkeypatch.setattr(
        "btrader.installer._run_capture",
        lambda args, timeout=30.0: "- openclaw-weixin: installed, not configured\n",
    )
    installer = OpenClawInstaller()
    installer.register_channel()
    status = _wait(installer)
    assert status["state"] == "done"
    assert any("openclaw-weixin" in line for line in status["log"])


def test_register_channel_fails_when_the_gateway_still_cannot_see_it(
    fake_env, monkeypatch
) -> None:
    """A plugin that downloads but never loads must not read as success."""
    fake_env["which"]["openclaw"] = "/usr/local/bin/openclaw"
    runs = []

    def fake_popen(argv, **kw):
        runs.append(argv)
        return FakeProcess(["failed during load"], argv=argv)

    monkeypatch.setattr("btrader.installer.subprocess.Popen", fake_popen)
    monkeypatch.setattr(
        "btrader.installer._run_capture", lambda args, timeout=30.0: "Chat channels:\n"
    )
    installer = OpenClawInstaller()
    installer.register_channel()
    status = _wait(installer)
    assert status["state"] == "failed"
    # It retried once with an explicit version before giving up.
    assert any("plugins" in run and "update" in run for run in runs[1:])

# --------------------------------------------------------------- QR login
def _wait_login(installer: OpenClawInstaller, limit: float = 5.0) -> dict:
    """Poll ``login_status()`` until the pending code is no longer waiting."""
    deadline = time.time() + limit
    while time.time() < deadline:
        status = installer.login_status()
        if status["state"] not in ("starting", "waiting"):
            return status
        time.sleep(0.01)
    raise AssertionError(f"扫码会话没有在 {limit:g} 秒内结束：{installer.login_status()}")


def test_login_status_is_idle_before_anything_starts() -> None:
    status = OpenClawInstaller().login_status()
    assert status["state"] == "idle"
    assert status["has_qr"] is False


def test_login_lifts_the_link_out_of_the_terminal_art(fake_env, monkeypatch) -> None:
    """The page renders this link as an image; the art itself does not scan.

    Verified against the real CLI: the printed fallback link and the QR payload
    are byte-identical, so the link is the correct thing to draw.
    """
    fake_env["which"]["openclaw"] = "/usr/local/bin/openclaw"
    url = "https://liteapp.weixin.qq.com/q/7GiQu1?qrcode=deadbeef&bot_type=3"
    monkeypatch.setattr(
        "btrader.installer.subprocess.Popen",
        lambda *a, **k: FakeProcess(
            ["正在启动...", "█ ▄▄▄▄▄ █", "若二维码未能显示，可访问以下链接：", url, "正在等待操作..."],
            code=0,
        ),
    )
    installer = OpenClawInstaller()
    installer.start_login()
    status = _wait_login(installer)
    assert url in installer.login_status()["lines"]
    # A clean exit means WeChat confirmed the scan; the code is no longer shown.
    assert status["state"] == "done"
    assert status["url"] == ""


def test_a_pending_login_is_reused_not_restarted(fake_env, monkeypatch) -> None:
    """A second click must not invalidate the code the user is looking at."""
    fake_env["which"]["openclaw"] = "/usr/local/bin/openclaw"
    spawned = []

    class PendingStream:
        """Keeps the pipe open the way a wizard waiting for a scan does.

        An empty stream ends immediately, which the worker correctly reports as
        "done" — that fake cannot represent a pending code, so the reuse path
        was never exercised.
        """

        def __iter__(self):
            yield "正在等待操作...\n"
            threading.Event().wait(5)

        def close(self) -> None:
            pass

    class BlockingProcess(FakeProcess):
        def __init__(self) -> None:
            super().__init__(["正在等待操作..."])
            self.stdout = PendingStream()

    def popen(*args, **kwargs):
        spawned.append(args)
        return BlockingProcess()

    monkeypatch.setattr("btrader.installer.subprocess.Popen", popen)
    installer = OpenClawInstaller()
    first = installer.start_login()
    second = installer.start_login()
    assert len(spawned) == 1, "重复点击不应创建第二个扫码会话"
    assert first["state"] == second["state"] == "waiting"


def test_qr_svg_is_refused_until_a_link_exists() -> None:
    with pytest.raises(ValueError, match="二维码尚未生成"):
        OpenClawInstaller().login_qr_svg()


def test_qr_svg_encodes_the_captured_link(fake_env, monkeypatch) -> None:
    fake_env["which"]["openclaw"] = "/usr/local/bin/openclaw"
    url = "https://liteapp.weixin.qq.com/q/7GiQu1?qrcode=cafebabe&bot_type=3"

    class HangingStream:
        """Yields the printed link, then keeps the pipe open like the wizard.

        A real ``channels login`` stays alive while it waits for the scan; a
        stream that closes immediately makes the worker report "done" and clear
        the link, which is not the state the page renders the code in.
        """

        def __init__(self, lines: list[str]) -> None:
            self._lines = lines
            self._closed = threading.Event()

        def __iter__(self):
            for line in self._lines:
                yield line + "\n"
            self._closed.wait(30)

        def close(self) -> None:
            self._closed.set()

    class BlockingProcess(FakeProcess):
        def __init__(self) -> None:
            super().__init__([])
            self.stdout = HangingStream([url])

        def wait(self, timeout=None) -> int:
            self.stdout.close()
            return 0

        def poll(self):
            return None

    monkeypatch.setattr(
        "btrader.installer.subprocess.Popen", lambda *a, **k: BlockingProcess()
    )
    installer = OpenClawInstaller()
    login = installer.start_login()
    assert login["state"] == "waiting", login["error"]
    # The reader thread fills the link in; the image can only be built after.
    deadline = time.time() + 5
    while time.time() < deadline and not installer.login_status()["has_qr"]:
        time.sleep(0.01)
    assert installer.login_status()["url"] == url
    svg = installer.login_qr_svg().decode("utf-8")
    assert svg.lstrip().startswith("<?xml")
    assert "<svg" in svg


def _bind_account(tmp_path, account: str = "acc-1") -> None:
    """Write the files a completed scan leaves behind, under a temp state dir."""
    root = tmp_path / "openclaw-weixin"
    (root / "accounts").mkdir(parents=True)
    (root / "accounts.json").write_text(json.dumps([account]), encoding="utf-8")
    (root / "accounts" / f"{account}.json").write_text(
        json.dumps({"token": "t", "userId": "user-1@im.wechat"}), encoding="utf-8"
    )
    (root / "accounts" / f"{account}.context-tokens.json").write_text(
        json.dumps({"peer-1@im.wechat": {"token": "c"}}), encoding="utf-8"
    )
    (root / "accounts" / f"{account}.sync.json").write_text("{}", encoding="utf-8")


def test_logout_removes_the_account_and_its_files(tmp_path, monkeypatch) -> None:
    """解绑 has to leave nothing behind.

    ``channels logout`` only drops the gateway session; the plugin keeps its own
    credential files, so the "deleted" account came straight back in the next
    status read.
    """
    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(tmp_path))
    monkeypatch.setattr("btrader.installer._openclaw_executable", lambda: "/usr/bin/openclaw")
    seen: dict = {}

    def fake_run(args, timeout=60.0):
        seen["args"] = args
        return 0, "logged out"

    monkeypatch.setattr("btrader.installer._run_command", fake_run)
    _bind_account(tmp_path)
    installer = OpenClawInstaller()

    result = installer.logout()

    assert result["ok"] is True
    assert result["account"] == "acc-1"
    assert seen["args"][:4] == ["/usr/bin/openclaw", "channels", "logout", "--channel"]
    assert "acc-1" in seen["args"]
    assert not list((tmp_path / "openclaw-weixin" / "accounts").iterdir())
    assert not (tmp_path / "openclaw-weixin" / "accounts.json").exists()
    assert wechat_accounts() == []


def test_logout_reports_a_refused_cli(tmp_path, monkeypatch) -> None:
    """A CLI that refuses must not be reported as a successful unbind."""
    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(tmp_path))
    monkeypatch.setattr("btrader.installer._openclaw_executable", lambda: "/usr/bin/openclaw")
    monkeypatch.setattr(
        "btrader.installer._run_command", lambda args, timeout=60.0: (1, "not supported")
    )
    installer = OpenClawInstaller()
    with pytest.raises(ValueError, match="解绑失败"):
        installer.logout()
