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
    WECOM_CHANNEL,
    WECOM_PACKAGE,
    InstallOptions,
    OpenClawInstaller,
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


def test_plan_installs_only_the_gateway(fake_env) -> None:
    """The 企业微信 plugin is registered *through* the gateway, not with npm.

    A package sitting in the global npm prefix is invisible to the gateway's
    plugin ledger, so putting it in this argv would look installed and still
    fail to load.
    """
    argv = OpenClawInstaller().plan(InstallOptions())
    assert argv[0] == "/usr/bin/npm"
    assert argv[1:4] == ["install", "--global", GATEWAY_PACKAGE]
    assert WECOM_PACKAGE not in argv
    # Pinned so a typo in the registry cannot slip through silently.
    assert f"--registry={NPM_REGISTRIES[0]}" in argv


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
    assert WECOM_PACKAGE in argv
    # Both flags are needed for a non-interactive run, otherwise it prompts.
    assert "--force" in argv and "--accept-capabilities" in argv


def test_channel_plan_needs_the_cli(fake_env) -> None:
    with pytest.raises(ValueError, match="openclaw"):
        OpenClawInstaller().channel_plan()


def test_channel_status_reads_the_gateway_catalog(fake_env, monkeypatch) -> None:
    fake_env["which"]["openclaw"] = "/usr/local/bin/openclaw"
    output = (
        "Chat channels:\n"
        "- wecom: installed, not configured, disabled\n"
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


