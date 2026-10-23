"""t93：清 update-state 的 .tmp 残渣，失败详情必须落盘、重启后第一次读就是这个失败任务。

量测 t87 的两条实测：进程内 8 线程一起点更新留下 2 个 `update-state.json.<pid>.<rand>.tmp`，
两个进程跑五轮留 1 个（每轮 `os.replace` 都成功，盘上没有半截 JSON）。这些临时文件
没人再管，只会越积越多——而「更新失败的原因」恰恰只能靠盘上那份记录：服务一旦重启，
内存里什么都没有了，`/api/update/status` 读的就是它。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from btrader.core import updater

ROOT = Path(__file__).resolve().parents[1]
HEAD = "49aee73efba6a4ba49b868144d9d544dc2d5f107"
RAW = "fatal: unable to access 'https://github.com/xiaofengxfc/python_jy.git/': Failed to connect\n"


@pytest.fixture(autouse=True)
def data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("BITRADER_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(updater, "_web_host", None)
    monkeypatch.setattr(updater, "_web_port", None)
    monkeypatch.setattr(updater, "_desktop_shell", "")
    monkeypatch.setattr(updater, "_last_restart_log", None)
    monkeypatch.setattr(updater, "_last_tmp_clean", 0.0)
    updater.set_shutdown_hook(None)
    yield tmp_path
    updater.set_shutdown_hook(None)


def _dead_pid() -> int:
    """一个确定已经死掉的 pid。"""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=60)
    return proc.pid


def _tmp_file(directory: Path, *parts: str) -> Path:
    path = directory / (".".join(parts))
    path.write_text("{}", encoding="utf-8")
    return path


def _age(path: Path, seconds: float) -> Path:
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))
    return path


def _tmp_leftovers(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.glob("update-state*.tmp"))


def _job_env(directory: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["BITRADER_DATA_DIR"] = str(directory)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT / "src"), env.get("PYTHONPATH") or ""]
    ).strip(os.pathsep)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


# ------------------------------------------------------------------ P3 临时文件
def test_a_write_that_cannot_replace_does_not_leave_its_tmp_behind(monkeypatch, data_dir):
    """os.replace 失败（Windows 上被别人读着就是 WinError 5）时，自己那份 .tmp 得删掉。"""
    updater.write_state({**updater._default_state(), "state": updater.STATE_DONE})
    before = updater.state_path().read_text(encoding="utf-8")

    def refuse(src, dst):
        raise OSError(5, "另一个程序正在使用此文件，进程无法访问。")

    monkeypatch.setattr(updater.os, "replace", refuse)
    updater.write_state({**updater._default_state(), "state": updater.STATE_FAILED})
    assert _tmp_leftovers(data_dir) == [], _tmp_leftovers(data_dir)
    # 换名没成，盘上还是上一份好状态——不是半个 JSON。
    assert updater.state_path().read_text(encoding="utf-8") == before


def test_a_replace_that_hits_a_sharing_violation_once_still_lands(monkeypatch, data_dir):
    """8 线程一起写时实测撞过 WinError 5：换名重试一次就能落盘，不该白丢这次写入。"""
    real = os.replace
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(5, "拒绝访问。")
        return real(src, dst)

    monkeypatch.setattr(updater.os, "replace", flaky)
    updater.write_state({**updater._default_state(), "state": updater.STATE_DONE, "job_id": "aa"})
    raw = json.loads(updater.state_path().read_text(encoding="utf-8"))
    assert raw["job_id"] == "aa", raw
    assert calls["n"] >= 2
    assert _tmp_leftovers(data_dir) == []


def test_stale_tmp_of_a_dead_process_is_removed_and_a_live_one_is_kept(data_dir):
    stale = _age(_tmp_file(data_dir, "update-state.json", str(_dead_pid()), "aaaa1111", "tmp"), 3600)
    other_name = _tmp_file(data_dir, "update-state.json", "notapid", "bbbb2222", "tmp")
    fresh = _tmp_file(data_dir, "update-state.json", str(_dead_pid()), "cccc3333", "tmp")
    unrelated = _age(_tmp_file(data_dir, "notify-spool.json", str(_dead_pid()), "dddd4444", "tmp"), 3600)
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        busy = _age(_tmp_file(data_dir, "update-state.json", str(live.pid), "eeee5555", "tmp"), 3600)
        removed = updater.clean_stale_state_tmp()
        assert stale in removed, removed
        assert not stale.exists()
        # 正在写的那个进程还活着：不许删
        assert busy.exists(), "写它的进程还活着，不能删"
        # 还没过期的、名字里认不出 pid 的、不是这个文件前缀的：都不许删
        assert fresh.exists()
        assert other_name.exists()
        assert unrelated.exists()
    finally:
        live.kill()
        live.wait(timeout=60)


def test_starting_a_job_cleans_the_leftovers_first(monkeypatch, data_dir):
    """start_job 一开始就扫一遍：上一轮被杀的进程留下的 .tmp 没人再管。"""
    stale = _age(_tmp_file(data_dir, "update-state.json", str(_dead_pid()), "aaaa1111", "tmp"), 3600)
    monkeypatch.setattr(updater, "_spawn", lambda fn: None)
    job_id = updater.start_job(updater.KIND_APPLY, {})
    assert job_id
    assert not stale.exists(), "start_job 应该把它清掉了"
    assert _tmp_leftovers(data_dir) == []
    # 清残渣不影响这一轮落盘
    assert json.loads(updater.state_path().read_text(encoding="utf-8"))["job_id"] == job_id


def test_a_job_that_fails_leaves_no_tmp_behind(monkeypatch, data_dir):
    monkeypatch.setattr(updater, "_spawn", lambda fn: fn())
    monkeypatch.setattr(updater, "_git_step", lambda repo, args, timeout=0.0: (False, RAW))
    job_id = updater.start_job(updater.KIND_APPLY, {})
    assert job_id
    state = json.loads(updater.state_path().read_text(encoding="utf-8"))
    assert state["state"] == updater.STATE_FAILED
    assert _tmp_leftovers(data_dir) == [], _tmp_leftovers(data_dir)


# ------------------------------------------------------------------ 失败详情落盘
def _run_failing_job(monkeypatch, *, port: int = 8799) -> str:
    """跑一条真的会失败的任务（真代码路径写盘），返回 job_id。"""
    updater.set_web_port(port)
    monkeypatch.setattr(updater, "_spawn", lambda fn: fn())
    monkeypatch.setattr(updater, "_git_step", lambda repo, args, timeout=0.0: (False, RAW))
    return updater.start_job(updater.KIND_APPLY, {})


def _status_from_a_new_process(directory: Path) -> dict:
    """新起一个进程读 /api/update/status：证明盘上有，不是上一个进程的内存里。"""
    script = (
        "import json\n"
        "from btrader.web import routes_update\n"
        "route = [r for r in routes_update.build_update_router().routes\n"
        "         if getattr(r, 'path', '') == '/api/update/status'][0]\n"
        "print(json.dumps(route.endpoint(), ensure_ascii=False))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        cwd=str(ROOT),
        env=_job_env(directory),
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_the_failure_details_survive_a_restart_and_first_read_is_that_job(monkeypatch, data_dir):
    job_id = _run_failing_job(monkeypatch)
    payload = _status_from_a_new_process(data_dir)
    # 不是 idle：新进程第一次读到的就是这个失败任务
    assert payload["state"] == updater.STATE_FAILED, payload
    assert payload["job_id"] == job_id
    # 每一步的原文都在盘上
    assert payload["steps"], payload
    bad = [step for step in payload["steps"] if not step["ok"]]
    assert bad, payload["steps"]
    assert "Failed to connect" in bad[-1]["output"], bad[-1]
    # 失败原因也在，而且是那一步的原文
    assert "Failed to connect" in payload["error"], payload["error"]
    # 端口与平台：手动命令要按真实端口拼，界面按平台决定给哪一条
    assert payload["port"] == 8799, payload
    assert payload["platform"] in ("windows", "linux", "darwin")
    assert payload["finished_at"], payload


def test_the_restart_failure_writes_the_manual_command_into_the_record(monkeypatch, data_dir):
    """重启那一步失败时服务已经停了：手动命令必须躺在盘上，用户手动起回来就能看到。"""
    updater.set_web_port(8799)
    monkeypatch.setattr(updater, "_spawn", lambda fn: fn())
    monkeypatch.setattr(updater, "_git_step", lambda repo, args, timeout=0.0: (True, "ok\n"))
    monkeypatch.setattr(updater, "_git_out", lambda repo, *args: HEAD)
    monkeypatch.setattr(updater, "_pip_step", lambda root: (True, "ok\n"))
    monkeypatch.setattr(updater, "_build_step", lambda root, state: (True, "ok\n"))
    monkeypatch.setattr(updater, "_upstream", lambda root: ("origin", "main"))
    monkeypatch.setattr(
        updater, "restart_service", lambda: (False, "重启失败：拉新进程失败：boom")
    )
    job_id = updater.start_job(updater.KIND_APPLY, {})
    payload = _status_from_a_new_process(data_dir)
    assert payload["job_id"] == job_id
    assert payload["state"] == updater.STATE_FAILED
    assert "手动拉起" in payload["error"], payload["error"]
    assert "--port 8799" in payload["error"], payload["error"]
    assert payload["manual_hint"] and "--port 8799" in payload["manual_hint"], payload
    # 重启没交出去，这个标记不能留在失败记录里
    assert payload["restart_pending"] is False, payload
    # 失败那一步的原文也在这条 payload 上
    assert [step for step in payload["steps"] if not step["ok"]], payload["steps"]


def test_a_terminal_state_never_keeps_restart_pending(data_dir):
    """done / failed 是终态，restart_pending 只能是 false，否则界面以为还在等新进程。"""
    for terminal in (updater.STATE_DONE, updater.STATE_FAILED):
        updater.write_state(
            {
                **updater._default_state(),
                "job_id": "abc12345",
                "state": terminal,
                "restart_pending": True,
                "restart_deadline": "2026-10-23T18:00:00+08:00",
                "port": 8799,
                "error": "重启没等到：……",
            }
        )
        state = updater.read_state()
        assert state["state"] == terminal
        assert state["restart_pending"] is False, terminal
        assert state["restart_deadline"] == ""
        assert state["port"] == 8799
        # 盘上的也一起收干净
        raw = json.loads(updater.state_path().read_text(encoding="utf-8"))
        assert raw["restart_pending"] is False, terminal
