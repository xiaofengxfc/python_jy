"""AI 生成策略代码的静态检查（Phase 4）。

**这里不是沙箱**：检查只降低风险，真正的安全边界是人工审核 + 只把审核过的
文件复制进正式策略目录。本模块从不 import / exec 候选代码，只做 AST 检查。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

MAX_CODE_BYTES = 60_000

BANNED_MODULES = {
    "os", "sys", "subprocess", "socket", "requests", "urllib", "http", "https",
    "ftplib", "telnetlib", "smtplib", "shutil", "pathlib", "tempfile", "glob",
    "ctypes", "multiprocessing", "threading", "asyncio", "importlib", "builtins",
    "pickle", "marshal", "code", "gc", "inspect", "signal", "platform", "webbrowser",
    "importlib.util", "runpy", "pty", "resource", "sysconfig", "site",
}
ALLOWED_MODULES = {
    "math", "statistics", "numpy", "pandas", "typing", "dataclasses",
    # `from __future__ import annotations` is a compile-time flag, not a runtime
    # module, and models add it to every file. Rejecting it made the reviewer
    # fight the linter instead of reading the strategy.
    "__future__",
    "btrader.strategy.base",
    # Read-only indicator helpers: a candidate may use them once a human copies
    # it into the real strategy package, which is where the import resolves.
    "btrader.core.indicators",
    "btrader.core",
}
BANNED_NAMES = {
    "eval", "exec", "compile", "open", "input", "breakpoint", "globals", "locals",
    "vars", "__import__", "getattr", "setattr", "delattr", "memoryview", "help",
}


@dataclass
class CheckIssue:
    code: str
    message: str
    line: int = 0


@dataclass
class CheckReport:
    ok: bool
    issues: list[CheckIssue] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    classes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "issues": [issue.__dict__ for issue in self.issues],
            "imports": list(self.imports),
            "classes": list(self.classes),
        }


def _root_module(name: str) -> str:
    return name.split(".", 1)[0]


def check_code(code: str) -> CheckReport:
    """Static checks for a candidate strategy module."""
    issues: list[CheckIssue] = []
    imports: list[str] = []
    classes: list[str] = []

    if not code or not code.strip():
        return CheckReport(False, [CheckIssue("empty", "候选代码为空")])
    if len(code.encode("utf-8")) > MAX_CODE_BYTES:
        return CheckReport(
            False, [CheckIssue("too_large", f"候选代码超过 {MAX_CODE_BYTES} 字节上限")]
        )

    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return CheckReport(
            False, [CheckIssue("syntax", f"语法错误：{exc.msg}", int(exc.lineno or 0))]
        )

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
                root = _root_module(alias.name)
                if alias.name in BANNED_MODULES or root in BANNED_MODULES:
                    issues.append(
                        CheckIssue("banned_import", f"禁止导入模块 {alias.name}", node.lineno)
                    )
                elif alias.name not in ALLOWED_MODULES and root not in ALLOWED_MODULES:
                    issues.append(
                        CheckIssue(
                            "unknown_import",
                            f"未在白名单中的模块 {alias.name}",
                            node.lineno,
                        )
                    )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imports.append(module)
            root = _root_module(module)
            # A relative import inside a candidate only matters once a human
            # copies the file into a package; it cannot reach the network or
            # the filesystem, so it is not treated as an escape hatch. Banned
            # module names are still rejected.
            if node.level:
                if module in BANNED_MODULES or root in BANNED_MODULES:
                    issues.append(
                        CheckIssue("banned_import", f"禁止从 {module} 导入", node.lineno)
                    )
                continue
            if module in BANNED_MODULES or root in BANNED_MODULES:
                issues.append(
                    CheckIssue("banned_import", f"禁止从 {module} 导入", node.lineno)
                )
            elif module not in ALLOWED_MODULES and root not in ALLOWED_MODULES:
                issues.append(
                    CheckIssue("unknown_import", f"未在白名单中的模块 {module}", node.lineno)
                )
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in BANNED_NAMES:
                issues.append(
                    CheckIssue("banned_call", f"禁止调用 {node.func.id}()", node.lineno)
                )
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("__") and node.attr.endswith("__"):
                issues.append(
                    CheckIssue(
                        "dunder_access", f"禁止访问特殊属性 {node.attr}", node.lineno
                    )
                )
        elif isinstance(node, ast.ClassDef):
            classes.append(node.name)
            bases = {
                base.id
                for base in node.bases
                if isinstance(base, ast.Name)
            }
            if "Strategy" not in bases:
                issues.append(
                    CheckIssue(
                        "bad_base",
                        f"类 {node.name} 必须直接继承 Strategy",
                        node.lineno,
                    )
                )
        elif isinstance(node, ast.While) and isinstance(node.test, ast.Constant) and node.test.value is True:
            issues.append(CheckIssue("infinite_loop", "禁止 while True 无限循环", node.lineno))

    if not classes:
        issues.append(CheckIssue("no_class", "候选代码必须定义一个 Strategy 子类"))
    elif len([c for c in classes]) > 3:
        issues.append(CheckIssue("too_many_classes", "候选代码最多定义 3 个类"))

    return CheckReport(not issues, issues, imports, classes)
