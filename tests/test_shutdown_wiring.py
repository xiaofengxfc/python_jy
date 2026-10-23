"""t86（F6）：关机钩子必须接在真正跑的那个进程上。

AGENTS §11 一直写着「`web/__main__.py` 把 Server 的开关交给 updater」，可本机真正在跑的是
`run_web.py`（原来用 `uvicorn.run`），那个机制在真实进程里根本不存在——在线更新走到「重启」
那一步时只能靠 SIGINT 兜底。这里钉三件事：两条启动路径共用一份 `build_server` / `serve`；
`run_web.py` 也把 `should_exit` 交给 updater；钩子触发时 uvicorn 是优雅退出（不硬切）。
"""

from __future__ import annotations

import importlib.util
import sys
import threading
import time
from pathlib import Path

import pytest

from btrader.core import updater
from btrader.web import __main__ as web_main

ROOT = Path(__file__).resolve().parents[1]


def _load_run_web():
    """把仓库根目录那个脚本按路径加载（它不是包里的模块）。"""
    spec = importlib.util.spec_from_file_location("run_web_under_test", ROOT / "run_web.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _clean_updater_globals(monkeypatch):
    """`build_server` 会把退出钩子和端口记进 updater 的模块级变量。

    这两个是全局的：测完不还回去，后面的用例（尤其是钉「端口兜底 8765」的那几条）
    就会看到这里传进去的临时端口，红得莫名其妙。
    """
    monkeypatch.setattr(updater, "_web_port", None)
    updater.set_shutdown_hook(None)
    yield
    updater.set_shutdown_hook(None)


async def _dummy_app(scope, receive, send) -> None:  # pragma: no cover - 只当参数
    """最简 ASGI 应用：这两条测试验的是启动与退出，不验路由。"""
    return None


def test_run_web_starts_through_the_shared_helper_and_installs_the_hook(monkeypatch):
    """run_web.py 必须走 web/__main__.build_server + serve，并且钩子在那时已经装上。"""
    run_web = _load_run_web()
    seen: dict[str, object] = {}

    monkeypatch.setattr("btrader.web.app.create_app", lambda: _dummy_app)

    def fake_serve(server, *, sockets=None):
        seen["server"] = server
        seen["hook"] = updater.SHUTDOWN_HOOK
        seen["sockets"] = sockets
        return 0

    monkeypatch.setattr(web_main, "serve", fake_serve)
    monkeypatch.setattr(
        sys, "argv", ["run_web.py", "--host", "127.0.0.1", "--port", "8123"]
    )
    try:
        assert run_web.main() == 0
    finally:
        updater.set_shutdown_hook(None)

    server = seen.get("server")
    assert server is not None, "run_web 没有经过 web.__main__.serve"
    import uvicorn

    assert isinstance(server, uvicorn.Server)
    assert server.config.port == 8123
    assert server.config.host == "127.0.0.1"
    hook = seen.get("hook")
    assert hook is not None, "run_web 没把 should_exit 交给 updater"
    hook()
    assert server.should_exit is True


def test_the_hook_flips_should_exit_instead_of_raising_a_signal(monkeypatch):
    """钩子这条路上不许发信号：Windows 上那是硬切。"""
    import uvicorn

    server = web_main.build_server(_dummy_app, host="127.0.0.1", port=8124)
    assert isinstance(server, uvicorn.Server)
    fired: list[int] = []
    monkeypatch.setattr(updater.signal, "raise_signal", lambda sig: fired.append(sig))
    monkeypatch.setattr(updater.os, "kill", lambda *a: fired.append(-1))
    try:
        assert updater.SHUTDOWN_HOOK is not None
        updater._request_shutdown()
    finally:
        updater.set_shutdown_hook(None)
    assert server.should_exit is True
    assert fired == [], "有钩子的时候不该再发信号"


def test_serve_turns_a_keyboard_interrupt_into_a_clean_exit():
    """Ctrl+C：uvicorn 把 SIGINT 变成 KeyboardInterrupt 时不许抛给外面。"""

    class _Boom:
        def run(self, sockets=None):
            raise KeyboardInterrupt

    assert web_main.serve(_Boom()) == 0

    class _Fine:
        def run(self, sockets=None):
            return None

    assert web_main.serve(_Fine()) == 0


def test_the_real_server_stops_gracefully_when_the_hook_fires():
    """真的起一个 uvicorn，钩子一响它自己退，而不是被 kill 掉。"""
    server = web_main.build_server(_dummy_app, host="127.0.0.1", port=0)
    done = threading.Event()

    def _run() -> None:
        try:
            web_main.serve(server)
        finally:
            done.set()

    worker = threading.Thread(target=_run, name="t86-server", daemon=True)
    worker.start()
    try:
        deadline = time.time() + 15
        while time.time() < deadline and not server.started:
            time.sleep(0.02)
        assert server.started, "服务器没起来"
        started_at = time.time()
        updater._request_shutdown()
        assert done.wait(15), "钩子响了 15 秒进程还没退出"
        assert server.should_exit is True
        assert time.time() - started_at < 15
    finally:
        server.should_exit = True
        updater.set_shutdown_hook(None)
        worker.join(10)
    assert not worker.is_alive()


def test_run_web_no_longer_calls_uvicorn_run_directly():
    """两条启动路径只能有一份 Server 装配代码。"""
    text = (ROOT / "run_web.py").read_text(encoding="utf-8")
    assert "uvicorn.run(" not in text
    assert "btrader.web.__main__" in text
    main_text = (ROOT / "src" / "btrader" / "web" / "__main__.py").read_text(encoding="utf-8")
    assert "def build_server(" in main_text
    assert "def serve(" in main_text
