"""Strategy source-editor endpoints.

Kept outside `routes.py` so the large trading router stays readable. The
controller-free functions are thin wrappers over `strategy/source.py`; all
validation and file confinement lives there and is covered by offline tests.
"""

from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import paths
from ..strategy.source import (
    StrategySourceError,
    read_source,
    save_source,
    validate_source,
)


class SourcePayload(BaseModel):
    code: str = ""
    key: str = ""
    create: bool = False


class ValidatePayload(BaseModel):
    code: str = Field(default="")


def build_strategy_router(get_state: Callable[[], Any]) -> APIRouter:
    router = APIRouter()

    def registry():
        return get_state().ctx.strategies

    @router.post("/api/strategies/validate")
    def validate(payload: ValidatePayload) -> dict[str, Any]:
        return validate_source(payload.code)

    @router.post("/api/strategies/reload")
    def reload() -> dict[str, Any]:
        reg = registry()
        reg.discover([paths.user_strategies_dir()])
        return {"ok": True, "errors": list(reg.errors), "count": len(reg)}

    @router.get("/api/strategies/{key}/source")
    def source(key: str) -> dict[str, Any]:
        try:
            return read_source(registry(), key)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"未知策略：{key}") from exc

    @router.put("/api/strategies/{key}/source")
    def save(key: str, payload: SourcePayload) -> dict[str, Any]:
        try:
            result = save_source(
                registry(),
                key=key,
                code=payload.code,
                create=payload.create,
            )
        except StrategySourceError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        return result

    @router.post("/api/strategies")
    def create(payload: SourcePayload) -> dict[str, Any]:
        try:
            result = save_source(
                registry(),
                key=payload.key,
                code=payload.code,
                create=True,
            )
        except StrategySourceError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        return result

    return router
