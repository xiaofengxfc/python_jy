"""Read, validate and save user strategy source files.

Only files inside the per-user strategy directory are writable. Built-in
strategies are read-only; the UI can copy one into a new user plugin instead of
editing the package source.

This module deliberately keeps the same static checks as the AI candidate
sandbox. It is *not* a security boundary: a user plugin is local Python code and
runs with the same privileges as biTrader. The checks catch accidents, not a
malicious author.
"""

from __future__ import annotations

import ast
import inspect
import re
import sys
import uuid
from pathlib import Path
from typing import Any

from .registry import StrategyRegistry
from .sandbox import check_code

_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")

DEFAULT_TEMPLATE = '''"""A minimal biTrader strategy.

`generate` returns a DataFrame with a `target` column:
  1.0 = long, 0.0 = flat, -1.0 = short.
Signals are evaluated on closed candles by the auto trader and the backtester.
"""

import pandas as pd

from btrader.strategy.base import IntParam, Strategy


class MyStrategy(Strategy):
    key = "my_strategy"
    name = "我的策略"
    description = "收盘价上穿均线做多，下穿离场。"
    params_spec = [IntParam("period", "均线周期", 20, 2, 200)]

    def generate(self, df: pd.DataFrame, params: dict) -> pd.DataFrame:
        ma = df["close"].rolling(int(params["period"])).mean()
        target = (df["close"] > ma).astype(float)
        return pd.DataFrame({"target": target}, index=df.index)
'''


class StrategySourceError(RuntimeError):
    """A user-facing source-editor error."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _user_dir(directory: Path | None) -> Path:
    from .. import paths

    return (directory or paths.user_strategies_dir()).resolve()


def _file_for(key: str, directory: Path) -> Path:
    if not _KEY_RE.match(key):
        raise StrategySourceError("策略 key 只能是小写字母、数字和下划线，且以字母开头")
    path = (directory / f"{key}.py").resolve()
    if path.parent != directory:
        raise StrategySourceError("策略路径越界")
    return path


def _class_key(code: str) -> str:
    """Return the first strategy class's ``key`` assignment, if literal."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return ""
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        for stmt in node.body:
            if isinstance(stmt, ast.Assign):
                for target in stmt.targets:
                    if isinstance(target, ast.Name) and target.id == "key":
                        if isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
                            return stmt.value.value
    return ""


def _report(code: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    report = check_code(code)
    data = report.as_dict()
    if extra:
        data.update(extra)
    return data


def _absolute_imports(segment: str, cls: type) -> str:
    """Rewrite relative imports for a copied built-in plugin.

    Built-ins live in ``btrader.strategy.builtin`` and use ``from ..base``.
    A user file lives directly in ``<data>/strategies`` and cannot resolve that
    package-relative path, so the copy must use the absolute module name.
    """
    package = cls.__module__.rsplit(".", 1)[0]

    def repl(match: re.Match[str]) -> str:
        dots, module = match.group(1), match.group(2)
        up = len(dots) - 1
        parts = package.split(".")
        if up:
            parts = parts[:-up] if up <= len(parts) else []
        if module:
            parts = [*parts, module]
        return f"from {'.'.join(parts)} import"

    return re.sub(r"^from (\.+)([A-Za-z_][\w.]*) import", repl, segment, flags=re.M)


def _class_source(cls: type) -> str:
    """A built-in class plus its module's imports, without its siblings.

    Copying a built-in into a user plugin must produce runnable code. Reading
    only the class body (``inspect.getsource(cls)``) drops ``import pandas`` and
    the base-class import, so the copy would fail the static check.
    """
    module = sys.modules.get(cls.__module__)
    if module is None:
        return inspect.getsource(cls)
    try:
        module_source = inspect.getsource(module)
    except (OSError, TypeError):
        return inspect.getsource(cls)
    try:
        tree = ast.parse(module_source)
    except SyntaxError:
        return inspect.getsource(cls)
    chunks: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            segment = ast.get_source_segment(module_source, node)
            if segment:
                chunks.append(_absolute_imports(segment, cls))
        elif isinstance(node, ast.ClassDef) and node.name == cls.__name__:
            segment = ast.get_source_segment(module_source, node)
            if segment:
                chunks.append(segment)
    return "\n\n\n".join(chunks) if chunks else inspect.getsource(cls)


def read_source(registry: StrategyRegistry, key: str, directory: Path | None = None) -> dict[str, Any]:
    directory = _user_dir(directory)
    strategy = registry.get(key)  # raises KeyError for an unknown key
    path = registry.source_path(key)
    if path is not None:
        code = path.read_text(encoding="utf-8")
        return {
            "key": key,
            "name": strategy.meta.name,
            "code": code,
            "filename": path.name,
            "editable": True,
            "builtin": False,
        }
    # Built-ins are shown read-only so the user can copy them. `inspect` is
    # safe here: the class was imported by this process already.
    try:
        code = _class_source(type(strategy))
    except (OSError, TypeError):
        code = ""
    return {
        "key": key,
        "name": strategy.meta.name,
        "code": code,
        "filename": "",
        "editable": False,
        "builtin": True,
    }


def validate_source(code: str) -> dict[str, Any]:
    report = _report(code)
    key = _class_key(code)
    if key and not _KEY_RE.match(key):
        report["ok"] = False
        report.setdefault("issues", []).append(
            {"code": "bad_key", "message": "策略 key 只能是小写字母、数字和下划线", "line": 0}
        )
    report["key"] = key
    return report


def save_source(
    registry: StrategyRegistry,
    *,
    code: str,
    key: str = "",
    filename: str = "",
    directory: Path | None = None,
    create: bool = False,
) -> dict[str, Any]:
    directory = _user_dir(directory)
    code = code.replace("\r\n", "\n")
    report = validate_source(code)
    if not report.get("ok"):
        raise StrategySourceError("静态检查未通过，请先修复问题", status=422)

    class_key = str(report.get("key") or "")
    key = (key or class_key).strip()
    if not key:
        raise StrategySourceError("策略缺少 key（类属性 key = \"...\"）")
    if class_key and class_key != key:
        raise StrategySourceError(f"类里的 key='{class_key}' 与文件名 key='{key}' 不一致")
    _file_for(key, directory)

    existing = registry.source_path(key)
    if existing is None and key in registry:
        raise StrategySourceError("内置策略只读；请复制为新策略后再修改", status=403)
    if existing is not None:
        if create:
            raise StrategySourceError(f"策略 {key} 已存在")
        target = existing.resolve()
        if target.parent != directory:
            raise StrategySourceError("只能编辑用户策略目录内的文件", status=403)
    else:
        if not create:
            # Saving under a new key is still useful: treat it as a new plugin.
            pass
        target = _file_for(key, directory)
        if target.exists():
            raise StrategySourceError(f"文件 {target.name} 已存在，请换一个 key 或先删除")

    tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(code, encoding="utf-8", newline="\n")
    tmp.replace(target)

    registry.discover([directory])
    if key not in registry:
        detail = "\n".join(registry.errors[-3:]) or "策略没有被注册"
        raise StrategySourceError(f"保存后加载失败：{detail}", status=422)
    if filename and target.name != filename:
        # The editor may have opened a file and the user changed its key. The
        # new file is what matters; the old one is left alone on purpose.
        pass
    return {
        "ok": True,
        "key": key,
        "filename": target.name,
        "source_name": target.name,
        "editable": True,
    }
