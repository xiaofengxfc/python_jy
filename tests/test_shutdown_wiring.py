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


# ---------------------------------------------- t108-① pythonw 下也得有日志
def test_run_web_wires_logging_up_like_the_other_launch_path():
    """run_web.py 以前一次都没调过 setup_logging：pythonw 起来的服务等于把日志整个丢掉。

    顺序也要钉住——先兜住 stdio（pythonw 下它是 None），再配日志，最后才装配 Server。
    """
    text = (ROOT / "run_web.py").read_text(encoding="utf-8")
    assert "_ensure_stdio()" in text
    assert "setup_logging(" in text
    assert "console_stream=sys.stderr" in text
    assert text.index("_ensure_stdio()") < text.index("setup_logging(")
    assert text.index("setup_logging(") < text.index("build_server(")


def test_ensure_stdio_gives_pythonw_somewhere_to_write(monkeypatch, tmp_path):
    """pythonw 是 GUI 子系统的解释器：不重定向时 sys.stdout / sys.stderr 都是 None。"""
    monkeypatch.setenv("BITRADER_DATA_DIR", str(tmp_path))
    run_web = _load_run_web()
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    run_web._ensure_stdio()
    assert sys.stdout is not None and sys.stderr is not None
    assert sys.stdout is sys.stderr, "两个都缺就都指向同一个文件"
    sys.stdout.write("pythonw 也得有地方写日志\n")
    sys.stdout.flush()
    log = tmp_path / "logs" / "console.log"
    assert log.exists(), "没有落到 <数据目录>/logs/console.log"
    assert "pythonw 也得有地方写日志" in log.read_text(encoding="utf-8")
    assert not sys.stdout.closed


def test_ensure_stdio_keeps_a_stream_that_still_works(monkeypatch, tmp_path):
    """只缺一个就只补那一个：能写的那条流不许被换掉。"""
    monkeypatch.setenv("BITRADER_DATA_DIR", str(tmp_path))
    run_web = _load_run_web()
    real_out = sys.stdout
    monkeypatch.setattr(sys, "stderr", None)
    run_web._ensure_stdio()
    assert sys.stdout is real_out
    assert sys.stderr is not None and sys.stderr is not real_out
    assert (tmp_path / "logs" / "console.log").exists()


def test_ensure_stdio_touches_nothing_when_both_streams_work(monkeypatch, tmp_path):
    """正常终端里起服务不该多出一个 console.log。"""
    monkeypatch.setenv("BITRADER_DATA_DIR", str(tmp_path))
    run_web = _load_run_web()
    real_out, real_err = sys.stdout, sys.stderr
    run_web._ensure_stdio()
    assert sys.stdout is real_out and sys.stderr is real_err
    assert not (tmp_path / "logs" / "console.log").exists()


def test_the_uvicorn_loggers_hand_their_lines_to_the_root_handler():
    """pythonw / 桌面壳没有控制台：uvicorn 自己挂一个 propagate=False 的 stderr 处理器，
    那几行「Started server process / Uvicorn running」就写到 None 上，日志里一个字都没有。"""
    loggers = web_main.LOG_CONFIG["loggers"]
    assert loggers["uvicorn"]["propagate"] is True
    assert not loggers["uvicorn"].get("handlers")
    assert loggers["uvicorn.error"]["propagate"] is True


def test_a_uvicorn_startup_line_lands_in_bitrader_log(monkeypatch, tmp_path):
    """按进程里真实的那套配置走一遍：dictConfig 之后 uvicorn 的行要进 bitrader.log。"""
    import io
    import logging.config

    from btrader import logging_setup

    monkeypatch.setenv("BITRADER_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(logging_setup, "_CONFIGURED", False)
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_hook = sys.excepthook
    try:
        logging_setup.setup_logging(logging.INFO, console_stream=io.StringIO())
        logging.config.dictConfig(web_main.LOG_CONFIG)
        logging.getLogger("uvicorn.error").info("Uvicorn running on http://127.0.0.1:8799")
        for handler in root.handlers:
            handler.flush()
        text = (tmp_path / "logs" / "bitrader.log").read_text(encoding="utf-8")
        assert "Uvicorn running on http://127.0.0.1:8799" in text, text
    finally:
        sys.excepthook = saved_hook
        for handler in root.handlers:
            if handler not in saved_handlers:
                handler.close()
        root.handlers[:] = saved_handlers


def test_ensure_stdio_happens_before_the_logging_setup(monkeypatch, tmp_path):
    """兜住 stdio 必须排在 setup_logging 之前：不然后者会把 None 当控制台流装上去。"""
    text = (ROOT / "run_web.py").read_text(encoding="utf-8")
    assert text.index("_ensure_stdio()") < text.index("setup_logging(")
