"""Tests for the LLM assistant: prompt building, parsing and the cost guards.

The network is never touched. What is pinned here is the part that decides what
the model is allowed to see and how its answer is interpreted — that is where a
bug would be silent and expensive:

* an entry must never be judged with the bar it entered on (lookahead),
* a level outside the traded range must never reach the chart,
* an unreadable verdict must not silently delete a trade,
* a disabled assistant, a missing key or an exhausted budget must fail closed.
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request

import pandas as pd
import pytest

from btrader import ai, llm
from btrader.llm import AISettings, LLMService

from helpers import make_candles


class FakeLLM:
    """Stands in for :class:`LLMService`; records what it was asked."""

    def __init__(self, reply: str = "", ok: bool = True, error: str = "") -> None:
        self.reply = reply
        self.ok = ok
        self.error = error
        self.prompts: list[str] = []
        self.keys: list[str] = []

    def ask(self, prompt: str, *, system: str = "", cache_key: str = "", kind: str = ""):
        self.prompts.append(prompt)
        self.keys.append(cache_key)
        if not self.ok:
            return {"ok": False, "text": "", "error": self.error, "kind": kind}
        return {
            "ok": True,
            "text": self.reply,
            "cached": False,
            "tokens": 10,
            "elapsed": 0.1,
            "kind": kind,
        }


# ------------------------------------------------------------------- digest
def test_digest_reports_computed_indicators_not_a_picture(candles) -> None:
    text = ai.digest(candles, timeframe="1h", label="BTC/USDT 1h")
    assert "BTC/USDT 1h" in text
    # The numbers the model reasons about must come from the frame, not from it.
    last_close = float(candles["close"].iloc[-1])
    assert f"{last_close:,.4f}" in text
    assert "RSI14" in text
    assert "ATR14" in text
    assert "均线" in text


def test_digest_survives_a_frame_too_short_for_indicators() -> None:
    tiny = make_candles(n=3)
    text = ai.digest(tiny, timeframe="1h")
    # No 200-period average can be computed from three bars, so it must not be
    # claimed. A NaN in the prompt is worse than a missing line.
    assert "MA200" not in text
    assert "nan" not in text.lower()


def test_digest_of_an_empty_frame_says_so() -> None:
    assert "数据不足" in ai.digest(pd.DataFrame(), timeframe="1h")


def test_candle_key_changes_when_a_bar_closes(candles) -> None:
    first = ai.candle_key("BTC/USDT", "1h", candles)
    assert first == ai.candle_key("BTC/USDT", "1h", candles)
    assert first != ai.candle_key("BTC/USDT", "1h", candles.iloc[:-1])
    assert "empty" in ai.candle_key("BTC/USDT", "1h", pd.DataFrame())


# ------------------------------------------------------------------- levels
def test_levels_outside_the_traded_range_are_dropped(candles) -> None:
    """A support 30% below the last bar would look authoritative and mean nothing."""
    high = float(candles["high"].max())
    low = float(candles["low"].min())
    levels = [
        {"price": float(candles["close"].iloc[-1]), "kind": "support", "label": "近端"},
        {"price": low * 0.5, "kind": "support", "label": "编造"},
        {"price": high * 2, "kind": "resistance", "label": "编造"},
    ]
    kept = ai.validate_levels(levels, candles)
    assert [item["label"] for item in kept] == ["近端"]


def test_levels_of_the_same_kind_are_deduped_and_capped(candles) -> None:
    last = float(candles["close"].iloc[-1])
    span = float(candles["high"].max()) - float(candles["low"].min())
    # Five supports packed inside 0.1% of the range: one line, not five.
    levels = [
        {"price": last + span * 0.0001 * index, "kind": "support", "label": f"s{index}"}
        for index in range(5)
    ]
    assert len(ai.validate_levels(levels, candles)) == 1

    spread = [
        {"price": last + span * 0.1 * index, "kind": "resistance", "label": f"r{index}"}
        for index in range(1, 6)
    ]
    assert len(ai.validate_levels(spread, candles)) <= 3


def test_level_distance_is_measured_from_the_last_close(candles) -> None:
    last = float(candles["close"].iloc[-1])
    kept = ai.validate_levels([{"price": last * 1.01, "kind": "resistance", "label": ""}], candles)
    assert kept[0]["distance"] == pytest.approx(0.01, abs=1e-6)


def test_unknown_level_kind_is_rejected(candles) -> None:
    price = float(candles["close"].iloc[-1])
    kept = ai.validate_levels([{"price": price, "kind": "vibes", "label": "?"}], candles)
    assert kept == []


def test_split_levels_keeps_the_prose_and_drops_the_json() -> None:
    text = (
        "1. 趋势：多头。\n2. 关键价位：98000 有支撑。\n"
        '{"levels": [{"price": 98000, "kind": "support", "label": "日线支撑"}]}'
    )
    levels, prose = ai._split_levels(text)
    assert levels == [{"price": 98000.0, "kind": "support", "label": "日线支撑"}]
    # The raw JSON must not be shown next to the analysis: it is machine output.
    assert "{" not in prose
    assert "趋势" in prose


def test_split_levels_without_json_returns_the_text_untouched() -> None:
    levels, prose = ai._split_levels("纯文字分析")
    assert levels == []
    assert prose == "纯文字分析"


def test_analyze_klines_attaches_validated_levels(candles) -> None:
    last = float(candles["close"].iloc[-1])
    reply = (
        "1. 趋势：震荡。\n"
        + json.dumps({"levels": [{"price": last, "kind": "support", "label": "当前价"}]})
    )
    fake = FakeLLM(reply=reply)
    result = ai.analyze_klines(fake, symbol="BTC/USDT", frames={"1h": candles})
    assert result["ok"] is True
    assert result["timeframes"] == ["1h"]
    assert len(result["levels"]) == 1
    assert "{" not in result["text"]


def test_analyze_klines_without_data_fails_without_calling_the_model() -> None:
    fake = FakeLLM()
    result = ai.analyze_klines(fake, symbol="BTC/USDT", frames={})
    assert result["ok"] is False
    assert fake.prompts == []


# ----------------------------------------------------------------- sentiment
def test_sentiment_parses_scores_and_the_overall_line() -> None:
    text = "1: 0.8 上币利好\n2: -0.5 监管\n3: 0 无关\n总体: 0.3 情绪偏暖"
    scores, overall = ai._parse_sentiment(text)
    assert scores == {1: 0.8, 2: -0.5, 3: 0.0}
    assert overall == pytest.approx(0.3)


def test_sentiment_falls_back_to_the_mean_when_the_overall_line_is_missing() -> None:
    _, overall = ai._parse_sentiment("1: 0.6 好\n2: -0.2 坏")
    assert overall == pytest.approx(0.2)


def test_sentiment_scores_are_clamped_to_the_scale() -> None:
    scores, _ = ai._parse_sentiment("1: 9 夸大\n2: -9 夸大")
    assert scores == {1: 1.0, 2: -1.0}


def test_news_analysis_without_headlines_fails_fast() -> None:
    fake = FakeLLM()
    result = ai.analyze_news(fake, symbol="BTC/USDT", items=[])
    assert result["ok"] is False
    assert fake.prompts == []


# ------------------------------------------------------------------- verdict
def test_verdict_reads_yes_and_no() -> None:
    assert ai._parse_verdict("YES\n趋势向上")[0] == "yes"
    assert ai._parse_verdict("no\n追高")[0] == "no"
    assert ai._parse_verdict("No. 这里风险太大")[0] == "no"


def test_an_unreadable_verdict_keeps_the_trade() -> None:
    """Dropping a trade because the model rambled would silently change the run."""
    verdict, _ = ai._parse_verdict("我觉得这个行情有点复杂")
    assert verdict == "yes"


def test_verdict_reason_is_short() -> None:
    _, reason = ai._parse_verdict("NO\n" + "很长的理由" * 100)
    assert len(reason) <= 120


# ------------------------------------------------------------- entry window
def test_prejudge_order_reports_the_size_bucket(candles) -> None:
    fake = FakeLLM(reply="可以\n量价配合")
    result = ai.prejudge_order(
        fake,
        symbol="BTC/USDT",
        timeframe="1h",
        side="buy",
        quote=120.0,
        frame=candles,
    )
    assert result["ok"] is True
    # 120 falls in the 500 bucket: a slider nudge inside the same bucket must
    # reuse the cached answer instead of billing a new call.
    assert result["bucket"] == "500"


def test_prejudge_cache_key_is_stable_inside_a_bucket_and_moves_between_them(candles) -> None:
    first = FakeLLM(reply="可以\nok")
    second = FakeLLM(reply="可以\nok")
    third = FakeLLM(reply="可以\nok")
    # 120 and 480 share the 500 bucket; 5000 is its own, so it must not reuse.
    for fake, quote in ((first, 120.0), (second, 480.0), (third, 5_000.0)):
        ai.prejudge_order(
            fake,
            symbol="BTC/USDT",
            timeframe="1h",
            side="buy",
            quote=quote,
            frame=candles,
        )
    assert first.keys == second.keys
    assert first.keys != third.keys
    assert ai._quote_bucket(0) == "0"
    assert ai._quote_bucket(9_999_999) == "big"


def test_prejudge_without_candles_fails_without_calling_the_model() -> None:
    fake = FakeLLM()
    result = ai.prejudge_order(
        fake,
        symbol="BTC/USDT",
        timeframe="1h",
        side="buy",
        quote=100.0,
        frame=pd.DataFrame(),
    )
    assert result["ok"] is False
    assert fake.prompts == []


# ------------------------------------------------------------------- windows
def test_entry_window_never_includes_the_entry_bar(candles) -> None:
    """The whole point: the model must not see the bar it is judging."""
    entry_ms = int(candles["timestamp"].iloc[-10])
    window = ai.entry_window(candles, entry_ms)
    assert len(window) > 0
    assert int(window["timestamp"].max()) < entry_ms


def test_entry_window_of_an_empty_frame_is_empty(candles) -> None:
    assert ai.entry_window(pd.DataFrame(), 1).empty


def test_filter_trades_skips_entries_without_enough_history(candles) -> None:
    first_ms = int(candles["timestamp"].iloc[1])
    trades = [{"entry_time": first_ms, "side": "long", "pnl": 5.0}]
    fake = FakeLLM(reply="YES\n可以")
    result = ai.filter_trades(
        fake, symbol="BTC/USDT", timeframe="1h", frame=candles, trades=trades
    )
    assert result["ok"] is True
    assert result["kept_count"] == 1
    assert result["judged"][0]["verdict"] == "keep"
    assert fake.prompts == []


def test_filter_trades_drops_a_vetoed_entry(candles) -> None:
    entry_ms = int(candles["timestamp"].iloc[-5])
    trades = [{"entry_time": entry_ms, "side": "long", "pnl": 12.0}]
    fake = FakeLLM(reply="NO\n量能不足")
    result = ai.filter_trades(
        fake, symbol="BTC/USDT", timeframe="1h", frame=candles, trades=trades
    )
    assert result["kept_count"] == 0
    assert result["judged"][0]["verdict"] == "no"
    assert "量能" in result["judged"][0]["reason"]


def test_filter_trades_keeps_a_trade_when_the_call_fails(candles) -> None:
    """A network failure must not quietly improve the strategy's numbers."""
    entry_ms = int(candles["timestamp"].iloc[-5])
    trades = [{"entry_time": entry_ms, "side": "long", "pnl": -3.0}]
    fake = FakeLLM(ok=False, error="超时")
    result = ai.filter_trades(
        fake, symbol="BTC/USDT", timeframe="1h", frame=candles, trades=trades
    )
    assert result["kept_count"] == 1
    assert result["failures"] == 1


def test_filter_trades_reports_truncation(candles) -> None:
    trades = [
        {"entry_time": int(candles["timestamp"].iloc[index]), "side": "long", "pnl": 1.0}
        for index in range(-40, -20)
    ]
    fake = FakeLLM(reply="YES\n可以")
    result = ai.filter_trades(
        fake, symbol="BTC/USDT", timeframe="1h", frame=candles, trades=trades, limit=3
    )
    assert result["total"] == 3
    assert result["truncated"] is True


def test_filter_trades_without_trades_fails() -> None:
    fake = FakeLLM()
    result = ai.filter_trades(
        fake, symbol="BTC/USDT", timeframe="1h", frame=make_candles(), trades=[]
    )
    assert result["ok"] is False


# ------------------------------------------------------------------- metrics
def test_trade_metrics_are_computed_from_the_trade_list() -> None:
    metrics = ai.trade_metrics(
        [{"pnl": 100.0}, {"pnl": -50.0}, {"pnl": 25.0}], initial_capital=1000.0
    )
    assert metrics["trades"] == 3.0
    assert metrics["net_pnl"] == pytest.approx(75.0)
    assert metrics["total_return"] == pytest.approx(0.075)
    assert metrics["win_rate"] == pytest.approx(2 / 3)
    assert metrics["profit_factor"] == pytest.approx(125 / 50)
    assert metrics["max_drawdown"] < 0


def test_trade_metrics_of_an_empty_run_do_not_divide_by_zero() -> None:
    metrics = ai.trade_metrics([], initial_capital=1000.0)
    assert metrics["trades"] == 0.0
    assert metrics["win_rate"] == 0.0
    assert metrics["profit_factor"] == 0.0


def test_compare_runs_calls_a_clean_improvement_what_it_is() -> None:
    comparison = ai.compare_runs(
        {"total_return": 0.10, "max_drawdown": -0.20, "trades": 20.0},
        {"total_return": 0.15, "max_drawdown": -0.12, "trades": 14.0},
    )
    assert comparison["dropped"] == 6.0
    assert "正贡献" in comparison["verdict"]


def test_compare_runs_refuses_to_flatter_a_worse_run() -> None:
    comparison = ai.compare_runs(
        {"total_return": 0.10, "max_drawdown": -0.10, "trades": 20.0},
        {"total_return": 0.02, "max_drawdown": -0.25, "trades": 5.0},
    )
    assert "没有帮助" in comparison["verdict"]


def test_compare_runs_handles_a_run_with_no_trades_left() -> None:
    comparison = ai.compare_runs(
        {"total_return": 0.10, "max_drawdown": -0.10, "trades": 20.0},
        {"total_return": 0.0, "max_drawdown": 0.0, "trades": 0.0},
    )
    assert "全部交易都否掉" in comparison["verdict"]


# -------------------------------------------------------------- strategy pick
def test_suggest_strategy_rejects_an_invented_key() -> None:
    fake = FakeLLM(reply='{"strategy": "moon", "params": {}, "reason": "瞎编"}')
    result = ai.suggest_strategy(
        fake,
        idea="随便",
        strategies=[{"key": "ma_cross", "name": "均线交叉", "params_spec": []}],
        frames={},
    )
    assert result["ok"] is False
    assert "moon" in result["error"]


def test_suggest_strategy_accepts_a_known_key() -> None:
    fake = FakeLLM(
        reply='```json\n{"strategy": "ma_cross", "params": {"fast": 9}, "reason": "顺势"}\n```'
    )
    result = ai.suggest_strategy(
        fake,
        idea="跟着趋势做",
        strategies=[{"key": "ma_cross", "name": "均线交叉", "params_spec": []}],
        frames={},
    )
    assert result["ok"] is True
    assert result["strategy"] == "ma_cross"
    assert result["params"] == {"fast": 9}


def test_suggest_strategy_without_a_catalogue_fails() -> None:
    fake = FakeLLM()
    result = ai.suggest_strategy(fake, idea="随便", strategies=[], frames={})
    assert result["ok"] is False
    assert fake.prompts == []


def test_json_extraction_survives_prose_around_the_object() -> None:
    parsed = ai._parse_json_object('好的，结果如下：\n{"a": 1}\n希望有帮助')
    assert parsed == {"a": 1}
    assert ai._parse_json_object("没有 JSON") is None


# ---------------------------------------------------------------- brief queue
def test_brief_queue_runs_the_job_off_the_caller() -> None:
    queue = ai.BriefQueue(size=2)
    done: list[int] = []
    assert queue.submit({"run": lambda: done.append(1)}) is True
    for _ in range(200):
        if done:
            break
        import time

        time.sleep(0.01)
    assert done == [1]


def test_brief_queue_drops_work_instead_of_blocking() -> None:
    """A full queue must never stall the trading loop."""
    import threading

    queue = ai.BriefQueue(size=1)
    started = threading.Event()
    gate = threading.Event()

    def slow() -> None:
        started.set()
        gate.wait(2.0)

    assert queue.submit({"run": slow}) is True
    # Wait for the worker to actually pick the job up: only then is the single
    # queue slot free and the next submit's answer meaningful.
    assert started.wait(1.0)
    assert queue.submit({"run": lambda: None}) is True
    assert queue.submit({"run": lambda: None}) is False
    gate.set()


# ---------------------------------------------------------------- llm service
def test_disabled_service_refuses_without_spending_anything() -> None:
    service = LLMService(AISettings(enabled=False))
    result = service.ask("hi")
    assert result["ok"] is False
    assert "未启用" in result["error"]


def test_missing_key_is_reported_before_the_request(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BITRADER_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(llm, "load_key", lambda: "")
    llm.reset_cache()
    service = LLMService(AISettings(enabled=True, provider="deepseek"))
    result = service.ask("hi")
    assert result["ok"] is False
    assert "API Key" in result["error"]


def test_daily_limit_stops_calls(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BITRADER_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(llm, "load_key", lambda: "sk-test")
    llm.reset_cache()
    store = llm.cache()
    for _ in range(3):
        store.count_call()
    service = LLMService(AISettings(enabled=True, daily_limit=3))
    result = service.ask("hi")
    assert result["ok"] is False
    assert "上限" in result["error"]


def test_cache_returns_the_same_answer_without_a_second_call(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BITRADER_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(llm, "load_key", lambda: "sk-test")
    llm.reset_cache()
    service = LLMService(AISettings(enabled=True, cache_seconds=600))
    calls: list[str] = []

    def fake_request(spec, settings, key, system, prompt, **kwargs):
        calls.append(prompt)
        return "答案", 7

    monkeypatch.setattr(service, "_request", fake_request)
    first = service.ask("问题", cache_key="kline|BTC|1h|1")
    second = service.ask("问题", cache_key="kline|BTC|1h|1")
    assert first["ok"] and second["ok"]
    assert second["cached"] is True
    assert len(calls) == 1
    # A cached answer is free: it must not consume the daily budget.
    assert llm.cache().calls_today() == 1


def test_cache_entry_expires(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BITRADER_DATA_DIR", str(tmp_path))
    llm.reset_cache()
    store = llm.cache()
    store.put("key", "旧答案", 1)
    assert store.get("key", 600) is not None
    assert store.get("key", 0) is None


def test_audit_tail_records_every_call(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BITRADER_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(llm, "load_key", lambda: "sk-test")
    llm.reset_cache()
    service = LLMService(AISettings(enabled=True))
    monkeypatch.setattr(service, "_request", lambda *args, **kwargs: ("好的", 5))
    service.ask("问题", kind="test")
    tail = llm.audit_tail(10)
    assert len(tail) == 1
    assert tail[0]["kind"] == "test"
    assert tail[0]["ok"] is True


def test_key_masking_never_reveals_the_secret() -> None:
    masked = llm.mask_key("sk-1234567890abcdef")
    assert "1234567890" not in masked
    assert masked.endswith("cdef")
    assert llm.mask_key("") == ""


def test_provider_defaults_are_filled_in() -> None:
    settings = AISettings(provider="anthropic").resolved()
    assert settings.model
    assert settings.base_url.startswith("https://")
    # An unknown provider must fall back rather than crash a page load.
    assert AISettings(provider="nope").resolved().provider == llm.DEFAULT_PROVIDER


# ------------------------------------------------------------- frame fallback
def test_market_frame_falls_back_to_the_mirror(candles, monkeypatch) -> None:
    """AI analysis must survive the network where the chart already does.

    Binance's futures domains are unreachable on some connections; ``/api/klines``
    quietly switches to a perpetual mirror. If the analysis endpoints did not do
    the same, the chart would work and "分析" would 500 — which is exactly what
    happened before this test existed.
    """
    from contextlib import nullcontext

    from btrader.web import routes

    class Broken:
        def fetch(self, *args, **kwargs):
            raise RuntimeError("币安不可达")

    class Ctx:
        klines = Broken()

    class State:
        locks = {"usdm": nullcontext()}

    rows = [
        [int(row.timestamp), row.open, row.high, row.low, row.close, row.volume]
        for row in candles.itertuples(index=False)
    ]
    monkeypatch.setattr(routes.mirror, "supports", lambda market, timeframe: True)
    monkeypatch.setattr(routes.mirror, "fetch_ohlcv", lambda *a, **k: rows)

    frame = routes._market_frame(State(), Ctx(), "usdm", "BTC/USDT", "1h", 100)
    assert len(frame) == len(rows)
    assert list(frame.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
    assert frame["timestamp"].dtype == "int64"


def test_market_frame_reports_both_failures(monkeypatch) -> None:
    from contextlib import nullcontext

    from fastapi import HTTPException

    from btrader.web import routes

    class Broken:
        def fetch(self, *args, **kwargs):
            raise RuntimeError("币安不可达")

    class Ctx:
        klines = Broken()

    class State:
        locks = {"usdm": nullcontext()}

    def boom(*args, **kwargs):
        raise RuntimeError("备用源也挂了")

    monkeypatch.setattr(routes.mirror, "supports", lambda market, timeframe: True)
    monkeypatch.setattr(routes.mirror, "fetch_ohlcv", boom)
    with pytest.raises(HTTPException) as caught:
        routes._market_frame(State(), Ctx(), "usdm", "BTC/USDT", "1h", 100)
    # Both causes have to be in the message: "备用源也挂了" alone does not say
    # why the primary was unavailable.
    assert "币安不可达" in str(caught.value.detail)
    assert "备用源" in str(caught.value.detail)


# --------------------------------------------------------- transport (the wire)
#
# A relay in front of an OpenAI-compatible endpoint rejected urllib's default
# ``Python-urllib/3.x`` User-Agent with HTTP 403 and a body reading
# "error code: 1010" — before the request ever reached the model. The old code
# mapped that to "这个 Key 没有访问该模型的权限。", which sent the user to the
# wrong fix. These tests pin the wire format (a real client name, JSON headers,
# the documented URLs and payloads) and the two 403 stories apart, so the bug
# cannot come back silently and the message cannot regress into blame.

_COMPLETIONS = {
    "openai": {
        "choices": [{"message": {"content": "好的"}}],
        "usage": {"total_tokens": 12},
    },
    "anthropic": {
        "content": [{"type": "text", "text": "好的"}],
        "usage": {"input_tokens": 10, "output_tokens": 2},
    },
    "ollama": {
        "message": {"content": "好的"},
        "prompt_eval_count": 10,
        "eval_count": 2,
    },
}


class FakeResponse:
    """Minimal ``urlopen`` result: a context manager whose ``read`` is all we use."""

    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc_info) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


def _header(request, name: str) -> str:
    """Case-insensitive lookup: ``urllib`` stores ``User-agent``, not ``User-Agent``."""
    for key, value in request.headers.items():
        if key.lower() == name.lower():
            return value
    return ""


def _service(tmp_path, monkeypatch, provider: str) -> LLMService:
    """An enabled service whose key, cache and audit trail are all throwaway."""
    monkeypatch.setenv("BITRADER_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(llm, "load_key", lambda: "sk-test")
    llm.reset_cache()
    return LLMService(AISettings(enabled=True, provider=provider))


def _capture_request(monkeypatch, payload: dict) -> list:
    """Replace ``urlopen`` with a recorder; returns the captured Requests."""
    seen: list[urllib.request.Request] = []

    def fake_urlopen(request, timeout=None):
        seen.append(request)
        return FakeResponse(payload)

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    return seen


def test_openai_request_has_the_documented_url_headers_and_payload(
    tmp_path, monkeypatch
) -> None:
    service = _service(tmp_path, monkeypatch, "openai")
    seen = _capture_request(monkeypatch, _COMPLETIONS["openai"])

    result = service.ask("问题", system="你是分析师")

    assert result["ok"] is True
    assert result["text"] == "好的"
    assert result["tokens"] == 12
    request = seen[0]
    assert request.full_url == "https://api.openai.com/v1/chat/completions"
    assert request.get_method() == "POST"
    # The whole point of the regression: a client name, not urllib's default.
    assert _header(request, "User-Agent") == llm.USER_AGENT
    assert _header(request, "Accept") == "application/json"
    assert _header(request, "Content-Type") == "application/json"
    assert _header(request, "Authorization") == "Bearer sk-test"
    payload = json.loads(request.data.decode("utf-8"))
    assert payload["model"] == "gpt-4o-mini"
    assert payload["stream"] is False
    assert payload["temperature"] == pytest.approx(0.2)
    assert payload["max_tokens"] == 3000
    assert payload["messages"][0] == {"role": "system", "content": "你是分析师"}
    assert payload["messages"][-1] == {"role": "user", "content": "问题"}


def test_ask_can_override_output_tokens_per_call(tmp_path, monkeypatch) -> None:
    """写策略代码要比默认上限更长。

    覆盖值必须真的进到请求体：被无声忽略的话，返回的 JSON 从字符串中间断掉，
    候选策略被否掉，而用户看不到原因。
    """
    service = _service(tmp_path, monkeypatch, "openai")
    seen = _capture_request(monkeypatch, _COMPLETIONS["openai"])

    result = service.ask("写一个策略", max_tokens=2400)

    assert result["ok"] is True
    payload = json.loads(seen[0].data.decode("utf-8"))
    assert payload["max_tokens"] == 2400


def test_anthropic_request_uses_its_own_headers_and_a_system_field(
    tmp_path, monkeypatch
) -> None:
    service = _service(tmp_path, monkeypatch, "anthropic")
    seen = _capture_request(monkeypatch, _COMPLETIONS["anthropic"])

    result = service.ask("问题", system="你是分析师")

    assert result["ok"] is True
    assert result["text"] == "好的"
    assert result["tokens"] == 12
    request = seen[0]
    assert request.full_url == "https://api.anthropic.com/v1/messages"
    assert _header(request, "x-api-key") == "sk-test"
    assert _header(request, "anthropic-version") == "2023-06-01"
    assert _header(request, "User-Agent") == llm.USER_AGENT
    assert _header(request, "Accept") == "application/json"
    assert "authorization" not in {key.lower() for key in request.headers}
    payload = json.loads(request.data.decode("utf-8"))
    # Anthropic takes the system prompt as a top-level field, not a message.
    assert payload["system"] == "你是分析师"
    assert payload["messages"] == [{"role": "user", "content": "问题"}]
    assert payload["max_tokens"] == 3000


def test_ollama_request_is_local_and_sends_no_credentials(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path, monkeypatch, "ollama")
    seen = _capture_request(monkeypatch, _COMPLETIONS["ollama"])

    result = service.ask("问题", system="你是分析师")

    assert result["ok"] is True
    assert result["text"] == "好的"
    request = seen[0]
    assert request.full_url == "http://127.0.0.1:11434/api/chat"
    assert _header(request, "User-Agent") == llm.USER_AGENT
    assert _header(request, "Accept") == "application/json"
    lowered = {key.lower() for key in request.headers}
    # A local model needs no key, and none may be leaked to it.
    assert "authorization" not in lowered
    assert "x-api-key" not in lowered
    payload = json.loads(request.data.decode("utf-8"))
    assert payload["stream"] is False
    assert payload["options"]["temperature"] == pytest.approx(0.2)
    assert payload["options"]["num_predict"] == 3000
    assert payload["messages"][0] == {"role": "system", "content": "你是分析师"}


@pytest.mark.parametrize("provider", ["openai", "anthropic", "ollama"])
def test_no_style_ever_sends_the_urllib_default_user_agent(
    tmp_path, monkeypatch, provider
) -> None:
    """The exact string Cloudflare-style relays reject with "error code: 1010"."""
    service = _service(tmp_path, monkeypatch, provider)
    seen = _capture_request(monkeypatch, _COMPLETIONS[provider])

    assert service.ask("问题")["ok"] is True
    user_agent = _header(seen[0], "User-Agent")
    assert user_agent == llm.USER_AGENT
    assert user_agent
    assert "python-urllib" not in user_agent.lower()


def test_a_waf_403_is_not_reported_as_a_key_permission_problem() -> None:
    text = llm._http_error_text(403, "error code: 1010", "openai")
    assert "防火墙" in text
    assert "Cloudflare" in text
    assert "1010" in text
    # Blaming the key is exactly the misleading message this test exists for.
    assert "没有访问该模型" not in text


def test_a_model_permission_403_keeps_the_permission_wording() -> None:
    text = llm._http_error_text(403, '{"error":{"message":"model not allowed"}}', "openai")
    assert "没有访问该模型" in text
    # A real permission error must not be excused as a firewall block.
    assert "防火墙" not in text


def test_ask_turns_a_waf_403_into_ok_false_with_the_detail(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path, monkeypatch, "openai")

    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url,
            403,
            "Forbidden",
            {},
            io.BytesIO(b"error code: 1010"),
        )

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    result = service.ask("问题")

    assert result["ok"] is False
    assert result["text"] == ""
    assert "防火墙" in result["error"]
    assert "error code: 1010" in result["error"]
    assert "没有访问该模型" not in result["error"]
    # A rejected request is not a billable call, and it must be audited.
    assert llm.cache().calls_today() == 0
    tail = llm.audit_tail(5)
    assert tail and tail[0]["ok"] is False


def test_ask_keeps_the_permission_wording_for_a_json_403(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path, monkeypatch, "openai")

    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url,
            403,
            "Forbidden",
            {},
            io.BytesIO(b'{"error":{"message":"model not allowed"}}'),
        )

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    result = service.ask("问题")

    assert result["ok"] is False
    assert "没有访问该模型" in result["error"]
    assert "防火墙" not in result["error"]

# ------------------------------------------------- reasoning models come back empty
def test_a_truncated_reasoning_model_says_the_budget_ran_out(tmp_path, monkeypatch) -> None:
    """思考过程也占输出上限。

    推理模型把预算花在思考上，正文是空的。只说「模型返回了空内容」用户无从下手，
    所以截断时要说明上限被用完了，并指出该往上调。
    """
    service = _service(tmp_path, monkeypatch, "openai")
    _capture_request(
        monkeypatch,
        {
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {"role": "assistant", "content": "", "reasoning_content": "用户在问…"},
                }
            ],
            "usage": {"total_tokens": 1200},
        },
    )

    result = service.ask("分析一下 BTCUSDT")

    assert result["ok"] is False
    assert "截断" in result["error"]
    assert "输出上限" in result["error"]


def test_reasoning_without_an_answer_asks_for_a_bigger_budget(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path, monkeypatch, "openai")
    _capture_request(
        monkeypatch,
        {
            "choices": [
                {"finish_reason": "stop", "message": {"content": "", "reasoning_content": "想了想"}}
            ]
        },
    )

    result = service.ask("分析一下 BTCUSDT")

    assert result["ok"] is False
    assert "思考过程" in result["error"]


def test_an_empty_answer_without_a_reason_keeps_the_plain_message(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path, monkeypatch, "openai")
    _capture_request(monkeypatch, {"choices": [{"finish_reason": "stop", "message": {"content": ""}}]})

    result = service.ask("分析一下 BTCUSDT")

    assert result["ok"] is False
    assert result["error"] == "模型返回了空内容。"
