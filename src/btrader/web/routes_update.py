"""在线更新的四个接口：检查、一键更新、读进度、回滚。

一键更新会重启后端，所以引擎在跑的时候一律 409 拒绝——不替用户停引擎，也不在
正在下单的进程上拔电源。工作树脏、已经是最新、历史分叉各有各的 400，原因写在
detail 里，界面原样显示。

只有 POST 那两个会建任务；任何一条前置检查不过都不建任务、不写状态文件。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..core import updater

log = logging.getLogger(__name__)


class RollbackPayload(BaseModel):
    """回滚要显式说一声 confirm，防止误点。"""

    confirm: bool = False


def running_engines() -> list[str]:
    """现在在跑的引擎，按用户看得懂的名字报。探针表由 routes.build_router 填。"""
    from .routes import RUNNING_ENGINE_PROBES

    busy: list[str] = []
    for label, probe in RUNNING_ENGINE_PROBES.items():
        try:
            if probe():
                busy.append(label)
        except Exception as exc:  # 探针出错不能拦住更新，记一条就够
            log.debug("读不到「%s」的运行状态：%s", label, exc)
    return busy


def build_update_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/update/info")
    def update_info() -> dict[str, Any]:
        """本地与 GitHub 的差距。读不到远端就是 error 非空，界面不许说「已是最新」。"""
        return updater.collect_info()

    @router.post("/api/update/apply")
    def update_apply() -> dict[str, Any]:
        """一键更新。前置检查全过才建任务。"""
        busy = running_engines()
        if busy:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{'、'.join(busy)}正在跑：更新要重启后端，正在下单的进程被重启就没人管"
                    "仓位了。先把它停下来再更新，我不替你停。"
                ),
            )
        dirty, why = updater.work_tree_state()
        if why:
            raise HTTPException(status_code=400, detail=f"读不到工作树状态：{why}")
        if dirty:
            example = dirty[0].strip()
            raise HTTPException(
                status_code=400,
                detail=(
                    f"工作树里有 {len(dirty)} 个没提交的改动（例如 {example}）："
                    "更新要把远端提交快进合进来，本地改动要么先提交、要么先 git stash，再回来点更新。"
                ),
            )
        info = updater.collect_info()
        refused = updater.refusal(info)
        if refused is not None:
            raise HTTPException(status_code=refused[0], detail=refused[1])
        return {"job_id": updater.start_job(updater.KIND_APPLY, info)}

    @router.get("/api/update/status")
    def update_status(job_id: str = "") -> dict[str, Any]:
        """当前（或最后一次）任务的进度。状态在磁盘上，重启之后照样读得到。"""
        state = updater.read_state()
        if not state.get("job_id"):
            state["job_id"] = job_id
        return state

    @router.post("/api/update/rollback")
    def update_rollback(payload: RollbackPayload) -> dict[str, Any]:
        """回到上次更新之前的那个提交。只允许在 done 之后调用。"""
        if not payload.confirm:
            raise HTTPException(
                status_code=400,
                detail=(
                    "回滚要先确认一次：请求里要带 confirm=true——这一步会把更新带上来的提交"
                    "退回去，服务也会重启。确认之后再点。"
                ),
            )
        state = updater.read_state()
        if state.get("state") != updater.STATE_DONE:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"还没有一次成功的更新（现在是 {state.get('state')}），没有可回滚的东西。"
                ),
            )
        target = state.get("from_sha")
        if not target:
            raise HTTPException(
                status_code=400, detail="上次更新没有记下起始提交，回滚不了。"
            )
        return {"job_id": updater.start_job(updater.KIND_ROLLBACK, {"target": target})}

    return router
