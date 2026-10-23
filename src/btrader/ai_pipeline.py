"""AI 交易的决策流水线：技术面 → 新闻面 → 多空辩论 → 风控复核 → 裁决。

老做法是「策略给方向，模型一次复核」。快，但只有一句理由，事后看不出是哪一步
判断歪了。这一版把一次决策拆成几段，每段只干一件事，输出拼成一条时间线，
界面上能逐段展开。

三段深度：

    quick     只跑裁决，一次调用，和原来等价
    standard  技术面 → 多空辩论 → 裁决
    deep      五段全跑（有新闻时才有新闻那段）

流水线只产出文本。方向约束、风控闸门和下单都在 engine.ai_trader 里，
没有因为分段而放宽一条。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

DEPTHS = ("quick", "standard", "deep")
DEPTH_LABELS = {"quick": "快速", "standard": "标准", "deep": "深度"}

# 裁决段的输出格式。解析在 engine.ai_trader.parse_decision，两边必须一致。
DECISION_PROMPT = (
    "你是量化交易员。只输出一个 JSON 对象，不要解释。字段：\n"
    '{"action":"open_long|open_short|close_long|close_short|hold",'
    '"symbol":"交易对","quote":下单金额USDT,"price":参考价,"stop_loss":止损价或null,'
    '"take_profit":止盈价或null,"confidence":0到1,"ttl_sec":决策有效期秒,"rationale":"一句话理由"}\n'
    "约束：没有把握就输出 hold。quote 不得超过账户可用资金。不要给出杠杆。"
    "止损必须在方向上合理，多单止损低于参考价，空单高于参考价。"
)

# 每段的标题。界面按这个顺序排时间线，缺的那段直接不显示。
TITLES = {
    "technical": "技术面",
    "news": "新闻面",
    "bull": "做多理由",
    "bear": "做空理由",
    "risk": "风控复核",
    "verdict": "裁决",
}

@dataclass
class PipelineContext:
    """一次决策的全部输入。流水线不碰交易所，也不读配置。"""

    symbol: str
    timeframe: str
    digest: str
    equity: float = 0.0
    position_quote: float = 0.0
    price: float | None = None
    signal_target: float | None = None
    max_order_quote: float = 0.0
    max_position_quote: float = 0.0
    min_confidence: float = 0.0
    news: str = ""

    def signal_line(self) -> str:
        if self.signal_target is None:
            return ""
        target = self.signal_target
        direction = "做多" if target > 0 else "做空" if target < 0 else "空仓"
        return (
            f"【策略信号】目标仓位 {target:+.0%}（{direction}）。"
            "你只做复核。同意该方向才可开对应方向，不同意就 hold。"
            "不得反向开仓，不得在空仓信号下开新仓。"
        )

    def account_block(self) -> str:
        price = "未知" if self.price is None else f"{self.price}"
        return (
            f"【账户】{self.symbol} · {self.timeframe} · 权益 {self.equity:.2f} USDT，"
            f"当前持仓 {self.position_quote:.2f} USDT，最新价 {price}。"
            f"【上限】单笔 {self.max_order_quote:.2f} USDT，"
            f"最大持仓 {self.max_position_quote:.2f} USDT，"
            f"最小置信度 {self.min_confidence:.2f}。"
        )


@dataclass
class PipelineResult:
    """裁决文本加整条流水线的记录。"""

    decision_text: str = ""
    trace: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.decision_text)


def stages_for(depth: str) -> list[str]:
    """深度决定跑几段。不认识的深度按标准跑。"""
    if depth == "quick":
        return ["verdict"]
    if depth == "deep":
        return ["technical", "news", "bull", "bear", "risk", "verdict"]
    return ["technical", "bull", "bear", "verdict"]


def _context_block(ctx: PipelineContext) -> str:
    parts = [ctx.account_block()]
    line = ctx.signal_line()
    if line:
        parts.append(line)
    return "\n".join(parts)


def _seen(trace: list[dict[str, Any]], keys: tuple[str, ...]) -> str:
    """把前面几段的结论拼成一段，给后面的段当输入。"""
    blocks = []
    for item in trace:
        if item["stage"] in keys and item.get("ok"):
            blocks.append(f"【{item['title']}】\n{item['text']}")
    return "\n\n".join(blocks)


def _technical(ctx: PipelineContext, trace: list[dict[str, Any]]) -> str:
    return (
        "你负责技术面。只看下面的行情摘要，不猜消息面。\n"
        f"{ctx.digest}\n\n"
        f"{_context_block(ctx)}\n\n"
        "用不超过 5 行说清：趋势方向、关键支撑和压力、量能是否配合、最可能出错的地方。"
        "不要给下单建议。"
    )


def _news(ctx: PipelineContext, trace: list[dict[str, Any]]) -> str:
    items = ctx.news.strip() or "（今天没有相关新闻）"
    return (
        "你负责消息面。下面是最近的新闻标题，只挑和这个交易对真正相关的。\n"
        f"{items}\n\n"
        f"交易对 {ctx.symbol}。用不超过 4 行说清：有没有事件级别的消息、偏向哪一边、"
        "影响大概持续多久。没有相关消息就直接写「没有相关消息」。"
    )


def _bull(ctx: PipelineContext, trace: list[dict[str, Any]]) -> str:
    return (
        "你是多头研究员。基于前面的分析，写出现在做多的理由，最多 4 条，每条一句话。\n"
        f"{_seen(trace, ('technical', 'news'))}\n\n"
        f"{_context_block(ctx)}\n\n"
        "理由站不住就直说「没有做多理由」。不要为了凑数编理由。"
    )


def _bear(ctx: PipelineContext, trace: list[dict[str, Any]]) -> str:
    return (
        "你是空头研究员。看到多头的理由之后，写下做空（或观望）的理由，最多 4 条。\n"
        f"{_seen(trace, ('technical', 'news', 'bull'))}\n\n"
        f"{_context_block(ctx)}\n\n"
        "指出多头理由里最弱的一环。站不住就直说「没有做空理由」。"
    )


def _risk(ctx: PipelineContext, trace: list[dict[str, Any]]) -> str:
    return (
        "你是风控。对照上面的分析判断这一笔值不值得动手，用不超过 4 行回答："
        "方向是否自洽、亏损能承受多少、单笔与持仓上限是否够用、有没有必须放弃的理由。"
        "最后一行写「可以执行」或「建议放弃」。\n"
        f"{_seen(trace, ('technical', 'news', 'bull', 'bear'))}\n\n"
        f"{_context_block(ctx)}"
    )


def _verdict(ctx: PipelineContext, trace: list[dict[str, Any]]) -> str:
    seen = _seen(trace, ("technical", "news", "bull", "bear", "risk"))
    head = f"{DECISION_PROMPT}\n\n{_context_block(ctx)}\n"
    if seen:
        head += f"\n以下是你自己前面几步的分析，裁决要和它一致：\n{seen}\n"
    head += f"\n{ctx.digest}"
    return head


BUILDERS: dict[str, Callable[[PipelineContext, list[dict[str, Any]]], str]] = {
    "technical": _technical,
    "news": _news,
    "bull": _bull,
    "bear": _bear,
    "risk": _risk,
    "verdict": _verdict,
}


def run(
    llm: Any,
    ctx: PipelineContext,
    *,
    depth: str = "standard",
    clock: Callable[[], float] = time.time,
) -> PipelineResult:
    """跑一遍流水线。某段失败就记下来继续，裁决段失败才算整次失败。"""
    trace: list[dict[str, Any]] = []
    for stage in stages_for(depth):
        if stage == "news" and not ctx.news.strip():
            trace.append(
                {
                    "stage": stage,
                    "title": TITLES[stage],
                    "ok": False,
                    "skipped": True,
                    "text": "没有相关新闻，跳过这一段",
                    "model": "",
                    "tokens": 0,
                    "elapsed": 0.0,
                }
            )
            continue
        prompt = BUILDERS[stage](ctx, trace)
        started = clock()
        # 不在这里给单段设上限：llm 那一路对支持省略的供应商根本不发 max_tokens，
        # 模型自己决定写到哪儿停。以前固定 4000，推理模型先把额度花在思考上，
        # 段内预算太小时整段预算都被思考吃掉，回答直接截成空。段内不再传额度，
        # 想收紧只在这一处传 max_tokens。
        answer = llm.ask(prompt, system="", cache_key="", kind=f"ai_{stage}") or {}
        text = str(answer.get("text") or "").strip()
        ok = bool(answer.get("ok")) and bool(text)
        trace.append(
            {
                "stage": stage,
                "title": TITLES[stage],
                "ok": ok,
                "skipped": False,
                "text": text or str(answer.get("error") or "没有回答"),
                "model": str(answer.get("model") or ""),
                "tokens": int(answer.get("tokens") or 0),
                "elapsed": round(max(0.0, clock() - started), 2),
            }
        )
        if stage == "verdict" and not ok:
            return PipelineResult("", trace, error=str(answer.get("error") or "模型没有给出裁决"))
    verdict = next((item for item in trace if item["stage"] == "verdict"), None)
    if verdict is None or not verdict.get("ok"):
        return PipelineResult("", trace, error="模型没有给出裁决")
    return PipelineResult(str(verdict["text"]), trace)
