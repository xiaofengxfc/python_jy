"""t96：发版脚本的拒法、顺序，和 token 有没有漏出去。

这里不碰网络、不打真 tag：git 与 HTTP 全是替身。钉三件事——四种情况下必须**什么都没做**
（不打 tag、不 push、不发请求）；happy path 的动作顺序是 tag → push → 建 Release → 传 exe；
任何一句打印里都不许出现 token 原文。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "packaging" / "release.py"
TOKEN = "ghp_" + "S3cr3t" * 6 + "abcd"   # 40 个字符，和 GitHub 的 token 一样长
VERSION = "0.6.1"
TAG = "v" + VERSION


def _load():
    """按路径加载脚本（packaging 不是包，import 不到）。"""
    spec = importlib.util.spec_from_file_location("release_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Proc:
    """subprocess.CompletedProcess 的那几个字段，够脚本用就行。"""

    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class Harness:
    """把 git 与 HTTP 换成记账替身：既当替身，也当「到底做了什么」的记录。"""

    def __init__(self, module, root: Path) -> None:
        self.module = module
        self.root = root
        self.git_calls: list[tuple[str, ...]] = []
        self.http_calls: list[tuple] = []
        self.git_status = ""
        self.existing_tags = ""
        self.credential_stdout = "username=xf\npassword=" + TOKEN + "\n"
        self.release = {
            "id": 4242,
            "html_url": "https://github.com/xiaofengxfc/python_jy/releases/tag/" + TAG,
        }
        self.asset = {"name": "biTrader-Setup-" + VERSION + ".exe", "size": 1024}
        self.fail_release: tuple[int, str] | None = None
        # 真实现留一份：有一条测试要看着它真的把文件一块块发出去
        self.real_upload = module._upload_asset
        module._git = self._git
        module._post_json = self._post_json
        module._upload_asset = self._upload_asset

    # ------------------------------------------------------------ 替身
    def _git(self, *args: str, cwd=None, stdin_text=None):
        self.git_calls.append(tuple(args))
        joined = " ".join(args)
        if joined.startswith("status --porcelain"):
            return _Proc(self.git_status)
        if joined.startswith("tag --list"):
            return _Proc(self.existing_tags)
        if joined.startswith("credential fill"):
            return _Proc(self.credential_stdout)
        if joined.startswith("log"):
            return _Proc("- 上一次的提交标题\n")
        return _Proc("")

    def _post_json(self, url: str, payload: dict, token: str, **kwargs):
        self.http_calls.append(("POST", url, payload, token))
        if self.fail_release is not None:
            code, body = self.fail_release
            raise self.module.ReleaseError(self.module.explain_http(code, body, "建 Release "))
        return dict(self.release)

    def _upload_asset(self, path, release_id, token: str, **kwargs):
        self.http_calls.append(("UPLOAD", str(path), release_id, token))
        return dict(self.asset)

    # ------------------------------------------------------------ 记账
    def writes(self) -> list[tuple[str, ...]]:
        """会改仓库的命令：`tag -a`（打 tag）与 `push`。

        `status --porcelain`、`tag --list`、`log`、`credential fill` 都是读，不算。
        """
        return [
            c
            for c in self.git_calls
            if c and (c[0] == "push" or (c[0] == "tag" and "-a" in c))
        ]

    def run(self, *extra: str) -> int:
        return self.module.main(["--version", VERSION, *extra])

    # ------------------------------------------------------------ 造情况
    def set_version(self, where: str, value: str) -> None:
        if where == "pyproject":
            (self.root / "pyproject.toml").write_text(
                '[project]\nname = "btrader"\nversion = "' + value + '"\n', encoding="utf-8"
            )
        elif where == "electron":
            (self.root / "electron" / "package.json").write_text(
                '{\n  "name": "bitrader-shell",\n  "version": "' + value + '"\n}\n', encoding="utf-8"
            )
        elif where == "init":
            (self.root / "src" / "btrader" / "__init__.py").write_text(
                '__version__ = "' + value + '"\n', encoding="utf-8"
            )
        else:  # pragma: no cover - 测试写错名字时立刻炸
            raise AssertionError("哪里：" + where)

    def drop_installer(self) -> None:
        (self.root / "dist-electron" / ("biTrader-Setup-" + VERSION + ".exe")).unlink()


@pytest.fixture
def repo(tmp_path):
    """一个最小的仓库：三处版本号 + 一个打好的安装包。"""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "btrader"\nversion = "0.6.1"\n', encoding="utf-8"
    )
    (tmp_path / "electron").mkdir()
    (tmp_path / "electron" / "package.json").write_text(
        '{\n  "name": "bitrader-shell",\n  "version": "0.6.1"\n}\n', encoding="utf-8"
    )
    (tmp_path / "src" / "btrader").mkdir(parents=True)
    (tmp_path / "src" / "btrader" / "__init__.py").write_text(
        '__version__ = "0.6.1"\n', encoding="utf-8"
    )
    (tmp_path / "dist-electron").mkdir()
    (tmp_path / "dist-electron" / ("biTrader-Setup-" + VERSION + ".exe")).write_bytes(b"x" * 4096)
    return tmp_path


@pytest.fixture
def box(monkeypatch, repo):
    module = _load()
    monkeypatch.setattr(module, "ROOT", repo)
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    return Harness(module, repo)


# ---------------------------------------------------------------- 必须拒的四种

def test_all_three_version_files_must_match(box, capsys):
    box.set_version("electron", "0.6.0")
    assert box.run() == 2
    err = capsys.readouterr().err
    assert "electron/package.json" in err
    assert "0.6.0" in err and VERSION in err
    assert box.writes() == []
    assert box.http_calls == []


def test_a_dirty_work_tree_is_refused(box, capsys):
    box.git_status = " M src/btrader/web/app.py\n"
    assert box.run() == 2
    err = capsys.readouterr().err
    assert "工作树" in err or "工作区" in err
    assert "src/btrader/web/app.py" in err
    assert box.writes() == []
    assert box.http_calls == []


def test_an_existing_tag_is_refused(box, capsys):
    box.existing_tags = TAG + "\n"
    assert box.run() == 2
    err = capsys.readouterr().err
    assert TAG in err
    assert box.writes() == []
    assert box.http_calls == []


def test_build_without_the_installer_is_refused(box, capsys):
    box.drop_installer()
    assert box.run("--build") == 2
    err = capsys.readouterr().err
    assert "biTrader-Setup-" + VERSION + ".exe" in err
    assert box.writes() == []
    assert box.http_calls == []


def test_without_build_the_installer_is_not_required(box):
    box.drop_installer()
    assert box.run() == 0
    assert [c[0] for c in box.http_calls] == ["POST"]


# ---------------------------------------------------------------- happy path

def test_the_happy_path_goes_tag_push_release_upload(box, capsys):
    notes = box.root / "notes.md"
    notes.write_text("这版修了什么\n", encoding="utf-8")
    assert box.run("--build", "--notes-file", str(notes)) == 0

    assert box.writes() == [
        ("tag", "-a", TAG, "-m", "biTrader " + VERSION),
        ("push", "xf", TAG),
    ]
    kinds = [c[0] for c in box.http_calls]
    assert kinds == ["POST", "UPLOAD"]

    _, url, payload, _token = box.http_calls[0]
    assert url == "https://api.github.com/repos/xiaofengxfc/python_jy/releases"
    assert payload["tag_name"] == TAG
    assert payload["name"] == TAG
    assert payload["body"] == "这版修了什么\n"
    assert payload["draft"] is False
    assert payload["prerelease"] is False

    _, path, release_id, _ = box.http_calls[1]
    assert Path(path).name == "biTrader-Setup-" + VERSION + ".exe"
    assert release_id == 4242

    out = capsys.readouterr().out
    assert "https://github.com/xiaofengxfc/python_jy/releases/tag/" + TAG in out


def test_the_body_falls_back_to_the_recent_commits(box):
    assert box.run() == 0
    payload = box.http_calls[0][2]
    assert payload["body"].strip()
    assert "上一次的提交标题" in payload["body"]


def test_a_failed_tag_stops_before_the_push(box, capsys):
    def boom(*args, **kwargs):
        box.git_calls.append(tuple(args))
        if args[:2] == ("tag", "-a"):
            return _Proc("", "fatal: tag already exists", 128)
        return _Proc("")

    box.module._git = boom
    assert box.run() == 1
    assert [c[0] for c in box.writes()] == ["tag"]
    assert box.http_calls == []
    assert "fatal: tag already exists" in capsys.readouterr().err


# ---------------------------------------------------------------- dry-run

def test_dry_run_prints_the_plan_and_does_nothing(box, capsys):
    assert box.run("--build", "--dry-run") == 0
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert TAG in out
    assert box.writes() == []
    assert box.http_calls == []


# ---------------------------------------------------------------- token

def test_the_token_never_reaches_any_printed_line(box, capsys):
    assert box.run("--build") == 0
    captured = capsys.readouterr()
    assert TOKEN not in captured.out
    assert TOKEN not in captured.err
    assert "（长度 " in captured.out


def test_the_token_stays_hidden_when_github_refuses(box, capsys):
    box.fail_release = (403, '{"message":"Resource not accessible by personal access token"}')
    assert box.run("--build") == 1
    captured = capsys.readouterr()
    assert TOKEN not in captured.out
    assert TOKEN not in captured.err
    assert "Contents: write" in captured.err


def test_a_422_mentions_the_likely_causes(box, capsys):
    box.fail_release = (422, '{"message":"Validation Failed"}')
    assert box.run("--build") == 1
    err = capsys.readouterr().err
    assert "422" in err
    assert "tag" in err
    assert "body" in err


def test_the_token_falls_back_to_git_credential(box, monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert box.run("--build") == 0
    assert ("credential", "fill") in box.git_calls
    assert box.http_calls[0][3] == TOKEN
    assert TOKEN not in capsys.readouterr().out


def test_no_token_at_all_refuses_before_the_tag(box, monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    box.credential_stdout = "username=xf\n"
    assert box.run("--build") == 2
    err = capsys.readouterr().err
    assert "GITHUB_TOKEN" in err
    assert box.writes() == []
    assert box.http_calls == []

# ---------------------------------------------------------------- 上传是边读边发

class _FakeResponse:
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self._body = body.encode("utf-8")

    def read(self) -> bytes:
        return self._body


class _FakeConnection:
    """替掉 http.client.HTTPSConnection：既记账，也证明文件是被一块块啃的。"""

    def __init__(self, host: str, timeout: float | None = None) -> None:
        self.host = host
        self.timeout = timeout
        self.request: tuple[str, str] | None = None
        self.headers: list[tuple[str, str]] = []
        self.chunks: list[int] = []
        self.closed = False
        _FakeConnection.last = self

    def putrequest(self, method: str, target: str, skip_accept_encoding: bool = False) -> None:
        self.request = (method, target)

    def putheader(self, name: str, value: str) -> None:
        self.headers.append((name, value))

    def endheaders(self) -> None:
        return None

    def send(self, chunk: bytes) -> None:
        self.chunks.append(len(chunk))

    def getresponse(self):
        return _FakeResponse(201, '{"name": "biTrader-Setup-0.6.1.exe", "size": 3145735}')

    def close(self) -> None:
        self.closed = True


def test_the_installer_is_streamed_in_chunks(box, monkeypatch):
    import http.client

    exe = box.root / "dist-electron" / ("biTrader-Setup-" + VERSION + ".exe")
    exe.write_bytes(b"z" * (3 * 1024 * 1024 + 7))   # 3 MB 多一点，多出来那 7 字节也要发出去

    monkeypatch.setattr(http.client, "HTTPSConnection", _FakeConnection)
    lines: list[str] = []
    asset = box.real_upload(exe, 4242, TOKEN, progress=lines.append)

    connection = _FakeConnection.last
    assert connection.host == "uploads.github.com"
    method, target = connection.request
    assert method == "POST"
    assert "/repos/xiaofengxfc/python_jy/releases/4242/assets" in target
    assert "name=biTrader-Setup-" + VERSION + ".exe" in target
    headers = dict(connection.headers)
    assert headers["Content-Type"] == "application/octet-stream"
    assert headers["Content-Length"] == str(exe.stat().st_size)
    # 一块一块发：每块都不超过 CHUNK，加起来正好是文件大小（没有把 146 MB 整个读进内存）
    assert max(connection.chunks) <= box.module.CHUNK
    assert sum(connection.chunks) == exe.stat().st_size
    assert len(connection.chunks) > 1
    assert any("已上传" in line for line in lines)
    assert asset["name"] == "biTrader-Setup-" + VERSION + ".exe"
    assert connection.closed

# ---------------------------------------------------------------- 文档别和脚本对不上

RELEASE_DOC = ROOT / "docs" / "RELEASE.md"


def test_the_release_doc_says_the_things_people_get_wrong():
    text = RELEASE_DOC.read_text(encoding="utf-8")
    # 三处版本号、权限、安装包不进仓库、桌面端必须传 exe、服务器版不看 Release
    for needle in (
        "pyproject.toml",
        "electron/package.json",
        "src/btrader/__init__.py",
        "Contents: write",
        "安装包不进仓库",
        "每次发版都要传安装包",
        "--ff-only",
        "packaging/release.py",
    ):
        assert needle in text, needle
    # 别把「三条命令」写成假的：脚本里那个 flag 和文档里写的要一致
    assert "--build" in text and "--version" in text and "--dry-run" in text