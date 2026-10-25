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
from datetime import datetime, timedelta
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
    # 地址 / 端口 / 桌面壳标记也是模块级全局，一个用例一个样
    monkeypatch.setattr(updater, "_web_host", None)
    monkeypatch.setattr(updater, "_web_port", None)
    monkeypatch.setattr(updater, "_desktop_shell", "")
    monkeypatch.setattr(updater, "_last_restart_log", None)
    return tmp_path


@pytest.fixture(autouse=True)
def no_real_health_probe(monkeypatch):
    """这条文件一条真请求都不许发。

    以前 _heal_state 那组没打桩，_health_target 回落到 web_port()＝8765，于是
    「提交前必须全绿」变成了「取决于你本机 8765 起没起」：8765 空着就红、开着就绿，
    同一个提交跑两次能得出两个结论。要真请求的用例自己打桩
    （tests/test_update_restart_confirm.py 里有一份现成的本地 HTTP 服务）。
    """

    def refuse(request, *args, **kwargs):
        url = getattr(request, "full_url", request)
        raise AssertionError(f"这次用例真的去请求了 {url}：健康探测要打桩，不许碰本机端口")

    monkeypatch.setattr(updater.urllib.request, "urlopen", refuse)


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
    """每次重启一个唯一的日志文件：共用的那个正被当前进程写着，Windows 锁成只读。

    文件名由父进程挑、写进去由垫片自己干（它 open 'a' 再 dup2 到 1/2），
    父进程的 stdout / stderr 一起给 DEVNULL——共享同一个句柄会把老进程那句话写花。
    """
    popen = _Popen()
    monkeypatch.setattr(updater.subprocess, "Popen", popen)
    answers = iter([True, True, True, True])
    monkeypatch.setattr(updater, "_wait_until", lambda fn, timeout, poll=0.0: next(answers))
    monkeypatch.setattr(updater, "_request_shutdown", lambda *a, **k: None)
    first = updater._restart_windows()
    second = updater._restart_windows()
    assert first[0] is True and second[0] is True
    paths = [next(str(a) for a in args if str(a).endswith(".log")) for args, _ in popen.calls]
    assert len(set(paths)) == 2, paths
    for path in paths:
        assert ".run_out.log" not in path
        assert os.path.exists(path)
    # 垫片自己开那份日志：父进程一个句柄都不递（两个句柄共享文件位置会把彼此写花）。
    for args, kwargs in popen.calls:
        assert kwargs.get("stdout") is subprocess.DEVNULL
        assert kwargs.get("stderr") is subprocess.DEVNULL
        assert kwargs.get("shell") in (None, False)
        assert next(str(a) for a in args if str(a).endswith(".log")) in args


def test_a_new_process_that_never_listens_is_a_failure_with_the_log_and_the_manual_command(
    monkeypatch, data_dir
):
    """新进程那一侧：端口放开了，但一直到期限都没有服务接上来 → failed。

    这条判据（连同手动命令、日志）现在落在**新进程**里做，而且判据是 /api/health，
    不是「端口有人听」——绑上 0.3 秒就关的 socket 能骗过端口探测。
    """
    log = data_dir / "logs" / "restart-20261023-010203-abcdef.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("", encoding="utf-8")
    past = (datetime.now() - timedelta(seconds=5)).astimezone().isoformat(timespec="seconds")
    with socket.socket() as probe:  # 一个空端口：没人听
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    _write_state(
        pid=777,
        port=port,
        restart_pending=True,
        restart_deadline=past,
        restart_log=str(log),
    )
    monkeypatch.setattr(updater, "_pid_alive", lambda pid: False)
    ok, detail = updater._confirm_restart(updater._default_state() | {"port": port})
    assert ok is False
    assert "还没有人听" in detail
    state = updater.read_state()
    assert state["state"] == updater.STATE_FAILED
    assert "重启没等到" in state["error"]
    assert f"127.0.0.1:{port}" in state["error"]
    assert "run_web.py" in state["error"]  # 手动命令
    assert "--port" in state["error"]
    assert "run_web.py" in log.read_text(encoding="utf-8")


def test_a_restart_that_comes_back_up_is_success(monkeypatch):
    popen = _Popen(pid=888)
    monkeypatch.setattr(updater.subprocess, "Popen", popen)
    monkeypatch.setattr(updater, "_request_shutdown", lambda *a, **k: None)
    # 老进程只等一段（自己放开端口）；「新进程起来了吗」由新进程自己认。
    answers = iter([True])
    monkeypatch.setattr(updater, "_wait_until", lambda fn, timeout, poll=0.0: next(answers))
    ok, output = updater._restart_windows()
    assert ok is True
    assert str(updater.WEB_PORT) in output
    assert "888" in output


@pytest.mark.skipif(sys.platform != "win32", reason="垫片用 creationflags 起进程，Linux 上传这个参数就是 ValueError")
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
             sys.executable, str(log_path), "127.0.0.1"],
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
    """记下的那个 pid 没了 → 新进程自己确认一次。地址与健康检查都归这条用例自己管。"""
    _write_state(
        state=updater.STATE_RUNNING,
        restart_pending=True,
        pid=4242,
        host="127.0.0.1",
        port=8792,  # 自己的端口：以前这里没写 port，就回落到本机 8765 真发了一次请求
        steps=[{"name": "git fetch --all --prune", "ok": True, "output": "", "seconds": 0.1}],
    )
    monkeypatch.setattr(updater, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(updater, "_port_free", lambda port=None, host=None: False)
    seen: list[tuple[Any, Any]] = []

    def healthy(host=None, port=None):
        seen.append((host, port))
        return True, f"http://{host}:{port}/api/health 回了 200"

    monkeypatch.setattr(updater, "_health_ok", healthy)
    state = updater.read_state()
    assert seen == [("127.0.0.1", 8792)], seen  # 确认的是状态里那个地址，不是本机默认的 8765
    assert state["state"] == updater.STATE_DONE
    assert state["steps"][-1]["name"] == updater.STEP_RESTART
    assert state["restart_pending"] is False




def test_the_restart_step_is_not_written_twice(monkeypatch):
    """老进程记过一条「重启服务」，新进程确认时不许再 append 一条。

    服务器上真跑一次 apply 撞出来的：systemctl 返回 0（或 SIGTERM）之后老进程已经记了
    一条 ok，_heal_state 又补一条，界面上就是两个「重启服务」并排。
    """
    _write_state(
        state=updater.STATE_RUNNING,
        restart_pending=True,
        pid=4242,
        host="127.0.0.1",
        port=8793,
        steps=[
            {"name": "git fetch --all --prune", "ok": True, "output": "", "seconds": 0.1},
            {
                "name": updater.STEP_RESTART,
                "ok": True,
                "output": "重启请求已被 systemd 受理（退出码 -15）",
                "seconds": 0.0,
            },
        ],
    )
    monkeypatch.setattr(updater, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(updater, "_port_free", lambda port=None, host=None: False)
    monkeypatch.setattr(updater, "_health_ok", lambda host=None, port=None: (True, "回了 200"))
    state = updater.read_state()
    names = [step["name"] for step in state["steps"]]
    assert names.count(updater.STEP_RESTART) == 1, names
    assert state["state"] == updater.STATE_DONE
    # 老进程那句留着，新进程的确认接在后面
    assert "已被 systemd 受理" in state["steps"][-1]["output"]
    assert "回了 200" in state["steps"][-1]["output"]


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

    def spy(port=None, host=updater.WEB_HOST):
        seen.append(port)  # 老进程只探这一下：自己有没有把端口放开
        return True

    monkeypatch.setattr(updater.subprocess, "Popen", popen)
    monkeypatch.setattr(updater, "_request_shutdown", lambda *a, **k: None)
    monkeypatch.setattr(updater, "_port_free", spy)
    ok, output = updater._restart_windows()
    assert ok is True
    args = popen.calls[0][0]
    assert "8799" in args, args
    assert "8765" not in args
    assert seen == [8799], seen
    assert "8799" in output


def test_without_a_real_port_it_falls_back_to_8765_and_says_so(monkeypatch):
    """拿不到真实端口时按 8765 兜底，而且状态里要写明这是兜底值。"""
    popen = _Popen(pid=1000)
    monkeypatch.setattr(updater.subprocess, "Popen", popen)
    monkeypatch.setattr(updater, "_request_shutdown", lambda *a, **k: None)
    monkeypatch.setattr(updater, "_wait_until", lambda fn, timeout, poll=0.0: True)
    ok, output = updater._restart_windows()
    assert ok is True
    # 「兜底就是 8765」这条口径单独钉在常量上；这里比的是「兜底值真被用了」，
    # 免得整条用例跟着本机起没起 8765 变红。
    assert updater.WEB_PORT == 8765
    assert str(updater.WEB_PORT) in popen.calls[0][0]
    assert "兜底" in output


def test_set_web_port_ignores_nonsense(monkeypatch):
    assert updater.WEB_PORT == 8765  # 兜底值本身
    for value in (None, "", "abc", 0, -1, 70000, object()):
        updater.set_web_port(value)
        assert updater.web_port() == updater.WEB_PORT
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


# ------------------------------------------- 静默启动：一条子进程都不许弹黑框
# Windows 上 DETACHED_PROCESS 与 CREATE_NO_WINDOW 互斥，带着前者 Windows 会无视后者：
# 以前 restart 那两层就是 0x00000008，看起来「分离」了，实际上控制台照弹。
_WIN_ONLY = pytest.mark.skipif(
    sys.platform != "win32", reason="这些常量与 STARTUPINFO 只有 Windows 上有"
)


def test_the_hidden_flags_carry_create_no_window_and_never_detached_process():
    assert updater.CREATE_NO_WINDOW == 0x08000000
    assert updater.CREATE_NEW_PROCESS_GROUP == 0x00000200
    assert updater.WINDOWS_HIDDEN == updater.CREATE_NO_WINDOW | updater.CREATE_NEW_PROCESS_GROUP
    assert updater.WINDOWS_HIDDEN & updater.CREATE_NO_WINDOW
    assert updater.WINDOWS_HIDDEN & updater.CREATE_NEW_PROCESS_GROUP
    # DETACHED_PROCESS 一位都不许带：带上它 CREATE_NO_WINDOW 就被 Windows 丢掉。
    assert updater.WINDOWS_HIDDEN & 0x00000008 == 0
    assert not hasattr(updater, "WINDOWS_DETACHED"), "旧的 WINDOWS_DETACHED 要删掉"


def test_the_no_window_helper_is_empty_off_windows(monkeypatch, tmp_path):
    """别的平台上多传一个 creationflags 就是 ValueError：那里必须一个参数都不加。"""
    monkeypatch.setattr(sys, "platform", "linux")
    assert updater._no_window() == {}
    seen: list[dict] = []

    def fake_run(args, **kwargs):
        seen.append(kwargs)
        return subprocess.CompletedProcess(list(args), 0, "ok", "")

    monkeypatch.setattr(updater.subprocess, "run", fake_run)
    updater._git(tmp_path, "status", "--porcelain")
    updater._run_command(["pip", "--version"], cwd=tmp_path, timeout=5.0)
    assert len(seen) == 2
    for kwargs in seen:
        assert "creationflags" not in kwargs
        assert "startupinfo" not in kwargs


@_WIN_ONLY
def test_the_no_window_helper_hides_the_console_on_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    kwargs = updater._no_window()
    assert kwargs["creationflags"] == updater.CREATE_NO_WINDOW
    info = kwargs["startupinfo"]
    assert info.dwFlags & updater.STARTF_USESHOWWINDOW
    assert info.wShowWindow == updater.SW_HIDE


@_WIN_ONLY
def test_git_and_pip_are_spawned_without_a_console(monkeypatch, tmp_path):
    """父进程没有控制台时，每条 git.exe / pip 都会闪一个黑框——这是用户看到的弹窗之一。"""
    seen: list[tuple[list[str], dict]] = []

    def fake_run(args, **kwargs):
        seen.append((list(args), kwargs))
        return subprocess.CompletedProcess(list(args), 0, "ok", "")

    monkeypatch.setattr(updater.subprocess, "run", fake_run)
    updater._git(tmp_path, "status", "--porcelain")
    updater._run_command(
        ["pip", "install", "-r", "requirements.txt"], cwd=tmp_path, timeout=5.0
    )
    assert len(seen) == 2
    for args, kwargs in seen:
        assert kwargs["creationflags"] == updater.CREATE_NO_WINDOW, args
        info = kwargs["startupinfo"]
        assert info.dwFlags & updater.STARTF_USESHOWWINDOW, args
        assert info.wShowWindow == updater.SW_HIDE, args


def test_the_shim_spawns_run_web_without_a_console():
    """垫片只用标准库、不能 import btrader，所以那段数字只能写死在源码里。

    断言的是**数值**，不是注释里的常量名：以前这里查的是 "CREATE_NO_WINDOW" in 源码，
    命中的是旁边那行注释，常量名改了、数值写错了它照样绿。
    """
    flags = hex(updater.WINDOWS_HIDDEN)
    assert flags in updater._WINDOWS_SHIM
    assert f"creationflags={flags})" in updater._WINDOWS_SHIM  # 真的接在 Popen 的参数上
    assert "0x00000008" not in updater._WINDOWS_SHIM
    assert "0x00000008" != flags
    # 注释留着：看代码的人得知道这个数是什么，但注释不是来源。
    assert "CREATE_NO_WINDOW" in updater._WINDOWS_SHIM
    assert "CREATE_NEW_PROCESS_GROUP" in updater._WINDOWS_SHIM


def test_the_shim_flags_come_from_the_constants(monkeypatch):
    """垫子源码由常量拼出来：常量一改，垫片跟着变（不许是两份来源）。"""
    original = updater._windows_shim()
    monkeypatch.setattr(updater, "WINDOWS_HIDDEN", 0x04000200)
    changed = updater._windows_shim()
    assert hex(0x04000200) in changed
    assert hex(updater.CREATE_NO_WINDOW | updater.CREATE_NEW_PROCESS_GROUP) not in changed
    assert changed != original
    compile(changed, "<shim>", "exec")  # 拼出来的还是能跑的代码


def test_the_shim_source_is_built_from_a_function():
    """生成逻辑抽成函数才测得了「常量变了字符串跟着变」。"""
    assert callable(updater._windows_shim)
    assert updater._windows_shim() == updater._WINDOWS_SHIM


@_WIN_ONLY
def test_the_outer_shim_process_is_spawned_hidden(monkeypatch, data_dir):
    popen = _Popen(pid=1234)
    monkeypatch.setattr(updater.subprocess, "Popen", popen)
    monkeypatch.setattr(updater, "_request_shutdown", lambda *a, **k: None)
    monkeypatch.setattr(updater, "_wait_until", lambda fn, timeout, poll=0.0: True)
    ok, output = updater._restart_windows()
    assert ok is True and "1234" in output
    kwargs = popen.calls[0][1]
    assert kwargs["creationflags"] == updater.WINDOWS_HIDDEN
    assert kwargs["creationflags"] & updater.CREATE_NO_WINDOW
    assert kwargs["creationflags"] & 0x00000008 == 0
    info = kwargs["startupinfo"]
    assert info is not None and info.wShowWindow == updater.SW_HIDE


# ------------------------------- t108-② 重启日志别再被两个句柄互相写花
def test_the_shim_opens_the_log_by_itself_and_takes_over_stdout_stderr():
    """父进程把 open(path,'ab') 的句柄当 stdout 递过去、自己又另开一个追加句柄，
    两个句柄共享同一个文件位置——老进程那句「重启已经交给垫片（pid …）」就被垫片的
    输出盖掉一半（量测三轮都复现）。现在垫片自己 open 'a' 再 dup2 到 1/2，各写各的。"""
    shim = updater._WINDOWS_SHIM
    assert "os.dup2(_log.fileno(), 1)" in shim
    assert "os.dup2(_log.fileno(), 2)" in shim
    assert "open(log_path" in shim
    assert "sys.stdout = sys.stderr = _log" in shim
    compile(shim, "<shim>", "exec")  # 垫片是被 -c 直接跑的，语法坏了整条重启就没了


def test_the_parent_hands_the_shim_no_shared_log_handle(monkeypatch, data_dir):
    """stdout / stderr 都给 DEVNULL，日志路径只当参数传过去。"""
    popen = _Popen(pid=555)
    monkeypatch.setattr(updater.subprocess, "Popen", popen)
    monkeypatch.setattr(updater, "_request_shutdown", lambda *a, **k: None)
    monkeypatch.setattr(updater, "_wait_until", lambda fn, timeout, poll=0.0: True)
    ok, output = updater._restart_windows()
    assert ok is True and "555" in output  # 垫片没起来（Popen 抛 OSError）仍然要报失败
    args, kwargs = popen.calls[0]
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL
    assert str(updater._last_restart_log) in args
    # 父进程自己那几行照旧追加进去：它的句柄跟垫片的不是同一个。
    text = updater._last_restart_log.read_text(encoding="utf-8")
    assert "重启已经交给垫片" in text


def test_a_shim_that_cannot_be_started_is_still_a_failure(monkeypatch, data_dir):
    """父进程不再握着日志句柄之后，「垫片根本没起来」这条别丢。"""

    def refuse(args, **kwargs):
        raise OSError("系统找不到指定的文件")

    monkeypatch.setattr(updater.subprocess, "Popen", refuse)
    monkeypatch.setattr(updater, "_request_shutdown", lambda *a, **k: None)
    ok, output = updater._restart_windows()
    assert ok is False
    assert "拉新进程失败" in output
    assert "日志" in output


# ------------------------------- t108-③ 垫片报的 pid 是转发器，不是真正监听的那个
def test_the_shim_does_not_pretend_it_knows_the_listening_pid():
    """venv 的 python.exe 是个转发器，会再拉一层：Popen 拿到的 pid 不是监听的那个
    （uvicorn 自己打的是另一个）。改文案，别编数字。"""
    shim = updater._WINDOWS_SHIM
    assert "转发器" in shim
    assert "uvicorn" in shim
    assert "child.pid" in shim, "pid 还是 Popen 给的那个，不许假装能拿到真 pid"
