"""Read, validate and save user strategy source files.

Only files inside the per-user strategy directory are writable. A built-in can
be edited too, but the edit lands as a same-key file in that directory and
shadows the package copy; the package source never changes and "restore" throws
the user file away.

This module deliberately keeps the same static checks as the AI candidate
sandbox. It is *not* a security boundary: a user plugin is local Python code and
runs with the same privileges as biTrader. The checks catch accidents, not a
malicious author.
"""

from __future__ import annotations

import ast
import inspect
import logging
import re
import sys
import uuid
from pathlib import Path
from typing import Any

from .registry import StrategyRegistry
from .sandbox import check_code

log = logging.getLogger(__name__)

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


# Refused for both a plain built-in and one that is currently overridden.
BUILTIN_RENAME_REFUSED = "内置策略不能改名，先还原或另存为新策略"


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


def read_source(registry: StrategyRegistry, key: str) -> dict[str, Any]:
    strategy = registry.get(key)  # raises KeyError for an unknown key
    path = registry.source_path(key)
    if path is not None:
        # A user file, either a standalone plugin or the override of a built-in.
        code = path.read_text(encoding="utf-8")
        return {
            "key": key,
            "name": strategy.meta.name,
            "code": code,
            "filename": path.name,
            "editable": True,
            "builtin": registry.is_builtin(key),
            "overridden": registry.is_overridden(key),
        }
    # Untouched built-in: show the package class so it can be edited or copied.
    # `inspect` is safe here: the class was imported by this process already.
    try:
        code = _class_source(type(strategy))
    except (OSError, TypeError):
        code = ""
    return {
        "key": key,
        "name": strategy.meta.name,
        "code": code,
        "filename": "",
        "editable": True,
        "builtin": True,
        "overridden": False,
    }


_KEY_ASSIGN = re.compile(r'(?m)^([ \t]*)key[ \t]*=[ \t]*(["\'])([^"\'\n]*)\2')
_NAME_ASSIGN = re.compile(r'(?m)^([ \t]*)name[ \t]*=[ \t]*(["\'])([^"\'\n]*)\2')


def _rename_literals(code: str, *, old_key: str, new_key: str, new_name: str = "") -> str:
    """Rewrite the class's ``key`` (and optionally ``name``) literal.

    Only the first ``key = "..."`` assignment is touched: that is the one the
    registry reads, and a strategy with two of them is already broken.
    """
    match = _KEY_ASSIGN.search(code)
    if match is None:
        raise StrategySourceError('策略源码缺少 key = "..." 字面量，无法重命名')
    if match.group(3) != old_key:
        raise StrategySourceError(
            f"源码里的 key='{match.group(3)}' 与要重命名的 '{old_key}' 不一致"
        )
    indent = match.group(1)
    head = code[: match.start()]
    key_line = f'{indent}key = "{new_key}"'
    code = head + key_line + code[match.end():]
    if not new_name:
        return code
    named = _NAME_ASSIGN.search(code)
    if named is not None:
        return code[: named.start()] + f'{named.group(1)}name = "{new_name}"' + code[named.end():]
    # No display name yet: put one directly under the key so the UI stops
    # showing the class name after a rename.
    insert_at = len(head) + len(key_line)
    return code[:insert_at] + f'\n{indent}name = "{new_name}"' + code[insert_at:]

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


def _short_reason(errors: list[str], fallback: str) -> str:
    """One readable line out of the registry's load errors.

    The entries carry a full traceback, and the editor shows the message in a
    banner: the last error's first line ("<file>: 加载失败") is the part that
    says what happened, the frames below it are noise there.
    """
    if not errors:
        return fallback
    for line in str(errors[-1]).splitlines():
        text = line.strip()
        if text:
            return text
    return fallback


def _undo_write(
    target: Path, previous: str | None, directory: Path, registry: StrategyRegistry
) -> None:
    """Put the file back the way it was and reload the registry.

    previous is None when this save created the file, so undoing means
    deleting it. The reload afterwards leaves the registry exactly as it was
    before the attempt: no half-broken plugin, no leftover error entries.
    """
    try:
        if previous is None:
            target.unlink(missing_ok=True)
        else:
            tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
            tmp.write_text(previous, encoding="utf-8", newline="\n")
            tmp.replace(target)
    except OSError:
        log.warning("保存失败后回滚文件失败: %s", target, exc_info=True)
    registry.discover([directory])


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
    if existing is not None:
        if create:
            raise StrategySourceError(f"策略 {key} 已存在")
        target = existing.resolve()
        if target.parent != directory:
            raise StrategySourceError("只能编辑自己策略目录里的文件", status=403)
    else:
        if create and key in registry:
            # Creating a strategy that already exists is a mistake even when it
            # is a built-in: editing one is PUT on its source, not POST.
            raise StrategySourceError(f"策略 {key} 已存在")
        # No file yet. For a built-in this is how an override starts: the file
        # lands in the user directory and shadows the package class.
        target = _file_for(key, directory)
        if target.exists():
            raise StrategySourceError(f"文件 {target.name} 已存在，请换一个 key 或先删除")

    # Kept so a save that does not load can be undone. A broken file left on
    # disk is invisible from the UI (the registry falls back to the built-in)
    # and keeps failing on every reload, so the user could only fix it by
    # editing the data directory by hand.
    previous = target.read_text(encoding="utf-8") if target.exists() else None

    tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(code, encoding="utf-8", newline="\n")
    tmp.replace(target)

    registry.discover([directory])
    problem = ""
    if key not in registry:
        reason = _short_reason(registry.errors, "策略没有被注册")
        problem = f"保存后加载失败：{reason}"
    elif registry.source_path(key) != target.resolve():
        # The file is on disk but something else is in effect: a broken
        # override leaves the built-in registered, so checking "key in
        # registry" alone would report success while nothing changed.
        reason = _short_reason(registry.errors, "")
        problem = "保存后没有生效，内置版本仍在生效"
        if reason:
            problem = f"{problem}：{reason}"
    if problem:
        _undo_write(target, previous, directory, registry)
        raise StrategySourceError(problem, status=422)
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
        "builtin": registry.is_builtin(key),
        "overridden": registry.is_overridden(key),
    }


def restore_builtin(
    registry: StrategyRegistry, key: str, directory: Path | None = None
) -> dict[str, Any]:
    """Throw away the user file overriding a built-in, bringing the original back.

    Only an active override can be restored: otherwise the key belongs either
    to the untouched built-in or to an ordinary user strategy, and deleting
    either one would not mean what the button says.
    """
    directory = _user_dir(directory)
    if not registry.is_overridden(key):
        raise StrategySourceError("这个策略不是覆盖版本")
    path = registry.source_path(key)
    if path is None:
        raise StrategySourceError("这个策略不是覆盖版本")
    path = path.resolve()
    # The file is deleted, not rewritten, so it has to hold nothing but this
    # override. One file may define several strategies; deleting it would take
    # the others with it.
    shared = sorted(
        other for other in registry.keys if other != key and registry.source_path(other) == path
    )
    if shared:
        raise StrategySourceError(f"该文件还定义了 {', '.join(shared)}，先拆开再还原")
    if path.parent != directory:
        raise StrategySourceError("只能还原自己策略目录里的文件", status=403)
    path.unlink(missing_ok=True)
    registry.discover([directory])
    if key not in registry:
        detail = "\n".join(registry.errors[-3:]) or "内置策略没有被注册"
        raise StrategySourceError(f"还原后内置策略没有回来：{detail}", status=422)
    # Reported as measured: another user file may take the key over the moment
    # this one disappears, and then the strategy is still overridden. The
    # deletion has already happened, so the message says so instead of
    # pretending the call did nothing.
    if registry.is_overridden(key):
        rival = registry.source_path(key)
        still = rival.name if rival is not None else "另一个用户文件"
        raise StrategySourceError(
            f"还原后仍有别的用户文件覆盖这个策略：已删除 {path.name}，{still} 仍在覆盖",
            status=400,
        )
    return {
        "ok": True,
        "key": key,
        "builtin": registry.is_builtin(key),
        "overridden": registry.is_overridden(key),
    }


def rename_source(
    registry: StrategyRegistry,
    *,
    key: str,
    new_key: str,
    new_name: str = "",
    directory: Path | None = None,
) -> dict[str, Any]:
    """Rename a user strategy: new key (and file name), optional display name.

    Built-ins are refused, including one that is currently overridden: the
    package copy would come back under the old key. The new file is written and
    hot-loaded *before*
    the old one is deleted, and rolled back if the reload fails, so a bad
    rename cannot leave the user without the strategy they had.
    """
    directory = _user_dir(directory)
    # An override is a built-in with a user file behind it. Renaming it would
    # leave the built-in behind under the old key, which is not what the user
    # asked for: restore first, or save it as a new strategy.
    if registry.is_overridden(key):
        raise StrategySourceError(BUILTIN_RENAME_REFUSED, status=403)
    old_path = registry.source_path(key)
    if old_path is None:
        if key in registry:
            raise StrategySourceError(BUILTIN_RENAME_REFUSED, status=403)
        raise StrategySourceError(f"未知策略：{key}", status=404)
    new_key = (new_key or "").strip()
    if new_key == key:
        raise StrategySourceError("新 key 与当前 key 相同")
    target = _file_for(new_key, directory)
    if target.exists():
        raise StrategySourceError(f"文件 {target.name} 已存在，请换一个 key")
    if new_key in registry:
        raise StrategySourceError(f"策略 {new_key} 已经存在，请换一个 key")
    code = old_path.read_text(encoding="utf-8")
    updated = _rename_literals(
        code, old_key=key, new_key=new_key, new_name=(new_name or "").strip()
    )
    report = validate_source(updated)
    if not report.get("ok"):
        raise StrategySourceError("重命名后的源码没有通过静态检查", status=422)
    if str(report.get("key") or "") != new_key:
        raise StrategySourceError("重命名后的 key 与文件名不一致", status=422)
    tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(updated, encoding="utf-8", newline="\n")
    tmp.replace(target)
    registry.discover([directory])
    if new_key not in registry:
        target.unlink(missing_ok=True)
        registry.discover([directory])
        detail = "\n".join(registry.errors[-3:]) or "策略没有被注册"
        raise StrategySourceError(f"重命名后加载失败：{detail}", status=422)
    old_path.unlink(missing_ok=True)
    registry.discover([directory])
    return {
        "ok": True,
        "key": new_key,
        "previous": key,
        "filename": target.name,
        "source_name": target.name,
        "editable": True,
    }
