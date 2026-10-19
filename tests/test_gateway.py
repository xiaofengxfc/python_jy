"""The gateway supervisor starts a process, so its rules get pinned here.

What matters: it must not fight a gateway the user already runs, it must report
failure instead of hanging startup, and it must only kill what it started.
Every subprocess and socket call is replaced, so nothing starts.
"""

from __future__ import annotations

import pytest

from btrader.gateway import GatewaySupervisor


class FakeProcess:
    def __init__(self, exits: bool = False) -> None:
        self.pid = 1234
        self._exits = exits
        self.terminated = False

    def poll(self) -> int | None:
        return 1 if self._exits else None

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        return 0


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


def test_status_before_start_is_idle(monkeypatch) -> None:
    supervisor = GatewaySupervisor()
    monkeypatch.setattr(supervisor, "_reachable", lambda: False)
    status = supervisor.status()
    assert status["state"] == "idle"
    assert status["managed"] is False
    assert status["port"] == 18789
