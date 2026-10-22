"""候选解析的离线测试：模型不按 JSON 回答时也要能把代码捞出来。

这些夹具照着审计日志里那几条真实回答的形状写（散文 + ```python 代码块 + 尾部表格，
全文没有 JSON），所以哪天解析退化成「只认纯 JSON」，这些用例会一起红。
"""

from __future__ import annotations

import json

import pytest

from btrader.ai_candidate import (
    CandidateParseError,
    absolute_strategy_imports,
    class_meta,
    from_answer,
    sanitize_key,
)

CODE = '''from btrader.strategy.base import IntParam, Strategy


class BandDivergence(Strategy):
    key = "band_divergence"
    name = "带宽发散回归"
    params_spec = [IntParam("period", "周期", 20, 5, 200)]

    def generate(self, df, params):
        ma = df["close"].rolling(int(params["period"])).mean()
        return df.assign(target=(df["close"] > ma).astype(float))[["target"]]
'''


def test_plain_json_is_taken_apart() -> None:
    text = json.dumps(
        {"strategy_key": "band_divergence", "name": "带宽发散回归", "code": CODE},
        ensure_ascii=False,
    )

    parsed = from_answer(text)

    assert parsed["strategy_key"] == "band_divergence"
    assert parsed["name"] == "带宽发散回归"
    assert "class BandDivergence(Strategy):" in parsed["code"]
    assert parsed["note"] == "从 JSON 里取到代码"


def test_a_json_fence_is_unwrapped() -> None:
    body = json.dumps({"strategy_key": "band_divergence", "code": CODE}, ensure_ascii=False)
    text = f"好的，下面是结果：\n\n```json\n{body}\n```\n"

    parsed = from_answer(text)

    assert parsed["code"].strip() == CODE.strip()
    assert parsed["note"] == "从 JSON 里取到代码"


def test_prose_plus_python_fence_plus_a_table_still_yields_code() -> None:
    """审计里那条真实回答的形状：说明文字、代码块、尾部参数表格，没有 JSON。"""
    text = (
        "## 改进思路\n\n"
        "带宽明显发散时才允许开新仓，平盘不交易。下面是改进后的策略代码。\n\n"
        "```python\n" + CODE + "```\n\n"
        "| 参数 | 说明 |\n| --- | --- |\n| period | 均线周期 |\n"
    )

    parsed = from_answer(text)

    assert parsed["strategy_key"] == "band_divergence"
    assert parsed["name"] == "带宽发散回归"
    assert parsed["note"] == "从 python 代码块里取到代码"
    assert parsed["code"].strip() == CODE.strip()
    assert "| 参数 |" not in parsed["code"]


def test_code_pasted_straight_into_prose_is_cut_at_the_class() -> None:
    text = "改进如下：\n\n" + CODE + "\n## 参数说明\n\nperiod 越大越平滑。\n"

    parsed = from_answer(text)

    assert parsed["note"] == "从正文里的 Strategy 类取到代码"
    assert parsed["strategy_key"] == "band_divergence"
    assert "## 参数说明" not in parsed["code"]
    # 尾部那段散文必须削掉，否则落盘的源码连编译都过不了。
    compile(parsed["code"], "<candidate>", "exec")


def test_prose_only_says_there_is_no_code() -> None:
    with pytest.raises(CandidateParseError) as info:
        from_answer("## 改进思路\n\n建议加一个带宽过滤器，具体代码就不写了。")

    message = str(info.value)
    assert "没有策略源码" in message
    assert "代码块" in message


def test_json_without_code_says_which_field_is_missing() -> None:
    text = json.dumps({"name": "双均线", "type": "trend_following"}, ensure_ascii=False)

    with pytest.raises(CandidateParseError) as info:
        from_answer(text)

    assert "缺少 code 字段" in str(info.value)


def test_an_empty_reply_is_reported_as_such() -> None:
    with pytest.raises(CandidateParseError) as info:
        from_answer("   \n  ")

    assert "没有返回任何内容" in str(info.value)


def test_relative_imports_copied_from_a_builtin_are_made_absolute() -> None:
    """模型照抄参考源码的 from ..base / from ...core，候选文件里那是 ImportError。"""
    raw = (
        "from ..base import FloatParam, IntParam, Strategy\n"
        "from ...core import indicators as ta\n"
        "import pandas as pd\n"
    )

    fixed = absolute_strategy_imports(raw)

    assert "from btrader.strategy.base import FloatParam, IntParam, Strategy" in fixed
    assert "from btrader.core import indicators as ta" in fixed
    assert "from ..base" not in fixed
    assert "import pandas as pd" in fixed


def test_a_relative_import_inside_a_fence_is_fixed_on_the_way_out() -> None:
    code = CODE.replace(
        "from btrader.strategy.base import IntParam, Strategy",
        "from ..base import IntParam, Strategy",
    )
    text = "```python\n" + code + "\n```"

    parsed = from_answer(text)

    assert "from btrader.strategy.base import IntParam, Strategy" in parsed["code"]
    assert "from ..base" not in parsed["code"]


def test_a_key_that_is_already_taken_gets_a_suffix_and_a_note() -> None:
    key, note = sanitize_key("bollinger", {"bollinger", "ma_cross"})

    assert key == "bollinger_ai"
    assert note == "原名 bollinger 已被占用，登记为 bollinger_ai"


def test_the_suffix_keeps_growing_until_the_key_is_free() -> None:
    key, note = sanitize_key("bollinger", {"bollinger", "bollinger_ai"})

    assert key == "bollinger_ai2"
    assert "bollinger_ai2" in note


def test_a_free_key_is_left_alone() -> None:
    assert sanitize_key("band_divergence", {"ma_cross"}) == ("band_divergence", "")


def test_an_illegal_key_is_normalized() -> None:
    key, note = sanitize_key("Band Divergence!", set())

    assert key == "band_divergence"
    assert "不是合法 key" in note
    assert sanitize_key("", set())[0] == "ai_strategy"
    assert sanitize_key("2fast", set())[0].startswith("ai_")


def test_class_meta_reads_the_literals_the_registry_reads() -> None:
    key, name = class_meta(CODE)

    assert key == "band_divergence"
    assert name == "带宽发散回归"
    assert class_meta("x = 1\n") == ("", "")

