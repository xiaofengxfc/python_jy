"""多轮「回测 → 反思 → 调参 → 再回测」优化循环（Phase 4）。

模型只负责提出**参数候选**，回测与接受/拒绝规则完全由本地确定性代码执行：
同一个 frame、同一个 BacktestConfig，保证每轮可比。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable

from . import ai
from .backtest import BacktestConfig, run_backtest

REFLECT_PROMPT = (
    "你是量化策略研究员。基于下面的回测指标与当前参数，提出下一组参数。"
    "只输出一个 JSON 对象，不要解释："
    '{"params": {参数名: 数值}, "hypothesis": "一句话假设", "reason": "为什么这样调"}。'
    "参数必须在给定上下界内；不要输出代码。"
)


@dataclass
class OptimizeRound:
    round: int
    params: dict[str, Any]
    metrics: dict[str, float]
    score: float
    accepted: bool
    reason: str
    reflection: str = ""
    tokens: int = 0
    elapsed: float = 0.0
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "round": self.round,
            "params": self.params,
            "metrics": self.metrics,
            "score": self.score,
            "accepted": self.accepted,
            "reason": self.reason,
            "reflection": self.reflection,
            "tokens": self.tokens,
            "elapsed": self.elapsed,
            "error": self.error,
        }


def _score(metrics: dict[str, float], metric: str) -> float:
    try:
        return float(metrics.get(metric, 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _drawdown(metrics: dict[str, float]) -> float:
    try:
        return abs(float(metrics.get("max_drawdown", 0.0) or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _clamp_step(
    old: float, new: float, bounds: list[Any] | None
) -> float:
    """Limit one round's move to half the allowed range (or half the value)."""
    if bounds and len(bounds) == 2:
        low, high = float(bounds[0]), float(bounds[1])
        span = max(high - low, 1e-9)
        step = span * 0.5
    else:
        low, high = None, None
        step = max(abs(old) * 0.5, 1e-6)
    value = max(old - step, min(old + step, new))
    if low is not None:
        value = max(low, value)
    if high is not None:
        value = min(high, value)
    return value


def optimize_params(
    llm: Any,
    strategy: Any,
    frame: Any,
    base_params: dict[str, Any],
    config: BacktestConfig,
    *,
    bounds: dict[str, list[Any]] | None = None,
    rounds: int = 3,
    metric: str = "sharpe",
    min_improvement: float = 0.0,
    max_drawdown_worse: float = 0.2,
    max_calls: int = 10,
    deadline: float | None = None,
    progress: Callable[[int, int, dict[str, Any]], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Run the deterministic optimize loop. Never raises on model failures."""
    bounds = bounds or {}
    params = strategy.validate_params(dict(base_params))
    baseline = run_backtest(frame, strategy, params, config)
    best_params = dict(params)
    best_metrics = dict(baseline.metrics)
    best_score = _score(best_metrics, metric)
    best_dd = _drawdown(best_metrics)

    history: list[OptimizeRound] = []
    calls = 0
    no_improvement = 0
    stopped = "rounds"

    for index in range(1, max(1, rounds) + 1):
        if calls >= max_calls:
            stopped = "budget"
            break
        if deadline is not None and time.time() >= deadline:
            stopped = "timeout"
            break
        if should_stop is not None and should_stop():
            stopped = "cancel"
            break

        prompt = (
            f"{REFLECT_PROMPT}\n\n【当前参数】{json.dumps(best_params, ensure_ascii=False)}\n"
            f"【指标】score={best_score:.4f} metrics={json.dumps(best_metrics, ensure_ascii=False)}\n"
            f"【参数上下界】{json.dumps(bounds, ensure_ascii=False)}\n"
            f"【已尝试】{json.dumps([r.to_dict() for r in history], ensure_ascii=False)}"
        )
        started = time.time()
        answer = llm.ask(prompt, kind="ai_lab") or {}
        calls += 1
        if not answer.get("ok"):
            history.append(
                OptimizeRound(
                    round=index,
                    params=dict(best_params),
                    metrics={},
                    score=best_score,
                    accepted=False,
                    reason="模型调用失败",
                    error=str(answer.get("error") or ""),
                    elapsed=time.time() - started,
                )
            )
            no_improvement += 1
            if no_improvement >= 2:
                stopped = "no_improvement"
                break
            continue

        found = ai._find_json_object(str(answer.get("text") or ""))
        if found is None:
            history.append(
                OptimizeRound(
                    round=index,
                    params=dict(best_params),
                    metrics={},
                    score=best_score,
                    accepted=False,
                    reason="模型没有返回可解析的 JSON",
                    elapsed=time.time() - started,
                )
            )
            no_improvement += 1
            if no_improvement >= 2:
                stopped = "no_improvement"
                break
            continue

        data, _start, _end = found
        raw = data.get("params")
        if not isinstance(raw, dict):
            history.append(
                OptimizeRound(
                    round=index,
                    params=dict(best_params),
                    metrics={},
                    score=best_score,
                    accepted=False,
                    reason="JSON 缺少 params 对象",
                    elapsed=time.time() - started,
                )
            )
            no_improvement += 1
            if no_improvement >= 2:
                stopped = "no_improvement"
                break
            continue

        candidate = dict(best_params)
        for name, value in raw.items():
            if name not in candidate:
                continue
            try:
                old = float(candidate[name])
                new = float(value)
            except (TypeError, ValueError):
                continue
            candidate[name] = _clamp_step(old, new, bounds.get(name))
        candidate = strategy.validate_params(candidate)

        try:
            result = run_backtest(frame, strategy, candidate, config)
        except Exception as exc:  # pragma: no cover - strategy errors are data errors
            history.append(
                OptimizeRound(
                    round=index,
                    params=candidate,
                    metrics={},
                    score=best_score,
                    accepted=False,
                    reason=f"回测失败：{exc}",
                    elapsed=time.time() - started,
                )
            )
            no_improvement += 1
            if no_improvement >= 2:
                stopped = "no_improvement"
                break
            continue

        metrics = dict(result.metrics)
        score = _score(metrics, metric)
        drawdown = _drawdown(metrics)
        accepted = score >= best_score + min_improvement and drawdown <= best_dd * (1 + max_drawdown_worse) + 1e-9
        reflection = str(data.get("hypothesis") or data.get("reason") or "")[:300]
        history.append(
            OptimizeRound(
                round=index,
                params=candidate,
                metrics=metrics,
                score=score,
                accepted=accepted,
                reason="接受" if accepted else "未改善或回撤变差",
                reflection=reflection,
                elapsed=time.time() - started,
            )
        )
        if progress is not None:
            try:
                progress(index, rounds, history[-1].to_dict())
            except Exception:  # pragma: no cover - progress must not break the loop
                pass
        if accepted:
            best_params, best_metrics, best_score, best_dd = candidate, metrics, score, drawdown
            no_improvement = 0
        else:
            no_improvement += 1
            if no_improvement >= 2:
                stopped = "no_improvement"
                break

    return {
        "ok": True,
        "rounds": [item.to_dict() for item in history],
        "best": best_params,
        "best_metrics": best_metrics,
        "best_score": best_score,
        "calls": calls,
        "stopped": stopped,
    }
