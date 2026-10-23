"""一条命令发版：打 tag、建 Release、把安装包传上去。

    python packaging/release.py --version 0.6.1 --build

它只做发版这一件事，不替你打包：`--build` 的意思是「这次把已经打好的安装包一起传」，
`dist-electron/biTrader-Setup-<版本>.exe` 不在就直接退出。打包另走一步
（`cd electron && npm run dist`），三步怎么串见 docs/RELEASE.md。

前置校验（三处版本号一致、工作区干净、安装包就位）任一不过就退出：一个 tag 都不打、
一个 push 都不发、一个改动都不做。

顺序是：打 tag → 推 tag → **建 draft Release** → 传安装包 → PATCH 成正式发布。
先 draft 后发布是刻意的：传输 150 MB 中途断掉很常见，正式发布过的 Release 会立刻被
`/releases/latest` 当成最新，桌面用户看到「有新版」却下不到包。传崩了就停在 draft，
用户看不到；同一条命令再跑一次会认出那个 draft 接着传（tag 还在，不重打、不重推）。

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


class ReleaseRefused(ReleaseError):
    """还没动手就发现不该继续：退出码 2，和前置校验不过一样什么都不做。"""


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

    # tag 打没打过不在这里判：打过也可能是上次停在 draft（见 tag_exists + main 里的续传）。

    if build:
        exe = installer_path(base, version)
        if not exe.is_file():
            problems.append(
                "带了 --build，但没有安装包："
                + str(exe.relative_to(base))
                + "（先在 electron 目录跑 npm run dist）"
            )
    return problems


def tag_exists(root: Path | None = None, tag: str = "") -> bool:
    """本地有没有这个 tag。有也不一定不能发——上次可能停在 draft（续传）。"""
    proc = _git("tag", "--list", tag, cwd=root if root is not None else ROOT)
    return bool((proc.stdout or "").strip())


def installer_name(version: str) -> str:
    return "biTrader-Setup-" + version + ".exe"


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

def _json_request(
    method: str,
    url: str,
    payload: dict[str, Any] | None,
    token: str,
    *,
    what: str = "请求",
    timeout: float = HTTP_TIMEOUT,
    allow_404: bool = False,
) -> dict[str, Any] | None:
    """发一个 JSON 请求。4xx/5xx 翻成中文人话，连不上原样贴。不重试。

    allow_404 只给「先问问这个 tag 上有没有 Release」用：404 是正常答案（没有），不是错。
    """
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
    }
    if data is not None:
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        if allow_404 and exc.code == 404:
            return None
        raise ReleaseError(explain_http(exc.code, detail, what)) from None
    except urllib.error.URLError as exc:
        raise ReleaseError(what + "连不上 GitHub：" + str(exc.reason)) from None
    try:
        return json.loads(raw)
    except ValueError:
        raise ReleaseError(what + "回来的东西读不懂：" + raw[:200]) from None


def _post_json(url: str, payload: dict[str, Any], token: str, *, what: str = "请求", timeout: float = HTTP_TIMEOUT) -> dict[str, Any]:
    return _json_request("POST", url, payload, token, what=what, timeout=timeout) or {}


def _patch_json(url: str, payload: dict[str, Any], token: str, *, what: str = "请求", timeout: float = HTTP_TIMEOUT) -> dict[str, Any]:
    return _json_request("PATCH", url, payload, token, what=what, timeout=timeout) or {}


def _get_json(url: str, token: str, *, what: str = "请求", timeout: float = HTTP_TIMEOUT) -> dict[str, Any] | None:
    """读一个 JSON。404 回 None（「没有」也是答案），其余照旧报错。"""
    return _json_request("GET", url, None, token, what=what, timeout=timeout, allow_404=True)


def releases_url() -> str:
    return API_BASE + "/repos/" + REPO + "/releases"


def release_url(release_id: Any) -> str:
    return releases_url() + "/" + str(release_id)


def release_by_tag_url(tag: str) -> str:
    return releases_url() + "/tags/" + urllib.parse.quote(tag)


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


def _tell_the_draft_is_waiting(tag: str, exc: ReleaseError) -> None:
    """传崩了 / 发布没成：那条件 Release 还是 draft，用户看不到。把恢复路径写清楚。"""
    print("发版没完成：" + str(exc), file=sys.stderr)
    print(
        "tag " + tag + " 上的 Release 还是 draft：用户看不到它（只有你登着 GitHub 才看得见），"
        "/releases/latest 也不会把它当最新，桌面端不会提示有新版。",
        file=sys.stderr,
    )
    print(
        "恢复办法：网络缓过来之后，把同一条命令再跑一次 —— tag 还在、" + tag
        + " 的 draft 还在，脚本会认出它接着传，传完自动发布（不重打 tag、不重推）。",
        file=sys.stderr,
    )
    print(
        "不想续传：在网页上删掉那个 draft Release，再 git tag -d " + tag
        + "，然后重新跑一遍。（这一节也写在 docs/RELEASE.md 第 5 节。）",
        file=sys.stderr,
    )


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


def _plan(version: str, *, build: bool, notes_file: str, tag_exists: bool = False) -> list[str]:
    tag = TAG_PREFIX + version
    steps: list[str] = []
    if tag_exists:
        steps.append(
            "GET " + release_by_tag_url(tag) + "（tag 已经在了：先问 GitHub 上次是不是停在 draft）"
        )
        steps.append(
            "那个 Release 不是 draft（或者根本没有）：停下，什么都不做 —— 抬一个版本再发"
        )
        steps.append("是 draft：跳过打 tag / 推 tag / 建 Release，直接接着传")
    else:
        steps.append("git tag -a " + tag + " -m \"biTrader " + version + "\"")
        steps.append("git push " + REMOTE + " " + tag)
        steps.append(
            "POST " + releases_url() + "（tag_name=" + tag + "，name=" + tag
            + "，draft=true —— 先藏起来，桌面端看不到，prerelease=false，"
            "body 来自 " + (notes_file or "最近 20 条提交") + "）"
        )
    if build:
        steps.append(
            "POST https://" + UPLOAD_HOST + "/repos/" + REPO + "/releases/<id>/assets?name="
            + installer_name(version) + "（边读边发，打印进度）"
        )
    else:
        steps.append("（没带 --build：不传安装包。桌面端看不到这一版）")
    steps.append(
        "PATCH " + release_url("<id>") + "（draft=false，body 来自 "
        + (notes_file or "最近 20 条提交") + "）—— 安装包传完才发布"
    )
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
    exe_name = installer_name(version)

    problems = preflight(ROOT, version, build=args.build)
    if problems:
        print(
            "发版停下：" + str(len(problems)) + " 个前置条件不满足。什么都没做（没打 tag、没 push、没发请求）。",
            file=sys.stderr,
        )
        for problem in problems:
            print("  ✗ " + problem, file=sys.stderr)
        return 2

    # tag 打没打过不拦人：可能上次停在 draft，接着传才对。dry-run 里连问都不问。
    tag_taken = tag_exists(ROOT, tag)

    if args.dry_run:
        print("--dry-run：下面是要做的事，一个请求都不发、一个 tag 都不打。")
        for index, step in enumerate(
            _plan(
                version,
                build=args.build,
                notes_file=args.notes_file,
                tag_exists=tag_taken,
            ),
            1,
        ):
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
        have_asset = False
        html_url = ""

        if tag_taken:
            # tag 已经推上去了：只可能是上次停在 draft。问一句，别瞎猜。
            existing = _get_json(
                release_by_tag_url(tag), token, what="问 tag " + tag + " 上的 Release "
            )
            if existing is None:
                raise ReleaseRefused(
                    "tag " + tag + " 已经存在，但 GitHub 上没有它的 Release（上次建 Release 那一步没成）。"
                    "在网页上手动发这一版，或者 git tag -d " + tag + " 之后重跑。"
                )
            if not existing.get("draft"):
                raise ReleaseRefused(
                    "tag " + tag + " 已经发过了（Release #" + str(existing.get("id"))
                    + " 不是 draft）：同一版不要发两次，抬一个版本再发。"
                )
            release_id = existing.get("id")
            if not release_id:
                raise ReleaseError(
                    "那个 draft Release 没有 id：" + json.dumps(existing, ensure_ascii=False)[:200]
                )
            html_url = str(existing.get("html_url") or "")
            have_asset = any(
                str(asset.get("name") or "") == exe_name for asset in (existing.get("assets") or [])
            )
            print(
                "tag " + tag + " 已经在，Release #" + str(release_id) + " 还是 draft：接着传，不重打 tag。"
            )
        else:
            _git_step(ROOT, ["tag", "-a", tag, "-m", "biTrader " + version], "打 tag " + tag + " ")
            print("打 tag " + tag + "：好了")

            _git_step(ROOT, ["push", REMOTE, tag], "推送 tag 到 " + REMOTE + " ")
            print("推送 tag 到 " + REMOTE + "：好了")

            created = _post_json(
                releases_url(),
                {
                    "tag_name": tag,
                    "name": tag,
                    "body": body,
                    "draft": True,
                    "prerelease": False,
                },
                token,
                what="建 Release ",
            )
            release_id = created.get("id")
            if not release_id:
                raise ReleaseError(
                    "建 Release 回来了，但没有 id：" + json.dumps(created, ensure_ascii=False)[:200]
                )
            html_url = str(created.get("html_url") or "")
            print(
                "建 draft Release " + tag + "：id " + str(release_id)
                + "（还没发布，用户看不到）"
            )

        if args.build:
            exe = installer_path(ROOT, version)
            if have_asset:
                print("这个 draft 里已经有 " + exe.name + "：跳过上传，直接发布。")
            else:
                print("上传 " + exe.name + "（" + _mb(exe.stat().st_size) + "）")
                try:
                    asset = _upload_asset(exe, release_id, token, progress=progress)
                except ReleaseError as exc:
                    _tell_the_draft_is_waiting(tag, exc)
                    return 1
                print("传完：" + str(asset.get("name") or exe.name))
        else:
            print("没带 --build：这次没传安装包，桌面端不会看到这一版。")

        try:
            published = _patch_json(
                release_url(release_id),
                {"draft": False, "body": body},
                token,
                what="发布 Release ",
            )
        except ReleaseError as exc:
            _tell_the_draft_is_waiting(tag, exc)
            return 1
        print("发布 Release " + tag + "：好了")

        url = str(published.get("html_url") or html_url or "")
        print("Release：" + (url or "（GitHub 没给 html_url）"))
        return 0 if url else 1
    except ReleaseRefused as exc:
        print("发版停下：" + str(exc), file=sys.stderr)
        print("什么都没做（没打 tag、没 push、没改任何东西）。", file=sys.stderr)
        return 2
    except ReleaseError as exc:
        print("发版失败：" + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())