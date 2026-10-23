"""一条命令发版：打 tag、建 Release、把安装包传上去。

    python packaging/release.py --version 0.6.1 --build

它只做发版这一件事，不替你打包：`--build` 的意思是「这次把已经打好的安装包一起传」，
`dist-electron/biTrader-Setup-<版本>.exe` 不在就直接退出。打包另走一步
（`cd electron && npm run dist`），三步怎么串见 docs/RELEASE.md。

前置校验（三处版本号一致、工作区干净、tag 不存在、安装包就位）任一不过就退出：
一个 tag 都不打、一个 push 都不发、一个请求都不发。

token 先看环境变量 `GITHUB_TOKEN`，没有再问 `git credential fill`；它只用来拼
Authorization 头，打印里最多出现 `gho_…（长度 40）` 这种样子。
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]

REPO = "xiaofengxfc/python_jy"
REMOTE = "xf"
API_BASE = "https://api.github.com"
UPLOAD_HOST = "uploads.github.com"
USER_AGENT = "biTrader-release"
TOKEN_ENV = "GITHUB_TOKEN"
HTTP_TIMEOUT = 60.0
UPLOAD_TIMEOUT = 1800.0
CHUNK = 1024 * 1024
REPORT_EVERY = 16 * 1024 * 1024
TAG_PREFIX = "v"
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


class ReleaseError(RuntimeError):
    """发版中途失败。message 直接给人看，里面不会有 token。"""


# ------------------------------------------------------------------ 哪三处版本号

def installer_path(root: Path, version: str) -> Path:
    return root / "dist-electron" / ("biTrader-Setup-" + version + ".exe")


def _first_match(path: Path, pattern: str) -> str:
    """文件里第一个匹配到的分组。读不到、没匹配都返回空串，交给前置校验去报。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    found = re.search(pattern, text, re.MULTILINE)
    return found.group(1).strip() if found else ""


def read_versions(root: Path | None = None) -> dict[str, str]:
    """三处版本号：pyproject（打包元数据）、electron（壳）、包里那个 __version__。

    三处必须一样——桌面端拿 Release 的 tag 比、后端在 BITRADER_READY 里报包里的号，
    对不上就会出现「显示 0.6.0 但其实是 0.6.1」这种查不出原因的怪事。
    """
    base = ROOT if root is None else root
    electron = base / "electron" / "package.json"
    electron_version = ""
    try:
        electron_version = str(json.loads(electron.read_text(encoding="utf-8")).get("version", ""))
    except (OSError, ValueError):
        electron_version = ""
    return {
        "pyproject.toml": _first_match(base / "pyproject.toml", r'^version\s*=\s*"([^"]+)"'),
        "electron/package.json": electron_version,
        "src/btrader/__init__.py": _first_match(
            base / "src" / "btrader" / "__init__.py", r'^__version__\s*=\s*"([^"]+)"'
        ),
    }


# ------------------------------------------------------------------ git

def _git(*args: str, cwd: Path | None = None, stdin_text: str | None = None):
    """唯一一处调 git 的地方。测试把整块换掉，所以它只管把参数摆好。"""
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd or ROOT),
        input=stdin_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _git_step(root: Path, args: list[str], what: str) -> str:
    """跑一条会改仓库的 git 命令。非 0 就带着原文停下——不重试、不吞。"""
    proc = _git(*args, cwd=root)
    if proc.returncode != 0:
        raw = (proc.stderr or proc.stdout or "").strip()
        raise ReleaseError(f"{what}失败（git 退出码 {proc.returncode}）：\n{raw or '（没有输出）'}")
    return (proc.stdout or "").strip()


# ------------------------------------------------------------------ 前置校验

def preflight(root: Path | None = None, version: str = "", *, build: bool = False) -> list[str]:
    """把「现在能不能发」一次问完，返回所有问题（空表就是能发）。

    一次报全而不是报第一个：三处版本号常常一起忘，逐个试要跑三遍。
    """
    base = ROOT if root is None else root
    problems: list[str] = []

    if not VERSION_RE.match(version or ""):
        problems.append(
            "--version 要是 0.6.1 这种三段数字，收到的是「" + (version or "（空）") + "」"
        )

    for where, got in read_versions(base).items():
        if got != version:
            problems.append(
                where
                + " 里是「"
                + (got or "（读不到）")
                + "」，不是「"
                + version
                + "」：三处版本号要一起改"
            )

    status = _git("status", "--porcelain", cwd=base)
    dirty = (status.stdout or "").strip()
    if status.returncode != 0:
        problems.append("读不出工作区状态：" + (status.stderr or "").strip())
    elif dirty:
        problems.append(
            "工作树不干净（tag 要指向一棵提交过的树，先把这些提交掉）：\n" + dirty
        )

    tag = TAG_PREFIX + (version or "")
    tags = _git("tag", "--list", tag, cwd=base)
    if (tags.stdout or "").strip():
        problems.append(
            "tag " + tag + " 已经存在：版本号没抬？已发的 tag 不要改，抬一个版本再发"
        )

    if build:
        exe = installer_path(base, version)
        if not exe.is_file():
            problems.append(
                "带了 --build，但没有安装包："
                + str(exe.relative_to(base))
                + "（先在 electron 目录跑 npm run dist）"
            )
    return problems


# ------------------------------------------------------------------ token

CREDENTIAL_INPUT = "protocol=https\nhost=github.com\n\n"


def mask_token(token: str) -> str:
    """能印出来的最详细程度：前四位 + 长度。别把原文印到任何地方。"""
    token = token or ""
    if not token:
        return "（空）"
    if len(token) < 8:
        return "…（长度 " + str(len(token)) + "）"
    return token[:4] + "…（长度 " + str(len(token)) + "）"


def find_token(root: Path | None = None, env: dict[str, str] | None = None) -> tuple[str, str]:
    """返回 (token, 来源)。来源只写「环境变量」或「git credential」，不写值。"""
    environ = os.environ if env is None else env
    token = (environ.get(TOKEN_ENV) or "").strip()
    if token:
        return token, "环境变量 " + TOKEN_ENV

    proc = _git("credential", "fill", cwd=root, stdin_text=CREDENTIAL_INPUT)
    if proc.returncode != 0:
        raise ReleaseError(
            "没拿到 token：环境变量 "
            + TOKEN_ENV
            + " 是空的，`git credential fill` 也失败了（退出码 "
            + str(proc.returncode)
            + "）。把 token 放进 "
            + TOKEN_ENV
            + " 再跑一次，它需要 Contents: write 权限。"
        )
    for line in (proc.stdout or "").splitlines():
        if line.startswith("password="):
            value = line[len("password="):].strip()
            if value:
                return value, "git credential fill（github.com）"
    raise ReleaseError(
        "没拿到 token：环境变量 "
        + TOKEN_ENV
        + " 是空的，`git credential fill` 里也没有 github.com 的 password。"
        "（不打印它的任何内容。）换一把有 Contents: write 的 token 放进 "
        + TOKEN_ENV
        + " 再跑。"
    )


# ------------------------------------------------------------------ 说明文字

def read_notes(root: Path | None = None, notes_file: str | None = None, version: str = "") -> str:
    """Release 的正文：给了文件就用文件，没给就用最近提交。空正文直接拒（422 的常客）。"""
    if notes_file:
        path = Path(notes_file)
        if not path.is_absolute() and not path.is_file():
            path = (ROOT if root is None else root) / notes_file
        try:
            body = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ReleaseError("读不到说明文件 " + str(path) + "：" + str(exc)) from None
        if not body.strip():
            raise ReleaseError("说明文件 " + str(path) + " 是空的：Release 正文不能空着发")
        return body

    head = "biTrader " + version
    log = _git("log", "--no-merges", "--pretty=format:- %s", "-20", cwd=root)
    lines = [line for line in (log.stdout or "").splitlines() if line.strip()]
    if not lines:
        return head + "\n"
    return "\n".join([head, "", "最近的提交：", *lines]) + "\n"


def explain_http(code: int, body: str, what: str) -> str:
    """403 / 422 说人话，剩下的原样贴。别自己编原因。"""
    raw = (body or "").strip() or "（空）"
    if code == 403:
        return (
            what
            + "被 GitHub 拒了（403）：这把 token 没有 Contents: write，换一把或手动在网页上发。"
            "GitHub 原话：" + raw
        )
    if code == 422:
        return (
            what
            + "被 GitHub 拒了（422）：常见是 tag 已经存在，或者 release 的 body 是空的。"
            "GitHub 原话：" + raw
        )
    return what + "失败（HTTP " + str(code) + "）。GitHub 原话：" + raw


# ------------------------------------------------------------------ HTTP

def _post_json(url: str, payload: dict[str, Any], token: str, *, what: str = "请求", timeout: float = HTTP_TIMEOUT) -> dict[str, Any]:
    """发一个 JSON POST。4xx/5xx 翻成中文人话，连不上原样贴。不重试。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise ReleaseError(explain_http(exc.code, detail, what)) from None
    except urllib.error.URLError as exc:
        raise ReleaseError(what + "连不上 GitHub：" + str(exc.reason)) from None
    try:
        return json.loads(raw)
    except ValueError:
        raise ReleaseError(what + "回来的东西读不懂：" + raw[:200]) from None


def _mb(size: int) -> str:
    return f"{size / (1024 * 1024):.1f} MB"


def _upload_asset(
    path: Path,
    release_id: Any,
    token: str,
    *,
    progress: Callable[[str], None] = print,
    timeout: float = UPLOAD_TIMEOUT,
) -> dict[str, Any]:
    """边读边发。安装包 140 多 MB，整个读进内存再发是给自己找麻烦。"""
    size = path.stat().st_size
    name = path.name
    target = "/repos/" + REPO + "/releases/" + str(release_id) + "/assets?" + urllib.parse.urlencode({"name": name})
    connection = http.client.HTTPSConnection(UPLOAD_HOST, timeout=timeout)
    try:
        connection.putrequest("POST", target, skip_accept_encoding=True)
        connection.putheader("Authorization", "Bearer " + token)
        connection.putheader("Accept", "application/vnd.github+json")
        connection.putheader("Content-Type", "application/octet-stream")
        connection.putheader("Content-Length", str(size))
        connection.putheader("User-Agent", USER_AGENT)
        connection.endheaders()
        sent = 0
        reported = 0
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(CHUNK)
                if not chunk:
                    break
                connection.send(chunk)
                sent += len(chunk)
                if sent - reported >= REPORT_EVERY or sent >= size:
                    reported = sent
                    percent = 100 if not size else int(sent * 100 / size)
                    progress("  已上传 " + _mb(sent) + " / " + _mb(size) + "（" + str(percent) + "%）")
        response = connection.getresponse()
        status = response.status
        raw = response.read().decode("utf-8", "replace")
    except OSError as exc:
        raise ReleaseError("上传 " + name + " 时断了：" + str(exc)) from None
    finally:
        connection.close()
    if status >= 400:
        raise ReleaseError(explain_http(status, raw, "上传 " + name + " "))
    try:
        return json.loads(raw)
    except ValueError:
        raise ReleaseError("上传 " + name + " 回来的东西读不懂：" + raw[:200]) from None


# ------------------------------------------------------------------ 主流程

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="release.py",
        description="打 tag、建 GitHub Release、上传安装包（不打包，只发版）",
    )
    parser.add_argument("--version", required=True, help="这次发的版本号，例如 0.6.1")
    parser.add_argument(
        "--build",
        action="store_true",
        help="这次把已经打好的 dist-electron/biTrader-Setup-<版本>.exe 一起传上去",
    )
    parser.add_argument("--notes-file", default="", help="Release 正文从哪个文件读（不给就用最近提交）")
    parser.add_argument("--dry-run", action="store_true", help="只打印要做的事，什么都不做")
    return parser.parse_args(argv)


def _plan(version: str, *, build: bool, notes_file: str) -> list[str]:
    tag = TAG_PREFIX + version
    steps = [
        "git tag -a " + tag + " -m \"biTrader " + version + "\"",
        "git push " + REMOTE + " " + tag,
        "POST " + API_BASE + "/repos/" + REPO + "/releases"
        "（tag_name=" + tag + "，name=" + tag + "，draft=false，prerelease=false，"
        "body 来自 " + (notes_file or "最近 20 条提交") + "）",
    ]
    if build:
        steps.append(
            "POST https://" + UPLOAD_HOST + "/repos/" + REPO + "/releases/<id>/assets?name=biTrader-Setup-"
            + version + ".exe（边读边发，打印进度）"
        )
    else:
        steps.append("（没带 --build：不传安装包。桌面端看不到这一版）")
    steps.append("打印 Release 的 html_url")
    return steps


def main(
    argv: list[str] | None = None,
    *,
    env: dict[str, str] | None = None,
    progress: Callable[[str], None] = print,
) -> int:
    args = parse_args(argv)
    version = args.version.strip()
    tag = TAG_PREFIX + version

    problems = preflight(ROOT, version, build=args.build)
    if problems:
        print(
            "发版停下：" + str(len(problems)) + " 个前置条件不满足。什么都没做（没打 tag、没 push、没发请求）。",
            file=sys.stderr,
        )
        for problem in problems:
            print("  ✗ " + problem, file=sys.stderr)
        return 2

    if args.dry_run:
        print("--dry-run：下面是要做的事，一个请求都不发、一个 tag 都不打。")
        for index, step in enumerate(_plan(version, build=args.build, notes_file=args.notes_file), 1):
            print("  " + str(index) + ". " + step)
        return 0

    try:
        token, source = find_token(ROOT, env)
        body = read_notes(ROOT, args.notes_file or None, version)
    except ReleaseError as exc:
        print("发版停下：" + str(exc), file=sys.stderr)
        print("什么都没做（没打 tag、没 push、没发请求）。", file=sys.stderr)
        return 2

    print("用的 token：" + mask_token(token) + "（来自 " + source + "）")

    try:
        _git_step(ROOT, ["tag", "-a", tag, "-m", "biTrader " + version], "打 tag " + tag + " ")
        print("打 tag " + tag + "：好了")

        _git_step(ROOT, ["push", REMOTE, tag], "推送 tag 到 " + REMOTE + " ")
        print("推送 tag 到 " + REMOTE + "：好了")

        release = _post_json(
            API_BASE + "/repos/" + REPO + "/releases",
            {
                "tag_name": tag,
                "name": tag,
                "body": body,
                "draft": False,
                "prerelease": False,
            },
            token,
            what="建 Release ",
        )
        release_id = release.get("id")
        if not release_id:
            raise ReleaseError("建 Release 回来了，但没有 id：" + json.dumps(release, ensure_ascii=False)[:200])
        print("建 Release " + tag + "：id " + str(release_id))

        if args.build:
            exe = installer_path(ROOT, version)
            print("上传 " + exe.name + "（" + _mb(exe.stat().st_size) + "）")
            asset = _upload_asset(exe, release_id, token, progress=progress)
            print("传完：" + str(asset.get("name") or exe.name))
        else:
            print("没带 --build：这次没传安装包，桌面端不会看到这一版。")

        url = str(release.get("html_url") or "")
        print("Release：" + (url or "（GitHub 没给 html_url）"))
        return 0 if url else 1
    except ReleaseError as exc:
        print("发版失败：" + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())