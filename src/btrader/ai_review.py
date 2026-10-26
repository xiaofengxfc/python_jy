"""把最近的决策时间线喂回模型，要一份「哪里不足、该改什么」的复盘。

这一段只出建议：findings + 一份候选补丁（准则文本 + 参数覆盖）。落盘、校验、
审计全在路由和 ai_policy 里，模型碰不到磁盘，也碰不到闸门。

原则和别的 AI 入口一致：模型回读不懂时保留现状，不因为一次复盘失败就改任何参数。
"""

from __future__ import annotations

import time
from typing import Any

from . import ai, ai_policy

REVIEW_SYSTEM = "你是一名交易系统的复盘员。你只输出一个 JSON 对象，不解释、不寒暄。"

REVIEW_PROMPT = """下面是这台机器最近 {count} 条 AI 交易决策（最新的在最后）。

每条决策有：时间、动作、结果（order=已下单 / reject=被闸门拒 / skip=没做）、
拒绝理由、置信度、决策节奏、以及各段分析的摘要（已截断）。

【当前生效的决策准则】
{policy}

【当前参数】
{params}

【可改的字段】
{fields}

只输出一个 JSON 对象：
{{
  "findings": [
    {{"issue": "一句话说清哪里不足", "evidence": "引用哪几条、哪个数", "severity": "high|medium|low"}}
  ],
  "patch": {{
    "text": "新的准则全文（整段，不是差异；不改就给空字符串）",
    "overrides": {{"字段": 新值}},
    "rationale": "为什么这样改，一句话"
  }},
  "expected_effect": "可被验证的预期效果，一句话"
}}

要求：
- findings 最多 5 条，每条必须引用具体条目，不许写「总体感觉」「需要优化」这类空话。
- 只许改上面列出的字段。不许要求关闭风控、急停、主网二次确认。
- 准则文本是给决策模型看的操作纪律：写清什么情况必须 hold、什么情况才允许开仓，
  不要复述行情，也不要写成策略代码。
- 参数改动要有依据，最好能从上面数出来，例如「被漂移闸拒了 6 次而价格只动了 0.2%，
  把闸从 0.5% 放到 1%」。

【决策记录】
{rows}
"""

MAX_FINDINGS = 5


def _stamp(value: Any) -> str:
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(float(value)))
    except (TypeError, ValueError, OSError):
        return "?"


def _verdict_text(row: dict[str, Any]) -> str:
    for item in row.get("trace") or []:
        if isinstance(item, dict) and item.get("stage") == "verdict":
            text = str(item.get("text") or "").strip()
            return text[:200] + ("…" if len(text) > 200 else "")
    return ""


def format_row(row: dict[str, Any]) -> str:
    """一条决策一行，加一句裁决要点。刻意不贴整段 trace：全贴上会把上下文吃光。"""
    parts = [
        f"- {_stamp(row.get('at'))}",
        str(row.get("action") or "-"),
        f"/ {row.get('outcome') or '-'}",
    ]
    reason = str(row.get("reason") or "").strip()
    if reason:
        parts.append(f"（{reason[:160]}）")
    bits = []
    confidence = row.get("confidence")
    try:
        if confidence is not None and float(confidence) > 0:
            bits.append(f"置信度 {float(confidence):.2f}")
    except (TypeError, ValueError):
        pass
    cadence = str(row.get("cadence") or "").strip()
    if cadence:
        bits.append(f"节奏 {cadence}")
    if row.get("signal_target") is not None:
        try:
            bits.append(f"策略信号 {float(row['signal_target']):+.0%}")
        except (TypeError, ValueError):
            pass
    if bits:
        parts.append(" · " + " · ".join(bits))
    line = " ".join(parts)
    verdict = _verdict_text(row)
    if verdict:
        line += f"\n  裁决：{verdict}"
    return line


def format_params(values: dict[str, Any]) -> str:
    lines = []
    for key in ai_policy.PATCH_FIELDS:
        if key not in values:
            continue
        lines.append(f"- {ai_policy.PATCH_LABELS.get(key, key)}（{key}）= {values[key]!r}")
    return "\n".join(lines) or "- （空）"


def format_fields() -> str:
    return "\n".join(
        f"- {key}：{ai_policy.PATCH_LABELS.get(key, key)}" for key in ai_policy.PATCH_FIELDS
    )


def build_prompt(
    rows: list[dict[str, Any]], *, ai_auto: dict[str, Any], policy_text: str
) -> str:
    body = "\n".join(format_row(row) for row in rows) or "- （最近没有决策记录）"
    return REVIEW_PROMPT.format(
        count=len(rows),
        policy=(policy_text or "").strip() or "（还没有准则，按默认提示词走）",
        params=format_params(ai_auto),
        fields=format_fields(),
        rows=body,
    )


def clean_findings(values: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for item in values if isinstance(values, list) else []:
        if not isinstance(item, dict):
            continue
        issue = str(item.get("issue") or "").strip()[:200]
        if not issue:
            continue
        severity = str(item.get("severity") or "").strip().lower()
        if severity not in ("high", "medium", "low"):
            severity = "medium"
        out.append(
            {
                "issue": issue,
                "evidence": str(item.get("evidence") or "").strip()[:300],
                "severity": severity,
            }
        )
        if len(out) >= MAX_FINDINGS:
            break
    return out


def parse_answer(text: str) -> dict[str, Any]:
    """把模型的回答拆成 findings + 候选补丁。读不懂就返回空字典，由调用方报错。"""
    parsed = ai._parse_json_object(text)
    if not isinstance(parsed, dict):
        return {}
    patch = parsed.get("patch")
    if not isinstance(patch, dict):
        patch = {}
    text_value = str(patch.get("text") or "").strip()
    overrides, rejected = ai_policy.coerce_overrides(patch.get("overrides"))
    if len(text_value) > ai_policy.POLICY_TEXT_LIMIT:
        rejected.append(
            f"准则文本 {len(text_value)} 字，超过 {ai_policy.POLICY_TEXT_LIMIT} 字上限，已丢弃"
        )
        text_value = ""
    return {
        "findings": clean_findings(parsed.get("findings")),
        "patch": {
            "text": text_value,
            "overrides": overrides,
            "rationale": str(patch.get("rationale") or "").strip()[:400],
        },
        "expected_effect": str(parsed.get("expected_effect") or "").strip()[:400],
        "rejected": rejected,
    }


def review(
    llm: Any,
    rows: list[dict[str, Any]],
    *,
    ai_auto: dict[str, Any],
    policy_text: str,
    limit: int = 30,
) -> dict[str, Any]:
    """跑一次复盘。rows 是审计里的 decision 行（按时间升序）。"""
    picked = [row for row in rows if isinstance(row, dict)][-max(1, int(limit)) :]
    empty = {
        "findings": [],
        "patch": {"text": "", "overrides": {}, "rationale": ""},
        "rejected": [],
    }
    if not picked:
        return {
            "ok": False,
            "error": "最近没有决策记录，先让它跑几轮再来复盘。",
            **empty,
            "count": 0,
        }
    prompt = build_prompt(picked, ai_auto=ai_auto, policy_text=policy_text)
    answer = llm.ask(prompt, system=REVIEW_SYSTEM, cache_key="", kind="ai_review") or {}
    if not answer.get("ok"):
        return {
            "ok": False,
            "error": str(answer.get("error") or "复盘调用失败"),
            **empty,
            "count": len(picked),
        }
    raw = str(answer.get("text") or "")
    parsed = parse_answer(raw)
    if not parsed:
        hint = (
            "回答被截断，没有完整 JSON。"
            if answer.get("truncated")
            else "回答里没有可解析的 JSON。"
        )
        return {"ok": False, "error": hint, **empty, "count": len(picked), "text": raw}
    result = {"ok": True, "error": "", "count": len(picked), "text": raw}
    result.update(parsed)
    return result
