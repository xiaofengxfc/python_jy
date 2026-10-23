"""更新重启与状态机的加固：评审 F1 / F2 / F4 / F5 / F8 各一条（F7 在 test_trade_leverage）。

这里钉的是「失败必须说实话」：重启起不来说 failed、写不了日志不许报成功、
另一个进程读一次状态不许把别人的活任务收成 done、一个任务没跑完不许开第二个。
全离线：重启桩、pid 探针桩，只有一条真的跑一次垫片（本机、不联网）。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from typing import Any

import pytest
from fastapi import HTTPException

from btrader.core import updater
from btrader.web import routes_update

HEAD = "49aee73efba6a4ba49b868144d9d544dc2d5f107"
OTHER = "1111111222222222222222222222222222222222"


@pytest.fixture(autouse=True)
def data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("BITRADER_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(updater, "_web_port", None)  # 端口是模块级全局，一个用例一个样
    return tmp_path


def _write_state(**overrides):
    state = {
        "job_id": "abc12345",
        "kind": "apply",
        "state": updater.STATE_RUNNING,
        "steps": [],
        "from_sha": OTHER,
        "to_sha": HEAD,
        "error": "",
        "started_at": "2026-10-23 15:45:00",
        "finished_at": "",
        "pid": 4242,
        "restart_pending": False,
    }
    state.update(overrides)
    return updater.write_state(state)


def _endpoint(path: str, method: str = "GET"):
    router = routes_update.build_update_router()
    for route in router.routes:
        if getattr(route, "path", "") == path and method in getattr(route, "methods", ()):
            return route.endpoint
    raise AssertionError(f"没有这个路由：{method} {path}")


# ------------------------------------------------------------------ F1 重启
class _Popen:
    """Popen 替身：记下参数，给出一个假 pid。"""

    def __init__(self, pid: int = 4321):
        self.calls: list[tuple[list[str], dict]] = []
        self.pid = pid

    def __call__(self, args, **kwargs):
        self.calls.append((list(args), kwargs))
        proc = subprocess.CompletedProcess(list(args), 0, "", "")
        proc.pid = self.pid
        return proc


def test_a_restart_that_cannot_open_its_log_is_refused_before_anything_moves(monkeypatch):
    """写不了日志就当场失败：以前垫片 open 抛异常、exit 1、stderr 给了 DEVNULL，
    老进程照样 return True 优雅退出——服务就这么停死了，还报成功。"""

    def refuse(path):
        raise PermissionError("另一个进程正锁着这个文件")

    popen = _Popen()
    shut: list[int] = []
    monkeypatch.setattr(updater, "_open_restart_log", refuse)
    monkeypatch.setattr(updater.subprocess, "Popen", popen)
    monkeypatch.setattr(updater, "_request_shutdown", lambda *a, **k: shut.append(1))
    ok, output = updater._restart_windows()
    assert ok is False
    assert "日志" in output
    assert "PermissionError" in output or "锁着" in output
    # 关键：没有拉新进程，也没有让本进程退出——服务还在。
    assert popen.calls == []
    assert shut == []


def test_every_restart_gets_a_log_file_of_its_own(monkeypatch, data_dir):
    """每次重启一个唯一的日志文件：共用的那个正被当前进程写着，Windows 锁成只读。"""
    popen = _Popen()
    monkeypatch.setattr(updater.subprocess, "Popen", popen)
    answers = iter([True, True, True, True])
    monkeypatch.setattr(updater, "_wait_until", lambda fn, timeout, poll=0.0: next(answers))
    monkeypatch.setattr(updater, "_request_shutdown", lambda *a, **k: None)
    first = updater._restart_windows()
    second = updater._restart_windows()
    assert first[0] is True and second[0] is True
    paths = [call[1]["stdout"].name for call in popen.calls]
    assert len(set(paths)) == 2, paths
    for path in paths:
        assert path.endswith(".log")
        assert ".run_out.log" not in path
        assert os.path.exists(path)
    # 子进程的 stdout / stderr 都指向那个日志，不再有 DEVNULL。
    for args, kwargs in popen.calls:
        assert kwargs.get("stderr") == subprocess.STDOUT
        assert kwargs.get("stdout") is not subprocess.DEVNULL
        assert kwargs.get("shell") in (None, False)
        assert str(kwargs["stdout"].name) in args  # 垫片也知道自己该往哪儿写


def test_a_new_process_that_never_listens_is_a_failure_with_the_log_and_the_manual_command(monkeypatch):
    popen = _Popen(pid=777)
    monkeypatch.setattr(updater.subprocess, "Popen", popen)
    monkeypatch.setattr(updater, "_request_shutdown", lambda *a, **k: None)
    # 第一次等（自己放开端口）成了；第二次等（新进程绑上来）没成
    answers = iter([True, False])
    monkeypatch.setattr(updater, "_wait_until", lambda fn, timeout, poll=0.0: next(answers))
    ok, output = updater._restart_windows()
    assert ok is False
    assert "新进程没起来" in output
    assert str(updater.WEB_PORT) in output
    log_path = popen.calls[0][1]["stdout"].name
    assert log_path in output
    assert "run_web.py" in output  # 手动命令
    assert "--port" in output


def test_a_restart_that_comes_back_up_is_success(monkeypatch):
    popen = _Popen(pid=888)
    monkeypatch.setattr(updater.subprocess, "Popen", popen)
    monkeypatch.setattr(updater, "_request_shutdown", lambda *a, **k: None)
    answers = iter([True, True])
    monkeypatch.setattr(updater, "_wait_until", lambda fn, timeout, poll=0.0: next(answers))
    ok, output = updater._restart_windows()
    assert ok is True
    assert str(updater.WEB_PORT) in output
    assert "888" in output


def test_the_shim_really_spawns_the_child_and_keeps_its_output(tmp_path):
    """真跑一次垫片：它等端口放开 → 拉起 run_web.py → 子进程的输出（含 stderr）进日志。"""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])

    app = tmp_path / "run_web.py"
    app.write_text(
        "import socket, sys, time\n"
        "port = int(sys.argv[sys.argv.index('--port') + 1])\n"
        "sock = socket.socket()\n"
        "sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        "sock.bind(('127.0.0.1', port)); sock.listen(1)\n"
        "print('FAKE-APP-UP', flush=True)\n"
        "sys.stderr.write('FAKE-APP-STDERR\\n'); sys.stderr.flush()\n"
        "time.sleep(4)\n",
        encoding="utf-8",
    )
    log_path = tmp_path / "shim.log"
    with open(log_path, "wb") as handle:
        proc = subprocess.run(
            [sys.executable, "-c", updater._WINDOWS_SHIM, str(port), str(tmp_path),
             sys.executable, str(log_path)],
            stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT,
            timeout=60,
        )
    assert proc.returncode == 0, log_path.read_text(encoding="utf-8", errors="replace")
    # 垫片只负责等端口 + 拉起新进程；新进程是另一个解释器，它的输出要等它起来才有。
    deadline = time.time() + 30
    listening = False
    text = ""
    while time.time() < deadline:
        text = log_path.read_text(encoding="utf-8", errors="replace")
        with socket.socket() as client:
            client.settimeout(0.2)
            listening = client.connect_ex(("127.0.0.1", port)) == 0
        if listening and "FAKE-APP-STDERR" in text:
            break
        time.sleep(0.2)
    assert "run_web.py" in text  # 垫片自己说了它拉的是什么
    assert "FAKE-APP-UP" in text, text
    assert "FAKE-APP-STDERR" in text, text  # 子进程的 stderr 也在这份日志里
    assert listening


def test_a_failed_sudo_restart_ends_the_job_and_keeps_the_stderr(monkeypatch):
    monkeypatch.setattr(updater.sys, "platform", "linux")
    monkeypatch.setattr(
        updater.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(
            args, 1, "Job for bitrader.service failed.\n", "sudo: a password is required\n"
        ),
    )
    ok, output = updater.restart_service()
    assert ok is False
    assert "a password is required" in output


# ------------------------------------------------------------------ F2 一次只跑一个
def test_a_second_apply_is_refused_while_one_is_running(monkeypatch):
    _write_state(state=updater.STATE_RUNNING, pid=4242, restart_pending=True)
    monkeypatch.setattr(updater, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(routes_update, "running_engines", lambda: [])
    with pytest.raises(HTTPException) as caught:
        _endpoint("/api/update/apply", "POST")()
    assert caught.value.status_code == 409
    assert "还在跑" in str(caught.value.detail)
    assert "abc12345" in str(caught.value.detail)


def test_a_second_rollback_is_refused_while_one_is_running(monkeypatch):
    _write_state(state=updater.STATE_DONE, pid=4242, restart_pending=False)
    payload = routes_update.RollbackPayload(confirm=True)
    # 第一个任务真的在跑：状态改成 running 且那个 pid 还活着
    _write_state(state=updater.STATE_RUNNING, pid=4242, restart_pending=False)
    monkeypatch.setattr(updater, "_pid_alive", lambda pid: True)
    with pytest.raises(HTTPException) as caught:
        _endpoint("/api/update/rollback", "POST")(payload)
    assert caught.value.status_code == 409
    assert "还在跑" in str(caught.value.detail)


def test_start_job_refuses_a_second_job_while_one_is_running(monkeypatch):
    _write_state(state=updater.STATE_RUNNING, pid=4242, restart_pending=False)
    monkeypatch.setattr(updater, "_pid_alive", lambda pid: True)
    with pytest.raises(updater.JobBusy):
        updater.start_job(updater.KIND_APPLY, {})


def test_the_state_file_tmp_name_is_unique_per_write(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(updater.os, "replace", lambda src, dst: seen.append(os.path.basename(str(src))))
    updater.write_state({"state": updater.STATE_IDLE})
    updater.write_state({"state": updater.STATE_IDLE})
    assert len(seen) == 2
    assert seen[0] != seen[1]
    for name in seen:
        assert str(os.getpid()) in name
        assert name.endswith(".tmp")


# ------------------------------------------------------------------ F4 断在半路的任务
def test_an_interrupted_job_is_reported_as_failed_on_the_next_read(monkeypatch):
    """running + restart_pending=false + 那个 pid 已经不在了 = 上次没跑完就断了。"""
    _write_state(
        state=updater.STATE_RUNNING,
        restart_pending=False,
        pid=4242,
        steps=[{"name": "git fetch --all --prune", "ok": True, "output": "", "seconds": 0.1}],
    )
    monkeypatch.setattr(updater, "_pid_alive", lambda pid: False)
    state = updater.read_state()
    assert state["state"] == updater.STATE_FAILED
    assert "没跑完" in state["error"]
    assert "4242" in state["error"]
    # 断在哪一步要说出来
    assert "git fetch --all --prune" in state["error"]
    # 收过一次就定住了
    assert updater.read_state()["state"] == updater.STATE_FAILED


def test_a_job_whose_process_is_still_alive_is_left_alone(monkeypatch):
    _write_state(state=updater.STATE_RUNNING, restart_pending=False, pid=4242)
    monkeypatch.setattr(updater, "_pid_alive", lambda pid: True)
    state = updater.read_state()
    assert state["state"] == updater.STATE_RUNNING
    assert state["error"] == ""


# ------------------------------------------------------------------ F5 谁有权收尾
def test_another_live_process_cannot_finish_a_job(monkeypatch):
    """外部进程读一次状态就把活任务收成 done 的老毛病：只有记下那个 pid 真的没了才收。"""
    _write_state(
        state=updater.STATE_RUNNING,
        restart_pending=True,
        pid=4242,
        steps=[{"name": "git fetch --all --prune", "ok": True, "output": "", "seconds": 0.1}],
    )
    monkeypatch.setattr(updater, "_pid_alive", lambda pid: True)
    state = updater.read_state()
    assert state["state"] == updater.STATE_RUNNING
    assert [step["name"] for step in state["steps"]] == ["git fetch --all --prune"]
    # 盘上也没被改
    raw = json.loads((updater.state_path()).read_text(encoding="utf-8"))
    assert raw["state"] == updater.STATE_RUNNING


def test_the_restart_is_finished_only_when_the_recorded_pid_is_gone(monkeypatch):
    _write_state(
        state=updater.STATE_RUNNING,
        restart_pending=True,
        pid=4242,
        steps=[{"name": "git fetch --all --prune", "ok": True, "output": "", "seconds": 0.1}],
    )
    monkeypatch.setattr(updater, "_pid_alive", lambda pid: False)
    state = updater.read_state()
    assert state["state"] == updater.STATE_DONE
    assert state["steps"][-1]["name"] == updater.STEP_RESTART
    assert state["restart_pending"] is False


def test_our_own_running_job_is_never_finished_by_a_read(monkeypatch):
    _write_state(state=updater.STATE_RUNNING, restart_pending=True, pid=os.getpid())
    monkeypatch.setattr(updater, "_pid_alive", lambda pid: True)
    assert updater.read_state()["state"] == updater.STATE_RUNNING


# ------------------------------------------------------------------ F8 from_sha
def test_rollback_refuses_a_from_sha_that_is_not_a_sha(monkeypatch):
    _write_state(state=updater.STATE_DONE, from_sha="--force", restart_pending=False)
    monkeypatch.setattr(updater, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(routes_update, "running_engines", lambda: [])
    with pytest.raises(HTTPException) as caught:
        _endpoint("/api/update/rollback", "POST")(routes_update.RollbackPayload(confirm=True))
    assert caught.value.status_code == 400
    assert "SHA" in str(caught.value.detail) or "sha" in str(caught.value.detail)


def test_rollback_refuses_a_from_sha_that_is_too_short(monkeypatch):
    _write_state(state=updater.STATE_DONE, from_sha="abc12", restart_pending=False)
    monkeypatch.setattr(updater, "_pid_alive", lambda pid: False)
    with pytest.raises(HTTPException) as caught:
        _endpoint("/api/update/rollback", "POST")(routes_update.RollbackPayload(confirm=True))
    assert caught.value.status_code == 400


def test_a_real_sha_still_passes(monkeypatch):
    _write_state(state=updater.STATE_DONE, from_sha=OTHER, restart_pending=False)
    monkeypatch.setattr(updater, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(updater, "_spawn", lambda fn: None)
    result = _endpoint("/api/update/rollback", "POST")(routes_update.RollbackPayload(confirm=True))
    assert result["job_id"]


# ------------------------------------------------------------------ F9 端口不能写死
def test_the_shim_waits_on_the_port_the_process_really_uses(monkeypatch):
    """在 8799 上跑的实例，垫片必须等 8799：等 8765 就会拉出一个 Errno 10048 的进程。"""
    updater.set_web_port(8799)
    popen = _Popen(pid=999)
    seen: list[Any] = []

    answers = iter([True, False])  # 第一次问「放开了吗」= 是，第二次问「有人听吗」= 是

    def spy(port=None, host=updater.WEB_HOST):
        seen.append(port)
        return next(answers, False)

    monkeypatch.setattr(updater.subprocess, "Popen", popen)
    monkeypatch.setattr(updater, "_request_shutdown", lambda *a, **k: None)
    monkeypatch.setattr(updater, "_port_free", spy)
    ok, output = updater._restart_windows()
    assert ok is True
    args = popen.calls[0][0]
    assert "8799" in args, args
    assert "8765" not in args
    assert seen == [8799, 8799], seen
    assert "8799" in output


def test_without_a_real_port_it_falls_back_to_8765_and_says_so(monkeypatch):
    """拿不到真实端口时按 8765 兜底，而且状态里要写明这是兜底值。"""
    popen = _Popen(pid=1000)
    monkeypatch.setattr(updater.subprocess, "Popen", popen)
    monkeypatch.setattr(updater, "_request_shutdown", lambda *a, **k: None)
    monkeypatch.setattr(updater, "_wait_until", lambda fn, timeout, poll=0.0: True)
    ok, output = updater._restart_windows()
    assert ok is True
    assert "8765" in popen.calls[0][0]
    assert "兜底" in output


def test_set_web_port_ignores_nonsense(monkeypatch):
    for value in (None, "", "abc", 0, -1, 70000, object()):
        updater.set_web_port(value)
        assert updater.web_port() == 8765
        assert updater.web_port_known() is False
    updater.set_web_port("8799")
    assert updater.web_port() == 8799
    assert updater.web_port_known() is True


def test_the_restart_step_records_the_port_it_waited_on(monkeypatch):
    updater.set_web_port(8799)
    state = updater._default_state()
    monkeypatch.setattr(updater, "restart_service", lambda: (True, "已重启。"))
    ok, _output = updater._restart_step(state)
    assert ok is True
    assert state["port"] == 8799
    raw = json.loads(updater.state_path().read_text(encoding="utf-8"))
    assert raw["port"] == 8799
    assert raw["restart_pending"] is True


# ------------------------------------------------------------------ 任务线程不许是 daemon
def test_the_update_thread_is_not_a_daemon(monkeypatch):
    """重启那一步要等端口两头确认，daemon 线程会在主线程退出时被掐掉：
    「到底起没起来」就没人体会到，状态又变成一句谎。"""
    seen: list[bool] = []

    def work():
        import threading

        seen.append(threading.current_thread().daemon)

    updater._spawn(work)
    for _ in range(100):
        if seen:
            break
        time.sleep(0.02)
    assert seen == [False]
