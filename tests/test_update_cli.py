"""命令行更新（btrader update / run_update.py）的离线测试。

钉住四件事：
1. 服务在跑就交给服务（同一个任务、同一份闸），没在跑就落离线模式并说清楚；
2. 闸不过就退出码 2 且**什么都不做**（脏工作树、已有更新在跑、已经最新、桌面版）；
3. 离线跑的是 updater 里那一条流水线（run_job_sync），步骤边跑边念；
4. 回滚不加 --yes 什么都不做，数据目录和服务不一致时拒绝执行。
"""

from __future__ import annotations

import json

import pytest

from btrader import update_cli as cli
from btrader.core import updater


@pytest.fixture(autouse=True)
def _no_real_unit(monkeypatch):
    """测试机上的 /etc/systemd 不能参与判断。"""
    monkeypatch.setattr(cli, "_unit_data_dir", lambda: "")
    monkeypatch.setattr(updater, "is_desktop_build", lambda: False)
    return monkeypatch


def _info(**overrides):
    info = {
        "supported": True,
        "mode": "repo",
        "current_sha": "a" * 40,
        "current_short": "aaaaaaa",
        "branch": "main",
        "remote_sha": "b" * 40,
        "remote_short": "bbbbbbb",
        "ahead_by": 0,
        "behind_by": 1,
        "commits": [{"sha": "b" * 40, "message": "feat: 新的东西\n\n正文"}],
        "checked_at": "2026-10-24 23:00:00",
        "source": "github",
        "error": "",
        "platform": "linux",
    }
    info.update(overrides)
    return info


def _gates(monkeypatch, *, info=None, dirty=None, holder=None, refusal=None):
    monkeypatch.setattr(updater, "collect_info", lambda refresh=False, **kw: info or _info())
    monkeypatch.setattr(updater, "work_tree_state", lambda repo=None: (dirty or [], ""))
    monkeypatch.setattr(updater, "lock_holder", lambda: holder)
    monkeypatch.setattr(updater, "refusal", lambda payload: refusal)


@pytest.fixture
def offline(monkeypatch):
    """默认：服务没应答。"""
    monkeypatch.setattr(cli, "_service_online", lambda: False)
    return monkeypatch


# --------------------------------------------------------------- check / status
def test_check_prints_the_gap_and_the_next_command(offline, capsys):
    _gates(offline)
    assert cli.main(["check"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "本机：aaaaaaa" in out
    assert "远端：bbbbbbb" in out
    assert "落后 1 个提交" in out
    assert "feat: 新的东西" in out, "提交标题只取第一行"
    assert "sudo bash deploy/update.sh apply" in out


def test_check_says_it_cannot_count_instead_of_pretending(offline, capsys):
    _gates(offline, info=_info(behind_by=None, ahead_by=None, error="git fetch: timed out"))
    assert cli.main(["check"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "数不出来" in out
    assert "timed out" in out


def test_check_json_is_machine_readable(offline, capsys):
    _gates(offline)
    assert cli.main(["check", "--json"]) == cli.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["behind_by"] == 1
    assert payload["source"] == "github"


def test_check_on_a_desktop_build_points_at_the_release(offline, capsys):
    offline.setattr(
        updater,
        "collect_info",
        lambda refresh=False, **kw: {
            "supported": False,
            "mode": "desktop",
            "error": "",
            "desktop": {
                "current": "0.6.4",
                "latest": "0.6.5",
                "download_url": "https://example.com/setup.exe",
                "error": "",
            },
        },
    )
    assert cli.main(["check"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "桌面版" in out
    assert "https://example.com/setup.exe" in out


def test_status_reads_the_state_file(offline, capsys):
    offline.setattr(
        updater,
        "read_state",
        lambda: {
            "job_id": "abc12345",
            "kind": "apply",
            "state": updater.STATE_DONE,
            "steps": [{"name": "git fetch --all --prune", "ok": True, "output": "", "seconds": 1.0}],
            "from_sha": "a" * 40,
            "to_sha": "b" * 40,
        },
    )
    assert cli.main(["status"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "abc12345" in out
    assert "done" in out
    assert "git fetch" in out


# --------------------------------------------------------------- apply
def _record_run(monkeypatch, *, state=None):
    calls: list[dict] = []

    def fake(kind, info=None, *, on_step=None):
        calls.append({"kind": kind, "info": info})
        if on_step is not None:
            on_step({"name": "git fetch --all --prune", "ok": True, "output": "ok", "seconds": 0.2})
            on_step({"name": "重启服务", "ok": True, "output": "交出去了", "seconds": 0.0})
        return state or {
            "state": updater.STATE_DONE,
            "steps": [],
            "error": "",
            "manual_hint": "",
        }

    monkeypatch.setattr(updater, "run_job_sync", fake)
    return calls


def test_apply_falls_back_to_offline_when_the_service_is_down(offline, capsys):
    _gates(offline)
    calls = _record_run(offline)
    assert cli.main(["apply"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "服务没应答" in out
    assert "离线模式" in out
    assert "[ok] git fetch --all --prune" in out
    assert "更新成功" in out
    assert calls and calls[0]["kind"] == updater.KIND_APPLY


def test_apply_refuses_a_dirty_work_tree_without_touching_anything(offline, capsys):
    _gates(offline, dirty=[" M src/btrader/app.py"])
    calls = _record_run(offline)
    assert cli.main(["apply"]) == cli.EXIT_REFUSED
    assert not calls, "闸不过就不许建任务"
    err = capsys.readouterr().err
    assert "没提交的改动" in err
    assert "src/btrader/app.py" in err


def test_apply_refuses_while_another_update_holds_the_lock(offline, capsys):
    _gates(offline, holder={"pid": 4321, "job_id": "zzz"})
    calls = _record_run(offline)
    assert cli.main(["apply"]) == cli.EXIT_REFUSED
    assert not calls
    assert "另一个更新正在跑" in capsys.readouterr().err


def test_apply_refuses_when_there_is_nothing_to_update(offline, capsys):
    _gates(offline, refusal=(400, "已经是最新提交，没有可更新的。"))
    calls = _record_run(offline)
    assert cli.main(["apply"]) == cli.EXIT_REFUSED
    assert not calls
    assert "已经是最新提交" in capsys.readouterr().err


def test_offline_apply_refuses_when_the_service_is_actually_running(offline, capsys):
    offline.setattr(cli, "_service_online", lambda: True)
    _gates(offline)
    calls = _record_run(offline)
    assert cli.main(["apply", "--offline"]) == cli.EXIT_REFUSED
    assert not calls
    err = capsys.readouterr().err
    assert "去掉 --offline" in err, "引擎跑在服务里，绕过它就没有那道闸"


def test_a_failed_offline_apply_exits_one_and_prints_the_manual_hint(offline, capsys):
    _gates(offline)
    _record_run(
        offline,
        state={
            "state": updater.STATE_FAILED,
            "steps": [],
            "error": "ERROR: No matching distribution found",
            "manual_hint": "python run_web.py --host 127.0.0.1 --port 8765",
        },
    )
    assert cli.main(["apply"]) == cli.EXIT_FAILED
    err = capsys.readouterr().err
    assert "No matching distribution" in err
    assert "手动拉起" in err


def test_job_busy_is_a_refusal_not_a_crash(offline, capsys):
    _gates(offline)

    def busy(kind, info=None, *, on_step=None):
        raise updater.JobBusy("上一个更新还在跑（任务 zzz，pid 1），等它结束再来。")

    offline.setattr(updater, "run_job_sync", busy)
    assert cli.main(["apply"]) == cli.EXIT_REFUSED
    assert "等它结束再来" in capsys.readouterr().err


# --------------------------------------------------------------- 在线
def test_apply_online_hands_the_job_to_the_service(offline, capsys):
    offline.setattr(cli, "_service_online", lambda: True)
    seen: list[tuple[str, object]] = []

    def fake_request(path, *, payload=None, timeout=cli.HTTP_TIMEOUT):
        seen.append((path, payload))
        if path == "/api/update/apply":
            return {"job_id": "abc12345"}
        return {
            "state": updater.STATE_DONE,
            "steps": [{"name": "重启服务", "ok": True, "output": "服务已重启", "seconds": 0.0}],
        }

    offline.setattr(cli, "_request", fake_request)
    assert cli.main(["apply"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "交给服务" in out
    assert "[ok] 重启服务" in out
    paths = [item[0] for item in seen]
    assert "/api/update/apply" in paths
    assert "/api/update/status" in paths


def test_apply_online_turns_a_409_into_a_refusal(offline, capsys):
    offline.setattr(cli, "_service_online", lambda: True)

    def fake_request(path, *, payload=None, timeout=cli.HTTP_TIMEOUT):
        raise cli.CliError("自动交易正在跑：先停下来再更新。")

    offline.setattr(cli, "_request", fake_request)
    assert cli.main(["apply"]) == cli.EXIT_REFUSED
    assert "自动交易正在跑" in capsys.readouterr().err


def test_the_watch_reports_a_failed_job_with_the_raw_error(offline, capsys):
    offline.setattr(cli, "_service_online", lambda: True)

    def fake_request(path, *, payload=None, timeout=cli.HTTP_TIMEOUT):
        if path == "/api/update/apply":
            return {"job_id": "abc12345"}
        return {
            "state": updater.STATE_FAILED,
            "steps": [{"name": "pip install -r requirements.txt", "ok": False, "output": "boom", "seconds": 3.0}],
            "error": "boom",
            "manual_hint": "手动拉起：python run_web.py",
        }

    offline.setattr(cli, "_request", fake_request)
    assert cli.main(["apply"]) == cli.EXIT_FAILED
    err = capsys.readouterr().err
    assert "boom" in err
    assert "手动拉起" in err


# --------------------------------------------------------------- rollback
def test_rollback_without_yes_does_nothing(offline, capsys):
    calls = _record_run(offline)
    assert cli.main(["rollback"]) == cli.EXIT_REFUSED
    assert not calls
    assert "--yes" in capsys.readouterr().err


def test_rollback_offline_reverts_to_the_recorded_sha(offline, capsys):
    offline.setattr(
        updater,
        "read_state",
        lambda: {"state": updater.STATE_DONE, "from_sha": "a" * 40, "job_id": "abc12345"},
    )
    calls = _record_run(offline)
    assert cli.main(["rollback", "--yes"]) == cli.EXIT_OK
    assert calls and calls[0]["kind"] == updater.KIND_ROLLBACK
    assert calls[0]["info"] == {"target": "a" * 40}
    assert "回滚成功" in capsys.readouterr().out


def test_rollback_refuses_a_state_that_is_not_done(offline, capsys):
    offline.setattr(
        updater,
        "read_state",
        lambda: {"state": updater.STATE_FAILED, "from_sha": "a" * 40},
    )
    calls = _record_run(offline)
    assert cli.main(["rollback", "--yes"]) == cli.EXIT_REFUSED
    assert not calls
    assert "只有上一次更新成功了才能回滚" in capsys.readouterr().err


def test_rollback_refuses_a_sha_that_is_not_a_sha(offline, capsys):
    offline.setattr(
        updater,
        "read_state",
        lambda: {"state": updater.STATE_DONE, "from_sha": "HEAD~1; rm -rf /"},
    )
    calls = _record_run(offline)
    assert cli.main(["rollback", "--yes"]) == cli.EXIT_REFUSED
    assert not calls
    assert "不像提交 SHA" in capsys.readouterr().err


# --------------------------------------------------------------- 数据目录
def test_a_different_data_dir_is_refused(offline, capsys):
    """状态与锁都在数据目录里：两边不一样等于两条流水线互相看不见。"""
    offline.setattr(cli, "_unit_data_dir", lambda: "/var/lib/bitrader")
    _gates(offline)
    calls = _record_run(offline)
    assert cli.main(["apply"]) == cli.EXIT_REFUSED
    assert not calls
    err = capsys.readouterr().err
    assert "/var/lib/bitrader" in err
    assert "BITRADER_DATA_DIR" in err


def test_the_same_data_dir_passes(offline, monkeypatch):
    monkeypatch.setattr(cli, "_unit_data_dir", lambda: "/var/lib/bitrader")
    monkeypatch.setattr(cli, "paths", type("P", (), {"data_dir": staticmethod(lambda: __import__("pathlib").Path("/var/lib/bitrader"))}))
    assert cli._data_dir_problem() == ""


def test_no_command_prints_the_help(capsys):
    assert cli.main([]) == cli.EXIT_REFUSED
    assert "check" in capsys.readouterr().out
