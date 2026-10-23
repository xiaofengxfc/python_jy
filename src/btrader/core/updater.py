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

import json
import logging
import os
import shutil
import signal
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

from .. import paths

log = logging.getLogger(__name__)

# 仓库是公开的，接口不要 token；写死在这儿是因为这里只服务这一个仓库。
GITHUB_OWNER = "xiaofengxfc"
GITHUB_REPO = "python_jy"
GITHUB_API = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/commits"
GITHUB_PER_PAGE = 20
GITHUB_TIMEOUT = 10.0
USER_AGENT = "biTrader-updater"

SERVICE_NAME = "bitrader"
WEB_PORT = 8765
WEB_HOST = "127.0.0.1"

GIT_TIMEOUT = 60.0
FETCH_TIMEOUT = 120.0
PIP_TIMEOUT = 900.0
NPM_TIMEOUT = 900.0
RESTART_TIMEOUT = 120.0

STATE_NAME = "update-state.json"
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

WINDOWS_DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP


def now_iso() -> str:
    """带时区的当前时间，前端直接显示。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ------------------------------------------------------------------ 子进程
def _text(proc) -> str:
    """一条命令说过的所有话，原样带走：stdout 和 stderr 都要。"""
    parts = [str(proc.stdout or "").strip(), str(proc.stderr or "").strip()]
    return "\n".join(part for part in parts if part)


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


def collect_info(repo=None) -> dict[str, Any]:
    """本地与 GitHub 的差距。读不到远端就说读不到，绝不说「已是最新」。"""
    root = Path(repo) if repo else paths.project_root()
    info: dict[str, Any] = {
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
        "restart_pending": False,
    }


def write_state(state: dict[str, Any]) -> dict[str, Any]:
    """原子落盘：先写临时文件再换名，重启撞上写盘也不会留下半个 JSON。"""
    path = state_path()
    try:
        payload = json.dumps(state, ensure_ascii=False, indent=2, allow_nan=False)
    except (TypeError, ValueError) as exc:
        log.warning("更新状态存不下去：%s", exc)
        return state
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("更新状态写不下去：%s", exc)
    return state


def _heal_after_restart(state: dict[str, Any]) -> dict[str, Any]:
    """重启那一步跑完，本进程就没机会再写 done 了。新进程读到「重启中」时收尾。

    判据是 pid 换了：同一个进程里读到的 running 就是还在跑，不许冒充完成。
    """
    if state.get("state") != STATE_RUNNING or not state.get("restart_pending"):
        return state
    if int(state.get("pid") or 0) == os.getpid():
        return state
    steps = list(state.get("steps") or [])
    steps.append(
        {
            "name": STEP_RESTART,
            "ok": True,
            "output": f"服务已重启，新进程（pid {os.getpid()}）读到这一步。",
            "seconds": 0.0,
        }
    )
    state = {
        **state,
        "steps": steps,
        "state": STATE_DONE,
        "restart_pending": False,
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
    return _heal_after_restart(state)


# ------------------------------------------------------------------ 跑任务
class _Job:
    """一条流水线的记账员：每一步都落盘，失败就停。"""

    def __init__(self, state: dict[str, Any], root: Path) -> None:
        self.state = state
        self.root = root
        self.steps: list[dict[str, Any]] = list(state.get("steps") or [])

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
        [sys.executable, "-m", "pip", "install", "-r", "requirements.txt"],
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


def _restart_step(state: dict[str, Any]) -> tuple[bool, str]:
    """重启前先落盘：进程随时会没，状态文件是重启之后唯一的记忆。"""
    state["restart_pending"] = True
    write_state(state)
    ok, output = restart_service()
    if not ok:
        state["restart_pending"] = False
        write_state(state)
    return ok, output


def _run_job(state: dict[str, Any], kind: str, info: dict[str, Any]) -> None:
    root = paths.project_root()
    job = _Job(state, root)
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
        state["state"] = STATE_DONE
        state["finished_at"] = now_iso()
        write_state(state)
    except Exception as exc:  # 兜底：绝不把任务留在 running 上让前端一直转
        log.exception("更新任务出错")
        state["state"] = STATE_FAILED
        state["error"] = state.get("error") or f"更新任务出错：{exc}"
        state["finished_at"] = now_iso()
        write_state(state)


def _spawn(fn: Callable[[], None]) -> None:
    """后台线程跑流水线，接口立刻返回 job_id。测试把它换成就地执行。"""
    thread = threading.Thread(target=fn, name="update-job", daemon=True)
    thread.start()


def start_job(kind: str, info: dict[str, Any] | None = None) -> str:
    """建任务、落盘、开线程，返回 job_id。"""
    state = _default_state()
    state.update(
        {
            "job_id": uuid.uuid4().hex[:8],
            "kind": kind,
            "state": STATE_RUNNING,
            "started_at": now_iso(),
            "pid": os.getpid(),
        }
    )
    write_state(state)
    _spawn(lambda: _run_job(state, kind, dict(info or {})))
    return str(state["job_id"])


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


# 等端口释放再拉起新进程的小垫片。只用标准库、不 import btrader，能被 -c 直接跑。
_WINDOWS_SHIM = "\n".join(
    [
        "import socket, subprocess, sys, time",
        "port, repo, py = int(sys.argv[1]), sys.argv[2], sys.argv[3]",
        "deadline = time.time() + 90",
        "while time.time() < deadline:",
        "    probe = socket.socket()",
        "    probe.settimeout(0.3)",
        "    try:",
        "        busy = probe.connect_ex(('127.0.0.1', port)) == 0",
        "    finally:",
        "        probe.close()",
        "    if not busy:",
        "        break",
        "    time.sleep(0.2)",
        "log = open(repo + '/.run_out.log', 'ab')",
        "subprocess.Popen([py, 'run_web.py', '--host', '127.0.0.1', '--port', str(port)],",
        "                 cwd=repo, stdin=subprocess.DEVNULL, stdout=log,",
        "                 stderr=subprocess.STDOUT, close_fds=True, creationflags=0x00000008)",
        "",
    ]
)


def _restart_linux() -> tuple[bool, str]:
    """Linux 上服务是 systemd 的 bitrader 单元；仓库里给它装了一条 NOPASSWD sudo。"""
    args = ["sudo", "-n", "systemctl", "restart", SERVICE_NAME]
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
        return False, f"重启失败：{exc}"
    output = _text(proc)
    if proc.returncode != 0:
        return False, output or f"重启失败：退出码 {proc.returncode}"
    return True, output or "已重启。"


def _restart_windows() -> tuple[bool, str]:
    """Windows 上没有 systemd：起一个分离的垫片，等端口释放后拉起 run_web.py，
    再让本进程优雅退出。垫片先等端口，是因为自己还没放手之前新进程绑不上。"""
    root = paths.project_root()
    shim = [sys.executable, "-c", _WINDOWS_SHIM, str(WEB_PORT), str(root), sys.executable]
    try:
        proc = subprocess.Popen(
            shim,
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=WINDOWS_DETACHED,
            close_fds=True,
        )
    except OSError as exc:
        return False, f"拉新进程失败：{exc}"
    pid = getattr(proc, "pid", "?")
    _request_shutdown()
    return True, (
        f"已让垫片（pid {pid}）等 {WEB_HOST}:{WEB_PORT} 端口释放后拉起 run_web.py，"
        "本进程随即优雅退出。"
    )


def restart_service() -> tuple[bool, str]:
    """重启后端。成不成都要把命令的原文带回来。"""
    if sys.platform == "win32":
        return _restart_windows()
    return _restart_linux()
