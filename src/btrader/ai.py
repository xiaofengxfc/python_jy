"""What the assistant is actually asked.

``btrader.llm`` moves bytes; this module decides what to show the model and how
to read the answer back. Everything here is a pure function of data passed in,
so it can be tested without a network, a context object or a running app.

The design principle is that **the numbers are computed here, never by the
model**. A candle chart is summarised with real indicator values before it is
sent, so the model's job is interpretation — if it invents a support level, that
level is visibly absent from the input instead of being silently wrong.

Two further rules:

* Analysis is text for a human, so the prompts ask for short sections rather
  than JSON. Only the places that need a decision (backtest filtering, strategy
  picking) ask for a parseable token, and those parse defensively.
* Nothing here may raise into a caller: every public function returns a dict
  that carries ``ok`` and an ``error`` the page can show.
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
from datetime import datetime
from typing import Any, Callable, Iterable, Sequence

import numpy as np
import pandas as pd

from .core import indicators
from .core.types import timeframe_seconds
from .llm import LLMService

log = logging.getLogger(__name__)

MAX_DIGEST_BARS = 16
SYSTEM = (
    "你是一名严谨的加密货币交易分析师，服务于一个真实在跑的交易系统。"
    "只根据给定的数据回答，不要编造数据里没有的价位或指标。"
    "如果数据不足以判断，直接说“数据不足”。"
    "结论要具体到可执行：给方向、给关键价位、给止损参考、给置信度。"
    "不要给投资建议免责声明，用户知道自己在做什么。"
    "用简体中文，控制在 300 字以内。"
)

# --------------------------------------------------------------------- digest
def digest(
    df: pd.DataFrame,
    *,
    timeframe: str = "1h",
    label: str = "",
    bars: int = MAX_DIGEST_BARS,
) -> str:
    """One candle frame as a compact, model-readable block of facts.

    Deliberately a list of measured values rather than a chart image: reading a
    price off a rendered PNG is exactly the mistake this format prevents.
    """
    if df is None or len(df) < 2:
        return f"【{label or timeframe}】数据不足"
    frame = df.reset_index(drop=True)
    close = frame["close"].astype(float)
    last = frame.iloc[-1]
    ts_ms = int(last["timestamp"])
    stamp = datetime.fromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d %H:%M")
    step = timeframe_seconds(timeframe)
    chg = (float(close.iloc[-1]) / float(close.iloc[-2]) - 1.0) if len(close) > 1 else 0.0

    parts: list[str] = []
    head = f"【{label or timeframe}】最近 {len(frame)} 根已收盘，最新 {stamp}"
    parts.append(head)
    parts.append(
        f"收盘价 {float(last['close']):,.4f}（本根 {chg:+.2%}），"
        f"高 {float(last['high']):,.4f} 低 {float(last['low']):,.4f}"
    )

    # Moving averages only where enough bars exist, so a 20-bar window never
    # reports a 200-period average that is mostly NaN.
    mas = []
    for period in (20, 50, 200):
        if len(close) >= period:
            value = indicators.sma(close, period).iloc[-1]
            if pd.notna(value):
                mas.append(f"MA{period} {float(value):,.2f}")
    if mas:
        parts.append("均线: " + " | ".join(mas))

    extra = []
    if len(close) >= 15:
        rsi_value = indicators.rsi(close).iloc[-1]
        if pd.notna(rsi_value):
            extra.append(f"RSI14 {float(rsi_value):.1f}")
    if len(close) >= 35:
        line, signal, hist = indicators.macd(close)
        if pd.notna(hist.iloc[-1]):
            extra.append(
                f"MACD DIF {float(line.iloc[-1]):.2f} DEA {float(signal.iloc[-1]):.2f} "
                f"柱 {float(hist.iloc[-1]):.2f}"
            )
    if len(close) >= 15:
        atr_value = indicators.atr(frame).iloc[-1]
        if pd.notna(atr_value):
            pct = float(atr_value) / float(last["close"]) if float(last["close"]) else 0.0
            extra.append(f"ATR14 {float(atr_value):,.2f}（{pct:.2%}）")
    if extra:
        parts.append("指标: " + " | ".join(extra))

    if len(close) >= 20:
        low, mid, up = indicators.bollinger(close)
        if pd.notna(up.iloc[-1]):
            width = (float(up.iloc[-1]) - float(low.iloc[-1])) / float(mid.iloc[-1])
            parts.append(
                f"布林20/2: 上 {float(up.iloc[-1]):,.2f} 中 {float(mid.iloc[-1]):,.2f} "
                f"下 {float(low.iloc[-1]):,.2f}（带宽 {width:.2%}）"
            )

    if "volume" in frame:
        volume = frame["volume"].astype(float)
        recent = float(volume.iloc[-1])
        base = float(volume.tail(20).mean()) if len(volume) >= 20 else float(volume.mean())
        ratio = recent / base - 1.0 if base > 0 else 0.0
        parts.append(f"成交量: 本根 {recent:,.2f}，较近 20 根均量 {ratio:+.0%}")

    swings = []
    for window in (20, 60):
        if len(frame) >= window:
            high = float(frame["high"].tail(window).max())
            low = float(frame["low"].tail(window).min())
            swings.append(f"{window} 根内 高点 {high:,.2f} / 低点 {low:,.2f}")
    if swings:
        parts.append("结构: " + " | ".join(swings))

    tail = frame.tail(min(bars, len(frame)))
    rows = []
    for _, row in tail.iterrows():
        when = datetime.fromtimestamp(int(row["timestamp"]) / 1000).strftime("%m-%d %H:%M")
        rows.append(
            f"{when} 开{float(row['open']):,.2f} 高{float(row['high']):,.2f} "
            f"低{float(row['low']):,.2f} 收{float(row['close']):,.2f} "
            f"量{float(row.get('volume') or 0):,.1f}"
        )
    parts.append(f"最近 {len(rows)} 根（每根 {timeframe}）：")
    parts.extend(rows)
    return "\n".join(parts)


def candle_key(symbol: str, timeframe: str, df: pd.DataFrame) -> str:
    """Identity of "this candle has been analysed": symbol, period, last close."""
    if df is None or df.empty:
        return f"kline|{symbol}|{timeframe}|empty"
    return f"kline|{symbol}|{timeframe}|{int(df['timestamp'].iloc[-1])}"


def _ok(text: str, **extra: Any) -> dict[str, Any]:
    return {"ok": True, "text": text, **extra}


def _fail(error: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "text": "", "error": error, **extra}


# ------------------------------------------------------------------ analysis
def analyze_klines(
    llm: LLMService,
    *,
    symbol: str,
    frames: dict[str, pd.DataFrame],
    market_label: str = "现货",
) -> dict[str, Any]:
    """Multi-timeframe read: trend, levels, stop reference, confidence."""
    blocks = []
    for timeframe, frame in frames.items():
        if frame is None or frame.empty:
            continue
        blocks.append(digest(frame, timeframe=timeframe, label=f"{symbol} {timeframe}"))
    if not blocks:
        return _fail("没有可用的 K 线数据。")

    primary = next(iter(frames))
    key_frame = frames.get(primary)
    cache_key = candle_key(f"{symbol}|multi", primary, key_frame) if key_frame is not None else ""

    prompt = (
        f"交易对 {symbol}（{market_label}），以下是多个周期已经走完的 K 线数据。\n\n"
        + "\n\n".join(blocks)
        + "\n\n请按这四段回答，每段一两句话：\n"
        "1. 趋势：各周期方向是否一致，当前处于趋势还是震荡。\n"
        "2. 关键价位：给出支撑、阻力，必须是上面数据里出现过的价位。\n"
        "3. 操作参考：偏多/偏空/观望，止损参考价位。\n"
        "4. 置信度：0-100，并说明最大的不确定性。\n\n"
        "回答完上面四段后，另起一行输出一个 JSON 对象，把第 2、3 段里提到的价位结构化，"
        "不要加任何解释或代码块标记：\n"
        '{"levels": [{"price": 数值, "kind": "support|resistance|stop", "label": "不超过 8 字"}]}\n'
        "levels 最多 6 条，price 必须落在上面数据出现过的高低价区间内。"
    )
    result = llm.ask(prompt, system=SYSTEM, cache_key=cache_key, kind="kline")
    if not result["ok"]:
        return result
    result["timeframes"] = list(frames)
    # The chart draws these. Parsed defensively and range-checked, because a
    # hallucinated level is worse than no line at all: it looks authoritative.
    primary_frame = frames.get(primary)
    raw_levels, prose = _split_levels(result["text"])
    result["text"] = prose or result["text"]
    result["levels"] = validate_levels(raw_levels, primary_frame)
    return result


_LEVEL_KINDS = ("support", "resistance", "stop")


def _split_levels(text: str) -> tuple[list[dict[str, Any]], str]:
    """Separate the trailing JSON block from the prose the user reads.

    The prose is what goes in the panel; the JSON is what goes on the chart.
    Showing the raw JSON next to the analysis is noise, so it is stripped.
    """
    found = _find_json_object(text or "")
    if not found:
        return [], text or ""
    payload, start, end = found
    levels = payload.get("levels")
    cleaned: list[dict[str, Any]] = []
    if isinstance(levels, list):
        for item in levels[:12]:
            if not isinstance(item, dict):
                continue
            try:
                price = float(item.get("price"))
            except (TypeError, ValueError):
                continue
            kind = str(item.get("kind") or "").strip().lower()
            if kind not in _LEVEL_KINDS:
                continue
            cleaned.append(
                {"price": price, "kind": kind, "label": str(item.get("label") or "")[:12]}
            )
    prose = (text[:start] + " " + text[end:]).strip()
    return cleaned, prose


def validate_levels(
    levels: Sequence[dict[str, Any]], frame: pd.DataFrame | None
) -> list[dict[str, Any]]:
    """Keep only levels inside the traded range, deduped and capped per kind.

    A model that invents a support 30% below the last bar produces a line the
    trader will trust and that means nothing. Anything outside the high/low of
    the data the model was shown is dropped here, not drawn.
    """
    if frame is None or frame.empty or not levels:
        return []
    high = float(frame["high"].max())
    low = float(frame["low"].min())
    last = float(frame["close"].iloc[-1])
    span = max(high - low, 1e-9)
    out: list[dict[str, Any]] = []
    seen: dict[str, list[float]] = {kind: [] for kind in _LEVEL_KINDS}
    for item in levels:
        price = float(item["price"])
        if not (low <= price <= high):
            continue
        kind = item.get("kind")
        if kind not in _LEVEL_KINDS:
            continue
        # Two supports 0.1% apart are the same line with two labels.
        if any(abs(price - other) < span * 0.002 for other in seen[kind]):
            continue
        if len(seen[kind]) >= 3:
            continue
        seen[kind].append(price)
        out.append(
            {
                "price": price,
                "kind": kind,
                "label": item.get("label") or "",
                # Where it sits relative to the last close, so the legend can say
                # "上方 1.2%" instead of making the trader do the subtraction.
                "distance": (price - last) / last if last else 0.0,
            }
        )
    return out


def analyze_news(
    llm: LLMService,
    *,
    symbol: str,
    items: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Score each headline, then aggregate into one temperature."""
    headlines = [item for item in items if item.get("title")][:20]
    if not headlines:
        return _fail("没有可分析的新闻。")
    lines = [
        f"{i}. [{item.get('source') or '?'}] {str(item.get('title'))[:160]}"
        for i, item in enumerate(headlines, 1)
    ]
    prompt = (
        f"以下是 {symbol} 相关的近期新闻标题。\n\n"
        + "\n".join(lines)
        + "\n\n请做两件事：\n"
        "A. 每条标题给一个情绪分：-1（强烈利空）到 +1（强烈利多），0 表示中性或无信息，"
        "格式严格为一行 `序号: 分数 一句话理由`。\n"
        "B. 最后单独一行输出 `总体: 分数`，分数是 -1 到 +1 的综合情绪，"
        "再用一句话说明主要驱动因素。\n"
        "标题信息不足时就给 0，不要脑补新闻内容。"
    )
    result = llm.ask(
        prompt,
        system=SYSTEM,
        cache_key=f"news|{symbol}|{_news_stamp(headlines)}",
        kind="news",
    )
    if not result["ok"]:
        return result
    scores, overall = _parse_sentiment(result["text"])
    result["scores"] = scores
    result["overall"] = overall
    result["headlines"] = [item.get("title") for item in headlines]
    return result


def _news_stamp(items: Sequence[dict[str, Any]]) -> str:
    joined = "|".join(str(item.get("id") or "") for item in items)
    return str(abs(hash(joined)) % (10**12))


_SENTENCE_RE = re.compile(r"^\s*(\d+)\s*[:：]\s*(-?\d*\.?\d+)")
_OVERALL_RE = re.compile(r"总体\s*[:：]\s*(-?\d*\.?\d+)")


def _parse_sentiment(text: str) -> tuple[dict[int, float], float]:
    scores: dict[int, float] = {}
    for line in text.splitlines():
        match = _SENTENCE_RE.match(line)
        if not match:
            continue
        try:
            scores[int(match.group(1))] = max(-1.0, min(1.0, float(match.group(2))))
        except ValueError:
            continue
    overall = 0.0
    match = _OVERALL_RE.search(text)
    if match:
        try:
            overall = max(-1.0, min(1.0, float(match.group(1))))
        except ValueError:
            overall = 0.0
    elif scores:
        overall = sum(scores.values()) / len(scores)
    return scores, overall


def review_position(
    llm: LLMService,
    *,
    symbol: str,
    position: dict[str, Any],
    risk: dict[str, Any],
    frame: pd.DataFrame | None,
    timeframe: str = "1h",
) -> dict[str, Any]:
    """Ask whether the open position is consistent with the risk settings."""
    facts = [f"交易对 {symbol}"]
    if position:
        facts.append(
            f"持仓：{position.get('side') or '多'} "
            f"{_num(position.get('contracts') or position.get('amount')):.6f}，"
            f"开仓价 {_num(position.get('entry_price')):,.4f}，"
            f"当前价 {_num(position.get('mark_price')):,.4f}，"
            f"浮动盈亏 {_num(position.get('unrealized_pnl')):,.2f}"
        )
    else:
        facts.append("当前没有持仓。")
    if risk:
        facts.append(
            f"风控：日内盈亏 {_num(risk.get('day_pnl')):,.2f}，"
            f"日内成交 {risk.get('day_trades') or 0} 笔，"
            f"熔断={'是' if risk.get('halted') else '否'}，"
            f"急停={'是' if risk.get('kill_switch') else '否'}"
        )
    body = "\n".join(facts)
    if frame is not None and not frame.empty:
        body += "\n\n" + digest(frame, timeframe=timeframe, label=f"{symbol} {timeframe}", bars=8)
    prompt = (
        body
        + "\n\n请回答两点：\n"
        "1. 这个仓位和当前风控状态有没有冲突或明显该止损的理由。\n"
        "2. 如果是你，会继续持有、减仓还是平掉？给一句理由。\n"
        "不要复述上面的数字，直接给判断。"
    )
    return llm.ask(
        prompt,
        system=SYSTEM,
        cache_key=f"review|{symbol}|{_num(position.get('entry_price')) if position else 0}"
        f"|{_num(position.get('mark_price')) if position else 0}",
        kind="review",
    )


def prejudge_order(
    llm: LLMService,
    *,
    symbol: str,
    timeframe: str,
    side: str,
    quote: float,
    frame: pd.DataFrame,
    position_note: str = "",
) -> dict[str, Any]:
    """A second opinion on one order, before it is placed.

    Cached per candle *and* per direction/notional bucket: a trader who changes
    the size by 10 USDT should not pay for a new opinion on the same bar, but a
    10x size change is a different question and gets a fresh one.
    """
    if frame is None or frame.empty:
        return _fail("没有 K 线，无法预判。")
    block = digest(frame, timeframe=timeframe, label=f"{symbol} {timeframe}", bars=8)
    bucket = _quote_bucket(quote)
    prompt = (
        f"{block}\n\n"
        f"准备{ '买入' if side == 'buy' else '卖出' } {symbol}，名义金额约 {quote:,.0f} USDT。"
        + (f"\n{position_note}" if position_note else "")
        + "\n\n这笔委托现在下合适吗？用一句话给结论（可以/建议等待/建议缩小），再一句话说明。"
        "不要重复上面的数字。"
    )
    result = llm.ask(
        prompt,
        system=SYSTEM,
        cache_key=f"prejudge|{symbol}|{timeframe}|{bucket}|{side}"
        f"|{int(frame['timestamp'].iloc[-1])}",
        kind="prejudge",
    )
    if result["ok"]:
        result["bucket"] = bucket
    return result


def _quote_bucket(quote: float) -> str:
    """Coarse size bucket so a nudge of the slider does not re-bill the user."""
    value = abs(float(quote or 0.0))
    if value <= 0:
        return "0"
    for edge in (10, 50, 100, 500, 1_000, 5_000, 10_000, 50_000):
        if value <= edge:
            return str(edge)
    return "big"


def explain_anomaly(
    llm: LLMService,
    *,
    kind: str,
    detail: str,
    context: str = "",
) -> dict[str, Any]:
    """A short 'what probably happened' next to a breaker or a rejection."""
    prompt = (
        f"交易系统出现异常：{kind}\n详细信息：{detail}\n"
        + (f"上下文：{context}\n" if context else "")
        + "\n请用两三句话说明最可能的原因，以及用户应该先检查什么。"
        "不要重复报错原文。"
    )
    return llm.ask(prompt, system=SYSTEM, cache_key="", kind="anomaly")


def describe_strategy(
    llm: LLMService,
    *,
    key: str,
    name: str,
    description: str,
    source: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Translate a strategy's code into 'when it earns, when it bleeds'."""
    prompt = (
        f"策略名：{name}（key={key}）\n说明：{description}\n"
        f"当前参数：{json.dumps(params, ensure_ascii=False)}\n\n"
        f"源码：\n```python\n{source[:6000]}\n```\n\n"
        "请回答：\n"
        "1. 它在什么行情下赚钱，什么行情下会连续亏损。\n"
        "2. 这几个参数各自控制什么，调大调小分别有什么后果。\n"
        "3. 最该警惕的一个失效场景。"
    )
    return llm.ask(prompt, system=SYSTEM, cache_key=f"desc|{key}|{hash(str(params))}", kind="describe")


def suggest_strategy(
    llm: LLMService,
    *,
    idea: str,
    strategies: Sequence[dict[str, Any]],
    frames: dict[str, pd.DataFrame],
) -> dict[str, Any]:
    """Pick one *existing* strategy and fill its params — never write code.

    Letting a model author a strategy file would hand it the same privileges as
    the backend (``StrategyRegistry`` imports user files into this process), so
    the only generated artifact here is a parameter dict for a strategy the user
    already trusts.
    """
    catalogue = []
    for item in strategies:
        specs = []
        for spec in item.get("params_spec") or []:
            # ``params_spec`` carries ParamSpec objects; ``meta_list`` is also
            # serialised straight to JSON, so tolerate plain dicts too.
            name = _spec_get(spec, "name", "")
            default = _spec_get(spec, "default", "")
            kind = _spec_get(spec, "kind", "")
            minimum = _spec_get(spec, "minimum", None)
            maximum = _spec_get(spec, "maximum", None)
            if kind in ("int", "float") and minimum is not None and maximum is not None:
                specs.append(f"{name}={default}（{minimum}~{maximum}）")
            else:
                specs.append(f"{name}={default}")
        catalogue.append(
            f"- {item.get('key')}「{item.get('name')}」：{item.get('description') or '无描述'}；"
            f"参数：{', '.join(specs) or '无'}"
        )
    if not catalogue:
        return _fail("没有可用策略。")

    body = "可用策略：\n" + "\n".join(catalogue)
    if frames:
        first = next(iter(frames.items()))
        body += "\n\n当前行情：\n" + digest(first[1], timeframe=first[0], label=first[0], bars=8)
    prompt = (
        f"用户的想法：{idea}\n\n{body}\n\n"
        "从上面这些策略里挑一个最接近用户想法的，并给出参数。"
        "只输出一个 JSON 对象，不要任何其他文字，格式：\n"
        '{"strategy": "策略key", "params": {"参数名": 数值}, "reason": "一句话理由"}'
    )
    result = llm.ask(prompt, system=SYSTEM, cache_key="", kind="suggest")
    if not result["ok"]:
        return result
    parsed = _parse_json_object(result["text"])
    if parsed is None:
        return _fail("模型没有按要求返回 JSON，请重试。", text=result["text"])
    key = str(parsed.get("strategy") or "").strip()
    known = {item.get("key") for item in strategies}
    if key not in known:
        return _fail(f"模型选了一个不存在的策略：{key}", text=result["text"])
    params = parsed.get("params") or {}
    if not isinstance(params, dict):
        params = {}
    result.update(
        strategy=key,
        params=params,
        reason=str(parsed.get("reason") or ""),
    )
    return result


def _parse_json_object(text: str) -> dict[str, Any] | None:
    """Pull the first JSON object out of a reply that may carry prose or fences."""
    found = _find_json_object(text)
    return found[0] if found else None


def _find_json_object(text: str) -> tuple[dict[str, Any], int, int] | None:
    """``(object, start, end)`` for the first balanced object that parses.

    The span matters as much as the object: the prose shown to the user is
    everything *outside* it, and stripping from the last brace instead would
    leave a half-object on screen.
    """
    if not text:
        return None
    stripped = text.strip()
    try:
        value = json.loads(stripped)
        if isinstance(value, dict):
            start = text.index(stripped)
            return value, start, start + len(stripped)
    except ValueError:
        pass
    start = text.find("{")
    while start >= 0:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            # Braces inside a string literal are data, not structure: a label
            # like "突破 {阻力}" would otherwise end the object early.
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        value = json.loads(text[start : index + 1])
                        if isinstance(value, dict):
                            return value, start, index + 1
                    except ValueError:
                        pass
                    break
        start = text.find("{", start + 1)
    return None


# --------------------------------------------------------------- backtest filter
FILTER_SYSTEM = (
    "你是一名交易复盘评审。你会看到一个「开仓时刻」之前的行情数据，"
    "以及这个策略当时准备做的方向。"
    "只判断这笔交易在当时的客观条件下是否值得做，不判断它后来赚没赚钱。"
    "回答必须只有一个词 YES 或 NO，然后换行写一句不超过 40 字的理由。"
)

_VERDICT_RE = re.compile(r"\b(YES|NO)\b", re.IGNORECASE)


def judge_entry(
    llm: LLMService,
    *,
    symbol: str,
    timeframe: str,
    side: str,
    entry_time_ms: int,
    frame: pd.DataFrame,
) -> dict[str, Any]:
    """Would the model have taken this entry, knowing only the past?

    ``frame`` must already be truncated to the bars *before* the entry, so the
    model cannot see the outcome it is being asked about.
    """
    block = digest(frame, timeframe=timeframe, label=f"{symbol} {timeframe}", bars=12)
    prompt = (
        f"{block}\n\n"
        f"策略在下一次开盘准备{'做多' if side == 'long' else '做空'} {symbol}。\n"
        "这笔开仓值得做吗？只回答 YES 或 NO，然后换行一句理由。"
    )
    result = llm.ask(
        prompt,
        system=FILTER_SYSTEM,
        cache_key=f"judge|{symbol}|{timeframe}|{entry_time_ms}|{side}",
        kind="judge",
    )
    if not result["ok"]:
        return result
    verdict, reason = _parse_verdict(result["text"])
    result.update(verdict=verdict, reason=reason)
    return result


def _parse_verdict(text: str) -> tuple[str, str]:
    match = _VERDICT_RE.search(text or "")
    if not match:
        # An unreadable answer must not silently drop a trade: default to YES so
        # the filtered run stays comparable to the original.
        return "yes", (text or "").strip()[:120]
    verdict = match.group(1).lower()
    rest = (text or "").replace(match.group(0), "", 1).strip().splitlines()
    reason = rest[0].strip()[:120] if rest and rest[0].strip() else ""
    return verdict, reason


def entry_window(frame: pd.DataFrame, entry_time_ms: int, *, lookback: int = 80) -> pd.DataFrame:
    """Bars strictly before ``entry_time_ms``, newest last.

    Strictly before is the whole point: feeding the entry bar itself would leak
    the outcome the model is being asked to predict.
    """
    if frame is None or frame.empty:
        return pd.DataFrame()
    subset = frame[frame["timestamp"].astype("int64") < int(entry_time_ms)]
    return subset.tail(lookback).reset_index(drop=True)


def filter_trades(
    llm: LLMService,
    *,
    symbol: str,
    timeframe: str,
    frame: pd.DataFrame,
    trades: Sequence[dict[str, Any]],
    progress: Callable[[int, int], None] | None = None,
    limit: int = 60,
) -> dict[str, Any]:
    """Replay a backtest's entries through the model, one call per entry.

    ``limit`` caps the number of calls: a 1000-bar backtest with hundreds of
    trades would otherwise cost real money for one click, and the first N trades
    are a fair sample of the strategy's behaviour.
    """
    entries = [trade for trade in trades if trade.get("entry_time")][: max(1, limit)]
    if not entries:
        return _fail("这次回测没有成交，没有可复核的开仓点。")
    if frame is None or frame.empty:
        return _fail("没有 K 线，无法还原开仓时的行情。")

    judged: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    failures = 0
    total = len(entries)
    for index, trade in enumerate(entries, 1):
        window = entry_window(frame, int(trade["entry_time"]))
        if len(window) < 10:
            # Too little history before this entry to judge fairly: keep it.
            kept.append(trade)
            judged.append({**trade, "verdict": "keep", "reason": "开仓前历史不足"})
        else:
            outcome = judge_entry(
                llm,
                symbol=symbol,
                timeframe=timeframe,
                side=str(trade.get("side") or "long"),
                entry_time_ms=int(trade["entry_time"]),
                frame=window,
            )
            if not outcome["ok"]:
                failures += 1
                kept.append(trade)
                judged.append({**trade, "verdict": "keep", "reason": outcome.get("error") or "调用失败"})
            else:
                verdict = outcome.get("verdict") or "yes"
                judged.append(
                    {**trade, "verdict": verdict, "reason": outcome.get("reason") or ""}
                )
                if verdict == "yes":
                    kept.append(trade)
        if progress:
            progress(index, total)

    return {
        "ok": True,
        "text": "",
        "judged": judged,
        "kept": kept,
        "total": total,
        "kept_count": len(kept),
        "failures": failures,
        "truncated": len(trades) > total,
    }


def trade_metrics(trades: Iterable[dict[str, Any]], initial_capital: float) -> dict[str, float]:
    """Metrics from a trade sequence alone.

    The bar-level curve only exists inside the engine, so the filtered run is
    measured on its trades: honest for comparing two trade sets, and labelled as
    such in the UI rather than passed off as a full equity curve.
    """
    rows = [trade for trade in trades if trade.get("pnl") is not None]
    pnls = [float(trade["pnl"]) for trade in rows]
    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value <= 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    cumulative = np.cumsum(pnls) if pnls else np.array([0.0])
    peak = np.maximum.accumulate(cumulative)
    drawdown = (cumulative - peak) / initial_capital if initial_capital else cumulative * 0.0
    return {
        "trades": float(len(rows)),
        "net_pnl": float(sum(pnls)),
        "total_return": float(sum(pnls) / initial_capital) if initial_capital else 0.0,
        "win_rate": (len(wins) / len(rows)) if rows else 0.0,
        "profit_factor": (gross_profit / gross_loss) if gross_loss > 0 else (
            999.0 if gross_profit > 0 else 0.0
        ),
        "avg_trade": float(np.mean(pnls)) if pnls else 0.0,
        "max_drawdown": float(drawdown.min()) if len(drawdown) else 0.0,
    }


def compare_runs(
    original: dict[str, float], filtered: dict[str, float]
) -> dict[str, Any]:
    """A plain-language verdict on whether filtering helped."""
    delta_return = filtered.get("total_return", 0.0) - original.get("total_return", 0.0)
    delta_dd = filtered.get("max_drawdown", 0.0) - original.get("max_drawdown", 0.0)
    dropped = original.get("trades", 0.0) - filtered.get("trades", 0.0)
    if filtered.get("trades", 0.0) <= 0:
        verdict = "AI 把全部交易都否掉了，这组参数没有可评估的结果。"
    elif delta_return > 0 and delta_dd >= 0:
        verdict = "过滤后收益更高、回撤更浅，AI 的判断在这组样本上有正贡献。"
    elif delta_return > 0:
        verdict = "过滤后收益更高，但回撤也更深：是修剪了烂单，不是降低了风险。"
    elif delta_dd > 0:
        verdict = "过滤后回撤更浅，但收益也降了：AI 更像在避险，而不是在选单。"
    else:
        verdict = "过滤后收益更低、回撤更深，这组样本上 AI 没有帮助。"
    return {
        "delta_return": delta_return,
        "delta_drawdown": delta_dd,
        "dropped": dropped,
        "verdict": verdict,
    }


# ------------------------------------------------------------------ briefings
class BriefQueue:
    """Fire-and-forget briefings for the auto-trader loop.

    The trader thread calls :meth:`submit` and moves on: waiting for a model
    would delay the next candle check, and a candle check that runs late is a
    missed stop, not a slow message.
    """

    def __init__(self, size: int = 4) -> None:
        self._queue: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._size = max(1, size)
        self.last_error = ""

    def submit(self, job: dict[str, Any]) -> bool:
        with self._lock:
            # A full queue drops the newest: a stale briefing is worthless, and
            # blocking the trading loop is unacceptable.
            if len(self._queue) >= self._size:
                return False
            self._queue.append(job)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._drain, name="ai-brief", daemon=True
                )
                self._thread.start()
            return True

    def _drain(self) -> None:
        while True:
            with self._lock:
                if not self._queue:
                    self._thread = None
                    return
                job = self._queue.pop(0)
            try:
                job["run"]()
            except Exception as exc:  # pragma: no cover - defensive
                self.last_error = str(exc)
                log.warning("AI 简报生成失败: %s", exc, exc_info=True)


def briefing(
    llm: LLMService,
    *,
    symbol: str,
    timeframe: str,
    frame: pd.DataFrame,
    target: float,
    target_delta: bool,
    equity: float | None = None,
    mode: str = "",
) -> dict[str, Any]:
    """The one-paragraph message that lands on the phone after a closed candle."""
    block = digest(frame, timeframe=timeframe, label=f"{symbol} {timeframe}", bars=8)
    lines = [
        block,
        "",
        f"策略当前目标仓位：{target:+.0%}"
        + ("（本轮刚刚调仓）" if target_delta else "（未变动）"),
    ]
    if equity:
        lines.append(f"账户权益约 {equity:,.2f} USDT")
    if mode:
        lines.append(f"运行模式：{mode}")
    lines.append("")
    lines.append(
        "请用三句话写一根收盘 K 线的简报：这一根发生了什么、接下来关注什么价位、"
        "当前仓位是否需要调整。不要写问候语和免责声明。"
    )
    prompt = "\n".join(lines)
    key_frame = frame
    return llm.ask(
        prompt,
        system=SYSTEM,
        cache_key=candle_key(symbol, timeframe, key_frame) if key_frame is not None else "",
        kind="brief",
    )


# --------------------------------------------------------------------- helpers
def _spec_get(spec: Any, name: str, default: Any = None) -> Any:
    """Read one field from a ParamSpec *or* a dict.

    ``StrategyRegistry.meta_list`` returns the dataclasses themselves, but the
    same payload goes over HTTP as JSON. Accepting both keeps the prompt builder
    usable from either side without a conversion layer.
    """
    if isinstance(spec, dict):
        return spec.get(name, default)
    return getattr(spec, name, default)


def _num(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if math.isnan(number) or math.isinf(number) else number
