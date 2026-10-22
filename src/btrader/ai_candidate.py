"""Turn a model reply into a strategy candidate.

策略实验室那条提示词要求「只输出一个 JSON 对象」，但模型不一定听。实测里它回的是散文
（「## 改进思路」）+ 一个 ```python 代码块 + 尾部参数表格，全文没有 JSON，于是整次生成
被当成「JSON 不完整」丢掉——代码其实往往是好的。

所以这里按五种形状依次试着把源码抠出来：纯 JSON、json 围栏、任意代码围栏、正文里的
Strategy 类、失败。抠到就交给 ``sandbox.check_code`` 做唯一闸门，抠不到才报错。

这个模块只处理文本：不写盘、不执行代码、不碰 registry。
"""

from __future__ import annotations

import ast
import json
import re
from typing import Any, Iterable, Iterator

from . import ai
from .strategy.source import _KEY_RE, _NAME_ASSIGN, _class_key


class CandidateParseError(RuntimeError):
    """回答里找不到策略源码；消息是一句人话，路由原样交给用户。"""


# ```python ... ``` / ``` ... ```：语言标记可有可无，闭合行单独一行。
_FENCE_RE = re.compile(r"(?ms)^[ \t]*```([A-Za-z0-9_+-]*)[ \t]*\n(.*?)^[ \t]*```")
# 正文里的 ``class Xxx(Strategy):``。模型把代码直接铺在散文里时从这里切。
_CLASS_START_RE = re.compile(r"(?m)^[ \t]*class[ \t]+[A-Za-z_]\w*[ \t]*\(([^)]*)\)[ \t]*:")
# ``from ..base import`` / ``from ...core import``：包内相对导入。
_REL_IMPORT_RE = re.compile(r"(?m)^([ \t]*from[ \t]+)(\.{2,3})([A-Za-z_][\w.]*)[ \t]+import[ \t]+")

_JSON_FENCE_LANGS = {"", "json", "json5", "javascript", "js"}
_CODE_FENCE_LANGS = {"", "python", "py", "python3", "py3"}

# 相对导入只在包内成立：内置策略住在 btrader.strategy.builtin，两个点等于
# btrader.strategy，三个点等于 btrader。模型读【参考源码】时会把这两个前缀原样
# 抄过来（实测就是这样），而候选文件是数据目录里的独立模块，那样的相对路径会
# ImportError。所以两层的映射写死在这里，别的层数不动——猜错了静态检查会挡下来。
_RELATIVE_ROOTS = {2: "btrader.strategy", 3: "btrader"}

_DEFAULT_KEY = "ai_strategy"


def from_answer(text: str) -> dict[str, Any]:
    """从模型回答里取出一段策略源码。

    依次尝试五种形状，第一个抠到代码的就赢。返回
    ``{"code", "strategy_key", "name", "note"}``；note 说明是从哪种形状里取到的。
    全都试完还不行就抛 :class:`CandidateParseError`。
    """
    raw = str(text or "")
    if not raw.strip():
        raise CandidateParseError("模型没有返回任何内容。重试一次。")

    # 1) 纯 JSON、```json 围栏、散文夹 JSON —— 都交给 ai 的现成解析。
    found = ai._find_json_object(raw)
    if found is not None:
        data = found[0]
        code = _json_code(data)
        if code:
            return _result(
                code,
                key=_json_text(data, "strategy_key"),
                name=_json_text(data, "name"),
                note="从 JSON 里取到代码",
            )

    # 2) 围栏里只有 JSON 正文（围栏没有标 json，或者外面还夹了散文）。
    for body in _fences(raw, _JSON_FENCE_LANGS):
        data = _load_json(body)
        if not isinstance(data, dict):
            continue
        code = _json_code(data)
        if code:
            return _result(
                code,
                key=_json_text(data, "strategy_key"),
                name=_json_text(data, "name"),
                note="从 JSON 代码块里取到代码",
            )

    # 3) 唯一的 python 代码块（模型最常给的形状）。
    code = _code_fence(raw)
    if code:
        return _result(code, note="从 python 代码块里取到代码")

    # 4) 代码直接铺在散文里：从第一个 Strategy 类切到结尾。
    code = _text_class(raw)
    if code:
        return _result(code, note="从正文里的 Strategy 类取到代码")

    # 5) 什么都没有。分清「给了 JSON 但没 code」和「只给了说明文字」。
    if found is not None:
        raise CandidateParseError(
            "模型的回答里没有策略源码：JSON 里缺少 code 字段。重试一次。"
        )
    raise CandidateParseError(
        "模型的回答里没有策略源码：只有说明文字，没有代码块。重试一次，或把改进要求写具体一点。"
    )


def absolute_strategy_imports(code: str) -> str:
    """把 ``from ..base import`` 换成 ``from btrader.strategy.base import``。

    模型会照抄【参考源码】的包内相对导入：``from ..base``、``from ...core``。
    候选文件是数据目录里的独立模块，这两个前缀在那里解析不了，源码会直接
    ImportError，用户看到的却只是「静态检查没过」。所以写盘前先改成绝对导入。
    """

    def repl(match: re.Match[str]) -> str:
        lead, dots, module = match.group(1), match.group(2), match.group(3)
        root = _RELATIVE_ROOTS.get(len(dots))
        if root is None:
            return match.group(0)
        return f"{lead}{root}.{module} import "

    return _REL_IMPORT_RE.sub(repl, code)


def class_meta(code: str) -> tuple[str, str]:
    """类属性里的 ``key`` / ``name`` 字面量，取不到就是空串。

    用的是源码编辑器那套现成的解析（``_class_key`` 走 ast，``_NAME_ASSIGN`` 走正则），
    不在这里再写一份：两处对「类的 key 是什么」必须永远一致。
    """
    match = _NAME_ASSIGN.search(code or "")
    return _class_key(code or ""), (match.group(3).strip() if match else "")


def sanitize_key(key: str, taken: Iterable[str] = ()) -> tuple[str, str]:
    """把模型给的 key 规整成合法 key，撞了已用的就加后缀。

    返回 ``(key, note)``，note 是给人看的一句话；没改动就是空串。
    撞库为什么要改名：核准候选会写到用户策略目录，同名 key 会顶掉内置策略
    （实测模型就照抄了 ``key = "bollinger"``）。
    """
    original = str(key or "").strip()
    slug = re.sub(r"[^a-z0-9_]+", "_", original.lower()).strip("_")
    slug = re.sub(r"_+", "_", slug)
    if not slug:
        slug = _DEFAULT_KEY
    if not slug[0].isalpha():
        slug = f"ai_{slug}"
    if len(slug) < 2:
        slug = f"{slug}_ai"
    slug = slug[:64].strip("_") or _DEFAULT_KEY

    used = {str(item) for item in taken}
    if slug not in used:
        if slug == original:
            return slug, ""
        return slug, f"原名 {original or '（空）'} 不是合法 key，规整为 {slug}"

    base = slug[:60]
    for index in range(1, 1000):
        candidate = f"{base}_ai" if index == 1 else f"{base}_ai{index}"
        if candidate not in used:
            return candidate, f"原名 {original or base} 已被占用，登记为 {candidate}"
    raise CandidateParseError(
        f"策略 key {base} 的备用后缀都被占用了，先清理候选再试"
    )  # pragma: no cover


# ------------------------------------------------------------------ internals
def _result(code: str, *, key: str = "", name: str = "", note: str = "") -> dict[str, Any]:
    """统一的返回形状：先修导入、再回读类属性补 key / name。"""
    fixed = _finalize(code)
    meta_key, meta_name = class_meta(fixed)
    return {
        "code": fixed,
        "strategy_key": (key or meta_key or "").strip(),
        "name": (name or meta_name or "").strip(),
        "note": note,
    }


def _finalize(code: str) -> str:
    text = absolute_strategy_imports(str(code or "").replace("\r\n", "\n"))
    text = text.strip("\n")
    if not text.strip():
        raise CandidateParseError("模型给的代码块是空的。重试一次。")
    return text + "\n"


def _json_text(data: dict[str, Any], field: str) -> str:
    value = data.get(field)
    return value.strip() if isinstance(value, str) else ""


def _json_code(data: Any) -> str:
    """JSON 里的 ``code``，没有就空串（不是「解析失败」，是形状不对）。"""
    if not isinstance(data, dict):
        return ""
    code = data.get("code")
    return code if isinstance(code, str) and code.strip() else ""


def _load_json(body: str) -> Any:
    try:
        return json.loads(body)
    except ValueError:
        return None


def _fences(text: str, langs: set[str]) -> Iterator[str]:
    for match in _FENCE_RE.finditer(text):
        language = (match.group(1) or "").strip().lower()
        if language not in langs:
            continue
        body = match.group(2).strip("\n")
        if body.strip():
            yield body


def _code_fence(text: str) -> str:
    """取一个代码围栏。带 python 标记或长得像策略模块的优先。

    多个围栏时按顺序挑第一个能编译的：挑错了静态检查会挡下来，
    比整段丢掉更值——模型往往只是把解释和代码分成两块写。
    """
    bodies = [body for body in _fences(text, _CODE_FENCE_LANGS) if _parses(body)]
    for body in bodies:
        if _CLASS_START_RE.search(body):
            return body
    return bodies[0] if bodies else ""


def _text_class(text: str) -> str:
    """正文里第一个 Strategy 类到结尾，再削到能编译为止。"""
    match = _CLASS_START_RE.search(text)
    if match is None:
        return ""
    return _trim_to_parseable(text[match.start():])


def _trim_to_parseable(code: str) -> str:
    """从尾部逐行剥，剥到第一次能编译的位置。

    「从类切到结尾」会把尾部的参数表格、总结段一起带进来，那不是 Python。
    尾部垃圾通常只有十几行，而真正的代码在它前面，所以从后面删到能解析为止，
    留下的正是模型写的那段类（删多了静态检查会挡下来）。
    """
    rows = str(code or "").rstrip().split("\n")
    while len(rows) >= 3:
        body = "\n".join(rows).rstrip()
        if _parses(body):
            return _strip_trailing_prose(body) + "\n"
        rows.pop()
    return ""


def _strip_trailing_prose(code: str) -> str:
    """把顶部就顶格的尾部说明行去掉。

    ``ast.parse`` 拦不住 markdown 标题：``## 参数说明`` 在 Python 里是合法注释，
    照样能编译，所以它会被留在源码尾部当噪音。顶格的注释行不可能属于类体，
    删掉不影响任何行为，留着会让用户以为那是策略的一部分。
    """
    rows = code.rstrip().split("\n")
    while rows:
        last = rows[-1]
        if not last.strip() or (last.startswith("#") and not last.startswith("#!")):
            rows.pop()
            continue
        break
    return "\n".join(rows)


def _parses(code: str) -> bool:
    try:
        ast.parse(code)
    except SyntaxError:
        return False
    return True
