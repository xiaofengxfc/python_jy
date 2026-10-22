"""决策流水线：分段、跳过、失败降级。

流水线只产出文本；方向约束和风控闸门在 ai_trader 里，这里只验证分段行为。
"""

from __future__ import annotations

import pytest

from btrader.ai_pipeline import (
    DEPTHS,
    PipelineContext,
    run,
    stages_for,
)


class FakeLLM:
    def __init__(self, answers: dict[str, str] | None = None, fail_on: str = "") -> None:
        self.answers = answers or {}
        self.fail_on = fail_on
        self.calls: list[tuple[str, str]] = []

    def ask(self, prompt: str, **kwargs) -> dict:
        kind = str(kwargs.get("kind") or "")
        self.calls.append((kind, prompt))
        if self.fail_on and self.fail_on in kind:
            return {"ok": False, "error": "超时", "text": ""}
        return {"ok": True, "text": self.answers.get(kind, f"{kind} 的回答")}


def ctx(**overrides) -> PipelineContext:
    base = dict(
        symbol="BTC/USDT",
        timeframe="15m",
        digest="【行情】收盘 100",
        equity=1000.0,
        position_quote=0.0,
        price=100.0,
        signal_target=1.0,
        max_order_quote=50.0,
        max_position_quote=200.0,
        min_confidence=0.6,
    )
    base.update(overrides)
    return PipelineContext(**base)


def test_depths_run_the_expected_stages() -> None:
    assert stages_for("quick") == ["verdict"]
    assert stages_for("standard") == ["technical", "bull", "bear", "verdict"]
    assert stages_for("deep") == ["technical", "news", "bull", "bear", "risk", "verdict"]
    # 不认识的深度按标准跑，不能因为拼错就退化成一次调用。
    assert stages_for("乱写") == stages_for("standard")
    assert set(DEPTHS) == {"quick", "standard", "deep"}


def test_quick_depth_makes_one_call_and_keeps_the_signal_line() -> None:
    llm = FakeLLM()

    result = run(llm, ctx(), depth="quick")

    assert result.ok
    assert len(llm.calls) == 1
    kind, prompt = llm.calls[0]
    assert kind == "ai_verdict"
    assert "策略信号" in prompt
    assert result.trace[-1]["stage"] == "verdict"


def test_standard_depth_walks_the_debate() -> None:
    llm = FakeLLM()

    result = run(llm, ctx(), depth="standard")

    assert [item["stage"] for item in result.trace] == [
        "technical",
        "bull",
        "bear",
        "verdict",
    ]
    # 空头看得到多头的论证，不然那不叫辩论。
    bear_prompt = next(prompt for kind, prompt in llm.calls if kind == "ai_bear")
    assert "做多理由" in bear_prompt


def test_the_news_stage_is_skipped_when_there_is_no_news() -> None:
    llm = FakeLLM()

    result = run(llm, ctx(), depth="deep")

    news = next(item for item in result.trace if item["stage"] == "news")
    assert news["skipped"] is True
    assert not any(kind == "ai_news" for kind, _ in llm.calls)


def test_the_news_stage_runs_when_headlines_are_given() -> None:
    llm = FakeLLM()

    result = run(llm, ctx(news="- 今天｜ETF 通过"), depth="deep")

    news = next(item for item in result.trace if item["stage"] == "news")
    assert news["skipped"] is False and news["ok"] is True
    assert any(kind == "ai_news" for kind, _ in llm.calls)


def test_a_failed_middle_stage_does_not_stop_the_pipeline() -> None:
    llm = FakeLLM(fail_on="technical")

    result = run(llm, ctx(), depth="standard")

    technical = next(item for item in result.trace if item["stage"] == "technical")
    assert technical["ok"] is False
    assert result.ok


def test_a_failed_verdict_fails_the_whole_run() -> None:
    llm = FakeLLM(fail_on="verdict")

    result = run(llm, ctx(), depth="standard")

    assert not result.ok
    assert result.error == "超时"
    assert result.trace[-1]["stage"] == "verdict"


def test_every_stage_carries_what_the_panel_shows() -> None:
    llm = FakeLLM()

    result = run(llm, ctx(), depth="standard")

    for item in result.trace:
        assert set(item) >= {"stage", "title", "ok", "text", "model", "tokens", "elapsed"}
        assert item["title"]
