"""t94：桌面端只查 GitHub Releases，一个 git 子进程都不起。

桌面版是 PyInstaller 冻结的 exe，里面没有 .git、没有 .venv、没有 npm：实测冻结包里
/api/update/info 立刻返回「读不到本地提交：[WinError 2]」，壳里还要再退到 git fetch
（FETCH_TIMEOUT=120），界面最长转两分钟。服务器那套 git fetch → merge → pip → build →
重启在桌面端一步都做不了，所以桌面端换成：查 Releases 最新 tag，与 btrader.__version__
按三元组比版本，只给下载链接——不下载、不自动装（没有代码签名，静默升级会被拦）。

全部离线：Releases 与 git 都是替身，冻结态直接 monkeypatch sys.frozen。
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error

import pytest
from fastapi import HTTPException

from pathlib import Path

from btrader import __version__ as APP_VERSION
from btrader.core import updater
from btrader.web import routes_update

ROOT = Path(__file__).resolve().parents[1]
HEAD = "49aee73efba6a4ba49b868144d9d544dc2d5f107"
REMOTE = "6ebef8311111111111111111111111111111111"
TAG_URL = "https://github.com/xiaofengxfc/python_jy/releases/tag/v0.6.1"
EXE_URL = "https://github.com/xiaofengxfc/python_jy/releases/download/v0.6.1/biTrader-Setup-0.6.1.exe"


@pytest.fixture(autouse=True)
def desktop_env(monkeypatch, tmp_path):
    """冻结标记、壳参数、Releases 缓存都是全局状态，一个用例一个样。"""
    monkeypatch.setenv("BITRADER_DATA_DIR", str(tmp_path))
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(updater, "_desktop_shell", "")
    monkeypatch.setattr(updater, "_ephemeral_port", False)
    monkeypatch.setattr(updater, "_release_cache", {"at": 0.0, "payload": None})
    yield tmp_path
    monkeypatch.delattr(sys, "frozen", raising=False)
    updater.set_desktop_shell("")
    updater.set_ephemeral_port(False)


def _release(
    tag: str = "v0.6.1",
    *,
    body: str = "## 修了什么\n- 桌面端改成查 Releases\n",
    assets: list[dict] | None = None,
    published: str = "2026-10-23T10:00:00Z",
) -> dict:
    """GitHub releases/latest 的回包，字段名照 GitHub 原样。"""
    return {
        "tag_name": tag,
        "body": body,
        "html_url": TAG_URL,
        "published_at": published,
        "assets": [] if assets is None else assets,
    }


def _exe_asset(url: str = EXE_URL, name: str = "biTrader-Setup-0.6.1.exe") -> dict:
    return {"name": name, "browser_download_url": url}


def _release_setup(name: str = "biTrader-Setup-0.6.1.exe") -> dict:
    return _exe_asset(EXE_URL, name)


def _no_subprocess(monkeypatch):
    """起子进程就直接炸：桌面端一次都不许碰 git。"""
    calls: list[tuple] = []

    def refuse(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("桌面端不许起子进程（git 那条路走不通）")

    monkeypatch.setattr(updater.subprocess, "run", refuse)
    monkeypatch.setattr(updater.subprocess, "Popen", refuse)
    monkeypatch.setattr(updater, "_git", refuse)
    monkeypatch.setattr(updater, "work_tree_state", refuse)
    return calls


def _stub_release(monkeypatch, payload: dict | None = None, error: str | None = None):
    """把 _latest_release 换成替身，记下被问了几次。"""
    seen: list[bool] = []

    def fake(force: bool = False):
        seen.append(bool(force))
        if error is not None:
            raise updater.ReleaseError(error)
        return payload if payload is not None else _release()

    monkeypatch.setattr(updater, "_latest_release", fake)
    return seen


class _FakeResponse:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self, *args):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _stub_urlopen(monkeypatch, *, payload: dict | None = None, error: BaseException | None = None):
    """把 urllib 换掉：_latest_release 自己那一层（UA、超时、404/403）才好验。"""
    seen: list[tuple] = []

    def fake(request, timeout=None):
        seen.append((request, timeout))
        if error is not None:
            raise error
        return _FakeResponse(payload if payload is not None else _release())

    monkeypatch.setattr(updater.urllib.request, "urlopen", fake)
    return seen


def _http_error(code: int, headers: dict | None = None) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://api.github.com/x", code, "boom", headers or {}, None
    )


# ------------------------------------------------------------------ 一、桌面态不碰 git
def test_a_frozen_build_reads_releases_and_never_starts_a_subprocess(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    calls = _no_subprocess(monkeypatch)
    _stub_release(monkeypatch, _release("v0.6.2", assets=[_exe_asset()]))
    info = updater.collect_info()
    assert info["supported"] is False
    assert info["mode"] == "desktop"
    assert info["version"] == APP_VERSION
    desktop = info["desktop"]
    assert desktop["latest"] == "0.6.2"
    assert desktop["tag"] == "v0.6.2"
    assert desktop["newer"] is True
    assert desktop["download_url"] == EXE_URL
    assert desktop["published_at"] == "2026-10-23T10:00:00Z"
    assert desktop["error"] == ""
    assert calls == [], "桌面端起过子进程"


def test_a_shell_launched_backend_counts_as_the_desktop_build(monkeypatch):
    """壳参数那条路 set_desktop_shell 已经有人喂了，别另起一套判据。"""
    updater.set_desktop_shell("--parent-pid 123")
    try:
        calls = _no_subprocess(monkeypatch)
        _stub_release(monkeypatch, _release("v0.6.2", assets=[_exe_asset()]))
        info = updater.collect_info()
        assert info["mode"] == "desktop"
        assert info["desktop"]["newer"] is True
        assert calls == []
    finally:
        updater.set_desktop_shell("")


def test_the_desktop_payload_only_reports_and_never_downloads(monkeypatch):
    """只给链接：不下载、不装、不建任务。"""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    _no_subprocess(monkeypatch)
    _stub_release(monkeypatch, _release("v0.6.2", assets=[_exe_asset()]))
    info = updater.collect_info()
    assert set(info) >= {"supported", "mode", "version", "platform", "checked_at", "desktop"}
    assert info["platform"] in ("windows", "linux", "darwin")
    assert info["checked_at"]
    # 桌面卡上用不到服务器那套字段，别让界面以为能点「立即更新」
    assert "current_sha" not in info


# ------------------------------------------------------------------ 二、版本按三元组比
def _desktop_tag(monkeypatch, tag: str) -> dict:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    _no_subprocess(monkeypatch)
    _stub_release(monkeypatch, _release(tag, assets=[_exe_asset()]))
    return updater.collect_info()["desktop"]


def test_a_newer_remote_tag_is_newer(monkeypatch):
    assert _desktop_tag(monkeypatch, "v0.6.2")["newer"] is True


def test_the_same_version_is_not_newer(monkeypatch):
    same = "v" + APP_VERSION
    desktop = _desktop_tag(monkeypatch, same)
    assert desktop["newer"] is False
    assert desktop["latest"] == APP_VERSION


def test_a_newer_local_build_is_not_newer(monkeypatch):
    assert _desktop_tag(monkeypatch, "v0.0.1")["newer"] is False


def test_versions_are_compared_as_numbers_not_strings(monkeypatch):
    """0.6.10 比 0.6.9 新：字符串比会把 '0.6.10' < '0.6.9' 判成旧版。"""
    app = updater._version_tuple("v0.6.10")
    assert app == (0, 6, 10)
    assert app > updater._version_tuple("v0.6.9")  # type: ignore[operator]
    other = updater._version_tuple("0.6.9")
    assert other is not None and other < app


@pytest.mark.parametrize("tag", ["latest", "0.6", "v0.6", "release-1", "", "v1.2.3.4", "v1.x.3"])
def test_a_tag_that_is_not_x_y_z_is_not_a_version(monkeypatch, tag):
    desktop = _desktop_tag(monkeypatch, tag)
    assert desktop["newer"] is None, "读不懂的 tag 不许当成最新，也不许当成有新版"
    assert desktop["error"], desktop
    assert "读不懂" in desktop["error"]
    assert desktop["download_url"] == EXE_URL  # 比不了版本，链接该给还是给


# ------------------------------------------------------------------ 三、失败分档
@pytest.mark.parametrize(
    ("code", "headers", "expected"),
    [
        (404, {}, "还没有发布过 Release"),
        (
            403,
            {"X-RateLimit-Reset": str(int(time.time()) + 1800), "X-RateLimit-Remaining": "0"},
            "限流",
        ),
        (500, {}, "500"),
    ],
)
def test_each_github_failure_says_what_happened(monkeypatch, code, headers, expected):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    _no_subprocess(monkeypatch)
    _stub_urlopen(monkeypatch, error=_http_error(code, headers))
    desktop = updater.collect_info()["desktop"]
    assert desktop["newer"] is None, "读不到远端时不许说「已最新」"
    assert expected in desktop["error"], desktop["error"]
    if code == 403:
        # 限流要说清额度什么时候回来：带上 X-RateLimit-Reset 换算出来的时刻
        assert "分钟" in desktop["error"], desktop["error"]
        reset = int(headers["X-RateLimit-Reset"]) if "X-RateLimit-Reset" in headers else 0
        assert f"{time.localtime(reset).tm_hour:02d}:" in desktop["error"], desktop["error"]


def test_a_timeout_is_reported_with_the_original_text(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    _no_subprocess(monkeypatch)
    _stub_urlopen(monkeypatch, error=urllib.error.URLError("timed out"))
    desktop = updater.collect_info()["desktop"]
    assert desktop["newer"] is None
    assert "timed out" in desktop["error"], desktop["error"]


def test_the_release_request_carries_a_user_agent_and_a_timeout(monkeypatch):
    seen = _stub_urlopen(monkeypatch, payload=_release("v0.6.2"))
    updater._latest_release()
    assert len(seen) == 1
    request, timeout = seen[0]
    assert timeout == updater.GITHUB_TIMEOUT
    headers = {key.lower(): value for key, value in request.header_items()}
    assert headers.get("user-agent") == updater.USER_AGENT
    assert updater.GITHUB_RELEASES.endswith("/releases/latest")


def test_the_release_answers_are_cached_for_five_minutes(monkeypatch):
    seen = _stub_urlopen(monkeypatch, payload=_release("v0.6.2"))
    first = updater._latest_release()
    second = updater._latest_release()
    assert first == second
    assert len(seen) == 1, "300 秒内同一个进程只该问一次 GitHub"
    updater._latest_release(force=True)
    assert len(seen) == 2, "refresh 要能跳过缓存"


def test_a_release_without_an_installer_says_so(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    _no_subprocess(monkeypatch)
    _stub_release(monkeypatch, _release("v0.6.2"))
    desktop = updater.collect_info()["desktop"]
    assert desktop["download_url"] is None
    assert "没有挂安装包" in desktop["download_note"], desktop


PORTABLE_URL = "https://x/biTrader-Portable-0.6.2.exe"


def _desktop_with_assets(monkeypatch, tag: str, assets: list[dict]) -> dict:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    _no_subprocess(monkeypatch)
    _stub_release(monkeypatch, _release(tag, assets=assets))
    return updater.collect_info()["desktop"]


def test_the_setup_installer_wins_whatever_the_upload_order(monkeypatch):
    """免安装版和安装版都挂在同一个 Release 上时，给的是安装版。

    实测（评审 P2-2）：以前取「第一个 .exe」，assets 顺序一换用户下到的就是目录版 exe——
    不建快捷方式、不写开始菜单，行为完全不同。
    """
    portable = {"name": "biTrader-Portable-0.6.2.exe", "browser_download_url": PORTABLE_URL}
    setup = {"name": "biTrader-Setup-0.6.2.exe", "browser_download_url": EXE_URL}
    for assets in ([portable, setup], [setup, portable]):
        desktop = _desktop_with_assets(monkeypatch, "v0.6.2", assets)
        assert desktop["download_url"] == EXE_URL, [a["name"] for a in assets]
        assert desktop["download_note"] == ""


def test_a_name_with_setup_beats_a_plain_exe(monkeypatch):
    desktop = _desktop_with_assets(
        monkeypatch,
        "v0.6.2",
        [
            {"name": "installer.exe", "browser_download_url": "https://x/installer.exe"},
            {"name": "biTrader-SETUP-latest.EXE", "browser_download_url": EXE_URL},
        ],
    )
    assert desktop["download_url"] == EXE_URL


def test_no_installer_at_all_says_how_many_exe_there_are(monkeypatch):
    """一个 Setup 都挑不出来就留空，并把「有几个 exe、自己挑」写清楚。"""
    desktop = _desktop_with_assets(
        monkeypatch,
        "v0.6.2",
        [
            {"name": "biTrader-Portable-0.6.2.exe", "browser_download_url": PORTABLE_URL},
            {"name": "biTrader-CLI-0.6.2.exe", "browser_download_url": "https://x/cli.exe"},
        ],
    )
    assert desktop["download_url"] is None
    assert "2 个 exe" in desktop["download_note"], desktop["download_note"]
    assert "自己挑" in desktop["download_note"], desktop["download_note"]


# ------------------------------------------------------------------ 四、路由
def _endpoint(path: str, method: str = "GET"):
    router = routes_update.build_update_router()
    for route in router.routes:
        if getattr(route, "path", "") == path and method in getattr(route, "methods", ()):
            return route.endpoint
    raise AssertionError(f"没有这个路由：{method} {path}")


def test_the_info_endpoint_skips_the_cache_when_refresh_is_one(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    _no_subprocess(monkeypatch)
    seen = _stub_urlopen(monkeypatch, payload=_release("v0.6.2", assets=[_release_setup()]))
    endpoint = _endpoint("/api/update/info")
    endpoint()
    endpoint()  # 缓存命中：不再问 GitHub
    assert len(seen) == 1, len(seen)
    endpoint(refresh=1)  # 重新检查：跳缓存
    assert len(seen) == 2, len(seen)


def test_apply_and_rollback_stay_refused_on_the_desktop(monkeypatch, tmp_path):
    """桌面端只给下载链接：两个 POST 一个都不许建任务、不许写状态文件。"""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    _no_subprocess(monkeypatch)
    monkeypatch.setattr(routes_update, "running_engines", lambda: [])
    state_file = tmp_path / "update-state.json"
    for path, payload in (
        ("/api/update/apply", None),
        ("/api/update/rollback", routes_update.RollbackPayload(confirm=True)),
    ):
        endpoint = _endpoint(path, "POST")
        with pytest.raises(HTTPException) as caught:
            endpoint() if payload is None else endpoint(payload)
        assert caught.value.status_code == 400, caught.value.detail
        assert "Releases" in str(caught.value.detail) or "桌面" in str(caught.value.detail)
    assert not state_file.exists(), "拒绝就是不建任务：状态文件都不该出现"


# ------------------------------------------------------------------ 五、服务器版不变
class _Git:
    def __init__(self, replies=None):
        self.replies = dict(replies or {})
        self.calls: list[str] = []

    def __call__(self, repo, *args, timeout=30.0, **kwargs):
        key = " ".join(str(a) for a in args)
        self.calls.append(key)
        code, out, err = self.replies.get(key, (0, "", ""))
        return subprocess.CompletedProcess(list(args), code, out, err)


def test_the_server_build_still_goes_through_git(monkeypatch):
    git = _Git(
        {
            "rev-parse HEAD": (0, HEAD, ""),
            "rev-parse --abbrev-ref HEAD": (0, "main", ""),
            "config --get remote.xf.url": (0, "https://github.com/xiaofengxfc/python_jy.git", ""),
            "fetch --all --prune": (0, "", ""),
            f"rev-list --count --left-right HEAD...{REMOTE}": (0, "0\t1", ""),
            "rev-parse --abbrev-ref --symbolic-full-name @{u}": (0, "xf/main", ""),
        }
    )
    monkeypatch.setattr(updater, "_git", git)
    monkeypatch.setattr(
        updater,
        "_github_commits",
        lambda branch: [{"sha": REMOTE, "short": REMOTE[:7], "title": "x", "date": ""}],
    )

    def refuse(*args, **kwargs):
        raise AssertionError("服务器版不该问 Releases")

    monkeypatch.setattr(updater, "_latest_release", refuse)
    info = updater.collect_info()
    assert info["supported"] is True
    assert info["mode"] == "repo"
    assert info["current_sha"] == HEAD
    assert info["remote_sha"] == REMOTE
    assert "rev-parse HEAD" in git.calls


# ------------------------------------------------------------------ 六、桌面判据只认壳参数
def test_port_zero_alone_is_not_a_desktop_build(monkeypatch):
    """P1-1：python -m btrader.web 的 --port 默认就是 0，它必须还是仓库版。

    以前 --port 0 被当成壳的证据，一个 python -m btrader.web 起的仓库版被判成桌面态，
    整张更新卡换成「已最新」，而那份 checkout 可能落后好几个提交——界面在骗人。
    """
    updater.set_desktop_shell("")
    updater.set_ephemeral_port(True)  # 就是 main() 在 --port 0 时做的事
    assert updater.is_desktop_build() is False
    git = _Git(
        {
            "rev-parse HEAD": (0, HEAD, ""),
            "rev-parse --abbrev-ref HEAD": (0, "main", ""),
            "config --get remote.xf.url": (0, "https://github.com/xiaofengxfc/python_jy.git", ""),
            "fetch --all --prune": (0, "", ""),
            f"rev-list --count --left-right HEAD...{REMOTE}": (0, "0\t3", ""),
            "rev-parse --abbrev-ref --symbolic-full-name @{u}": (0, "xf/main", ""),
        }
    )
    monkeypatch.setattr(updater, "_git", git)
    monkeypatch.setattr(
        updater,
        "_github_commits",
        lambda branch: [{"sha": REMOTE, "short": REMOTE[:7], "title": "x", "date": ""}],
    )

    def refuse(*args, **kwargs):
        raise AssertionError("仓库版不该问 Releases")

    monkeypatch.setattr(updater, "_latest_release", refuse)
    info = updater.collect_info()
    assert info["supported"] is True
    assert info["mode"] == "repo"
    assert info["behind_by"] == 3, "落后几个提交就得照实说，不能显示「已最新」"


def test_port_zero_alone_still_refuses_apply_but_tells_the_truth(monkeypatch, tmp_path):
    """--port 0 的进程照样不许在线更新，但不许再冒充「壳盯着这个 pid 与 stdin」。"""
    updater.set_desktop_shell("")
    updater.set_ephemeral_port(True)
    _no_subprocess(monkeypatch)
    monkeypatch.setattr(routes_update, "running_engines", lambda: [])
    state_file = tmp_path / "update-state.json"
    for path, payload in (
        ("/api/update/apply", None),
        ("/api/update/rollback", routes_update.RollbackPayload(confirm=True)),
    ):
        endpoint = _endpoint(path, "POST")
        with pytest.raises(HTTPException) as caught:
            endpoint() if payload is None else endpoint(payload)
        detail = str(caught.value.detail)
        assert caught.value.status_code == 400, detail
        assert "--port 0" in detail, detail
        assert "临时" in detail, detail
        assert "不是桌面壳" in detail, detail
        # 别再声称壳盯着 pid / stdin —— 这个进程根本没有壳
        assert "壳还盯着" not in detail, detail
    assert not state_file.exists()


def test_the_launcher_tells_updater_the_two_things_separately():
    from btrader.web import __main__ as web_main

    # 壳参数只有这两个：--port 0 是「让系统挑临时端口」，不是壳
    assert web_main.desktop_flags(parent_pid=0, watch_stdin=False) == []
    assert web_main.desktop_flags(parent_pid=777, watch_stdin=True) == [
        "--parent-pid 777",
        "--watch-stdin",
    ]
    assert web_main.ephemeral_port(0) is True
    assert web_main.ephemeral_port(8765) is False
    text = (ROOT / "src" / "btrader" / "web" / "__main__.py").read_text(encoding="utf-8")
    assert "set_ephemeral_port" in text, "临时端口要单独告诉 updater"


# ------------------------------------------------------------------ 七、P2-3 比不出新旧也要给链接
def test_an_unreadable_tag_still_hands_out_the_installer(monkeypatch):
    desktop = _desktop_with_assets(monkeypatch, "v0.6.1-beta", [_release_setup()])
    assert desktop["newer"] is None
    assert "读不懂" in desktop["error"]
    assert desktop["download_url"] == EXE_URL, "比不出新旧不等于没有东西可下"
    assert "比不出新旧" in desktop["download_note"], desktop["download_note"]
    assert "自己下" in desktop["download_note"], desktop["download_note"]


# ------------------------------------------------------------------ 八、P2-4 说清何时问过 GitHub
def test_the_card_says_when_github_was_actually_asked(monkeypatch):
    """缓存命中时 fetched_at 不许变，cached 要说是缓存——「查于 12:35」不能是假的。"""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    _no_subprocess(monkeypatch)
    seen = _stub_urlopen(monkeypatch, payload=_release("v0.6.2", assets=[_release_setup()]))
    first = updater.collect_info()
    second = updater.collect_info()
    assert len(seen) == 1, "300 秒内只该问一次 GitHub"
    assert first["checked_at"] and second["checked_at"], "响应时刻照旧给"
    assert first["desktop"]["cached"] is False
    assert second["desktop"]["cached"] is True
    assert first["desktop"]["fetched_at"] == second["desktop"]["fetched_at"], (
        "缓存命中时抓取时刻不该变"
    )
    # 强制刷新之后才算刚问过
    third = updater.collect_info(refresh=True)
    assert third["desktop"]["cached"] is False
    assert len(seen) == 2


# ------------------------------------------------------------------ 九、P3-3 403 不一定是限流
def test_a_403_without_a_spent_quota_is_not_called_rate_limiting(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    _no_subprocess(monkeypatch)
    _stub_urlopen(
        monkeypatch,
        error=_http_error(403, {"X-RateLimit-Remaining": "57"}),
    )
    desktop = updater.collect_info()["desktop"]
    assert "限流" not in desktop["error"], desktop["error"]
    assert "403" in desktop["error"], desktop["error"]
    assert "拒绝" in desktop["error"], desktop["error"]
    assert desktop["newer"] is None
