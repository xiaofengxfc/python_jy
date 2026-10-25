"""在线更新：检查 / 一键更新 / 回滚。全部离线：git、GitHub、重启都是替身。

这里钉的是几件不许退化的事：
  * 远端读不到时 state 里一个字都不许说「已是最新」；
  * 五种前置拒绝各返回各的状态码，且不建任务、不落盘；
  * 任一步失败就停在那一格，error 是那一步的原始输出；
  * 状态文件在重启之后还能读到 done；
  * 命令行永远是列表参数，出现 --force / reset --hard / clean 就算失败。
"""

from __future__ import annotations

import subprocess
import sys

import pytest
from fastapi import HTTPException

from btrader.core import updater
from btrader.web import routes_update


HEAD = "49aee73efba6a4ba49b868144d9d544dc2d5f107"

# autouse 的 offline 夹具把这些换成「报错」，测它们本身的用例把真的装回去。
REAL_RESTART = updater.restart_service
REAL_SHUTDOWN = updater._request_shutdown
REMOTE = "6ebef8311111111111111111111111111111111"
OLD = "1111111222222222222222222222222222222222"


class _Git:
    """git 替身：按「子命令拼起来的那一行」查表回答，顺带记下每一次调用。"""

    def __init__(self, replies: dict[str, tuple[int, str, str]] | None = None):
        self.replies = dict(replies or {})
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, repo, *args, timeout=30.0, **kwargs):
        self.calls.append(tuple(str(a) for a in args))
        key = " ".join(str(a) for a in args)
        code, out, err = self.replies.get(key, (0, "", ""))
        return subprocess.CompletedProcess(list(args), code, out, err)

    def names(self) -> list[str]:
        return [" ".join(call) for call in self.calls]


def _base_git(**overrides) -> _Git:
    replies = {
        "rev-parse HEAD": (0, HEAD, ""),
        "rev-parse --abbrev-ref HEAD": (0, "main", ""),
        "rev-parse --abbrev-ref --symbolic-full-name @{u}": (0, "xf/main", ""),
        "config --get remote.xf.url": (0, "https://github.com/xiaofengxfc/python_jy.git", ""),
        "fetch --all --prune": (0, "", ""),
        f"rev-list --count --left-right HEAD...{REMOTE}": (0, "6\t0", ""),
    }
    replies.update(overrides)
    return _Git(replies)


@pytest.fixture(autouse=True)
def data_dir(monkeypatch, tmp_path):
    """状态文件写进临时目录，别碰真实数据目录。"""
    monkeypatch.setenv("BITRADER_DATA_DIR", str(tmp_path))
    # 端口是模块级全局（启动路径会 set_web_port），别的测试文件可能设过它。
    monkeypatch.setattr(updater, "_web_port", None)
    return tmp_path


@pytest.fixture(autouse=True)
def offline(monkeypatch, data_dir):
    """默认把网络和重启都堵死，单条测试再按需要打开。"""

    def refuse(*args, **kwargs):
        raise AssertionError("测试里不许真的发请求或重启")

    monkeypatch.setattr(updater, "_github_commits", refuse)
    monkeypatch.setattr(updater, "_ls_remote", refuse)
    monkeypatch.setattr(updater, "restart_service", refuse)
    monkeypatch.setattr(updater, "_request_shutdown", lambda *a, **k: None)
    # 就地跑：不真的开线程，测试才能立刻读结果。
    monkeypatch.setattr(updater, "_spawn", lambda fn: fn())
    return refuse


def _github(branch: str = "main") -> list[dict]:
    return [
        {
            "sha": REMOTE,
            "short": REMOTE[:7],
            "title": "把杠杆记进设置",
            "date": "2026-10-23 15:45:00",
        }
    ]


# ------------------------------------------------------------------ 检查更新

def test_the_github_api_gives_the_sha_and_the_commit_titles(monkeypatch):
    monkeypatch.setattr(updater, "_git", _base_git())
    monkeypatch.setattr(updater, "_github_commits", _github)
    info = updater.collect_info()
    assert info["current_sha"] == HEAD
    assert info["current_short"] == HEAD[:7]
    assert info["branch"] == "main"
    assert info["remote_url"] == "https://github.com/xiaofengxfc/python_jy.git"
    assert info["remote_sha"] == REMOTE
    assert info["remote_short"] == REMOTE[:7]
    assert info["source"] == "github"
    assert info["ahead_by"] == 6
    assert info["behind_by"] == 0
    assert [c["title"] for c in info["commits"]] == ["把杠杆记进设置"]
    assert info["error"] == ""
    assert info["checked_at"]


def test_a_github_failure_falls_back_to_ls_remote(monkeypatch):
    def boom(branch):
        raise RuntimeError("HTTP 403 rate limit")

    monkeypatch.setattr(updater, "_git", _base_git())
    monkeypatch.setattr(updater, "_github_commits", boom)
    monkeypatch.setattr(updater, "_ls_remote", lambda repo, remote, branch: REMOTE)
    info = updater.collect_info()
    assert info["remote_sha"] == REMOTE
    assert info["source"].startswith("ls-remote")
    assert "标题" in info["source"]
    # ls-remote 只问得到 SHA，没有标题：空数组，不编。
    assert info["commits"] == []
    assert info["error"] == ""


def test_both_sources_failing_say_so_and_never_claim_up_to_date(monkeypatch):
    def boom(branch):
        raise RuntimeError("HTTP 403 rate limit")

    def also_boom(repo, remote, branch):
        raise RuntimeError("fatal: unable to access")

    monkeypatch.setattr(updater, "_git", _base_git())
    monkeypatch.setattr(updater, "_github_commits", boom)
    monkeypatch.setattr(updater, "_ls_remote", also_boom)
    info = updater.collect_info()
    assert info["remote_sha"] is None
    assert info["remote_short"] is None
    assert info["behind_by"] is None
    assert info["commits"] == []
    assert "HTTP 403 rate limit" in info["error"]
    assert "unable to access" in info["error"]
    assert "已是最新" not in info["error"]
    assert info["source"] == ""
    # 读不到远端的时候不许放行更新。
    refused = updater.refusal(info)
    assert refused is not None and refused[0] == 400
    assert "读不到远端" in refused[1]


def test_a_directory_that_is_not_a_repo_says_so(monkeypatch):
    monkeypatch.setattr(
        updater, "_git", _Git({"rev-parse HEAD": (128, "", "fatal: not a git repository")})
    )
    info = updater.collect_info()
    assert info["current_sha"] is None
    assert "not a git repository" in info["error"]


# ------------------------------------------------------------------ 前置拒绝

def _settle(monkeypatch, **overrides):
    monkeypatch.setattr(updater, "_git", _base_git(**overrides))
    monkeypatch.setattr(updater, "_github_commits", _github)


def _endpoint(path: str, method: str = "GET"):
    router = routes_update.build_update_router()
    for route in router.routes:
        if getattr(route, "path", "") == path and method in getattr(route, "methods", ()):
            return route.endpoint
    raise AssertionError(f"没有这个路由：{method} {path}")


def test_apply_refuses_while_an_engine_is_running(monkeypatch):
    _settle(monkeypatch, **{f"rev-list --count --left-right HEAD...{REMOTE}": (0, "0\t3", "")})
    monkeypatch.setattr(
        routes_update, "running_engines", lambda: ["自动交易 / 执行台"]
    )
    apply_route = _endpoint("/api/update/apply", "POST")
    with pytest.raises(HTTPException) as caught:
        apply_route()
    assert caught.value.status_code == 409
    assert "自动交易 / 执行台" in str(caught.value.detail)
    # 拒绝就是不建任务：状态还是 idle，盘上也没有任务。
    assert updater.read_state()["state"] == updater.STATE_IDLE


def test_apply_refuses_a_dirty_work_tree(monkeypatch):
    _settle(
        monkeypatch,
        **{f"rev-list --count --left-right HEAD...{REMOTE}": (0, "0\t3", ""), "status --porcelain": (0, " M src/x.py\n", "")}
    )
    monkeypatch.setattr(routes_update, "running_engines", lambda: [])
    with pytest.raises(HTTPException) as caught:
        _endpoint("/api/update/apply", "POST")()
    assert caught.value.status_code == 400
    assert "工作树" in str(caught.value.detail)
    assert updater.read_state()["state"] == updater.STATE_IDLE


def test_apply_refuses_when_already_up_to_date(monkeypatch):
    _settle(
        monkeypatch,
        **{f"rev-list --count --left-right HEAD...{REMOTE}": (0, "0\t0", ""), "status --porcelain": (0, "", "")}
    )
    monkeypatch.setattr(routes_update, "running_engines", lambda: [])
    with pytest.raises(HTTPException) as caught:
        _endpoint("/api/update/apply", "POST")()
    assert caught.value.status_code == 400
    assert "最新" in str(caught.value.detail)
    assert updater.read_state()["state"] == updater.STATE_IDLE


def test_apply_refuses_a_forked_history(monkeypatch):
    _settle(
        monkeypatch,
        **{f"rev-list --count --left-right HEAD...{REMOTE}": (0, "3\t2", ""), "status --porcelain": (0, "", "")}
    )
    monkeypatch.setattr(routes_update, "running_engines", lambda: [])
    with pytest.raises(HTTPException) as caught:
        _endpoint("/api/update/apply", "POST")()
    assert caught.value.status_code == 400
    assert "分叉" in str(caught.value.detail)
    assert updater.read_state()["state"] == updater.STATE_IDLE


def test_apply_refuses_an_unreadable_remote(monkeypatch):
    def boom(branch):
        raise RuntimeError("HTTP 403")

    def also_boom(repo, remote, branch):
        raise RuntimeError("fatal: unable to access")

    monkeypatch.setattr(
        updater, "_git", _base_git(**{"status --porcelain": (0, "", "")})
    )
    monkeypatch.setattr(updater, "_github_commits", boom)
    monkeypatch.setattr(updater, "_ls_remote", also_boom)
    monkeypatch.setattr(routes_update, "running_engines", lambda: [])
    with pytest.raises(HTTPException) as caught:
        _endpoint("/api/update/apply", "POST")()
    assert caught.value.status_code == 400
    assert "读不到远端" in str(caught.value.detail)


# ------------------------------------------------------------------ 跑一次更新

def _git_ready(**overrides) -> _Git:
    replies = {
        "rev-parse HEAD": (0, HEAD, ""),
        "rev-parse --abbrev-ref HEAD": (0, "main", ""),
        "rev-parse --abbrev-ref --symbolic-full-name @{u}": (0, "xf/main", ""),
        "config --get remote.xf.url": (0, "https://github.com/xiaofengxfc/python_jy.git", ""),
        "fetch --all --prune": (0, "From github.com:xiaofengxfc/python_jy\n", ""),
        f"rev-list --count --left-right HEAD...{REMOTE}": (0, "0\t3", ""),
        "status --porcelain": (0, "", ""),
        "merge --ff-only xf/main": (0, "Updating 49aee73..6ebef83\nFast-forward\n", ""),
        "diff --name-only 49aee73efba6a4ba49b868144d9d544dc2d5f107..HEAD": (0, "", ""),
    }
    replies.update(overrides)
    return _Git(replies)


def _run_apply(monkeypatch, git: _Git, *, run=None, which=None):
    monkeypatch.setattr(updater, "_git", git)
    monkeypatch.setattr(updater, "_github_commits", _github)
    monkeypatch.setattr(routes_update, "running_engines", lambda: [])
    monkeypatch.setattr(
        updater, "_run_command", run or (lambda args, **kwargs: (True, "ok\n"))
    )
    monkeypatch.setattr(updater.shutil, "which", which or (lambda name: None))
    monkeypatch.setattr(
        updater, "restart_service", lambda: (True, "已拉起新进程 pid=4321\n")
    )
    return _endpoint("/api/update/apply", "POST")()


def test_a_successful_update_hands_the_restart_over_and_writes_the_state_file(
    monkeypatch, data_dir
):
    """跑完一整轮：重启交出去之后**不写 done**，只留 running + restart_pending。

    以前这里顺手写 done，于是盘上同时有 done 和 restart_pending=true；而且那个 done
    是「拉起了就算成功」，服务有没有起来根本没人验。现在收尾在新进程里做
    （/api/health 2xx 才 done），见 tests/test_update_restart_confirm.py。
    """
    git = _git_ready()
    result = _run_apply(monkeypatch, git)
    assert result["job_id"]
    state = updater.read_state()
    assert state["state"] == updater.STATE_RUNNING
    assert state["restart_pending"] is True
    assert state["restart_deadline"], "期限要落到盘上，新进程靠它判断超时"
    assert state["host"] and state["port"], "地址与端口都要落盘"
    assert state["from_sha"] == HEAD
    assert state["to_sha"] == HEAD
    assert state["error"] == ""
    # 步骤名按顺序钉住：跳过任何一步都要在输出里说清，不能少一格。
    names = [step["name"] for step in state["steps"]]
    assert names[0] == "git fetch --all --prune"
    assert names[1] == "记录当前提交"
    assert names[2].startswith("git merge --ff-only")
    assert names[3] == "pip install -r requirements.txt"
    assert names[4] == "前端构建（web/ 有改动且本机有 npm）"
    assert names[5] == "重启服务"
    assert all(step["ok"] for step in state["steps"])
    assert all(isinstance(step["seconds"], (int, float)) for step in state["steps"])
    # 状态文件真的落在数据目录里，重启之后前端读的就是它。
    saved = (data_dir / "update-state.json").read_text(encoding="utf-8")
    assert "\"state\": \"running\"" in saved
    assert "\"restart_pending\": true" in saved
    assert updater.read_state()["steps"][3]["output"] == "ok\n"


def test_the_build_step_skips_on_a_server_without_npm(monkeypatch):
    git = _git_ready(
        **{"diff --name-only 49aee73efba6a4ba49b868144d9d544dc2d5f107..HEAD": (0, "web/src/App.tsx\n", "")}
    )
    _run_apply(monkeypatch, git)
    step = updater.read_state()["steps"][4]
    assert step["ok"] is True
    assert "服务器不需要构建" in step["output"]


def test_a_failed_merge_stops_there_with_the_raw_output(monkeypatch):
    git = _git_ready(**{"merge --ff-only xf/main": (1, "CONFLICT (content): merge conflict in a.py\n", "fatal: Not possible to fast-forward")})
    _run_apply(monkeypatch, git)
    state = updater.read_state()
    assert state["state"] == updater.STATE_FAILED
    assert state["error"].startswith("CONFLICT (content)")
    assert "Not possible to fast-forward" in state["error"]
    names = [step["name"] for step in state["steps"]]
    assert names[-1].startswith("git merge --ff-only")
    assert len(names) == 3  # fetch / 记录 / merge —— 后面的步骤一个都没跑
    assert state["steps"][-1]["ok"] is False
    assert not any("pip" in name for name in names)


def test_a_failed_pip_stops_before_the_restart(monkeypatch):
    git = _git_ready()
    restarts: list[int] = []
    monkeypatch.setattr(updater, "restart_service", lambda: restarts.append(1) or (True, ""))
    _run_apply(
        monkeypatch,
        git,
        run=lambda args, **kwargs: (False, "ERROR: No matching distribution found\n"),
    )
    state = updater.read_state()
    assert state["state"] == updater.STATE_FAILED
    assert "No matching distribution" in state["error"]
    assert [step["name"] for step in state["steps"]][-1] == "pip install -r requirements.txt"
    assert restarts == []


def test_the_update_never_uses_force_reset_or_clean(monkeypatch):
    git = _git_ready()
    _run_apply(monkeypatch, git)
    for call in git.names():
        assert "--force" not in call
        assert "reset" not in call
        assert "clean" not in call


# ------------------------------------------------------------------ 状态与回滚

def test_the_state_file_is_read_back_after_a_restart(monkeypatch):
    # 收尾的判据是「记下的那个 pid 真的不在了」，光「不是我这个 pid」不算：
    # 那是别人还活着的任务，任何进程读一次都不许把它收成 done。
    monkeypatch.setattr(updater, "_pid_alive", lambda pid: False)
    # 收尾还要自己确认服务活着：桩掉，别去连本机 8765（那是开发服务器）。
    monkeypatch.setattr(updater, "_port_free", lambda port=None, host=None: False)
    monkeypatch.setattr(
        updater, "_health_ok", lambda host=None, port=None: (True, "回了 200")
    )
    updater.write_state(
        {
            "job_id": "abc12345",
            "kind": "apply",
            "state": updater.STATE_RUNNING,
            "steps": [{"name": "git fetch --all --prune", "ok": True, "output": "", "seconds": 0.1}],
            "from_sha": OLD,
            "to_sha": HEAD,
            "error": "",
            "started_at": "2026-10-23 15:45:00",
            "finished_at": "",
            "pid": 4242,
            "restart_pending": True,
        }
    )
    state = updater.read_state()
    assert state["state"] == updater.STATE_DONE
    assert state["steps"][-1]["name"] == "重启服务"
    assert state["steps"][-1]["ok"] is True
    # 盘上的也一起收干净，下一次读还是 done。
    assert updater.read_state()["state"] == updater.STATE_DONE


def test_status_on_a_fresh_install_is_idle(monkeypatch):
    status = _endpoint("/api/update/status")()
    assert status["state"] == updater.STATE_IDLE
    assert status["steps"] == []
    assert status["from_sha"] is None and status["to_sha"] is None


def _finished_state(**overrides):
    state = {
        "job_id": "abc12345",
        "kind": "apply",
        "state": updater.STATE_DONE,
        "steps": [],
        "from_sha": OLD,
        "to_sha": HEAD,
        "error": "",
        "started_at": "2026-10-23 15:45:00",
        "finished_at": "2026-10-23 15:46:00",
        "pid": 4242,
        "restart_pending": False,
    }
    state.update(overrides)
    return state


def _rollback_payload(confirm: bool = True):
    return routes_update.RollbackPayload(confirm=confirm)


def test_rollback_only_works_after_a_finished_update(monkeypatch):
    rollback = _endpoint("/api/update/rollback", "POST")
    with pytest.raises(HTTPException) as caught:
        rollback(_rollback_payload())
    assert caught.value.status_code == 400
    assert "没有更新过" in str(caught.value.detail) or "更新" in str(caught.value.detail)


def test_rollback_needs_the_confirmation_flag(monkeypatch):
    updater.write_state(_finished_state())
    with pytest.raises(HTTPException) as caught:
        _endpoint("/api/update/rollback", "POST")(_rollback_payload(False))
    assert caught.value.status_code == 400
    assert "确认" in str(caught.value.detail)


def test_rollback_reverts_to_the_sha_the_update_started_from(monkeypatch):
    updater.write_state(_finished_state())
    git = _git_ready(**{"revert --no-edit " + OLD + "..HEAD": (0, "Reverted 3 commits\n", "")})
    monkeypatch.setattr(updater, "_git", git)
    monkeypatch.setattr(updater, "_run_command", lambda args, **kwargs: (True, "ok\n"))
    monkeypatch.setattr(updater.shutil, "which", lambda name: None)
    monkeypatch.setattr(updater, "restart_service", lambda: (True, ""))
    result = _endpoint("/api/update/rollback", "POST")(_rollback_payload())
    assert result["job_id"]
    state = updater.read_state()
    # 回滚也走同一条流水线：重启交出去之后停在 running，done 由新进程确认之后写。
    assert state["state"] == updater.STATE_RUNNING
    assert state["restart_pending"] is True
    assert state["to_sha"] == OLD
    assert state["from_sha"] == HEAD
    assert any("revert" in name for name in [step["name"] for step in state["steps"]])


# ------------------------------------------------------------------ 重启两条分支

def test_the_linux_restart_uses_sudo_with_a_list(monkeypatch):
    seen: list[list[str]] = []

    def fake_run(args, **kwargs):
        seen.append(list(args))
        assert "shell" not in kwargs or kwargs["shell"] is False
        return subprocess.CompletedProcess(list(args), 0, "restarted\n", "")

    monkeypatch.setattr(updater, "restart_service", REAL_RESTART)
    monkeypatch.setattr(updater.sys, "platform", "linux")
    monkeypatch.setattr(updater.subprocess, "run", fake_run)
    monkeypatch.setattr(updater, "_request_shutdown", lambda *a, **k: None)
    ok, output = updater.restart_service()
    assert ok is True
    # --no-block 不能少：不带它，systemctl 要等本单元停完才返回，而本单元就是发命令的
    # 这个进程——互锁到 TimeoutStopSec 被 SIGKILL，或者 -15 把成功记成失败。
    assert seen == [["sudo", "-n", "systemctl", "--no-block", "restart", "bitrader"]]
    assert "restarted" in output


def test_a_failed_linux_restart_reports_the_raw_output(monkeypatch):
    monkeypatch.setattr(updater, "restart_service", REAL_RESTART)
    monkeypatch.setattr(updater.sys, "platform", "linux")
    monkeypatch.setattr(
        updater.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 1, "", "sudo: a password is required\n"),
    )
    ok, output = updater.restart_service()
    assert ok is False
    assert "a password is required" in output


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 垫片与 STARTUPINFO 只有 Windows 上有")
def test_the_windows_restart_spawns_a_detached_process_then_exits_gracefully(monkeypatch):
    spawned: list[tuple[list[str], dict]] = []
    shut: list[str] = []

    def fake_popen(args, **kwargs):
        spawned.append((list(args), kwargs))
        return object()

    monkeypatch.setattr(updater, "restart_service", REAL_RESTART)
    monkeypatch.setattr(updater.sys, "platform", "win32")
    monkeypatch.setattr(updater.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(updater, "_request_shutdown", lambda *a, **k: shut.append("asked"))
    # 「自己放开端口」这一段在测试里直接算过：这里只验装配。真等待与新进程的
    # /api/health 确认在 tests/test_update_restart_confirm.py / test_update_hardening.py 里钉着。
    monkeypatch.setattr(updater, "_wait_until", lambda fn, timeout, poll=0.0: True)
    ok, output = updater.restart_service()
    assert ok is True
    assert len(spawned) == 1
    args, kwargs = spawned[0]
    assert args[0] == updater.sys.executable
    # 起的是「等端口释放 → 再拉起 run_web.py」的小垫片，不是当前进程自己。
    assert "run_web.py" in " ".join(args)
    assert kwargs.get("shell") in (None, False)
    assert kwargs.get("creationflags")
    assert shut == ["asked"]
    assert f"{updater.WEB_HOST}:" in output  # 报的是「哪个端口有人在听」
    assert "日志" in output  # 出问题时有地方可查


def test_the_shutdown_hook_is_preferred_over_a_signal(monkeypatch):
    called: list[int] = []
    monkeypatch.setattr(updater, "_request_shutdown", REAL_SHUTDOWN)
    updater.set_shutdown_hook(lambda: called.append(1))
    try:
        monkeypatch.setattr(
            updater.os, "kill", lambda *a: called.append(2)
        )
        updater._request_shutdown()
    finally:
        updater.set_shutdown_hook(None)
    assert called == [1]

