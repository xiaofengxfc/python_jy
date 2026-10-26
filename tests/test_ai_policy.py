"""决策准则（ai_policy）与复盘（ai_review）的离线测试。

覆盖四块：
1. 准则文件的读写、损坏容错、版本历史；
2. 补丁白名单与数值清洗（NaN / bool / 非整数 / 未知字段）；
3. 复盘的解析与降级（读不懂、没记录、调用失败都不改任何参数）；
4. 路由层的应用与回滚：落盘、记版本、写审计，且三条不变量改不到。
"""

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from btrader import ai_policy, ai_review, paths
from btrader.core.config import AIAutoSettings, RiskSettings
from btrader.engine.ai_trader import read_audit_rows
from btrader.web import routes
from btrader.context import AppContext


# ------------------------------------------------------------------ policy file
def test_missing_policy_file_loads_blank():
    state = ai_policy.load(paths.data_dir() / "nope.json")
    assert state["version"] == 0
    assert state["text"] == ""
    assert state["history"] == []


def test_policy_round_trips_through_disk():
    state = ai_policy.apply_version(
        ai_policy.blank_state(),
        text="结构一致才开仓。",
        overrides={"min_confidence": 0.7},
        note="复盘后收紧",
        author="人工",
        before={"min_confidence": 0.6},
        after={"min_confidence": 0.7},
        now=1000.0,
    )
    ai_policy.save(state)
    loaded = ai_policy.load()
    assert loaded["version"] == 1
    assert loaded["text"] == "结构一致才开仓。"
    assert loaded["overrides"] == {"min_confidence": 0.7}
    assert loaded["history"][0]["note"] == "复盘后收紧"


def test_a_broken_policy_file_does_not_raise():
    path = paths.data_dir() / ai_policy.POLICY_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    state = ai_policy.load()
    assert state["version"] == 0 and state["history"] == []


def test_history_is_newest_first_and_bounded():
    state = ai_policy.blank_state()
    for index in range(3):
        state = ai_policy.apply_version(
            state,
            text=f"第{index}版",
            overrides={"cooldown_sec": 30.0},
            note="",
            author="人工",
            before={},
            after={},
            now=float(index),
        )
    assert state["version"] == 3
    assert [item["version"] for item in state["history"]] == [3, 2, 1]


def test_policy_text_limit_is_enforced():
    with pytest.raises(ai_policy.PolicyError):
        ai_policy.apply_version(
            ai_policy.blank_state(),
            text="x" * (ai_policy.POLICY_TEXT_LIMIT + 1),
            overrides={},
            note="",
            author="",
            before={},
            after={},
        )


def test_public_view_hides_full_config_snapshots():
    state = ai_policy.apply_version(
        ai_policy.blank_state(),
        text="t",
        overrides={"leverage": 2.0},
        note="",
        author="人工",
        before={"leverage": 1.0},
        after={"leverage": 2.0},
    )
    view = ai_policy.public_view(state)
    assert view["version"] == 1
    assert "before" not in view["history"][0]
    assert "after" not in view["history"][0]
    assert view["history"][0]["overrides"] == {"leverage": 2.0}


# -------------------------------------------------------------------- overrides
@pytest.mark.parametrize(
    "field_name",
    ["enabled", "kill_switch", "confirm_live", "max_daily_loss", "strategy_params"],
)
def test_fields_outside_the_patch_list_are_refused(field_name):
    """这三类东西不是策略参数：开关、急停、以及所有下单路径共用的风控硬顶。"""
    with pytest.raises(ai_policy.PolicyError):
        ai_policy.clean_overrides({field_name: True})


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1.0])
def test_numbers_must_be_finite_and_non_negative(bad):
    with pytest.raises(ai_policy.PolicyError):
        ai_policy.clean_overrides({"max_order_quote": bad})


def test_a_number_field_refuses_booleans():
    with pytest.raises(ai_policy.PolicyError):
        ai_policy.clean_overrides({"leverage": True})


def test_integer_fields_refuse_fractions():
    with pytest.raises(ai_policy.PolicyError):
        ai_policy.clean_overrides({"max_decisions_per_day": 12.5})
    assert ai_policy.clean_overrides({"max_decisions_per_day": 12.0}) == {
        "max_decisions_per_day": 12
    }


def test_text_fields_refuse_blank():
    with pytest.raises(ai_policy.PolicyError):
        ai_policy.clean_overrides({"cadence": "  "})


def test_coerce_keeps_the_good_ones_and_reports_the_rest():
    cleaned, rejected = ai_policy.coerce_overrides(
        {"min_confidence": 0.7, "kill_switch": False, "leverage": "abc"}
    )
    assert cleaned == {"min_confidence": 0.7}
    assert len(rejected) == 2


# ----------------------------------------------------------------------- review
def _answer(**patch) -> str:
    body = {
        "findings": [
            {"issue": "空仓时也开仓", "evidence": "3 条 order 里 2 条置信度 0.61", "severity": "high"}
        ],
        "patch": {"text": "空仓时只在 4h 结构同向才开仓。", "overrides": {"min_confidence": 0.65}, "rationale": "少打无结构的单"},
        "expected_effect": "被拒率下降",
    }
    body.update(patch)
    return json.dumps(body, ensure_ascii=False)


class FakeLLM:
    def __init__(self, text: str = "", *, ok: bool = True, error: str = "", truncated: bool = False):
        self.text = text
        self.ok = ok
        self.error = error
        self.truncated = truncated
        self.prompts: list[str] = []

    def ask(self, prompt: str, **kwargs):
        self.prompts.append(prompt)
        if not self.ok:
            return {"ok": False, "error": self.error}
        return {"ok": True, "text": self.text, "truncated": self.truncated}


def _rows(count: int = 3) -> list[dict]:
    return [
        {
            "kind": "decision",
            "at": 1000.0 + index,
            "action": "open_long",
            "outcome": "order" if index % 2 == 0 else "reject",
            "reason": "" if index % 2 == 0 else "价格漂移超过 0.5%",
            "confidence": 0.61,
            "cadence": "15m",
            "trace": [{"stage": "verdict", "text": "理由"}],
        }
        for index in range(count)
    ]


def test_review_parses_findings_and_patch():
    result = ai_review.review(
        FakeLLM(_answer()), _rows(), ai_auto={"min_confidence": 0.6}, policy_text=""
    )
    assert result["ok"]
    assert result["findings"][0]["issue"] == "空仓时也开仓"
    assert result["patch"]["overrides"] == {"min_confidence": 0.65}
    assert result["patch"]["text"].startswith("空仓时")


def test_review_drops_unusable_override_keys():
    text = _answer(
        patch={
            "text": "",
            "overrides": {"kill_switch": False, "min_confidence": 0.7},
            "rationale": "",
        }
    )
    result = ai_review.review(
        FakeLLM(text), _rows(), ai_auto={}, policy_text=""
    )
    assert result["patch"]["overrides"] == {"min_confidence": 0.7}
    assert result["rejected"] and "kill_switch" in result["rejected"][0]


def test_review_drops_overlong_policy_text():
    text = _answer(patch={"text": "x" * (ai_policy.POLICY_TEXT_LIMIT + 10), "overrides": {}})
    result = ai_review.review(FakeLLM(text), _rows(), ai_auto={}, policy_text="")
    assert result["patch"]["text"] == ""
    assert any("上限" in item for item in result["rejected"])


def test_review_without_rows_says_so():
    result = ai_review.review(FakeLLM(_answer()), [], ai_auto={}, policy_text="")
    assert not result["ok"]
    assert "没有决策记录" in result["error"]


def test_review_reports_llm_failure_without_touching_anything():
    result = ai_review.review(
        FakeLLM(ok=False, error="模型超时"), _rows(), ai_auto={}, policy_text=""
    )
    assert not result["ok"]
    assert result["error"] == "模型超时"
    assert result["patch"]["overrides"] == {}


def test_review_flags_a_truncated_answer():
    result = ai_review.review(
        FakeLLM("半截 JSON", truncated=True), _rows(), ai_auto={}, policy_text=""
    )
    assert not result["ok"]
    assert "截断" in result["error"]


def test_findings_are_capped_and_severity_normalised():
    many = [
        {"issue": f"问题{index}", "evidence": "e", "severity": "urgent"} for index in range(9)
    ]
    cleaned = ai_review.clean_findings(many)
    assert len(cleaned) == ai_review.MAX_FINDINGS
    assert cleaned[0]["severity"] == "medium"


def test_prompt_carries_the_decisions_and_the_policy():
    prompt = ai_review.build_prompt(
        _rows(2), ai_auto={"min_confidence": 0.6}, policy_text="只在同向时开仓"
    )
    assert "open_long" in prompt
    assert "只在同向时开仓" in prompt
    assert "min_confidence" in prompt


# ----------------------------------------------------------------------- routes
def test_clamp_ai_auto_pulls_limits_back_to_risk():
    draft = AIAutoSettings(max_order_quote=999.0, max_position_quote=99999.0, leverage=50.0)
    risk = RiskSettings(max_order_quote=500.0, max_position_quote=5000.0, max_leverage=10.0)
    notes = routes.clamp_ai_auto(draft, risk)
    assert draft.max_order_quote == 500.0
    assert draft.max_position_quote == 5000.0
    assert draft.leverage == 10.0
    assert len(notes) == 3


def test_clamp_ai_auto_leaves_zero_caps_alone():
    """风控里 0 表示「这条不限」，这时不该把 AI 的上限也压成 0。"""
    draft = AIAutoSettings(max_order_quote=80.0)
    notes = routes.clamp_ai_auto(draft, RiskSettings(max_order_quote=0.0))
    assert draft.max_order_quote == 80.0
    assert notes == []


def test_apply_patch_persists_audits_and_bumps_the_version():
    ctx = AppContext()
    ctx.settings.ai_auto = AIAutoSettings()

    result = routes.apply_policy_patch(
        ctx,
        routes.AIPolicyApplyPayload(
            text="空仓时只在 4h 同向才开仓。",
            overrides={"min_confidence": 0.7},
            note="复盘后收紧",
            author="AI 建议",
        ),
    )

    assert result["changed"] == {"min_confidence": 0.7}
    assert ctx.settings.ai_auto.min_confidence == 0.7
    assert result["policy"]["version"] == 1
    stored = ai_policy.load()
    assert stored["version"] == 1
    assert stored["history"][0]["before"]["min_confidence"] == 0.6

    rows = read_audit_rows(paths.logs_dir() / "ai-trade-audit.jsonl", since=0, limit=10)
    policy_rows = [row for row in rows if row.get("kind") == "policy"]
    assert policy_rows and policy_rows[-1]["version"] == 1


def test_apply_patch_with_no_change_still_records_the_text():
    ctx = AppContext()
    ctx.settings.ai_auto = AIAutoSettings()
    result = routes.apply_policy_patch(
        ctx, routes.AIPolicyApplyPayload(text="只有文本的变化。")
    )
    assert result["changed"] == {}
    assert result["policy"]["text"] == "只有文本的变化。"


def test_rollback_restores_the_previous_parameters():
    ctx = AppContext()
    ctx.settings.ai_auto = AIAutoSettings()
    routes.apply_policy_patch(
        ctx, routes.AIPolicyApplyPayload(overrides={"min_confidence": 0.9, "cooldown_sec": 5.0})
    )
    assert ctx.settings.ai_auto.min_confidence == 0.9

    result = routes.rollback_policy(ctx, 1)
    assert ctx.settings.ai_auto.min_confidence == 0.6
    assert ctx.settings.ai_auto.cooldown_sec == 30.0
    assert result["policy"]["version"] == 2
    assert "撤销 v1" in result["policy"]["history"][0]["note"]


def test_rollback_to_a_missing_version_is_404():
    ctx = AppContext()
    with pytest.raises(HTTPException) as caught:
        routes.rollback_policy(ctx, 99)
    assert caught.value.status_code == 404


def test_apply_patch_rejects_setting_level_errors():
    """补丁里的值也要过设置层：未知节奏照样 400，而不是悄悄写进配置。"""
    ctx = AppContext()
    ctx.settings.ai_auto = AIAutoSettings()
    with pytest.raises(HTTPException) as caught:
        routes.apply_policy_patch(ctx, routes.AIPolicyApplyPayload(overrides={"cadence": "7s"}))
    assert caught.value.status_code == 400
    assert "决策节奏" in caught.value.detail


def test_apply_patch_records_clamped_limits_in_warnings():
    ctx = AppContext()
    ctx.settings.ai_auto = AIAutoSettings()
    ctx.settings.risk = RiskSettings(max_order_quote=500.0, max_position_quote=5000.0, max_leverage=10.0)
    result = routes.apply_policy_patch(
        ctx, routes.AIPolicyApplyPayload(overrides={"max_order_quote": 900.0})
    )
    assert ctx.settings.ai_auto.max_order_quote == 500.0
    assert result["warnings"] and "风控上限" in result["warnings"][0]
