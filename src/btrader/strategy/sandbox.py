"""AI 生成策略代码的静态检查。

检查只降低风险，不是沙箱。真正的安全边界是人工审核，以及只把审核过的文件
复制进正式策略目录。本模块不 import、不 exec 候选代码，只做 AST 检查。
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
# 参数构造器：候选里用到就必须在模块顶层导入，否则类定义时就 NameError。
PARAM_FACTORIES = {"IntParam", "FloatParam", "BoolParam", "ChoiceParam", "ParamSpec"}


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


def _safe_getattr(node: ast.Call) -> bool:
    """A literal, non-dunder attribute name is the only allowed getattr form."""
    if len(node.args) < 2 or node.keywords:
        return False
    name = node.args[1]
    if not isinstance(name, ast.Constant) or not isinstance(name.value, str):
        return False
    attr = name.value
    return not (attr.startswith("__") and attr.endswith("__"))


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

    # 模块顶层能看见哪些名字：候选是数据目录里的独立模块，只有导入过或赋值过的
    # 名字在类定义时能直接用。少了这一条，`class X(Strategy)` 这种漏了 import 的
    # 代码静态检查过得去（基类名对得上），导入时却直接 NameError。
    bound: set[str] = set()
    star_import = False
    # 只看模块顶层：函数体里的 import 不会让这个名字在模块级可见，按 ast.walk 收
    # 会把「基类名没导入」这一条放开，而那种候选在导入时必 NameError。
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound.add(alias.asname or alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    star_import = True
                else:
                    bound.add(alias.asname or alias.name)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    bound.add(target.id)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                bound.add(node.target.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)

    used_params: dict[str, int] = {}

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
            # 候选是数据目录里的独立模块，`from ..base import Strategy` 这种相对
            # 导入解析不了。以前只在「能不能越权」上放行，结果是静态检查通过、
            # 批准时 ImportError；同一条 import 换成绝对模块名就能用，所以直接
            # 判不通过，让模型在生成阶段就改对。
            if node.level:
                dots = "." * node.level
                issues.append(
                    CheckIssue(
                        "relative_import",
                        f"候选不能用相对导入 from {dots}{module}，请写绝对模块名（如 btrader.strategy.base）",
                        node.lineno,
                    )
                )
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
            if node.func.id in PARAM_FACTORIES:
                used_params.setdefault(node.func.id, node.lineno)
            if node.func.id == "getattr" and _safe_getattr(node):
                # `getattr(params, "period", 20)` is ordinary strategy code.
                # The dangerous form is a computed or dunder name, which can
                # reach attributes the dunder check above was written to stop.
                pass
            elif node.func.id in BANNED_NAMES:
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
            # 基类名必须在模块顶层看得见，否则类定义时就 NameError（漏 import 的
            # 候选全卡在这一条上）。`import *` 时名字集合不可知，只放过这种情况。
            for base in sorted(bases):
                if base not in bound and not star_import:
                    issues.append(
                        CheckIssue(
                            "unbound_base",
                            f"类 {node.name} 的基类 {base} 没有在模块顶层导入",
                            node.lineno,
                        )
                    )
        elif (
            isinstance(node, ast.While)
            and isinstance(node.test, ast.Constant)
            # 只看「恒真」这个性质，不看写法：while 1 和 while True 一样不会自己结束。
            # 以前只认 is True，换成 1 就从这一条下面溜过去了。
            and bool(node.test.value)
        ):
            issues.append(CheckIssue("infinite_loop", "禁止 while True 无限循环", node.lineno))

    if not star_import:
        for name, lineno in sorted(used_params.items()):
            if name not in bound:
                issues.append(
                    CheckIssue(
                        "unbound_param",
                        f"用了 {name} 却没有在模块顶层导入（from btrader.strategy.base import {name}）",
                        lineno,
                    )
                )

    if not classes:
        issues.append(CheckIssue("no_class", "候选代码必须定义一个 Strategy 子类"))
    elif len([c for c in classes]) > 3:
        issues.append(CheckIssue("too_many_classes", "候选代码最多定义 3 个类"))

    return CheckReport(not issues, issues, imports, classes)
