"""Strategy lab REST endpoints.

Candidate code never leaves the candidate directory and is never imported here;
the optimizer only tunes numeric parameters of an already-registered strategy.
The controller is a plain class so the logic can be tested without HTTP.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import ai, llm, paths
from ..ai_optimize import optimize_params
from ..backtest import BacktestConfig
from ..core.types import MarketType
from ..strategy.candidates import Candidate, CandidateError, CandidateStore
from ..strategy.source import StrategySourceError, save_source
from .routes import JobStore, _closed_only, _market_frame, _strategy_source

GENERATE_SYSTEM = (
    "你是量化策略工程师。只输出一个 JSON 对象，不要解释，不要输出 Markdown 代码块："
    '{"strategy_key": "小写下划线", "name": "中文名", "code": "Python 源码"}。'
    "code 必须是完整模块：从 btrader.strategy.base 导入 Strategy 与参数构造器"
    "（IntParam/FloatParam/BoolParam/ChoiceParam，必须使用绝对导入），"
    "定义一个直接继承 Strategy 的类，实现 generate(self, df, params) 并返回带 target 列的 DataFrame。"
    "禁止导入 os/sys/subprocess/socket/requests/urllib 等模块，禁止 eval/exec/open。"
)


class CandidateGeneratePayload(BaseModel):
    base_key: str = ""
    instruction: str = ""
    market: str = "spot"
    symbol: str = "BTC/USDT"
    timeframe: str = "1h"


class CandidateReviewPayload(BaseModel):
    approve: bool
    note: str = ""
    reviewer: str = ""


class CandidateBacktestPayload(BaseModel):
    metrics: dict[str, Any] = Field(default_factory=dict)
    symbol: str = ""
    timeframe: str = ""
    source: str = "btrader"


class OptimizePayload(BaseModel):
    base_key: str
    market: str = "spot"
    symbol: str = "BTC/USDT"
    timeframe: str = "1h"
    params: dict[str, Any] = Field(default_factory=dict)
    rounds: int = 3
    metric: str = "sharpe"
    bounds: dict[str, list[Any]] = Field(default_factory=dict)


def _market(value: str) -> MarketType:
    try:
        return MarketType(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"未知市场：{value}") from exc


class LabController:
    def __init__(
        self,
        get_state: Callable[[], Any],
        store: CandidateStore | None = None,
        *,
        strategy_dir: Path | None = None,
    ) -> None:
        self.get_state = get_state
        self.candidates = store or CandidateStore()
        self.strategy_dir = strategy_dir
        self.jobs = JobStore()
        self._cancels: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------- candidates
    def list_candidates(self) -> dict[str, Any]:
        return {"items": [item.to_dict() for item in self.candidates.list()]}

    def get_candidate(self, candidate_id: str) -> dict[str, Any]:
        try:
            candidate = self.candidates.get(candidate_id)
            code = self.candidates.read_code(candidate_id)
        except CandidateError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"candidate": candidate.to_dict(), "code": code}

    def generate(self, payload: CandidateGeneratePayload) -> dict[str, Any]:
        state = self.get_state()
        ctx = state.ctx
        if not ctx.settings.ai.enabled:
            raise HTTPException(status_code=400, detail="AI 助手未启用，请先在设置里启用")
        base_source = ""
        if payload.base_key:
            try:
                base_source = _strategy_source(ctx.strategies.get(payload.base_key))
            except Exception:
                base_source = ""
        prompt = (
            f"【参考策略】{payload.base_key or '无'}\n"
            f"【参考源码】\n{base_source[:4000]}\n"
            f"【改进要求】{payload.instruction or '给出一个稳健、可解释的趋势或均值回归策略'}"
        )
        answer = ctx.ai.ask(
            prompt,
            system=GENERATE_SYSTEM,
            kind="ai_lab",
            # 写代码比问答长：一个完整的策略模块 3-6k 字符，走专门的额度常量，
            # 让 JSON 一次写完整。改额度只动 llm 里那两个常量，别在这里写数字。
            max_tokens=llm.OUTPUT_TOKENS_CODE,
        )
        # 截断和「答非所问」要分开报：前者是改进要求写太长，后者重试一次就好。
        if answer.get("truncated"):
            raise HTTPException(
                status_code=502,
                detail=(
                    f"模型输出被截断（上限 {llm.OUTPUT_TOKENS_CODE} tokens），"
                    "把改进要求写短一点再试"
                ),
            )
        if not answer.get("ok"):
            raise HTTPException(
                status_code=502, detail=f"模型调用失败：{answer.get('error') or '未知错误'}"
            )
        found = ai._find_json_object(str(answer.get("text") or ""))
        if found is None:
            raise HTTPException(status_code=502, detail="模型返回的 JSON 不完整，重试一次")
        data, _start, _end = found
        code = data.get("code")
        if not isinstance(code, str) or not code.strip():
            raise HTTPException(status_code=502, detail="模型返回的 JSON 缺少 code 字段")
        key = str(data.get("strategy_key") or "ai_strategy")
        candidate = self.candidates.create(
            code,
            strategy_key=key,
            name=str(data.get("name") or key),
            base_key=payload.base_key,
            model=getattr(ctx.ai.settings, "model", ""),
        )
        return {"ok": True, "candidate": candidate.to_dict()}

    def review(self, candidate_id: str, payload: CandidateReviewPayload) -> dict[str, Any]:
        try:
            if payload.approve:
                self._register_candidate(candidate_id)
            candidate = self.candidates.review(
                candidate_id,
                approve=payload.approve,
                note=payload.note,
                reviewer=payload.reviewer,
            )
        except CandidateError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except StrategySourceError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        return {"ok": True, "candidate": candidate.to_dict()}

    def _register_candidate(self, candidate_id: str) -> Candidate:
        """Create a real user strategy when a candidate is approved.

        Approval is not the deployment: the deploy
        button wrote a differently named file later. Keep approval as the
        single hand-off point so the lab, backtest page and AI trader all see
        the same strategy.
        """
        candidate = self.candidates.get(candidate_id)
        if candidate.registered_key:
            return candidate
        if not candidate.checks.get("ok"):
            raise CandidateError("静态检查未通过，不能批准")

        ctx = self.get_state().ctx
        base = re.sub(r"[^a-z0-9_]", "_", (candidate.strategy_key or "ai_strategy").lower())
        base = base[:48] or "ai_strategy"
        if not base[0].isalpha():
            base = f"ai_{base}"
        key = base
        user_dir = self.strategy_dir or paths.user_strategies_dir()
        taken = key in ctx.strategies or ctx.strategies.source_path(key) is not None
        if taken or (user_dir / f"{key}.py").exists():
            suffix = re.sub(r"[^a-z0-9]", "", candidate.id.lower())[-8:] or "candidate"
            key = f"{base[:48]}_{suffix}"

        code = self.candidates.read_code(candidate_id)
        rewritten, count = re.subn(
            r'(?m)^(\s*key\s*=\s*)["\'][^"\']*["\']',
            lambda match: f'{match.group(1)}"{key}"',
            code,
            count=1,
        )
        if not count:
            raise CandidateError("候选源码缺少类属性 key，无法注册为策略")
        result = save_source(
            ctx.strategies,
            code=rewritten,
            key=key,
            directory=user_dir,
            create=not (user_dir / f"{key}.py").exists(),
        )
        source_path = ctx.strategies.source_path(result["key"])
        return self.candidates.mark_registered(
            candidate_id,
            key=result["key"],
            strategy_path=str(source_path or result["filename"]),
        )

    def record_backtest(
        self, candidate_id: str, payload: CandidateBacktestPayload
    ) -> dict[str, Any]:
        if not payload.metrics:
            raise HTTPException(status_code=400, detail="回测指标不能为空")
        metrics = dict(payload.metrics)
        metrics.update(
            {
                "symbol": payload.symbol,
                "timeframe": payload.timeframe,
                "source": payload.source,
            }
        )
        try:
            candidate = self.candidates.record_backtest(candidate_id, metrics)
        except CandidateError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True, "candidate": candidate.to_dict()}

    # --------------------------------------------------------------- optimize
    def start_optimize(self, payload: OptimizePayload) -> dict[str, Any]:
        state = self.get_state()
        ctx = state.ctx
        if not ctx.settings.ai.enabled:
            raise HTTPException(status_code=400, detail="AI 助手未启用，请先在设置里启用")
        try:
            strategy = ctx.strategies.get(payload.base_key)
        except Exception as exc:
            raise HTTPException(status_code=404, detail=f"策略不存在：{payload.base_key}") from exc
        market = _market(payload.market)
        timeframe = payload.timeframe
        base_params = strategy.validate_params(payload.params or strategy.default_params())
        rounds = max(1, min(int(payload.rounds), 5))

        job_id = self.jobs.create("lab-optimize")
        cancel = threading.Event()
        with self._lock:
            self._cancels[job_id] = cancel

        def run() -> None:
            try:
                frame = _closed_only(
                    _market_frame(state, ctx, market, payload.symbol, timeframe, 1000),
                    timeframe,
                )
                if frame is None or getattr(frame, "empty", True):
                    raise RuntimeError("没有可用的 K 线")
                result = optimize_params(
                    ctx.ai,
                    strategy,
                    frame,
                    base_params,
                    BacktestConfig(timeframe=timeframe),
                    bounds=payload.bounds or {},
                    rounds=rounds,
                    metric=payload.metric,
                    should_stop=cancel.is_set,
                    progress=lambda i, total, row: self.jobs.update(
                        job_id, progress=min(0.99, i / max(1, total))
                    ),
                )
                self.jobs.update(job_id, status="done", progress=1.0, result=result)
            except Exception as exc:  # pragma: no cover - depends on live data
                self.jobs.update(job_id, status="failed", error=str(exc))
            finally:
                with self._lock:
                    self._cancels.pop(job_id, None)

        threading.Thread(target=run, name=f"lab-optimize-{job_id}", daemon=True).start()
        return {"ok": True, "job_id": job_id}

    def get_optimize(self, job_id: str) -> dict[str, Any]:
        return self.jobs.get(job_id)

    def cancel_optimize(self, job_id: str) -> dict[str, Any]:
        self.jobs.get(job_id)  # 404 for unknown ids
        with self._lock:
            event = self._cancels.get(job_id)
        if event is not None:
            event.set()
            self.jobs.update(job_id, status="cancelling")
        return {"ok": True}


def build_lab_router(
    get_state: Callable[[], Any], store: CandidateStore | None = None
) -> APIRouter:
    router = APIRouter()
    controller = LabController(get_state, store)

    router.add_api_route(
        "/api/ai/strategies/candidates", controller.list_candidates, methods=["GET"]
    )
    router.add_api_route(
        "/api/ai/strategies/candidates/{candidate_id}",
        controller.get_candidate,
        methods=["GET"],
    )
    router.add_api_route(
        "/api/ai/strategies/candidates/generate", controller.generate, methods=["POST"]
    )
    router.add_api_route(
        "/api/ai/strategies/candidates/{candidate_id}/review",
        controller.review,
        methods=["POST"],
    )
    router.add_api_route(
        "/api/ai/strategies/candidates/{candidate_id}/backtest",
        controller.record_backtest,
        methods=["POST"],
    )
    router.add_api_route("/api/ai/lab/optimize", controller.start_optimize, methods=["POST"])
    router.add_api_route(
        "/api/ai/lab/optimize/{job_id}", controller.get_optimize, methods=["GET"]
    )
    router.add_api_route(
        "/api/ai/lab/optimize/{job_id}/cancel",
        controller.cancel_optimize,
        methods=["POST"],
    )
    return router
