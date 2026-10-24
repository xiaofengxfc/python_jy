"""t89：重启确认挪进新进程、跨进程更新锁、platform、地址不写死。

评审 t88 的实测结论都在这儿：

* H1 Linux 用 `systemctl --no-block`：默认的 `systemctl restart` 要等本单元停完才返回，
  而本单元就是发命令的那个进程——老进程等 systemctl、systemctl 等老进程退出，互锁到
  `TimeoutStopSec=20` 被 SIGKILL 硬切（AGENTS 明令不许），或者 stop 时的 SIGTERM 先把
  systemctl 客户端杀掉、拿到 -15，把一次已经成功的重启记成「重启失败」。
* H2 端口通了不等于起来了：评审造过一个绑上 0.3 秒就关的 socket，`connect_ex` 被骗过。
* M1 进程内锁拦不住三个共用数据目录的进程（实测三轮全部 3/3 都建成了任务）。

全离线：只有回环 HTTP 与真子进程抢锁，不碰网络。
"""

from __future__ import annotations

import contextlib
import http.client
import http.server
import json
import os
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException

from btrader.core import updater
from btrader.web import routes_update

ROOT = Path(__file__).resolve().parents[1]
OTHER = "1111111222222222222222222222222222222222"


@pytest.fixture(autouse=True)
def data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("BITRADER_DATA_DIR", str(tmp_path))
    # 地址 / 端口 / 桌面壳标记都是模块级全局，一个用例一个样。
    monkeypatch.setattr(updater, "_web_host", None)
    monkeypatch.setattr(updater, "_web_port", None)
    monkeypatch.setattr(updater, "_desktop_shell", "")
    monkeypatch.setattr(updater, "_last_restart_log", None)
    updater.set_shutdown_hook(None)
    yield tmp_path
    updater.set_shutdown_hook(None)


def _iso(seconds_from_now: float) -> str:
    return (
        (datetime.now() + timedelta(seconds=seconds_from_now))
        .astimezone()
        .isoformat(timespec="seconds")
    )


def _write_state(**overrides):
    state = {
        "job_id": "abc12345",
        "kind": "apply",
        "state": updater.STATE_RUNNING,
        "steps": [],
        "from_sha": OTHER,
        "to_sha": None,
        "error": "",
        "started_at": "2026-10-23 15:45:00",
        "finished_at": "",
        "pid": 4242,
        "host": "127.0.0.1",
        "port": 8765,
        "restart_pending": False,
        "restart_deadline": "",
        "restart_log": "",
        "lock_note": "",
    }
    state.update(overrides)
    return updater.write_state(state)


def _endpoint(path: str, method: str = "GET"):
    router = routes_update.build_update_router()
    for route in router.routes:
        if getattr(route, "path", "") == path and method in getattr(route, "methods", ()):
            return route.endpoint
    raise AssertionError(f"没有这个路由：{method} {path}")


def _raw_state() -> dict[str, Any]:
    return json.loads(updater.state_path().read_text(encoding="utf-8"))


def _boom(*args, **kwargs):
    raise AssertionError("这一条不许走到这里")


# ------------------------------------------------------------------ 回环上的假服务
@contextlib.contextmanager
def _http_server(status: int):
    """真的 HTTP 服务：回指定的状态码。"""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - 标准库的命名
            body = json.dumps({"ok": status == 200}).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, name="fake-health", daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


@contextlib.contextmanager
def _slam_server():
    """绑上端口、谁来都连、连上就关：`connect_ex` 通，但一句话都不回答。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    port = int(sock.getsockname()[1])
    stop = threading.Event()

    def serve():
        while not stop.is_set():
            try:
                conn, _ = sock.accept()
            except OSError:
                return
            try:
                conn.close()
            except OSError:
                pass

    thread = threading.Thread(target=serve, name="slam", daemon=True)
    thread.start()
    try:
        yield port
    finally:
        stop.set()
        try:
            sock.close()
        except OSError:
            pass
        thread.join(5)


# ------------------------------------------------------------------ H1 Linux 只交出重启
def test_the_linux_restart_is_handed_to_systemd_without_waiting(monkeypatch):
    """`--no-block` 必须在，而且老进程不许在这儿等端口、等重启、请求自己退出。"""
    seen: list[list[str]] = []

    def fake_run(args, **kwargs):
        seen.append(list(args))
        assert kwargs.get("shell") in (None, False)
        return subprocess.CompletedProcess(list(args), 0, "", "")

    monkeypatch.setattr(updater.sys, "platform", "linux")
    monkeypatch.setattr(updater.subprocess, "run", fake_run)
    monkeypatch.setattr(updater, "_wait_until", _boom)  # 不再「等新进程起来」
    monkeypatch.setattr(updater, "_port_free", _boom)  # 也不再探端口
    monkeypatch.setattr(updater, "_request_shutdown", _boom)  # systemd 自己停我们
    ok, output = updater.restart_service()
    assert ok is True
    assert seen == [["sudo", "-n", "systemctl", "--no-block", "restart", "bitrader"]], seen
    # 输出要说清「交给谁了」和「确认在谁那儿做」
    assert "systemd" in output
    assert "/api/health" in output
    assert "新进程" in output


def test_a_failed_linux_restart_keeps_the_raw_output_and_writes_the_log(monkeypatch, data_dir):
    monkeypatch.setattr(updater.sys, "platform", "linux")
    monkeypatch.setattr(
        updater.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(
            args, 1, "", "sudo: a password is required\n"
        ),
    )
    ok, output = updater.restart_service()
    assert ok is False
    assert "a password is required" in output
    logs = sorted((data_dir / "logs").glob("restart-*.log"))
    assert logs, "这一次重启的日志没有落地"
    text = logs[0].read_text(encoding="utf-8")
    assert "a password is required" in text, text


class _Popen:
    def __init__(self, pid: int = 4321):
        self.calls: list[tuple[list[str], dict]] = []
        self.pid = pid

    def __call__(self, args, **kwargs):
        self.calls.append((list(args), kwargs))
        proc = subprocess.CompletedProcess(list(args), 0, "", "")
        proc.pid = self.pid
        return proc


def test_the_windows_restart_only_confirms_that_we_let_go(monkeypatch):
    """Windows 同一条语义：垫片拉起来了、本进程放手了，就算交出去了；
    「新进程起没起来」由新进程自己认，老进程不再等第二段。"""
    popen = _Popen(pid=777)
    waits: list[float] = []
    monkeypatch.setattr(updater.subprocess, "Popen", popen)
    monkeypatch.setattr(updater, "_request_shutdown", lambda *a, **k: None)
    monkeypatch.setattr(
        updater,
        "_wait_until",
        lambda fn, timeout, poll=0.0: waits.append(timeout) or True,
    )
    ok, output = updater.restart_service()
    assert ok is True
    assert len(waits) == 1, waits  # 只有「自己放开端口」那一段
    assert waits[0] == updater.RELEASE_TIMEOUT
    assert "/api/health" in output
    assert "手动" not in output


def test_a_failure_after_we_asked_ourselves_to_exit_goes_into_the_log(monkeypatch, data_dir):
    """确认失败时服务已经停着，/api/update/status 要等用户手动起回来才看得到——
    失败详情与手动命令必须同时进那一轮的 restart-*.log。"""
    popen = _Popen(pid=555)
    monkeypatch.setattr(updater.subprocess, "Popen", popen)
    monkeypatch.setattr(updater, "_request_shutdown", lambda *a, **k: None)
    monkeypatch.setattr(updater, "_wait_until", lambda fn, timeout, poll=0.0: False)
    ok, output = updater.restart_service()
    assert ok is False
    assert "run_web.py" in output  # 手动命令
    # 日志路径现在只当参数传给垫片（它自己 open 一份），父进程的 stdout 是 DEVNULL。
    assert popen.calls[0][1]["stdout"] is subprocess.DEVNULL
    log_path = Path(updater._last_restart_log)
    assert log_path.exists()
    text = log_path.read_text(encoding="utf-8")
    assert "run_web.py" in text, text
    assert "占在本进程手里" in text, text


# ------------------------------------------------------------------ H1 收尾挪到新进程
def test_the_old_process_does_not_write_done_after_handing_the_restart_over(monkeypatch):
    """重启交出去之后本进程马上就没：这里写 done 会留下 done + restart_pending=true。"""
    state = updater._default_state()
    state.update({"state": updater.STATE_RUNNING, "job_id": "job123", "pid": os.getpid()})
    monkeypatch.setattr(updater, "restart_service", lambda: (True, "已经交给 systemd"))
    ok, _output = updater._restart_step(state)
    assert ok is True
    raw = _raw_state()
    assert raw["state"] == updater.STATE_RUNNING
    assert raw["restart_pending"] is True
    assert raw["restart_deadline"], raw["restart_deadline"]
    assert raw["host"] and raw["port"]


def test_the_new_process_finishes_the_job_after_asking_its_own_health(monkeypatch):
    _write_state(restart_pending=True, pid=4242, restart_deadline=_iso(120))
    monkeypatch.setattr(updater, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(updater, "_port_free", lambda port=None, host=None: False)
    monkeypatch.setattr(
        updater,
        "_health_ok",
        lambda host=None, port=None: (True, "http://127.0.0.1:8765/api/health 回了 200"),
    )
    state = updater.read_state()
    assert state["state"] == updater.STATE_DONE
    assert state["steps"][-1]["name"] == updater.STEP_RESTART
    assert "/api/health" in state["steps"][-1]["output"]
    # L1：收尾时 restart_pending 一起清掉，不留 done + true
    assert state["restart_pending"] is False
    assert state["restart_deadline"] == ""
    raw = _raw_state()
    assert raw["state"] == updater.STATE_DONE
    assert raw["restart_pending"] is False


def test_a_restart_nobody_confirmed_within_the_deadline_is_a_failure(monkeypatch, data_dir):
    log = data_dir / "logs" / "restart-20261023-000000-abcdef.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("", encoding="utf-8")
    _write_state(
        restart_pending=True,
        pid=4242,
        restart_deadline=_iso(-5),
        restart_log=str(log),
    )
    monkeypatch.setattr(updater, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(updater, "_port_free", lambda port=None, host=None: True)
    monkeypatch.setattr(
        updater, "_health_ok", lambda host=None, port=None: (False, "127.0.0.1:8765 连不上")
    )
    state = updater.read_state()
    assert state["state"] == updater.STATE_FAILED
    assert state["restart_pending"] is False
    assert "手动拉起" in state["error"]
    assert "run_web.py" in state["error"]
    # 失败详情与手动命令同时写进那一轮的日志
    text = log.read_text(encoding="utf-8")
    assert "run_web.py" in text, text


def test_a_restart_still_inside_the_deadline_is_left_alone(monkeypatch):
    """期限内还没确认：既不假装成功，也不急着判死——下一次读再算。"""
    _write_state(restart_pending=True, pid=4242, restart_deadline=_iso(120))
    monkeypatch.setattr(updater, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(updater, "_port_free", lambda port=None, host=None: False)
    monkeypatch.setattr(
        updater, "_health_ok", lambda host=None, port=None: (False, "回了 503")
    )
    state = updater.read_state()
    assert state["state"] == updater.STATE_RUNNING
    assert state["restart_pending"] is True


# ------------------------------------------------------------------ H2 端口通了不算起来
def test_a_socket_that_listens_but_never_answers_is_not_a_live_service():
    """评审造的场景：绑上 0.3 秒就关的 socket，`connect_ex` 通，服务其实不存在。"""
    with _slam_server() as port:
        assert updater._port_free(port, "127.0.0.1") is False, "端口探测这里应该通"
        ok, detail = updater._health_ok("127.0.0.1", port)
        assert ok is False, detail


def test_health_only_accepts_a_2xx_answer():
    with _http_server(200) as port:
        ok, detail = updater._health_ok("127.0.0.1", port)
        assert ok is True, detail
        assert "200" in detail
    with _http_server(503) as port:
        ok, detail = updater._health_ok("127.0.0.1", port)
        assert ok is False
        assert "503" in detail


def test_a_listening_port_with_a_lying_health_endpoint_does_not_finish_the_job(monkeypatch):
    _write_state(restart_pending=True, pid=4242, restart_deadline=_iso(120))
    monkeypatch.setattr(updater, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(updater, "_port_free", lambda port=None, host=None: False)
    real = updater._health_ok
    with _http_server(500) as port:
        monkeypatch.setattr(
            updater, "_health_ok", lambda host=None, p=None: real("127.0.0.1", port)
        )
        state = updater.read_state()
    assert state["state"] == updater.STATE_RUNNING


# ------------------------------------------------------------------ M1 跨进程更新锁
def _child_tries_the_lock(data_dir: Path) -> str:
    """真起一个进程去抢锁，把它说的那一句原样带回来。"""
    script = (
        "import sys\n"
        "from btrader.core import updater\n"
        "try:\n"
        "    updater.acquire_update_lock('job-b')\n"
        "except updater.JobBusy as exc:\n"
        "    print('BUSY:' + str(exc))\n"
        "else:\n"
        "    print('GOT-IT')\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT / "src"), env.get("PYTHONPATH") or ""]
    ).strip(os.pathsep)
    env["BITRADER_DATA_DIR"] = str(data_dir)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        env=env,
    )
    return f"{proc.stdout or ''}{proc.stderr or ''}".strip()


def test_another_process_cannot_take_the_update_lock(data_dir):
    """三个进程共用一份数据目录：进程内锁什么都拦不住，文件锁才拦得住。"""
    path, note = updater.acquire_update_lock("job-a")
    assert path == updater.lock_path()
    assert path.exists()
    assert note == ""
    output = _child_tries_the_lock(data_dir)
    assert "BUSY" in output, output
    assert "另一个进程正在更新" in output, output
    updater.release_update_lock()
    assert not updater.lock_path().exists()
    output = _child_tries_the_lock(data_dir)
    assert "GOT-IT" in output, output


def test_a_lock_left_by_a_dead_process_is_taken_over_with_a_note(monkeypatch):
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait(timeout=60)
    updater.lock_path().write_text(
        json.dumps({"pid": dead.pid, "job_id": "old", "at": "2026-10-23T10:00:00"}),
        encoding="utf-8",
    )
    assert updater.lock_holder() is None, "pid 已经不在了，这锁是残留的"
    path, note = updater.acquire_update_lock("job-new")
    assert path.exists()
    assert "接管" in note, note
    assert str(dead.pid) in note
    holder = updater.lock_holder()
    assert holder is not None and int(holder["pid"]) == os.getpid()
    updater.release_update_lock()


def test_the_lock_file_semantics_are_create_exclusive(data_dir):
    """`O_CREAT|O_EXCL`：建不出来就是别人握着，不用 flock（Windows 上没有）。"""
    path = updater.lock_path()
    path.write_text("", encoding="utf-8")  # 建好了但还没落内容
    with pytest.raises(updater.JobBusy):
        updater.acquire_update_lock("job-x")
    assert path.read_text(encoding="utf-8") == ""


def test_start_job_refuses_when_another_process_holds_the_lock(monkeypatch):
    monkeypatch.setattr(updater, "_pid_alive", lambda pid: True)
    updater.lock_path().write_text(
        json.dumps({"pid": 4242, "job_id": "other", "at": "2026-10-23T10:00:00"}),
        encoding="utf-8",
    )
    with pytest.raises(updater.JobBusy) as caught:
        updater.start_job(updater.KIND_APPLY, {})
    assert "另一个进程正在更新" in str(caught.value)


def test_the_route_says_another_process_is_updating(monkeypatch):
    monkeypatch.setattr(updater, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(routes_update, "running_engines", lambda: [])
    updater.lock_path().write_text(
        json.dumps({"pid": 4242, "job_id": "other", "at": "2026-10-23T10:00:00"}),
        encoding="utf-8",
    )
    with pytest.raises(HTTPException) as caught:
        _endpoint("/api/update/apply", "POST")()
    assert caught.value.status_code == 409
    assert "另一个进程" in str(caught.value.detail)
    # 拒绝就是不建任务：状态文件都没写
    assert not updater.state_path().exists()


def test_the_job_releases_the_lock_when_it_ends(monkeypatch):
    updater.write_state({"state": updater.STATE_IDLE})
    monkeypatch.setattr(updater, "_git_step", lambda repo, args, timeout=0.0: (False, "停在这里"))
    monkeypatch.setattr(updater, "_spawn", lambda fn: fn())
    job_id = updater.start_job(updater.KIND_APPLY, {})
    assert job_id
    assert updater.read_state()["state"] == updater.STATE_FAILED
    assert not updater.lock_path().exists(), "跑完必须把锁放开"


# ------------------------------------------------------------------ M2 platform
@pytest.mark.parametrize(
    ("raw", "expected"),
    [("linux", "linux"), ("linux2", "linux"), ("win32", "windows"), ("darwin", "darwin")],
)
def test_the_backend_reports_the_platform(monkeypatch, raw, expected):
    monkeypatch.setattr(updater.sys, "platform", raw)
    assert updater.platform_name() == expected
    monkeypatch.setattr(
        updater,
        "_git",
        lambda repo, *args, **kwargs: subprocess.CompletedProcess(
            [], 128, "", "fatal: not a git repository"
        ),
    )
    info = updater.collect_info()
    assert info["platform"] == expected


def test_the_status_endpoint_also_carries_the_platform(monkeypatch):
    monkeypatch.setattr(updater.sys, "platform", "linux")
    status = _endpoint("/api/update/status")()
    assert status["platform"] == "linux"


# ------------------------------------------------------------------ M3 地址不写死
def test_the_restart_uses_the_real_bind(monkeypatch):
    updater.set_web_bind("0.0.0.0", 8799)
    popen = _Popen(pid=999)
    monkeypatch.setattr(updater.subprocess, "Popen", popen)
    monkeypatch.setattr(updater, "_request_shutdown", lambda *a, **k: None)
    probed: list[Any] = []
    # 谓词真的要叫一次，否则看不见它探的是哪个地址
    monkeypatch.setattr(updater, "_wait_until", lambda fn, timeout, poll=0.0: bool(fn() or True))
    real = updater._port_free
    monkeypatch.setattr(
        updater, "_port_free", lambda port=None, host=None: probed.append((host, port)) or False
    )
    ok, output = updater._restart_windows()
    assert ok is True
    args = popen.calls[0][0]
    assert "8799" in args, args
    assert "0.0.0.0" in args, args
    assert "8765" not in args and "127.0.0.1" not in args
    # 0.0.0.0 是「所有地址」，本机探它得走回环；报给人的却是真实地址
    assert probed == [("0.0.0.0", 8799)], probed
    assert "0.0.0.0:8799" in output
    assert "'--host', host" in updater._WINDOWS_SHIM or "--host', host" in updater._WINDOWS_SHIM


def test_the_manual_command_uses_the_real_bind():
    updater.set_web_bind("0.0.0.0", 8799)
    hint = updater._manual_hint(Path("D:/repo"))
    assert "--host 0.0.0.0" in hint, hint
    assert "--port 8799" in hint, hint
    assert "127.0.0.1" not in hint and "8765" not in hint


def test_probe_host_turns_a_wildcard_into_loopback():
    updater.set_web_bind("0.0.0.0", 8799)
    assert updater.probe_host() == "127.0.0.1"
    updater.set_web_bind("::", 8799)
    assert updater.probe_host() == "127.0.0.1"
    updater.set_web_bind("192.168.1.9", 8799)
    assert updater.probe_host() == "192.168.1.9"


def test_an_update_started_by_the_desktop_shell_is_refused(monkeypatch):
    """Electron 那条路径带着 --parent-pid / --watch-stdin / --port 0：更新完拉起的是
    裸 run_web.py，壳会失联。不许静默把壳弄丢。"""
    updater.set_desktop_shell("--parent-pid 1234、--watch-stdin")
    monkeypatch.setattr(routes_update, "running_engines", lambda: [])
    for path, payload in (
        ("/api/update/apply", None),
        ("/api/update/rollback", routes_update.RollbackPayload(confirm=True)),
    ):
        endpoint = _endpoint(path, "POST")
        with pytest.raises(HTTPException) as caught:
            endpoint() if payload is None else endpoint(payload)
        assert caught.value.status_code == 400
        assert "桌面壳" in str(caught.value.detail)
        assert "在应用里重启" in str(caught.value.detail)
    assert not updater.state_path().exists()


def test_the_desktop_shell_flags_are_read_from_the_command_line():
    from btrader.web import __main__ as web_main

    # --port 0 不是壳的证据：python -m btrader.web 的默认端口就是 0（见 t99 P1-1）
    assert web_main.desktop_flags(parent_pid=0, watch_stdin=False) == []
    assert web_main.desktop_flags(parent_pid=777, watch_stdin=True) == [
        "--parent-pid 777",
        "--watch-stdin",
    ]
    # 临时端口单独标记：它照样拦 apply，只是文案不说「壳」
    assert web_main.ephemeral_port(0) is True
    assert web_main.ephemeral_port(8765) is False
    text = (ROOT / "src" / "btrader" / "web" / "__main__.py").read_text(encoding="utf-8")
    assert "set_desktop_shell" in text, "启动路径要把这件事告诉 updater"
    assert "set_ephemeral_port" in text, "--port 0 要和壳参数分开告诉 updater"


# ------------------------------------------------------------------ L2 优雅退出的宽限
def test_the_server_gives_a_slow_request_a_bounded_grace_period():
    from btrader.web import __main__ as web_main

    server = web_main.build_server(lambda *a: None, host="127.0.0.1", port=8123)
    assert server.config.timeout_graceful_shutdown == web_main.GRACEFUL_TIMEOUT
    assert 0 < web_main.GRACEFUL_TIMEOUT <= 120
