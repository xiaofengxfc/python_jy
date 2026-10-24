"""静态闸门：放过跑不起来的代码，等于把「静态检查通过」变成一句假话。

这三段以前都判 ok=True，候选列表上显示「静态检查通过、可批准」，
点批准才在导入那一步 NameError。闸门是唯一的一道，就得当场拦住。
"""

from __future__ import annotations

import pytest

from btrader.strategy.candidates import CandidateError, CandidateStore
from btrader.strategy.sandbox import check_code

SAFE = '''from btrader.strategy.base import IntParam, Strategy
import pandas as pd


class MyStrategy(Strategy):
    key = "my_strategy"
    name = "我的策略"
    params_spec = [IntParam("period", "周期", 20, 2, 200)]

    def generate(self, df, params):
        ma = df["close"].rolling(int(params["period"])).mean()
        return pd.DataFrame({"target": (df["close"] > ma).astype(float)}, index=df.index)
'''

IMPORT_LINE = "from btrader.strategy.base import IntParam, Strategy\n"


def _codes(report) -> list[str]:
    return [issue.code for issue in report.issues]


def test_a_base_class_that_is_never_imported_is_rejected() -> None:
    """模型最常犯的错：照抄 class X(Strategy)，忘了上面那行 import。

    这段代码静态检查以前是过的，导入时才 NameError: name 'Strategy' is not defined。
    """
    report = check_code(SAFE.replace(IMPORT_LINE, ""))

    assert not report.ok
    assert "unbound_base" in _codes(report)
    assert any("Strategy" in issue.message for issue in report.issues)


def test_a_relative_import_is_rejected_with_the_absolute_spelling() -> None:
    """候选是数据目录里的独立模块，from ..base 解析不了，要教它写绝对模块名。"""
    report = check_code("from ..base import IntParam, Strategy\n" + SAFE.replace(IMPORT_LINE, ""))

    assert not report.ok
    assert "relative_import" in _codes(report)
    assert any("btrader.strategy.base" in issue.message for issue in report.issues)


def test_a_param_constructor_that_is_never_imported_is_rejected() -> None:
    """用了 IntParam 却没导入：类定义时就 NameError，参数表单也读不到。"""
    report = check_code(
        SAFE.replace(IMPORT_LINE, "from btrader.strategy.base import Strategy\n")
    )

    assert not report.ok
    assert "unbound_param" in _codes(report)
    assert any("IntParam" in issue.message for issue in report.issues)


def test_normal_spellings_still_pass() -> None:
    """收紧要收得准：几种常见写法一个都不能误伤。"""
    assert check_code(SAFE).ok, check_code(SAFE).issues
    assert check_code("from __future__ import annotations\n" + SAFE).ok
    star = SAFE.replace(IMPORT_LINE, "from btrader.strategy.base import *\n")
    assert check_code(star).ok
    # 模块级常量、工具函数、指标别名一起出现
    richer = SAFE.replace(
        IMPORT_LINE,
        IMPORT_LINE + "from btrader.core import indicators as ta\n\nFALLBACK = 20\n\n\n"
        "def pick(frame):\n    return frame['close']\n",
    )
    assert check_code(richer).ok


def test_the_report_shape_is_unchanged() -> None:
    """只多几条失败项，返回结构一个字段都不许变。"""
    assert set(check_code(SAFE).as_dict()) == {"ok", "issues", "imports", "classes"}
    bad = check_code(SAFE.replace(IMPORT_LINE, "")).as_dict()
    assert set(bad) == {"ok", "issues", "imports", "classes"}
    assert set(bad["issues"][0]) == {"code", "message", "line"}
    assert bad["ok"] is False
    assert "MyStrategy" in bad["classes"]


def test_a_candidate_with_a_missing_import_is_stored_as_rejected(tmp_path) -> None:
    """端到端：这类候选以前是 pending（界面上可批准），批准时才炸。"""
    store = CandidateStore(tmp_path)
    candidate = store.create(SAFE.replace(IMPORT_LINE, ""), strategy_key="no_import")

    assert candidate.status == "rejected"
    assert candidate.checks["ok"] is False
    with pytest.raises(CandidateError):
        store.review(candidate.id, approve=True)

def test_an_import_inside_a_function_does_not_count_as_module_level() -> None:
    """函数体里的 import 不会让这个名字在模块级可见，不能拿它当「导入过」。

    以前 bound 是按 ast.walk 收的：任意一层函数里写一行
    from btrader.strategy.base import Strategy，模块级的 class X(Strategy)
    就被判「基类可见」，静态检查通过，导入时照旧 NameError。
    """
    code = SAFE.replace(
        IMPORT_LINE,
        "",
    ).replace(
        '    key = "my_strategy"',
        '    key = "my_strategy"',
    )
    code = "def _load():\n    " + IMPORT_LINE.strip() + "\n    return Strategy\n\n\n" + code
    report = check_code(code)

    assert not report.ok
    assert "unbound_base" in _codes(report)


def test_a_truthy_constant_loop_is_rejected_however_it_is_written() -> None:
    """while 1 和 while True 一样不会自己结束，不能只认 True 这一种写法。"""
    loop = SAFE.replace(
        "    def generate(self, df, params):\n",
        "    def generate(self, df, params):\n        while 1:\n            pass\n",
    )
    report = check_code(loop)

    assert not report.ok
    assert "infinite_loop" in _codes(report)
    # 假常量仍然放过
    never = loop.replace("while 1:", "while 0:")
    assert "infinite_loop" not in _codes(check_code(never))

def test_a_corrupt_candidate_file_reads_as_a_candidate_error(tmp_path) -> None:
    """手改坏的 JSON 要报「候选文件读不出来」，不是一路冒成 500。"""
    store = CandidateStore(tmp_path)
    candidate = store.create(SAFE, strategy_key="corrupt_me")
    (tmp_path / f"{candidate.id}.json").write_text("{ not json", encoding="utf-8")

    with pytest.raises(CandidateError) as caught:
        store.get(candidate.id)
    assert "读不出来" in str(caught.value)


def test_a_candidate_write_leaves_no_temporary_files(tmp_path) -> None:
    """写盘用的临时文件名带 pid 与纳秒，写完就该换上去、不留在目录里。"""
    store = CandidateStore(tmp_path)
    for index in range(3):
        store.create(SAFE, strategy_key=f"batch_{index}")

    assert not list(tmp_path.glob("*.tmp"))
    assert len(list(tmp_path.glob("cand-*.json"))) == 3
    assert len(list(tmp_path.glob("cand-*.py"))) == 3


def test_two_same_second_creates_get_their_own_files(tmp_path) -> None:
    """同一秒里连写两条候选：id 加后缀，代码各写各的文件，谁也不覆盖谁。"""
    store = CandidateStore(tmp_path)
    first = store.create(SAFE, strategy_key="same_second")
    second = store.create(SAFE, strategy_key="same_second")

    assert first.id != second.id
    assert first.code_path.read_text(encoding="utf-8") == SAFE
    assert second.code_path.read_text(encoding="utf-8") == SAFE

def test_two_concurrent_creates_never_pick_the_same_id(tmp_path, monkeypatch) -> None:
    """同一瞬间进来的两个写入者不能选中同一个 id。

    「查重名 → 定 id」原来在锁外：两个线程都能看到「不冲突」，于是拿到同一个 id，
    后写的把先写的那份 .py 换掉——列表里只剩一条，代码是别人的。这里把 exists()
    卡在「两边都到齐」那一刻，正是那个窗口。
    """
    from pathlib import Path
    import threading

    store = CandidateStore(tmp_path)
    both = threading.Event()
    real_exists = Path.exists

    def slow_exists(self):
        if self.suffix == ".py":
            both.set()
            both.wait(0.5)  # 对方被锁挡住时，这里半秒后自己放行
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", slow_exists)
    created: list = []
    errors: list = []

    def run() -> None:
        try:
            created.append(store.create(SAFE, strategy_key="race"))
        except Exception as exc:  # pragma: no cover - 失败要在断言里看见
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)

    assert not errors
    assert len(created) == 2
    assert len({item.id for item in created}) == 2, "两条候选选中了同一个 id，后写的会覆盖先写的"
    for item in created:
        assert item.code_path.read_text(encoding="utf-8") == SAFE


