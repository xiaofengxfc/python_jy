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