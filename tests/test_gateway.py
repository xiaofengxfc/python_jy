"""The gateway supervisor starts a process, so its rules get pinned here.

What matters: it must not fight a gateway the user already runs, it must report
failure instead of hanging startup, and it must only kill what it started.
Every subprocess and socket call is replaced, so nothing starts.
"""

from __future__ import annotations

import pytest

from btrader.gateway import GatewaySupervisor


class FakeProcess:
    def __init__(self, exits: bool = False, output: list[str] | None = None) -> None:
        self.pid = 1234
        self._exits = exits
        self.terminated = False
        self.stdout = iter(output or [])

    def poll(self) -> int | None:
        return 1 if self._exits else None

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        return 0


class FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_an_already_running_gateway_is_left_alone(monkeypatch) -> None:
    """Someone else's gateway is never ours to start or stop."""
    supervisor = GatewaySupervisor()
    monkeypatch.setattr(supervisor, "_reachable", lambda: True)
    launched = []
    monkeypatch.setattr("btrader.gateway.subprocess.Popen", lambda *a, **k: launched.append(a))

    status = supervisor.start()
    assert status["state"] == "external"
    assert status["reachable"] is True
    assert status["managed"] is False
    assert launched == []

    supervisor.stop()  # must not try to kill anything
    assert supervisor.status()["state"] == "external"


def test_missing_cli_is_reported_not_raised(monkeypatch) -> None:
    supervisor = GatewaySupervisor()
    monkeypatch.setattr(supervisor, "_reachable", lambda: False)
    monkeypatch.setattr("btrader.gateway.shutil.which", lambda name: None)
    status = supervisor.start()
    assert status["state"] == "failed"
    assert "openclaw" in status["error"]


def test_a_gateway_that_never_opens_its_port_fails_and_is_cleaned_up(monkeypatch) -> None:
    supervisor = GatewaySupervisor(timeout=0.05)
    monkeypatch.setattr(supervisor, "_reachable", lambda: False)
    monkeypatch.setattr("btrader.gateway.shutil.which", lambda name: "/usr/local/bin/openclaw")
    killed = []
    monkeypatch.setattr("btrader.gateway.subprocess.Popen", lambda *a, **k: FakeProcess())
    monkeypatch.setattr(
        "btrader.gateway.subprocess.run", lambda *a, **k: killed.append(a)
    )
    status = supervisor.start()
    assert status["state"] == "failed"
    assert "没有就绪" in status["error"]
    # A half-started gateway must not survive to fight the next launch.
    assert killed, "未清理启动失败的网关进程"


def test_becoming_reachable_marks_it_running(monkeypatch) -> None:
    supervisor = GatewaySupervisor(timeout=2.0)
    calls = {"n": 0}

    def reachable() -> bool:
        # Down on the first probe (start), up afterwards (the wait loop).
        calls["n"] += 1
        return calls["n"] > 1

    monkeypatch.setattr(supervisor, "_reachable", reachable)
    monkeypatch.setattr("btrader.gateway.shutil.which", lambda name: "/usr/local/bin/openclaw")
    monkeypatch.setattr("btrader.gateway.subprocess.Popen", lambda *a, **k: FakeProcess())
    status = supervisor.start()
    assert status["state"] == "running"
    assert status["managed"] is True


def test_stop_only_kills_a_gateway_we_started(monkeypatch) -> None:
    supervisor = GatewaySupervisor()
    killed = []
    monkeypatch.setattr("btrader.gateway.subprocess.run", lambda *a, **k: killed.append(a))

    supervisor.stop()  # nothing was started
    assert killed == []

    monkeypatch.setattr(supervisor, "_reachable", lambda: False)
    monkeypatch.setattr("btrader.gateway.shutil.which", lambda name: "openclaw")
    monkeypatch.setattr("btrader.gateway.subprocess.Popen", lambda *a, **k: FakeProcess())
    supervisor.start()
    supervisor.stop()
    assert killed, "自己启动的网关应在退出时停止"


def test_detach_leaves_the_gateway_running(monkeypatch) -> None:
    """App shutdown must not kill the gateway: a restart costs the WeChat login.

    The ilink bot token is bound to the running plugin, so stopping the process
    forces the user to scan a new QR. ``detach`` forgets the handle instead.
    """
    supervisor = GatewaySupervisor()
    killed = []
    monkeypatch.setattr("btrader.gateway.subprocess.run", lambda *a, **k: killed.append(a))
    monkeypatch.setattr(supervisor, "_reachable", lambda: True)

    # A managed, running gateway: ``stop`` would taskkill this pid.
    supervisor._process = FakeProcess()
    supervisor._info.state = "running"
    supervisor._info.managed = True

    supervisor.detach()

    assert killed == [], "退出时不应该杀掉网关"
    assert supervisor.status()["state"] == "external"
    assert supervisor.status()["managed"] is False
    # The next start adopts whatever is listening instead of spawning a second one.
    assert supervisor.start()["state"] == "external"


def test_status_before_start_is_idle(monkeypatch) -> None:
    supervisor = GatewaySupervisor()
    monkeypatch.setattr(supervisor, "_reachable", lambda: False)
    status = supervisor.status()
    assert status["state"] == "idle"
    assert status["managed"] is False
    assert status["port"] == 18789


def test_a_failed_start_quotes_what_the_child_said(monkeypatch) -> None:
    """``exit code 78`` alone is not actionable; the child's own words are.

    The gateway prints its refusal to stderr and exits, so the supervisor keeps
    the tail of that output and puts it in the error the settings page shows.
    """
    import time

    supervisor = GatewaySupervisor(timeout=0.05)
    monkeypatch.setattr(supervisor, "_reachable", lambda: False)
    monkeypatch.setattr("btrader.gateway.shutil.which", lambda name: "/usr/local/bin/openclaw")
    monkeypatch.setattr(
        "btrader.gateway.subprocess.Popen",
        lambda *a, **k: FakeProcess(
            exits=True, output=["[openclaw] The CLI command failed.", "run doctor"]
        ),
    )
    monkeypatch.setattr(
        "btrader.gateway.subprocess.run", lambda *a, **k: FakeCompleted(returncode=1, stdout="")
    )
    # The failure is not the gateway.mode one, so the supervisor must not touch
    # the user's configuration.
    monkeypatch.setattr(supervisor, "_mode_configured", lambda exe: None)
    set_mode = []
    monkeypatch.setattr(supervisor, "_set_mode_local", lambda exe: set_mode.append(exe) or True)

    status = supervisor.start()
    assert status["state"] == "failed"
    for _ in range(100):  # the output is drained by a reader thread
        if supervisor.output():
            break
        time.sleep(0.01)
    assert set_mode == [], "未确认原因时不得改写用户的网关配置"
    assert "The CLI command failed" in status["error"]
    assert "退出码 1" in status["error"]


def test_unset_gateway_mode_is_set_and_the_start_retried(monkeypatch) -> None:
    """The reported bug: ``gateway.mode`` unset made the gateway exit at once.

    The app owns the OpenClaw install, so it authors the value ``openclaw
    doctor`` prescribes and tries once more instead of showing a bare exit code.
    """
    supervisor = GatewaySupervisor(timeout=2.0)
    state = {"ready": False, "spawned": 0}

    def reachable() -> bool:
        return state["ready"]

    def popen(*args, **kwargs):
        state["spawned"] += 1
        return FakeProcess(exits=not state["ready"])

    def set_mode(exe: str) -> bool:
        state["ready"] = True  # after the fix the gateway comes up
        return True

    monkeypatch.setattr(supervisor, "_reachable", reachable)
    monkeypatch.setattr("btrader.gateway.shutil.which", lambda name: "/usr/local/bin/openclaw")
    monkeypatch.setattr("btrader.gateway.subprocess.Popen", popen)
    monkeypatch.setattr("btrader.gateway.subprocess.run", lambda *a, **k: FakeCompleted())
    monkeypatch.setattr(supervisor, "_mode_configured", lambda exe: False)
    monkeypatch.setattr(supervisor, "_set_mode_local", set_mode)

    status = supervisor.start()
    assert status["state"] == "running"
    assert status["managed"] is True
    assert state["spawned"] == 2, "配置修复后应重试一次启动"


def test_authored_gateway_mode_is_never_overwritten(monkeypatch) -> None:
    """A user who chose ``remote`` must not be silently switched to ``local``."""
    supervisor = GatewaySupervisor(timeout=0.05)
    monkeypatch.setattr(supervisor, "_reachable", lambda: False)
    monkeypatch.setattr("btrader.gateway.shutil.which", lambda name: "/usr/local/bin/openclaw")
    monkeypatch.setattr(
        "btrader.gateway.subprocess.Popen", lambda *a, **k: FakeProcess(exits=True)
    )
    monkeypatch.setattr("btrader.gateway.subprocess.run", lambda *a, **k: FakeCompleted())
    monkeypatch.setattr(supervisor, "_mode_configured", lambda exe: True)
    set_mode = []
    monkeypatch.setattr(supervisor, "_set_mode_local", lambda exe: set_mode.append(exe) or True)

    status = supervisor.start()
    assert status["state"] == "failed"
    assert set_mode == [], "已配置的 gateway.mode 不得被覆盖"


def test_a_second_start_while_starting_does_not_spawn_another(monkeypatch) -> None:
    """Two clicks must not leave two gateways fighting over the port."""
    supervisor = GatewaySupervisor(timeout=0.05)
    monkeypatch.setattr(supervisor, "_reachable", lambda: False)
    monkeypatch.setattr("btrader.gateway.shutil.which", lambda name: "/usr/local/bin/openclaw")
    spawned = []
    monkeypatch.setattr(
        "btrader.gateway.subprocess.Popen",
        lambda *a, **k: spawned.append(a) or FakeProcess(exits=True),
    )
    monkeypatch.setattr("btrader.gateway.subprocess.run", lambda *a, **k: FakeCompleted())
    monkeypatch.setattr(supervisor, "_mode_configured", lambda exe: None)

    supervisor._starting = True  # a first attempt is already probing
    status = supervisor.start()
    assert spawned == []
    assert status["state"] != "running"
