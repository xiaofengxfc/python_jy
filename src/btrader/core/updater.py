"""检查更新与一键更新（方案 B：只在你点的时候动，绝不自动更）。

远端是 GitHub 上的公开仓库，仓库里一个 tag 都没有，所以版本比较一律按提交 SHA，
不比版本号。远端信息优先问 GitHub API（公开仓库免 token，还能拿到提交标题），失败
回落 git ls-remote（只有 SHA，没有标题）。两条都失败时 remote_sha 是 None、
error 是原始原因——**这时一个字都不许说「已是最新」**，因为根本不知道远端是什么。

更新是一条流水线：每一步记 name / ok / output / seconds，任一步失败就停在那一步，
error 是那一步的原始输出（原样贴给用户，不替他解释原因）。最后一步是重启，重启
会把本进程带走，所以状态先落盘（<数据目录>/update-state.json）：新进程读到「重启中、
还没收尾」那一条，就把状态收成 done，前端刷新后看到的仍是同一个任务。

绝对不用的命令：git reset --hard / git clean / 任何 --force / shell=True。
回滚也不改写历史：用 git revert 把内容退回原来的提交，命令里没有一个危险开关。
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .. import __version__ as APP_VERSION
from .. import paths

log = logging.getLogger(__name__)

# 仓库是公开的，接口不要 token；写死在这儿是因为这里只服务这一个仓库。
GITHUB_OWNER = "xiaofengxfc"
GITHUB_REPO = "python_jy"
GITHUB_API = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/commits"
GITHUB_PER_PAGE = 20
GITHUB_TIMEOUT = 10.0
USER_AGENT = "biTrader-updater"
# 桌面端只查 Releases：没有 .git、没有 venv、没有 npm，服务器那套一步都做不了。
GITHUB_RELEASES = (
    f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
)
# 未认证的 GitHub API 每小时只给 60 次：同一个进程里问过就记 5 分钟。
RELEASE_CACHE_SECONDS = 300.0
# 只认 v?X.Y.Z 三段纯数字。别的写法不猜：拿字符串比版本会把 0.6.10 判成比 0.6.9 旧。
VERSION_RE = re.compile(r"^[vV]?(\d+)\.(\d+)\.(\d+)$")

SERVICE_NAME = "bitrader"
WEB_PORT = 8765  # 拿不到真实端口时的兜底
WEB_HOST = "127.0.0.1"  # 拿不到真实地址时的兜底（--host 0.0.0.0 的实例不能按它重启）

HEALTH_PATH = "/api/health"
HEALTH_TIMEOUT = 2.0
HEALTH_ATTEMPTS = 3
HEALTH_POLL = 0.4
# 重启交出去之后，新进程要在这么久之内把状态收回来。systemd 那边 stop 最多等到
# TimeoutStopSec（仓库里那个 unit 是 20 秒），再给起进程、装依赖之后加载留足余量。
RESTART_CONFIRM_TIMEOUT = 180.0
LOCK_NAME = "update.lock"

GIT_TIMEOUT = 60.0
FETCH_TIMEOUT = 120.0
PIP_TIMEOUT = 900.0
NPM_TIMEOUT = 900.0
RESTART_TIMEOUT = 120.0
# 老进程只等一件事：自己把端口放开（之后就是新进程的事了）。
RELEASE_TIMEOUT = 30.0
# 垫片等「老进程放开端口」的时限（它跑在自己的进程里，不受我们退出影响）。
LISTEN_TIMEOUT = 90.0
LISTEN_POLL = 0.2

STATE_NAME = "update-state.json"
# 临时文件前缀就是状态文件名去掉后缀：update-state.json.<pid>.<rand>.tmp。
STATE_TMP_GLOB = "update-state*.tmp"
# 多久以前的临时文件算「没人管了」。只按时间删是不够的：那个进程可能还在慢慢写，
# 所以还要看名字里那个 pid 是不是已经不在了（见 clean_stale_state_tmp）。
STATE_TMP_GRACE = 300.0
# 读接口天天在轮询，残渣清理按这个间隔做一次就够。
TMP_CLEAN_INTERVAL = 60.0
# 换名撞上 Windows 的共享冲突（WinError 5：目标文件正被别人读着）时重试几次：
# 8 个线程一起写实测撞得到，重试一次就落盘了，不重试等于白丢这一次写入。
STATE_REPLACE_ATTEMPTS = 3
STATE_REPLACE_PAUSE = 0.05
STATE_IDLE = "idle"
STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_FAILED = "failed"

KIND_APPLY = "apply"
KIND_ROLLBACK = "rollback"

STEP_FETCH = "git fetch --all --prune"
STEP_REMEMBER = "记录当前提交"
STEP_MERGE = "git merge --ff-only"
STEP_REVERT = "git revert --no-edit"
STEP_PIP = "pip install -r requirements.txt"
STEP_BUILD = "前端构建（web/ 有改动且本机有 npm）"
STEP_RESTART = "重启服务"

# GitHub 这条路走不通时用 ls-remote：只有 SHA，没有标题，这一点要写在 source 里。
LS_REMOTE_SOURCE = "ls-remote（只读到远端 SHA，没有提交标题）"

# 子进程的窗口。**只有 CREATE_NO_WINDOW 能真的让控制台不出现**：
# DETACHED_PROCESS(0x00000008) 与它互斥，两者一起给时 Windows 会把 CREATE_NO_WINDOW
# 丢掉——以前这条重启路径给的就是 0x00000008，看起来「分离」了，屏幕上照样闪黑框。
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
# 重启垫片那两层用它：没有控制台，也就不会被老进程的控制台关闭连带杀掉。
WINDOWS_HIDDEN = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
# STARTUPINFO 里的两格：照字面隐藏窗口（有些启动器只看这两格）。
STARTF_USESHOWWINDOW = 0x00000001
SW_HIDE = 0

# 记下的起始提交得是这个样子才准拼进 git 命令（回滚用它）。
SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")

# 本进程实际绑的地址与端口，由启动路径（web/__main__.build_server）交给 set_web_bind。
# 以前一律写死 127.0.0.1:8765：在 8799 上跑的实例，垫片盯的还是 8765，于是拉起来的
# run_web.py 直接 Errno 10048（端口被占）死掉；`--host 0.0.0.0` 的实例更新完则只剩回环，
# 外面那些客户端全连不上——而重启那一步还「看起来成功」。
_web_host: str | None = None
_web_port: int | None = None


def set_web_bind(host: Any, port: Any) -> None:
    """启动路径告诉这里：我这个进程实际绑在哪个地址上。"""
    global _web_host
    text = str(host or "").strip()
    if text:
        _web_host = text
    set_web_port(port)


def set_web_port(port: Any) -> None:
    """只给端口（老调用点用它；地址仍是兜底值 127.0.0.1）。"""
    global _web_port
    try:
        value = int(port)
    except (TypeError, ValueError):
        return
    if 0 < value < 65536:
        _web_port = value


def web_port() -> int:
    """现在该盯着哪个端口。不知道就按 8765 兜底（状态里会写明是兜底值）。"""
    return _web_port or WEB_PORT


def web_host() -> str:
    """现在该在哪个地址上。不知道就按 127.0.0.1 兜底。"""
    return _web_host or WEB_HOST


def probe_host(host: Any = None) -> str:
    """从本机去探一个监听地址时该连哪儿。

    `0.0.0.0` / `::` 是「所有地址」，不是一个能连的目标，本机得走回环。
    """
    text = str(web_host() if host is None else host).strip()
    if text in ("", "0.0.0.0", "::", "*"):
        return WEB_HOST
    return text


def web_port_known() -> bool:
    """端口是不是从启动参数里拿到的。"""
    return _web_port is not None


def web_host_known() -> bool:
    """地址是不是从启动参数里拿到的。"""
    return _web_host is not None


def web_bind_known() -> bool:
    """地址和端口都是真的（不是兜底值）。"""
    return web_host_known() and web_port_known()


def _port_note() -> str:
    """用兜底值时得说一声，别让人以为它知道真实地址。"""
    if web_bind_known():
        return ""
    said: list[str] = []
    if not web_host_known():
        said.append(f"地址按兜底值 {WEB_HOST}")
    if not web_port_known():
        said.append(f"端口按兜底值 {WEB_PORT}")
    return "（没拿到本进程的真实地址，" + "、".join(said) + "）"


def platform_name() -> str:
    """给界面用的平台名：`windows` / `linux` / `darwin`。

    前端按它决定手动命令怎么写。以前后端从来没给过这个字段，界面只能猜浏览器 UA：
    服务器在 Linux、人坐在 Windows 前面时，界面教人敲的是 `cd /d` 和 venv 里的 exe。
    其余 Unix 一律按 linux 给（这台程序唯一支持的服务器就是 Linux）。
    """
    value = str(sys.platform or "")
    if value.startswith("win"):
        return "windows"
    if value == "darwin":
        return "darwin"
    return "linux"


# 桌面壳（Electron）拉起来的进程带着 --parent-pid / --watch-stdin / --port 0：壳盯着这个
# pid 与 stdin，进程一换壳就失联。这种进程不许在线更新（400，让人回应用里重启）。
_desktop_shell = ""


def set_desktop_shell(reason: Any) -> None:
    """启动路径告诉这里：我这个进程是桌面壳拉起来的，带着这几个参数。"""
    global _desktop_shell
    _desktop_shell = str(reason or "").strip()


def desktop_shell_reason() -> str:
    """是桌面壳启动的就返回那串参数（拿来说人话），不是就是空串。"""
    return _desktop_shell


# --port 0 是「让系统挑个临时端口」，不是桌面壳——python -m btrader.web 的 --port 默认就是 0。
# 这种进程照样不许在线更新（重启后新进程接不上那个临时端口），但拒绝文案不许冒充桌面壳。
# 所以它是**另一个**标记，不进 _desktop_shell（把两件事混成一件，仓库版会被判成桌面态）。
_ephemeral_port = False


def set_ephemeral_port(flag: Any) -> None:
    """启动路径告诉这里：我这个进程是 --port 0 起的（端口由系统临时分配）。"""
    global _ephemeral_port
    _ephemeral_port = bool(flag)


def ephemeral_port() -> bool:
    """这个进程的端口是系统临时分的吗。和「是不是桌面壳」是两件事。"""
    return _ephemeral_port


def now_iso() -> str:
    """带时区的当前时间，前端直接显示。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ------------------------------------------------------------------ 子进程
def _text(proc) -> str:
    """一条命令说过的所有话，原样带走：stdout 和 stderr 都要。"""
    parts = [str(proc.stdout or "").strip(), str(proc.stderr or "").strip()]
    return "\n".join(part for part in parts if part)


def _hidden_startupinfo():
    """STARTUPINFO(SW_HIDE)；别的平台给 None（那里传非 None 会直接 ValueError）。"""
    if sys.platform != "win32":
        return None
    # getattr 兜底：平台判据被 mock / 未来的解释器上少这个类时，退回 None，
    # 不要让重启这一步死在 AttributeError 上（那会变成「什么都没做却报失败」）。
    klass = getattr(subprocess, "STARTUPINFO", None)
    if klass is None:
        return None
    info = klass()
    info.dwFlags |= STARTF_USESHOWWINDOW
    info.wShowWindow = SW_HIDE
    return info


def _no_window() -> dict[str, Any]:
    """起子进程时额外带的 kwargs：Windows 上不给它控制台，别的平台一个都不加。

    用法是 subprocess.run([...], **_no_window())。

    为什么必须有这个：本程序常常自己就没有控制台（pythonw、桌面壳、更新时重启）。
    那种情况下每起一条 git.exe / pip / npm 子进程，Windows 都会给它新开一个控制台窗口——
    用户看到的就是更新时满屏闪的黑框。CREATE_NO_WINDOW 让它干脆不建控制台。

    注意**不能**顺手加 DETACHED_PROCESS：它与 CREATE_NO_WINDOW 互斥，一起给时后者被丢掉。
    """
    if sys.platform != "win32":
        return {}
    return {"creationflags": CREATE_NO_WINDOW, "startupinfo": _hidden_startupinfo()}


def _git(repo, *args, timeout: float = GIT_TIMEOUT):
    """跑一条 git 命令：列表参数、不开 shell、工作目录固定在仓库里。"""
    return subprocess.run(
        ["git", *[str(a) for a in args]],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        # git fetch / merge / status / revert：每一条都会闪黑框，一条都不例外。
        **_no_window(),
    )


def _git_out(repo, *args) -> str:
    """跑一条只读的 git 命令，要它的输出；失败就是空串。"""
    try:
        proc = _git(repo, *args)
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("git %s 读不出来：%s", " ".join(str(a) for a in args), exc)
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def _git_step(repo, args: list[str], timeout: float = GIT_TIMEOUT) -> tuple[bool, str]:
    """一步 git 命令：(成不成, 原始输出)。失败时输出就是 error 的原文。"""
    shown = " ".join(["git", *args])
    try:
        proc = _git(repo, *args, timeout=timeout)
    except FileNotFoundError:
        return False, "找不到 git：这台机器上没装 git。"
    except subprocess.TimeoutExpired:
        return False, f"{shown} 超时（{timeout:.0f} 秒）"
    except OSError as exc:
        return False, f"{shown} 起不来：{exc}"
    output = _text(proc)
    if proc.returncode != 0:
        return False, output or f"{shown} 退出码 {proc.returncode}"
    return True, output


def _run_command(
    args: list[str], *, cwd, timeout: float, env: dict[str, str] | None = None
) -> tuple[bool, str]:
    """跑一条命令并把它的话原样带回来。列表参数，不开 shell。"""
    shown = " ".join(str(a) for a in args)
    merged = dict(os.environ)
    merged.update(env or {})
    try:
        proc = subprocess.run(
            [str(a) for a in args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=merged,
            # pip / npm 这一层同理：父进程没有控制台时，子进程会被塞一个新窗口。
            **_no_window(),
        )
    except FileNotFoundError:
        return False, f"{shown}：找不到这个命令。"
    except subprocess.TimeoutExpired:
        return False, f"{shown} 超时（{timeout:.0f} 秒）"
    except OSError as exc:
        return False, f"{shown} 起不来：{exc}"
    output = _text(proc)
    if proc.returncode != 0:
        return False, output or f"{shown} 退出码 {proc.returncode}"
    return True, output


# ------------------------------------------------------------------ 远端
def _upstream(repo) -> tuple[str, str]:
    """(remote, branch)。没配 upstream 就退回「第一个带地址的 remote + 当前分支」，
    不猜 origin——这个仓库的远端叫 xf。"""
    ref = _git_out(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if "/" in ref:
        remote, _, branch = ref.partition("/")
        return remote, branch
    branch = _git_out(repo, "rev-parse", "--abbrev-ref", "HEAD")
    for name in _git_out(repo, "remote").split():
        if _git_out(repo, "config", "--get", f"remote.{name}.url"):
            return name, branch
    return "", branch


def _github_commits(branch: str) -> list[dict[str, Any]]:
    """GitHub API 的提交列表：第一个就是远端最新，标题取 message 的第一行。"""
    query = urllib.parse.urlencode({"sha": branch or "main", "per_page": GITHUB_PER_PAGE})
    request = urllib.request.Request(
        f"{GITHUB_API}?{query}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=GITHUB_TIMEOUT) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, list) or not payload:
        raise RuntimeError(f"GitHub 没给出提交列表：{str(payload)[:120]}")
    commits: list[dict[str, Any]] = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        sha = str(row.get("sha") or "")
        if not sha:
            continue
        commit = row.get("commit") if isinstance(row.get("commit"), dict) else {}
        message = str(commit.get("message") or "")
        author = commit.get("author") if isinstance(commit.get("author"), dict) else {}
        date = str(author.get("date") or "")
        commits.append(
            {
                "sha": sha,
                "short": sha[:7],
                "title": message.splitlines()[0].strip() if message.strip() else "",
                "date": date[:19].replace("T", " "),
            }
        )
    return commits


def _ls_remote(repo, remote: str, branch: str) -> str:
    """git ls-remote 只回答 SHA。问不到就抛，让调用方把原因记进 error。"""
    if not remote or not branch:
        raise RuntimeError("没有配置远端，或者当前不在分支上")
    ok, output = _git_step(
        repo, ["ls-remote", remote, f"refs/heads/{branch}"], timeout=FETCH_TIMEOUT
    )
    if not ok:
        raise RuntimeError(output)
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            return parts[0]
    raise RuntimeError(output or f"{remote} 上没有 {branch} 这个分支")


def _counts(repo, remote_sha: str, remote: str, branch: str) -> tuple[int | None, int | None, str]:
    """(领先, 落后, 读不出来的原因)。用 rev-list --left-right --count 数，不自己走图。"""
    candidates = [remote_sha]
    if remote and branch:
        candidates.append(f"{remote}/{branch}")
    reason = ""
    for ref in candidates:
        ok, output = _git_step(repo, ["rev-list", "--count", "--left-right", f"HEAD...{ref}"])
        if not ok:
            reason = output
            continue
        parts = output.split()
        if len(parts) != 2:
            reason = f"rev-list 的回答读不懂：{output}"
            continue
        try:
            return int(parts[0]), int(parts[1]), ""
        except ValueError:
            reason = f"rev-list 的回答读不懂：{output}"
    return None, None, reason or "本地没有远端那几个提交，数不出差距"


def is_desktop_build() -> bool:
    """这个进程是不是桌面端：PyInstaller 冻结包，或者 Electron 壳拉起来的后端。

    判据就这两条，别另写一套：壳参数那条路由 set_desktop_shell 在启动时喂过。
    **--port 0 不算**：那是 python -m btrader.web 的默认值，只说明端口是系统临时分的
    （见 ephemeral_port），一个普通的仓库版必须还能走 git 那条路。
    """
    if getattr(sys, "frozen", False):
        return True
    return bool(desktop_shell_reason())


class ReleaseError(RuntimeError):
    """问不到 Releases。message 已经是给人看的中文，原样带给界面。"""


def _rate_limit_note(value: Any) -> str:
    """把 X-RateLimit-Reset 的 epoch 秒说成人话；读不出来就空串。"""
    try:
        stamp = int(str(value).strip())
    except (TypeError, ValueError):
        return ""
    if stamp <= 0:
        return ""
    try:
        when = datetime.fromtimestamp(stamp).astimezone()
    except (OverflowError, OSError, ValueError):
        return ""
    wait = max(1, int(round((stamp - time.time()) / 60.0)))
    return f"，额度要到 {when:%H:%M} 才回来（约 {wait} 分钟后）"


def _error_body(exc: urllib.error.HTTPError) -> str:
    """HTTP 错误正文（GitHub 会说清是哪一条）。读不出来就退回状态说明。"""
    try:
        body = exc.read()
    except Exception:  # pragma: no cover - 读不回来不影响报错
        return str(exc.reason or exc)
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
    text = text.strip()
    return text[:400] or str(exc.reason or exc)


# 进程内缓存：{"at": 时间戳, "payload": GitHub 的回包}
_release_cache: dict[str, Any] = {"at": 0.0, "payload": None}


def _latest_release(force: bool = False) -> dict[str, Any]:
    """GitHub Releases 里最新那一条（原样回包）。

    force=True 跳过缓存（界面上「再查一次」用）。失败一律抛 ReleaseError，文案分三档：
    没发过 Release（404）、限流（403，带额度回来的时间）、其它照贴原文。
    """
    cached = _release_cache.get("payload")
    if (
        not force
        and isinstance(cached, dict)
        and time.time() - float(_release_cache.get("at") or 0.0) < RELEASE_CACHE_SECONDS
    ):
        return cached
    request = urllib.request.Request(
        GITHUB_RELEASES,
        headers={"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=GITHUB_TIMEOUT) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise ReleaseError(
                "这个仓库还没有发布过 Release（GitHub 回 404）：发一个 tag 之后这里才会有内容。"
            ) from None
        if exc.code == 403:
            headers = exc.headers or {}
            # 403 不都是限流：abuse detection、被封、令牌没权限都回 403。
            # 只有「额度真的用完了」（X-RateLimit-Remaining == 0）才说限流。
            if str(headers.get("X-RateLimit-Remaining", "")).strip() == "0":
                note = _rate_limit_note(headers.get("X-RateLimit-Reset"))
                raise ReleaseError(
                    f"GitHub 限流了（403，未认证请求每小时 60 次，额度已经用完）{note}。"
                    f"原话：{_error_body(exc)}"
                ) from None
            raise ReleaseError(
                f"GitHub 拒绝了这次请求（403）：{_error_body(exc)}"
            ) from None
        raise ReleaseError(f"问不到 GitHub Releases（HTTP {exc.code}）：{_error_body(exc)}") from None
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        # 超时、DNS、断网：原文照贴，不替用户编原因。
        raise ReleaseError(f"问不到 GitHub Releases：{exc}") from None
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise ReleaseError(f"GitHub Releases 的回包读不懂：{exc}") from None
    if not isinstance(payload, dict):
        raise ReleaseError(f"GitHub Releases 的回包不是一条 Release：{raw[:200]}")
    _release_cache.update({"at": time.time(), "payload": payload})
    return payload


def _version_tuple(text: Any) -> tuple[int, int, int] | None:
    """v0.6.10 → (0, 6, 10)。只认 v?X.Y.Z，别的读不懂返回 None（不猜、不补位）。"""
    match = VERSION_RE.match(str(text or "").strip())
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _pick_installer(release: dict[str, Any], version: str = "") -> tuple[str | None, str]:
    """(安装包地址, 给用户的一句说明)。

    先认 `biTrader-Setup-<版本>.exe`（不分大小写），再认名字里带 setup 的 .exe。
    都不认就直接留空并说清有几个 exe——**不退回「第一个 .exe」**：同一个 Release 同时挂
    免安装版和安装版时，上传顺序一换，用户下到的就是目录版 exe（不建快捷方式、不写开始
    菜单，行为完全不同）。挑不出来比挑错强。
    """
    assets = release.get("assets")
    if not isinstance(assets, list):
        return None, ""
    exe: list[tuple[str, str]] = []
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name") or "")
        url = str(asset.get("browser_download_url") or "")
        if url and name.lower().endswith(".exe"):
            exe.append((name, url))
    if not exe:
        return None, "这个 Release 没有挂安装包：只能自己去 Release 页面取。"
    wanted = f"bitrader-setup-{version}.exe".lower() if version else ""
    if wanted:
        for name, url in exe:
            if name.lower() == wanted:
                return url, ""
    for name, url in exe:
        if "setup" in name.lower():
            return url, ""
    return None, (
        f"这个 Release 里挂了 {len(exe)} 个 exe，看不出哪个是安装包：去 Release 页面自己挑。"
    )


def _release_fetched(force: bool = False) -> tuple[dict[str, Any], float, bool]:
    """(GitHub 回包, 真正问它的那一刻的 epoch, 是不是缓存命中)。

    缓存命中的回包是几分钟前抓的：卡片上必须说得清「什么时候问的」，不然「查于 12:35」
    其实写着 12:31 抓的东西，正是骗人那一类。
    """
    cached = _release_cache.get("payload")
    at = float(_release_cache.get("at") or 0.0)
    if not force and isinstance(cached, dict) and time.time() - at < RELEASE_CACHE_SECONDS:
        return cached, at, True
    payload = _latest_release(force=True)
    stamp = float(_release_cache.get("at") or 0.0) or time.time()
    return payload, stamp, False


def desktop_update_info(force: bool = False) -> dict[str, Any]:
    """桌面端的更新信息：只查 GitHub Releases，一个 git 子进程都不起。

    桌面端没有 .git、没有 .venv、没有 npm，服务器那套 fetch → merge → pip → build → 重启
    一步都做不了。这里只回答三件事：远端最新是哪个版本、你的是不是旧了、安装包在哪儿。
    **只报不装**：没有代码签名，静默升级会被 SmartScreen 拦。

    newer 一定是三元组比出来的：读不懂就看不出新旧，那时给 None 并附 error——
    绝不说「已是最新」，但**该给的下载链接照给**（比不出新旧不等于没东西可下）。

    时间戳分两个：`checked_at` 是这次响应的时间，`fetched_at` 是真正去问 GitHub 的那一刻，
    `cached` 说明这一次是不是吃的 300 秒缓存。卡片写「查于 HH:MM」时要用 fetched_at。
    """
    current = str(APP_VERSION)
    checked_at = now_iso()
    desktop: dict[str, Any] = {
        "current": current,
        "latest": None,
        "tag": None,
        "newer": None,
        "download_url": None,
        "download_note": "",
        "published_at": None,
        "notes": "",
        "fetched_at": checked_at,
        "cached": False,
        "error": "",
    }
    info: dict[str, Any] = {
        "supported": False,
        "mode": "desktop",
        "version": current,
        "platform": platform_name(),
        "checked_at": checked_at,
        "desktop": desktop,
    }
    try:
        release, fetched, cached = _release_fetched(force=force)
    except ReleaseError as exc:
        desktop["error"] = str(exc)
        return info
    desktop["cached"] = cached
    desktop["fetched_at"] = (
        datetime.fromtimestamp(fetched).astimezone().isoformat(timespec="seconds")
    )
    tag = str(release.get("tag_name") or "").strip()
    desktop["tag"] = tag or None
    desktop["notes"] = str(release.get("body") or "")
    desktop["published_at"] = str(release.get("published_at") or "") or None
    remote = _version_tuple(tag)
    version = tag[1:] if tag[:1] in ("v", "V") else tag
    desktop["download_url"], note = _pick_installer(release, version)
    desktop["download_note"] = note
    if remote is None:
        desktop["error"] = (
            f"远端 tag 读不懂（{tag or '空'}）：只认 vX.Y.Z 这种写法，这次比不了版本。"
        )
        if desktop["download_url"]:
            desktop["download_note"] = (
                "比不出新旧，这个 Release 挂着安装包，可以自己下：" + note
                if note
                else "比不出新旧，这个 Release 挂着安装包，可以自己下。"
            )
        return info
    desktop["latest"] = version
    local = _version_tuple(current)
    if local is None:
        desktop["error"] = f"本地版本号读不懂（{current}）：只认 X.Y.Z 这种写法，这次比不了版本。"
        return info
    desktop["newer"] = remote > local
    return info


def collect_info(repo=None, refresh: bool = False) -> dict[str, Any]:
    """本地与 GitHub 的差距。读不到远端就说读不到，绝不说「已是最新」。

    桌面端（冻结包 / 壳拉起来的后端）走另一条路：那里没有 .git，git 一个子进程都不起，
    直接查 Releases 比版本号。界面只认 `supported` / `mode` 这两个判据。
    """
    if is_desktop_build():
        return desktop_update_info(force=refresh)
    root = Path(repo) if repo else paths.project_root()
    info: dict[str, Any] = {
        # 界面按这两个字段决定给哪一张卡：服务器版能在线更新，桌面版只能给下载链接。
        "supported": True,
        "mode": "repo",
        "current_sha": None,
        "current_short": None,
        "branch": None,
        "remote_url": None,
        "remote_sha": None,
        "remote_short": None,
        "ahead_by": None,
        "behind_by": None,
        "commits": [],
        "checked_at": now_iso(),
        "source": "",
        "error": "",
        # 界面按平台给手动命令。以前没有这个字段，界面只能猜浏览器 UA：服务器在 Linux、
        # 人坐在 Windows 前面时，教人敲的是 cd /d 和 venv 里的 exe。
        "platform": platform_name(),
    }
    try:
        head = _git(root, "rev-parse", "HEAD")
    except (OSError, subprocess.SubprocessError) as exc:
        info["error"] = f"读不到本地提交：{exc}"
        return info
    if head.returncode != 0:
        info["error"] = f"读不到本地提交：{_text(head) or '不是 git 仓库'}"
        return info
    info["current_sha"] = (head.stdout or "").strip()
    info["current_short"] = info["current_sha"][:7]
    info["branch"] = _git_out(root, "rev-parse", "--abbrev-ref", "HEAD") or None
    remote, branch = _upstream(root)
    if remote:
        info["remote_url"] = _git_out(root, "config", "--get", f"remote.{remote}.url") or None
    # 远端那几个提交得先在本地，rev-list 才数得出来。fetch 失败不致命，下面记下原因。
    fetched, fetch_output = _git_step(
        root, ["fetch", "--all", "--prune"], timeout=FETCH_TIMEOUT
    )
    errors: list[str] = []
    if not fetched:
        # fetch 失败只进日志：apply 自己会再 fetch 一次，那一步失败会在流水线里停下来。
        log.warning("检查更新时 git fetch 失败：%s", fetch_output)
        errors.append(f"git fetch：{fetch_output}")
    commits: list[dict[str, Any]] = []
    try:
        commits = _github_commits(branch or "main")
    except (urllib.error.URLError, OSError, ValueError, RuntimeError) as exc:
        errors.append(f"GitHub API：{exc}")
    if commits:
        info["source"] = "github"
        info["commits"] = commits
        info["remote_sha"] = commits[0]["sha"]
    else:
        try:
            info["remote_sha"] = _ls_remote(root, remote, branch)
            info["source"] = LS_REMOTE_SOURCE
        except RuntimeError as exc:
            errors.append(f"git ls-remote：{exc}")
    if info["remote_sha"]:
        info["remote_short"] = info["remote_sha"][:7]
        ahead, behind, why = _counts(root, info["remote_sha"], remote, branch)
        info["ahead_by"], info["behind_by"] = ahead, behind
        if why:
            errors.append(f"git rev-list：{why}")
    # 只有「读不到远端」和「数不出差距」才算 error：这两件事一问出来就不知道有没有
    # 更新。降级（GitHub 挂了改用 ls-remote、fetch 没成功）不算——那会把界面上的
    # 更新按钮白白变灰，而实际上一切都还能用，source 里也写清了来源。
    if not info["remote_sha"] or info["behind_by"] is None or info["ahead_by"] is None:
        info["error"] = "；".join(errors)
    return info


def refusal(info: dict[str, Any]) -> tuple[int, str] | None:
    """能不能更新。(状态码, 文案) 或者 None（可以更新）。"""
    if not info.get("current_sha"):
        reason = info.get("error") or "这个目录不是 git 仓库"
        return 400, f"读不到本地提交：{reason}。"
    if not info.get("remote_sha"):
        reason = info.get("error") or "远端没有回应"
        return 400, (
            f"读不到远端提交：{reason}。现在没法判断有没有更新，"
            "先手动看一眼 git 和网络，再回来点检查。"
        )
    behind = info.get("behind_by")
    ahead = info.get("ahead_by")
    if behind is None or ahead is None:
        reason = info.get("error") or "git rev-list 没有结果"
        return 400, f"数不出本地和远端的差距：{reason}。"
    if behind == 0 and ahead == 0:
        return 400, "已经是最新提交，没有可更新的。"
    if behind == 0:
        return 400, f"远端没有新提交（本地还领先 {ahead} 个，先把这些提交 push 上去）。"
    if ahead > 0:
        return 400, (
            f"本地和远端各有新提交（本地领先 {ahead} 个、落后 {behind} 个），这是一次分叉："
            "程序只做快进合并，分叉得手动处理——先把本地提交 push 或 rebase 到远端，再回来点更新。"
        )
    return None


def work_tree_state(repo=None) -> tuple[list[str], str]:
    """(没提交的改动, 读不出来的原因)。有改动就不许更新：合并会撞上它们。"""
    root = Path(repo) if repo else paths.project_root()
    ok, output = _git_step(root, ["status", "--porcelain"])
    if not ok:
        return [], output or "git status 没有输出"
    return [line for line in output.splitlines() if line.strip()], ""


# ------------------------------------------------------------------ 任务状态
def state_path() -> Path:
    return paths.data_dir() / STATE_NAME

def is_commit_sha(value: Any) -> bool:
    """看着像一个提交 SHA 才准往下走：回滚会把这段字符串拼进 git 命令。"""
    return bool(SHA_RE.match(str(value or "").strip()))


def _pid_alive(pid: Any) -> bool:
    """那个进程还活着吗。

    Windows 上绝不能用 os.kill(pid, 0) 试探：CPython 在 Windows 上把它实现成
    TerminateProcess(handle, sig)——探一次就把人家杀了。所以那边走 OpenProcess。
    """
    try:
        pid = int(pid or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _default_state() -> dict[str, Any]:
    return {
        "job_id": "",
        "kind": "",
        "state": STATE_IDLE,
        "steps": [],
        "from_sha": None,
        "to_sha": None,
        "error": "",
        "started_at": "",
        "finished_at": "",
        "pid": 0,
        "host": "",
        "port": 0,
        "restart_pending": False,
        "restart_deadline": "",
        "restart_log": "",
        "lock_note": "",
        # 失败时照着敲的那一条（按真实的 --host/--port 拼）。以前只有自由文本里带一句，
        # 界面上想单独摆出来就没得用。
        "manual_hint": "",
    }


def write_state(state: dict[str, Any]) -> dict[str, Any]:
    """原子落盘：先写临时文件再换名，重启撞上写盘也不会留下半个 JSON。"""
    path = state_path()
    try:
        payload = json.dumps(state, ensure_ascii=False, indent=2, allow_nan=False)
    except (TypeError, ValueError) as exc:
        log.warning("更新状态存不下去：%s", exc)
        return state
    # 临时文件名带 pid 和随机后缀：两个进程（或两个任务）同时写时，共用一个 .tmp
    # 会一个把另一个还没写完的半个 JSON 换上去。
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    error: OSError | None = None
    try:
        tmp.write_text(payload, encoding="utf-8")
        for attempt in range(STATE_REPLACE_ATTEMPTS):
            try:
                os.replace(tmp, path)
                return state
            except OSError as exc:
                # Windows 上「目标文件正被别人读着」会回 WinError 5，等一瞬再换一次。
                error = exc
                if attempt + 1 < STATE_REPLACE_ATTEMPTS:
                    time.sleep(STATE_REPLACE_PAUSE)
    except OSError as exc:
        error = exc
    log.warning("更新状态写不下去：%s", error)
    # 换名没成（Windows 上被别人读着就是 WinError 5），自己那份临时文件要当场删掉：
    # 留着没人认领，只会和别的残渣一起越积越多。
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
    except OSError as cleanup:
        log.warning("更新状态的临时文件没删掉（%s）：%s", tmp, cleanup)
    return state


def _tmp_pid(name: str) -> int:
    """临时文件名里那个 pid（update-state.json.<pid>.<rand>.tmp）。认不出就是 0。"""
    parts = name.split(".")
    if len(parts) < 4:
        return 0  # 老写法 update-state.json.tmp：没有 pid
    try:
        return int(parts[-3])
    except ValueError:
        return 0


def clean_stale_state_tmp(grace: float | None = None) -> list[Path]:
    """清掉没人管的 update-state 临时文件，返回删掉的那几个。

    临时文件是「先写一份再换名」的中间产物，正常路径下 os.replace 一换就没了。留下的
    都是写盘途中被杀的进程，或者换名撞上 Windows 共享冲突的那一份——实测进程内 8 线程
    一轮能留 2 个、两个进程五轮留 1 个，没人再管。

    判定两道一起过：mtime 超过 grace 秒，**而且**名字里那个 pid 已经不在了。只看时间会
    删掉一个正在慢慢写的进程的东西；只看 pid 会把刚崩掉、还没人接手的那份也当成残渣。
    """
    limit = STATE_TMP_GRACE if grace is None else grace
    directory = state_path().parent
    keep_alive = os.getpid()
    removed: list[Path] = []
    now = time.time()
    try:
        candidates = sorted(directory.glob(STATE_TMP_GLOB))
    except OSError:
        return removed
    for candidate in candidates:
        try:
            if now - candidate.stat().st_mtime < limit:
                continue
        except OSError:
            continue
        pid = _tmp_pid(candidate.name)
        if pid and pid != keep_alive and _pid_alive(pid):
            continue  # 写它的人还活着，别抢
        try:
            candidate.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            log.warning("更新状态的临时文件删不掉（%s）：%s", candidate, exc)
            continue
        removed.append(candidate)
    if removed:
        log.info(
            "清掉 %d 个更新状态的临时文件：%s",
            len(removed),
            "、".join(item.name for item in removed),
        )
    return removed


# 上一次扫残渣是什么时候（读接口一直在轮询，不能每次都扫目录）。
_last_tmp_clean = 0.0


def _maybe_clean_stale_tmp() -> None:
    """按 TMP_CLEAN_INTERVAL 节流地扫一次残渣。读状态的路上顺手做，不另起线程。"""
    global _last_tmp_clean
    now = time.time()
    if now - _last_tmp_clean < TMP_CLEAN_INTERVAL:
        return
    _last_tmp_clean = now
    clean_stale_state_tmp()


def _deadline_iso(seconds: float) -> str:
    """从此刻往后 `seconds` 秒的那个时刻（ISO-8601，带时区）。"""
    stamp = datetime.fromtimestamp(time.time() + seconds, tz=datetime.now().astimezone().tzinfo)
    return stamp.isoformat(timespec="seconds")


def _deadline_epoch(state: dict[str, Any]) -> float | None:
    """重启那一步记下的期限（epoch 秒）。读不出来就是 None。"""
    raw = state.get("restart_deadline")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw)).timestamp()
    except ValueError:
        return None


def _health_target(state: dict[str, Any]) -> tuple[str, int]:
    """这次重启该确认哪个地址：状态里记下的那一份，读不到就按本进程现在的。"""
    host = str(state.get("host") or "") or web_host()
    try:
        port = int(state.get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    return host, port or web_port()


def _health_ok(host: Any = None, port: Any = None) -> tuple[bool, str]:
    """真起来了吗：`GET /api/health` 拿到 2xx 才算。

    「端口有人听」不够。评审造过一个绑上 0.3 秒就关的 socket：`connect_ex` 通，
    半秒后端口就空了；真服务还得能回答健康检查。给几次重试是因为新进程刚绑上端口时
    应用可能还没装配完。
    """
    where = probe_host(host)
    value = int(port if port is not None else web_port())
    url = f"http://{where}:{value}{HEALTH_PATH}"
    detail = ""
    for attempt in range(HEALTH_ATTEMPTS):
        if attempt:
            time.sleep(HEALTH_POLL)
        try:
            with urllib.request.urlopen(url, timeout=HEALTH_TIMEOUT) as response:
                status = int(getattr(response, "status", 0) or 0)
                body = response.read(200).decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = f"{url} 回了 HTTP {exc.code}"
            continue
        except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as exc:
            detail = f"{url} 连不上：{exc}"
            continue
        if 200 <= status < 300:
            return True, f"{url} 回了 {status}：{body.strip()[:80]}"
        detail = f"{url} 回了 HTTP {status}：{body.strip()[:80]}"
    return False, detail or f"{url} 没有回应"


def _confirm_restart(state: dict[str, Any]) -> tuple[bool, str]:
    """新进程自己确认这次重启：地址通了，而且 `/api/health` 回答 2xx。"""
    host, port = _health_target(state)
    shown = f"{probe_host(host)}:{port}"
    if _port_free(port, host):
        return False, f"{shown} 上还没有人听"
    return _health_ok(host, port)


def _merge_restart_step(steps: list[dict[str, Any]], output: str) -> None:
    """把「重启服务」这一步收成一条，别出现两个。

    老进程那次 systemctl 交出去之后已经记过一条；新进程确认重启时再 append 一份，
    界面上就是两个「重启服务」（服务器上真跑一次 apply 撞出来的）。已经有就补一句，
    没有才新增——判据只看最后一步的名字，顺序就是执行顺序。
    """
    if steps and isinstance(steps[-1], dict) and steps[-1].get("name") == STEP_RESTART:
        steps[-1] = {
            **steps[-1],
            "ok": True,
            "output": f"{steps[-1].get('output') or ''}{output}",
        }
        return
    steps.append({"name": STEP_RESTART, "ok": True, "output": output, "seconds": 0.0})


def _heal_state(state: dict[str, Any]) -> dict[str, Any]:
    """把盘上那条 running 收个尾。两种结局，判据都只认「记下的那个 pid」。

    * restart_pending 为真：重启那一步一定会把本进程带走，新进程读到这条时记下的
      pid 已经不在了，就**自己**确认一次（`GET /api/health` 拿到 2xx 才算真的起来了），
      成了把这次更新收成 done 并补上「重启服务」这一步，过了期限还没成收成 failed
      并写清等到什么时候、手动怎么拉。老进程不在这儿等——它等就是互锁（见 _restart_step）。
    * restart_pending 为假：最后一步没跑完进程就没了（崩了、被杀了、断电），
      收成 failed 并说清断在哪一步。不修这一条，那个任务在界面上永远转圈。

    别人的进程读到这里时，记下的 pid 可能正是一个**还活着的**更新进程：那就一个字都
    不许改。以前只比「不是我这个 pid」，于是任何别的进程读一次就把活任务收成 done，
    还补上一条根本没发生过的「重启服务」。
    """
    if state.get("state") != STATE_RUNNING:
        if state.get("restart_pending") or state.get("restart_deadline"):
            # 终态不该再挂着「等新进程接手」：done / failed 都已经收过尾了。盘上留着它，
            # 界面会一直显示「重启中」，而且没有任何一步会再去清（重启前的 finally 只管
            # 本进程的任务）。这里顺手收干净并写回盘。
            log.warning(
                "终态（%s）上还挂着 restart_pending，清掉", state.get("state")
            )
            state = {**state, "restart_pending": False, "restart_deadline": ""}
            write_state(state)
        return state
    owner = int(state.get("pid") or 0)
    if owner == os.getpid():
        return state  # 本进程正在跑这一个任务
    if _pid_alive(owner):
        return state  # 另一个还活着的进程在跑，不替它做决定
    steps = list(state.get("steps") or [])
    if state.get("restart_pending"):
        ok, detail = _confirm_restart(state)
        deadline = _deadline_epoch(state)
        late = deadline is not None and time.time() > deadline
        log_path = Path(str(state.get("restart_log"))) if state.get("restart_log") else None
        if ok:
            # 确认在自己身上做：读到这条状态的多半就是新进程，所以这里问一句
            # 「我这个地址上的 /api/health 好使吗」。拿到 2xx 才算这一步真的成。
            late_note = (
                f"（比期限晚了 {int(time.time() - deadline)} 秒：systemd 那边停旧单元慢过预期，"
                "服务现在是好的，这次更新照 done 收。）"
                if late and deadline
                else ""
            )
            _merge_restart_step(
                steps,
                f"新进程（pid {os.getpid()}）确认：{detail}{late_note}"
                f"（pid {owner} 已经退出）",
            )
            _append_restart_log(log_path, f"新进程确认：{detail}", who="新进程")
            state = {
                **state,
                "steps": steps,
                "state": STATE_DONE,
                "restart_pending": False,
                "restart_deadline": "",
                "finished_at": state.get("finished_at") or now_iso(),
            }
        elif late:
            # 期限过了还没人接上：这次重启就是没成，收成 failed 并把手动命令写清楚。
            hint = _manual_hint(paths.project_root())
            error = (
                f"重启没等到：pid {owner} 把重启交出去之后，到 {state.get('restart_deadline')} "
                f"还没有新进程接上来（{detail}）。{_port_note()}\n手动拉起：{hint}"
            )
            _append_restart_log(log_path, error, who="新进程")
            state = {
                **state,
                "state": STATE_FAILED,
                "restart_pending": False,
                "restart_deadline": "",
                "error": error,
                "manual_hint": hint,
                "finished_at": state.get("finished_at") or now_iso(),
            }
        else:
            # 还在期限内：不假装成功，也不急着判死，下一次读再算。
            return state
    else:
        last = ""
        if steps and isinstance(steps[-1], dict):
            last = str(steps[-1].get("name") or "")
        state = {
            **state,
            "state": STATE_FAILED,
            "restart_pending": False,
            "error": (
                f"上次更新没跑完就断了（pid {owner} 已经不在了"
                + (f"，断在「{last}」这一步" if last else "，一步都还没记下来")
                + "）。盘上留下的是断之前的那些步骤，进程里的日志已经没了。"
            ),
            "finished_at": state.get("finished_at") or now_iso(),
        }
    write_state(state)
    return state


def read_state() -> dict[str, Any]:
    """当前（或最后一次）更新任务。没有就是 idle，读坏了也退回 idle 并记一条。"""
    path = state_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _default_state()
    except (OSError, ValueError) as exc:
        log.warning("更新状态读不出来：%s", exc)
        return _default_state()
    state = _default_state()
    if isinstance(raw, dict):
        for key in state:
            if key in raw:
                state[key] = raw[key]
    if not isinstance(state["steps"], list):
        state["steps"] = []
    _maybe_clean_stale_tmp()  # 节流：读接口天天被轮询，隔一阵扫一次残渣
    return _heal_state(state)


# ------------------------------------------------------------------ 跑任务
class _Job:
    """一条流水线的记账员：每一步都落盘，失败就停。"""

    def __init__(
        self,
        state: dict[str, Any],
        root: Path,
        on_step: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.state = state
        self.root = root
        self.steps: list[dict[str, Any]] = list(state.get("steps") or [])
        # CLI 要边跑边念：界面那条路不需要（它轮询状态文件），所以默认没有回调。
        self.on_step = on_step

    def record(self, name: str, ok: bool, output: str, started: float) -> None:
        self.steps.append(
            {
                "name": name,
                "ok": bool(ok),
                "output": output,
                "seconds": round(time.monotonic() - started, 2),
            }
        )
        self.state["steps"] = self.steps
        write_state(self.state)
        if self.on_step is not None:
            try:
                self.on_step(self.steps[-1])
            except Exception:  # 回调只负责打印，坏了自己兜住，别把更新带崩
                log.debug("步骤回调出错", exc_info=True)

    def fail(self, name: str, output: str) -> bool:
        self.state["state"] = STATE_FAILED
        self.state["error"] = output or f"「{name}」这一步没有输出。"
        self.state["finished_at"] = now_iso()
        write_state(self.state)
        return False

    def step(self, name: str, fn: Callable[[], tuple[bool, str]]) -> bool:
        started = time.monotonic()
        try:
            ok, output = fn()
        except Exception as exc:  # 这一步里没兜住的异常也算这一步失败，原文带走
            log.warning("更新步骤「%s」抛异常：%s", name, exc)
            ok, output = False, f"{name}：{exc}"
        self.record(name, ok, output, started)
        if not ok:
            return self.fail(name, output)
        return True


def _pip_step(root: Path) -> tuple[bool, str]:
    """装依赖用 sys.executable：跑更新的是哪个解释器，装的就是哪个环境。"""
    if not (root / "requirements.txt").exists():
        return True, "仓库里没有 requirements.txt，跳过依赖安装。"
    return _run_command(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            # 服务器上没有人盯着这个终端：pip 一旦要问一句（依赖冲突、要不要继续），
            # 它就会一直等到 PIP_TIMEOUT（900 秒）才失败，界面上只是一步卡住。
            "--no-input",
            # 小盘机器（40G 云盘很常见）装一次留一堆 wheel 缓存没必要，装完就没人再要了。
            "--no-cache-dir",
            "-r",
            "requirements.txt",
        ],
        cwd=root,
        timeout=PIP_TIMEOUT,
        env={"PYTHONIOENCODING": "utf-8", "PIP_DISABLE_PIP_VERSION_CHECK": "1"},
    )


def _changed_under(repo: Path, from_sha: str | None, prefix: str) -> list[str] | None:
    """这次更新动了哪些文件（只看 prefix 这一层）。读不出来返回 None。"""
    if not from_sha:
        return None
    ok, output = _git_step(repo, ["diff", "--name-only", f"{from_sha}..HEAD"])
    if not ok:
        return None
    return [line for line in output.splitlines() if line.strip().startswith(prefix)]


def _node_command(npm: str, *args: str) -> list[str]:
    """Windows 上的 npm 是 .cmd 批处理，CreateProcess 执行不了它，只能借 cmd.exe。

    参数仍然是列表，没有一个字符进 shell。
    """
    if os.name == "nt" and str(npm).lower().endswith((".cmd", ".bat")):
        return ["cmd.exe", "/c", str(npm), *[str(a) for a in args]]
    return [str(npm), *[str(a) for a in args]]


def _build_step(root: Path, state: dict[str, Any]) -> tuple[bool, str]:
    """只有本机有 npm 且 web/ 这次真的有改动才构建；服务器上根本不构建。"""
    web = root / "web"
    if not web.exists():
        return True, "仓库里没有 web 目录，跳过前端构建。"
    changed = _changed_under(root, state.get("from_sha"), "web")
    if changed is not None and not changed:
        return True, "这次更新没有动 web/，产物跟着仓库走，不用构建。"
    npm = shutil.which("npm")
    if not npm:
        return True, "本机没有 npm：服务器不需要构建，产物跟着仓库走（static/ 已经在仓库里）。"
    note = "" if changed is not None else "读不到改动的文件清单，保险起见构建一次。"
    ok, output = _run_command(
        _node_command(npm, "run", "build"),
        cwd=web,
        timeout=NPM_TIMEOUT,
        env={"PYTHONIOENCODING": "utf-8"},
    )
    return ok, " ".join(part for part in (note, output) if part)


def _unit_note(root: Path, state: dict[str, Any]) -> str:
    """这次更新动了 systemd unit 的话，给「重启服务」那一步加一句警告。

    不新增一步：步骤序列是测试与界面共同依赖的契约（6 步），多一格会让所有按位置
    读步骤的地方都错位；这条信息本来就属于重启那一步。

    为什么不直接替 root 重写 /etc/systemd/system/bitrader.service：那需要「把一个
    bitrader 可写的文件装进 /etc」的权限，等于把 root 交给服务用户 —— set-domain.sh
    之所以放在 /usr/local/lib/bitrader（root:root）就是这个道理。所以只警告，
    把该做的事写清楚，让人自己决定什么时候重装。
    """
    changed = _changed_under(root, state.get("from_sha"), "deploy/")
    if not changed:
        return ""
    units = [path for path in changed if path.endswith(".service")]
    if not units:
        return ""
    return (
        f"注意：这次更新动了 {'、'.join(units)}，但重启用的仍是服务器上那份旧 unit —— "
        "unit 文件不归程序改（那要 root 写入权）。让它生效：在服务器上重跑一次安装"
        "（sudo bash deploy/install.sh，参数与当初一致，幂等），或者手动拷到 "
        "/etc/systemd/system/ 再 systemctl daemon-reload。\n"
    )


def _restart_step(state: dict[str, Any]) -> tuple[bool, str]:
    """重启：把这一步**交出去**，收尾留给新进程。

    进程随时会没，状态文件是重启之后唯一的记忆，所以交出去之前先落盘：谁在跑、
    哪个地址、期限到什么时候、这次重启的日志在哪儿。老进程**不在这里等**「新进程
    起来了没有」——Linux 上 systemd 的 stop 正等着本进程退出（unit 里
    TimeoutStopSec=20），在这儿同步等 systemctl、等端口就是互锁，结局只有 SIGKILL。
    确认挪到新进程：它读到「重启中」时自己打一次 /api/health（见 _heal_state）。
    """
    root = paths.project_root()
    note = _unit_note(root, state)
    state["host"] = web_host()
    state["port"] = web_port()
    if not web_bind_known():
        log.warning(
            "没拿到本进程的真实地址，重启按兜底值 %s:%s 处理", WEB_HOST, WEB_PORT
        )
    state["restart_pending"] = True
    state["restart_deadline"] = _deadline_iso(RESTART_CONFIRM_TIMEOUT)
    global _last_restart_log
    _last_restart_log = None
    write_state(state)
    ok, output = restart_service()
    if note:
        output = f"{note}{output}"
    if _last_restart_log is not None:
        state["restart_log"] = str(_last_restart_log)
    if ok:
        write_state(state)
        return True, output
    state["restart_pending"] = False
    state["restart_deadline"] = ""
    hint = _manual_hint(root)
    state["manual_hint"] = hint
    if "手动" not in output:
        output = f"{output}\n手动拉起：{hint}"
    # 失败详情与手动命令同时写一份进这一轮的日志：这时候本进程马上就要停了，
    # /api/update/status 要等用户手动起回来才看得到。
    _append_restart_log(_last_restart_log, f"重启失败：{output}", who="老进程")
    write_state(state)
    return False, output


def _run_job(
    state: dict[str, Any],
    kind: str,
    info: dict[str, Any],
    *,
    job: _Job | None = None,
    sync: bool = False,
) -> None:
    root = paths.project_root()
    job = job or _Job(state, root)
    try:
        fetched = job.step(
            STEP_FETCH,
            lambda: _git_step(root, ["fetch", "--all", "--prune"], timeout=FETCH_TIMEOUT),
        )
        if not fetched:
            return

        def remember() -> tuple[bool, str]:
            sha = _git_out(root, "rev-parse", "HEAD")
            state["from_sha"] = sha or None
            return bool(sha), sha or "读不到当前提交"

        if not job.step(STEP_REMEMBER, remember):
            return

        if kind == KIND_ROLLBACK:
            target = str(info.get("target") or "")
            name = f"{STEP_REVERT} {target[:7] if target else '?'}..HEAD"

            def revert() -> tuple[bool, str]:
                if not target:
                    return False, "没有可回滚的目标提交。"
                if not is_commit_sha(target):
                    return False, f"记下的起始提交「{target}」不像一个提交 SHA，不拿它拼 git 命令。"
                if _git_out(root, "rev-parse", "HEAD") == target:
                    return True, f"已经在 {target[:7]} 上，这次不用回滚。"
                # 不改写历史：反向提交把内容退回目标提交，命令里没有 reset / force。
                return _git_step(root, ["revert", "--no-edit", f"{target}..HEAD"])

            if not job.step(name, revert):
                return
            state["to_sha"] = target or None
            write_state(state)
        else:
            remote, branch = _upstream(root)
            if not remote or not branch:
                job.step(
                    f"{STEP_MERGE} <远端>/<分支>",
                    lambda: (False, "读不到远端：没有配置任何 remote，或者当前不在分支上。"),
                )
                return
            ref = f"{remote}/{branch}"
            merged = job.step(
                f"{STEP_MERGE} {ref}", lambda: _git_step(root, ["merge", "--ff-only", ref])
            )
            if not merged:
                return
            state["to_sha"] = _git_out(root, "rev-parse", "HEAD") or None
            write_state(state)

        if not job.step(STEP_PIP, lambda: _pip_step(root)):
            return
        if not job.step(STEP_BUILD, lambda: _build_step(root, state)):
            return
        if not job.step(STEP_RESTART, lambda: _restart_step(state)):
            return
        if state.get("restart_pending"):
            if sync:
                # CLI 自己还活着，它能自己等这次重启（界面那条路不行：发起更新的进程
                # 马上就要被 systemd 停掉，在那儿等就是互锁）。判据与 _heal_state 同一个。
                settle_restart(state)
            # 重启已经交出去了，本进程马上就没。done 由新进程确认之后写——以前这里
            # 顺手写了 done，盘上于是留下 done + restart_pending=true 的自相矛盾（L1）。
            return
        state["state"] = STATE_DONE
        state["finished_at"] = now_iso()
        write_state(state)
    except Exception as exc:  # 兜底：绝不把任务留在 running 上让前端一直转
        log.exception("更新任务出错")
        state["state"] = STATE_FAILED
        state["error"] = state.get("error") or f"更新任务出错：{exc}"
        state["finished_at"] = now_iso()
        write_state(state)
    finally:
        release_update_lock()
        # 失败分支也可能留下半截临时文件：收尾时按同一套判据（过期 + pid 不在）再扫一次。
        clean_stale_state_tmp()


# 一次只跑一个更新任务：两道 start_job 撞在一起时，第二个在这儿排队而不是一起跑。
JOB_LOCK = threading.Lock()


def _spawn(fn: Callable[[], None]) -> None:
    """后台线程跑流水线，接口立刻返回 job_id。测试把它换成就地执行。

    刻意不是 daemon 线程：重启那一步要等「本进程放开端口」+「新进程绑上端口」两段确认
    （最多 LISTEN_TIMEOUT 秒）。daemon 线程会在 uvicorn 主线程退出时被直接掐掉，于是
    「到底起没起来」永远没人知道，状态又变成一句谎——那正是 F1 要修的东西。
    代价是退出时得等这一轮更新跑完；每一步都有 timeout（git / pip / build / 重启都有），
    不会无限挂着。
    """
    thread = threading.Thread(target=fn, name="update-job", daemon=False)
    thread.start()


class JobBusy(RuntimeError):
    """已经有一个更新在跑。接口把它翻成 409，别让它变成 500。"""


def running_job() -> dict[str, Any] | None:
    """现在是不是已经有一个更新在跑（正在重启的那个也算）。"""
    state = read_state()
    if state.get("state") != STATE_RUNNING:
        return None
    return state


def lock_path() -> Path:
    """跨进程的更新锁：一份数据目录只许一个更新在跑。"""
    return paths.data_dir() / LOCK_NAME


def _read_lock() -> dict[str, Any]:
    """锁文件里写了什么。写者可能刚建好文件、还没落内容，所以等它一下。"""
    path = lock_path()
    for attempt in range(5):
        try:
            raw = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return {}
        except OSError as exc:
            log.warning("更新锁读不出来：%s", exc)
            return {}
        if raw:
            try:
                payload = json.loads(raw)
            except ValueError:
                return {}
            return payload if isinstance(payload, dict) else {}
        if attempt < 4:
            time.sleep(0.05)
    return {}


def _lock_aged(grace: float = 10.0) -> bool:
    """锁文件是不是老得可以当成残留了（刚建好的那种不能抢）。"""
    try:
        return time.time() - lock_path().stat().st_mtime > grace
    except OSError:
        return True


def lock_holder() -> dict[str, Any] | None:
    """谁握着更新锁。pid 已经不在了的锁算没有——那是上一个进程死在半路。"""
    info = _read_lock()
    if not info:
        return None
    try:
        pid = int(info.get("pid") or 0)
    except (TypeError, ValueError):
        return None
    if pid and _pid_alive(pid):
        return info
    return None


def acquire_update_lock(job_id: str = "") -> tuple[Path, str]:
    """拿跨进程锁：(锁文件, 「接管了残留锁」的说明)。

    `O_CREAT|O_EXCL` 建文件，建不出来就是别人握着。JOB_LOCK 只管本进程里的两条线程：
    三个进程共用同一份数据目录时它什么都拦不住（评审实测三轮全部 3/3 都建成了任务，
    还有一轮 `os.replace` 撞 WinError 5、状态根本没落盘）。不用 flock：Windows 上没有。
    """
    path = lock_path()
    payload = json.dumps(
        {"pid": os.getpid(), "job_id": job_id, "at": now_iso()}, ensure_ascii=False
    )
    note = ""
    for _attempt in range(2):
        try:
            handle = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            info = _read_lock()
            try:
                pid = int(info.get("pid") or 0)
            except (TypeError, ValueError):
                pid = 0
            if info and pid and _pid_alive(pid):
                raise JobBusy(
                    f"另一个进程正在更新（pid {pid}，任务 {info.get('job_id') or '?'}，"
                    f"从 {info.get('at') or '?'} 开始）：等它跑完再来——两个进程同时动同一"
                    "个工作树，会互相把对方的提交和状态搅乱。"
                ) from None
            if not info and not _lock_aged():
                # 建好了还没写内容：另一个进程正卡在 os.open 和 write 之间，别抢。
                raise JobBusy(
                    f"另一个进程正在更新（{path.name} 刚建好，还没来得及写内容）："
                    "等它跑完再来。"
                ) from None
            # 锁还在、pid 已经不在了：上一个进程死在半路，接管它。
            note = (
                f"接管了一个残留的更新锁（{path.name} 里记的 pid {pid or '?'} 已经不在了），"
                "那个进程大概是更新途中被杀掉的。"
            )
            log.warning("%s", note)
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise JobBusy(f"更新锁 {path} 拿不掉：{exc}") from None
            continue
        except OSError as exc:
            raise JobBusy(f"更新锁建不起来（{path}）：{exc}") from None
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(payload)
        return path, note
    raise JobBusy(f"拿不到更新锁（{path}）：两次都被别人抢先了。")


def release_update_lock() -> None:
    """放锁。只删自己那一份：别人的锁轮不到我删。"""
    path = lock_path()
    info = _read_lock()
    try:
        pid = int(info.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    if pid != os.getpid():
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("更新锁删不掉：%s", exc)


def start_job(kind: str, info: dict[str, Any] | None = None) -> str:
    """建任务、落盘、开线程，返回 job_id。

    一次只许一个任务：两条流水线同时跑会互相抢工作树（第二个的 fetch/merge 撞上第一个的
    merge），而且会互相把对方从盘上抹掉——第二个 start_job 写完 running，第一个每写一步
    就盖一次，界面上看是两个任务在两个 job_id 之间跳。闸放在锁里，接口那边先查一遍只是
    为了给 409 一句人话。
    """
    with JOB_LOCK:
        busy = running_job()
        if busy is not None:
            raise JobBusy(
                f"上一个更新还在跑（任务 {busy.get('job_id') or '?'}，pid {busy.get('pid')}），"
                "等它结束再来。"
            )
        # 上一轮被杀的进程留下的临时文件没人认领，开新任务前先扫一遍。
        clean_stale_state_tmp()
        state = _default_state()
        state.update(
            {
                "job_id": uuid.uuid4().hex[:8],
                "kind": kind,
                "state": STATE_RUNNING,
                "started_at": now_iso(),
                "pid": os.getpid(),
                "host": web_host(),
                "port": web_port(),
            }
        )
        # 第二道锁管进程之外：同一份数据目录被两个进程同时更新时，JOB_LOCK 看不见对方。
        _path, note = acquire_update_lock(str(state["job_id"]))
        state["lock_note"] = note
        write_state(state)
    try:
        _spawn(lambda: _run_job(state, kind, dict(info or {})))
    except Exception:
        release_update_lock()
        raise
    return str(state["job_id"])

def settle_restart(state: dict[str, Any]) -> dict[str, Any]:
    """重启交出去之后，由一个**还活着**的进程自己等结果（CLI 的离线模式）。

    界面那条路不能这么等：发起更新的进程就是要被重启的那一个，在那里等 systemctl
    就是互锁（见 _restart_step 的说明）。CLI 是另一个进程，服务停多久都不影响它，
    所以它自己等。判据和 _heal_state 用的是同一个 _confirm_restart：地址上有
    `/api/health` 的 2xx 才算起来了，端口通了不算。
    """
    steps = list(state.get("steps") or [])
    deadline = _deadline_epoch(state) or (time.time() + RESTART_CONFIRM_TIMEOUT)
    log_path = Path(str(state["restart_log"])) if state.get("restart_log") else None
    detail = ""
    while True:
        ok, detail = _confirm_restart(state)
        if ok:
            break
        if time.time() >= deadline:
            hint = _manual_hint(paths.project_root())
            error = (
                f"重启没等到：{state.get('restart_deadline') or '期限已过'} 还没有新进程接上来"
                f"（{detail}）。{_port_note()}\n手动拉起：{hint}"
            )
            _append_restart_log(log_path, error, who="CLI")
            state.update(
                {
                    "steps": steps,
                    "state": STATE_FAILED,
                    "restart_pending": False,
                    "restart_deadline": "",
                    "error": error,
                    "manual_hint": hint,
                    "finished_at": state.get("finished_at") or now_iso(),
                }
            )
            write_state(state)
            return state
        time.sleep(1.0)
    _merge_restart_step(steps, f"已确认服务起来了：{detail}")
    _append_restart_log(log_path, f"CLI 确认：{detail}", who="CLI")
    state.update(
        {
            "steps": steps,
            "state": STATE_DONE,
            "restart_pending": False,
            "restart_deadline": "",
            "finished_at": state.get("finished_at") or now_iso(),
        }
    )
    write_state(state)
    return state


def run_job_sync(
    kind: str,
    info: dict[str, Any] | None = None,
    *,
    on_step: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """在**调用方线程**里跑同一条流水线（`btrader update` 的离线模式）。

    和 start_job 只差两处：跑在调用方线程里（CLI 要一行行打印），重启由调用方自己
    确认（settle_restart）。锁、状态文件、步骤格式、失败即停全部共用同一份代码 ——
    别在这里另写一条流水线，两份实现迟早会分叉（install.sh 里那句 reset --hard
    就是这么漂出来的）。
    """
    with JOB_LOCK:
        busy = running_job()
        if busy is not None:
            raise JobBusy(
                f"上一个更新还在跑（任务 {busy.get('job_id') or '?'}，pid {busy.get('pid')}），"
                "等它结束再来。"
            )
        # 上一轮被杀的进程留下的临时文件没人认领，开新任务前先扫一遍。
        clean_stale_state_tmp()
        state = _default_state()
        state.update(
            {
                "job_id": uuid.uuid4().hex[:8],
                "kind": kind,
                "state": STATE_RUNNING,
                "started_at": now_iso(),
                "pid": os.getpid(),
                "host": web_host(),
                "port": web_port(),
            }
        )
        _path, note = acquire_update_lock(str(state["job_id"]))
        state["lock_note"] = note
        write_state(state)
    job = _Job(state, paths.project_root(), on_step=on_step)
    _run_job(state, kind, dict(info or {}), job=job, sync=True)
    return state


# ------------------------------------------------------------------ 重启
SHUTDOWN_HOOK: Callable[[], None] | None = None


def set_shutdown_hook(hook: Callable[[], None] | None) -> None:
    """让 web 进程把自己那个 uvicorn Server 的退出开关交给这里。"""
    global SHUTDOWN_HOOK
    SHUTDOWN_HOOK = hook


def _request_shutdown() -> None:
    """让本进程优雅退出：有 uvicorn 的钩子就用它，否则发一个它认得的信号。

    不用 os._exit：那等于硬切，状态文件可能正写到一半。Windows 上也不用
    os.kill(SIGTERM)——那在 Windows 上是 TerminateProcess，同样是硬切。
    """
    hook = SHUTDOWN_HOOK
    if hook is not None:
        try:
            hook()
            return
        except Exception as exc:
            log.warning("优雅退出的钩子没成功：%s", exc)
    try:
        if sys.platform == "win32":
            signal.raise_signal(signal.SIGINT)
        else:
            os.kill(os.getpid(), signal.SIGTERM)
    except (OSError, ValueError, RuntimeError) as exc:
        log.warning("没能发出退出信号：%s", exc)


# 等端口释放、拉起新进程的小垫片。只用标准库、不 import btrader，能被 -c 直接跑。
#
# 日志由**垫片自己开**（open(log_path, 'a') 再 dup2 到 1/2）。以前是父进程把手里的
# open(path,'ab') 递过来当 stdout，自己又另开一个追加句柄写它那几行——两个句柄共享同一个
# 文件位置，于是老进程那句「重启已经交给垫片（pid …）」被垫片的输出盖掉一半（量测三轮都复现）。
# 各开各的句柄 + O_APPEND 之后，两边都是整行追加，谁也不踩谁。
#
# 文件名仍由父进程挑（每次唯一）、开不出来当场失败：垫片自己 open(<仓库>/.run_out.log) 那套
# 早就删了——那个文件正被活着的进程写着，Windows 上锁成只读，open 抛 PermissionError。
def _windows_shim() -> str:
    """垫片源码：现拼一份，数值一律从常量来。

    垫片是被 -c 直接跑的，不能 import btrader，所以那段十六进制数字只能写死在源码里——
    但它不该是**第二份**来源：以前这里是字面量 0x08000200，常量改了垫片不跟着改，
    测试断言的 CREATE_NO_WINDOW 命中的还是注释文字、不是数值。现在由 hex(WINDOWS_HIDDEN) 拼。
    """
    flags = hex(WINDOWS_HIDDEN)
    return "\n".join(
        [
            "import os, socket, subprocess, sys, time",
            "port, repo, py = int(sys.argv[1]), sys.argv[2], sys.argv[3]",
            "log_path, host = sys.argv[4], sys.argv[5]",
            "try:",
            "    _log = open(log_path, 'a', buffering=1, encoding='utf-8', errors='replace')",
            "except OSError:",
            "    sys.exit(4)",
            "os.dup2(_log.fileno(), 1)",
            "os.dup2(_log.fileno(), 2)",
            "sys.stdout = sys.stderr = _log",
            "probe_host = '127.0.0.1' if host in ('', '0.0.0.0', '::', '*') else host",
            "def say(msg):",
            "    print(time.strftime('%Y-%m-%d %H:%M:%S ') + msg, flush=True)",
            "say('垫片起来，日志 ' + log_path + '，等 ' + probe_host + ':%d 放开端口' % port)",
            f"deadline = time.time() + {LISTEN_TIMEOUT:.0f}",
            "while time.time() < deadline:",
            "    probe = socket.socket()",
            "    probe.settimeout(0.3)",
            "    try:",
            "        busy = probe.connect_ex((probe_host, port)) == 0",
            "    finally:",
            "        probe.close()",
            "    if not busy:",
            "        break",
            "    time.sleep(0.2)",
            "else:",
            f"    say('{LISTEN_TIMEOUT:.0f} 秒里端口一直没放开，放弃拉起新进程')",
            "    sys.exit(2)",
            "try:",
            "    child = subprocess.Popen([py, 'run_web.py', '--host', host, '--port', str(port)],",
            "                             cwd=repo, stdin=subprocess.DEVNULL, stdout=sys.stdout,",
            "                             stderr=subprocess.STDOUT, close_fds=True,",
            f"                             # {flags} = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP（垫片不能 import btrader，所以写数字）",
            f"                             creationflags={flags})",
            "except OSError as exc:",
            "    say('拉新进程失败：%r' % (exc,))",
            "    sys.exit(3)",
            "say(('垫片已拉起 run_web.py（pid {0} 是转发器进程，真正监听的那个 pid 看下面 uvicorn 那行）：--host ' + host + ' --port {1}').format(child.pid, port))",
            "",
        ]
)



# 模块级那一份给 _restart_windows 和测试直接用；要「常量改了跟着变」就走 _windows_shim()。
_WINDOWS_SHIM = _windows_shim()


def _restart_log_path() -> Path:
    """这一次重启自己的日志。绝不复用活进程正写着的那个文件（Windows 会把它锁成只读）。"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return paths.logs_dir() / f"restart-{stamp}-{uuid.uuid4().hex[:6]}.log"


def _open_restart_log(path: Path):
    """开这次的日志。开不出来就抛出去——以前这一步失败被垫片吞掉，然后整条重启报成功。"""
    return open(path, "ab")


# 这一轮重启自己的日志在哪儿。restart_service() 的返回值已经有 (ok, output) 两格，
# 不为了一个路径去改它的签名（启动路径与测试都在用那个接口）。
_last_restart_log: Path | None = None


def _append_restart_log(path: Path | None, text: str, *, who: str = "") -> None:
    """把一句话写进这一轮的日志。写不进去只记一条，不能让重启流程崩掉。

    失败详情与手动命令必须写在这儿：重启失败时服务已经停了，/api/update/status 要等
    用户手动起回来才看得到，日志是他唯一能立刻翻到的地方。
    """
    if path is None or not text:
        return
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    label = f" [{who}]" if who else ""
    body = "".join(f"{stamp}{label} {line}\n" for line in text.splitlines())
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(body)
    except OSError as exc:
        log.warning("重启日志写不进去（%s）：%s", path, exc)


def _port_free(port: int | None = None, host: Any = None) -> bool:
    """这个端口现在没人听？连得上就算有人在听。

    注意：连得上**不等于**服务起来了——评审造过一个绑上 0.3 秒就关的 socket。
    真要「起来了」得 /api/health 回答 2xx（_health_ok），端口探测只用来决定
    「可以开始问 health 了」。
    """
    value = web_port() if port is None else int(port)
    probe = socket.socket()
    probe.settimeout(0.3)
    try:
        return probe.connect_ex((probe_host(host), value)) != 0
    finally:
        probe.close()


def _wait_until(
    predicate: Callable[[], bool], timeout: float, poll: float = LISTEN_POLL
) -> bool:
    """等到 predicate 成真，或者超时。超时返回 False，不假装成功。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(poll)
    return bool(predicate())


def _manual_hint(root: Path) -> str:
    """起不来的时候给用户照着敲的那一条（按这个进程真实的地址与端口拼）。"""
    prefix = f'cd /d "{root}" && ' if os.name == "nt" else f'cd "{root}" && '
    exe = str(sys.executable)
    if " " in exe:
        exe = f'"{exe}"'
    return f"{prefix}{exe} run_web.py --host {web_host()} --port {web_port()}"


def _restart_linux() -> tuple[bool, str]:
    """Linux 上服务是 systemd 的 bitrader 单元；仓库里给它装了一条 NOPASSWD sudo。

    `--no-block` 是这条路的要点。不带它，systemctl 要等本单元停完才返回，而本单元
    就是发命令的这个进程：老进程等 systemctl、systemctl 等老进程退出，互锁到
    TimeoutStopSec（unit 里是 20 秒）被 SIGKILL 硬切（AGENTS 明令不许硬切）；或者
    stop 时的 SIGTERM 先把 systemctl 客户端杀掉、拿到 -15，把一次已经成功的重启记成
    「重启失败：退出码 -15」。带上它，返回 0 就代表「已经交给 systemd 了」，
    确认由新进程做（_heal_state 里自己打 /api/health）。

    返回码非 0 仍是失败：stdout / stderr 原样带走（systemctl 与 sudo 的报错都在里面），
    不替它编原因。唯一的例外是 SIGTERM（-15 / 143）——那是本单元正在停，命令已经被
    systemd 受理了，见下面 returncode 那一段。
    """
    global _last_restart_log
    args = ["sudo", "-n", "systemctl", "--no-block", "restart", SERVICE_NAME]
    shown = " ".join(args)
    log_path = _restart_log_path()
    try:
        _open_restart_log(log_path).close()
    except OSError as exc:  # 日志开不出来不影响重启，只是没有现场可查
        log.warning("这次重启的日志开不出来（%s）：%s", log_path, exc)
        log_path = None
    _last_restart_log = log_path
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=RESTART_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        detail = f"重启失败（{shown}）：{exc}"
        _append_restart_log(log_path, detail, who="老进程")
        return False, detail
    output = _text(proc)
    # -15 / 143（128+15）：systemctl 是跟着本单元一起被 SIGTERM 的——unit 默认
    # KillMode=control-group，stop 时整组都收信号。这不是「重启没做成」，恰恰说明
    # 请求已经被 systemd 受理、本单元正在停。实测 --no-block 也躲不掉这段（请求入队
    # 之后 stop 立刻就开始了），只看「非 0 即失败」会把一次成功的重启记成 failed，
    # 界面显示更新失败、实际代码已经换掉。新进程的 /api/health 才是判据。
    if proc.returncode in (-15, 143):
        message = (
            f"重启请求已被 systemd 受理（{shown} 被 SIGTERM 结束，退出码 {proc.returncode}）："
            f"本单元正跟着停。确认留给新进程——它起来之后自己请求 {HEALTH_PATH}，拿到 2xx 才把"
            f"这次更新收成 done（{RESTART_CONFIRM_TIMEOUT:.0f} 秒内没新进程接手就算失败）。"
            f"日志 {log_path}{_port_note()}。"
        )
        _append_restart_log(log_path, message, who="老进程")
        return True, message
    if proc.returncode != 0:
        detail = output or f"重启失败：退出码 {proc.returncode}"
        _append_restart_log(log_path, f"重启失败（{shown}）：{detail}", who="老进程")
        return False, detail
    message = (
        f"重启已经交给 systemd（{shown} 返回 0）：它自己停旧进程、起新进程。"
        f"确认留给新进程——它起来之后自己请求 {HEALTH_PATH}，拿到 2xx 才把这次更新收成 done"
        f"（{RESTART_CONFIRM_TIMEOUT:.0f} 秒内没新进程接手就算失败）。日志 {log_path}"
        f"{_port_note()}。"
    )
    _append_restart_log(
        log_path, message + (f"\nsystemctl 说：{output}" if output else ""), who="老进程"
    )
    return True, (message + (f"\n{output}" if output else ""))


def _restart_windows() -> tuple[bool, str]:
    """Windows 上没有 systemd：起一个分离的垫片，它等端口放开之后拉起 run_web.py。

    这里只做「交出去」：垫片起来了、本进程的端口放开了，就 return True。服务到底有没有
    起来由新进程确认（_heal_state 里打 /api/health），和老进程无关——老进程马上就没了，
    让它在这儿等新进程，等于把「到底起没起来」交给一个正在死掉的进程去判断。

    三件事以前都缺，合起来就是「服务停死了还报成功」：日志写进了一个正被本进程写着的
    文件（垫片 open 就 PermissionError）、垫片的 stderr 给了 DEVNULL（它死了没人知道）、
    拉起之后没确认就当成功。现在：

    1. 每次一个唯一日志文件，开不出来当场失败，什么都不动；
    2. 日志由垫片自己打开（open 'a' + dup2 到 1/2），父进程的 stdout / stderr 给 DEVNULL：
       两个句柄共享同一个文件位置会把彼此的行写花，各开各的 + O_APPEND 才不会；
    3. 地址与端口都是真的（--host 0.0.0.0 / 8799 的实例不会被拉成 127.0.0.1:8765）。
    """
    global _last_restart_log
    root = paths.project_root()
    host, port = web_host(), web_port()
    log_path = _restart_log_path()
    _last_restart_log = None
    try:
        handle = _open_restart_log(log_path)
    except OSError as exc:
        return False, (
            f"重启没做成：这次的日志开不出来（{log_path}：{exc}）。"
            "什么都没动，服务还在这个进程里跑着。"
        )
    # 这个句柄只用来确认「这个路径写得进去」，不递给垫片：垫片自己 open 一份。
    handle.close()
    _last_restart_log = log_path
    shim = [
        sys.executable,
        "-c",
        _WINDOWS_SHIM,
        str(port),
        str(root),
        sys.executable,
        str(log_path),
        host,
    ]
    try:
        proc = subprocess.Popen(
            shim,
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            # 垫片自己把 log_path 接成 stdout/stderr。这里给 DEVNULL 是有意的：
            # 父进程把自己的句柄递过去，两个句柄共享同一个文件位置，父进程随后追加的
            # 那句「重启已经交给垫片（pid …）」就会被垫片的输出覆盖掉一半。
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            # 垫片自己也不许弹窗：它是个普通 python 进程，一样会被塞一个控制台。
            creationflags=WINDOWS_HIDDEN,
            startupinfo=_hidden_startupinfo(),
            close_fds=True,
        )
    except OSError as exc:
        detail = f"拉新进程失败：{exc}（日志 {log_path}）"
        _append_restart_log(log_path, detail, who="老进程")
        return False, detail
    pid = getattr(proc, "pid", "?")
    _request_shutdown()
    # 只等一件事：本进程把端口放开（顺序不能反，垫片正等在这儿）。等服务真起来是
    # 新进程的事——它读状态时自己打 /api/health，2xx 才把这次更新收成 done。
    if not _wait_until(lambda: _port_free(port, host), RELEASE_TIMEOUT):
        detail = (
            f"重启没做成：等了 {RELEASE_TIMEOUT:.0f} 秒，{host}:{port} 还占在本进程手里"
            f"（探的是 {probe_host(host)}:{port}），新进程没法绑上去（垫片 pid {pid} 一直在等）。"
            f"日志 {log_path}{_port_note()}"
            f"\n手动拉起：{_manual_hint(root)}"
        )
        _append_restart_log(log_path, detail, who="老进程")
        return False, detail
    message = (
        f"重启已经交给垫片（pid {pid}）：{host}:{port} 上的端口本进程已放开"
        f"（探的是 {probe_host(host)}:{port}），它接着拉 "
        f"run_web.py --host {host} --port {port}。确认留给新进程——它起来之后自己"
        f"请求 {HEALTH_PATH}，2xx 才把这次更新收成 done"
        f"（{RESTART_CONFIRM_TIMEOUT:.0f} 秒内没人接手就算失败）。日志 {log_path}"
        f"{_port_note()}。"
    )
    _append_restart_log(log_path, message, who="老进程")
    return True, message


def restart_service() -> tuple[bool, str]:
    """重启后端。成不成都要把命令的原文带回来。"""
    if sys.platform == "win32":
        return _restart_windows()
    return _restart_linux()
