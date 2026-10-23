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


def _busy_refusal() -> None:
    """已经有一个更新在跑？两个 POST 都先过这一道。

    git 工作树只有一份，两条流水线同时跑会互相踩；而且第二个任务会把第一个的
    job_id 与步骤从盘上抹掉，界面上看是两个任务在两个 job_id 之间跳。

    两道都查：状态文件里那条 running（本进程 + 别人进程都会写），以及跨进程的
    `<数据目录>/update.lock`。评审实测过：三个进程共用同一份数据目录、同时点更新，
    只查状态时三轮全部 3/3 都建成了任务。
    """
    busy = updater.running_job()
    if busy is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"上一个更新还在跑（任务 {busy.get('job_id') or '?'}，pid {busy.get('pid')}，"
                f"状态 {busy.get('state')}）：等它跑完再来——两条流水线同时动同一个工作树，"
                "会互相把对方的提交和状态搅乱。"
            ),
        )
    holder = updater.lock_holder()
    if holder is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"另一个进程正在更新（pid {holder.get('pid')}，任务 "
                f"{holder.get('job_id') or '?'}，从 {holder.get('at') or '?'} 开始）："
                "这份数据目录只许一个更新在跑——等它跑完再来。"
            ),
        )


def _desktop_refusal() -> None:
    """桌面端不许在线更新：那里只查 Releases，给下载链接。

    两种桌面进程都拦：
      * 冻结包（PyInstaller）里没有 .git、没有 venv、没有 npm，服务器那套一步都做不了；
      * 桌面壳拉起来的进程带着 `--port 0` + `--parent-pid` / `--watch-stdin`：更新完重启
        拉起的是裸 run_web.py，新进程既不知道临时端口，也没有壳盯着——壳当场失联，界面上
        是「更新完了但窗口连不上后端」。
    两种都返回 400：不建任务、不写状态文件。
    """
    if not updater.is_desktop_build():
        return
    reason = updater.desktop_shell_reason()
    if reason:
        raise HTTPException(
            status_code=400,
            detail=(
                f"当前这个后端是桌面壳启动的（{reason}）：更新最后要重启后端，而这里拉起的是"
                "裸的 run_web.py，新进程接不上壳（端口是壳当场选的、壳还盯着这个 pid 与 stdin）。"
                "请关掉窗口再打开应用，或者在应用里重启后端；服务端部署请用 run_web.py 或 systemd 那份。"
            ),
        )
    raise HTTPException(
        status_code=400,
        detail=(
            "桌面版是打包好的 exe，里面没有 .git、没有 venv、没有 npm：服务器那套"
            "git fetch / merge / pip / 重启一步都做不了，所以这里不做在线更新。"
            "桌面版只查 GitHub Releases：有新版本时界面上会给下载链接，装完重新打开应用即可。"
        ),
    )


def build_update_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/update/info")
    def update_info(refresh: int = 0) -> dict[str, Any]:
        """本地与 GitHub 的差距。读不到远端就是 error 非空，界面不许说「已是最新」。

        桌面端只查 Releases，结果进程内缓存 300 秒（未认证 API 每小时 60 次）；
        refresh=1 跳过缓存，界面上「再查一次」用。
        """
        return updater.collect_info(refresh=bool(refresh))

    @router.post("/api/update/apply")
    def update_apply() -> dict[str, Any]:
        """一键更新。前置检查全过才建任务。"""
        _busy_refusal()
        _desktop_refusal()
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
        try:
            job_id = updater.start_job(updater.KIND_APPLY, info)
        except updater.JobBusy as exc:
            # 上面那道闸和这里之间还有窗口：锁里再判一次，谁先拿到锁谁跑。
            raise HTTPException(status_code=409, detail=str(exc)) from None
        return {"job_id": job_id}

    @router.get("/api/update/status")
    def update_status(job_id: str = "") -> dict[str, Any]:
        """当前（或最后一次）任务的进度。状态在磁盘上，重启之后照样读得到。"""
        state = updater.read_state()
        if not state.get("job_id"):
            state["job_id"] = job_id
        # 界面按平台给手动命令：服务器在 Linux、人坐在 Windows 前面时，只靠浏览器 UA
        # 猜出来的那条命令是错的（cd /d 与 venv 里的 exe）。
        state["platform"] = updater.platform_name()
        return state

    @router.post("/api/update/rollback")
    def update_rollback(payload: RollbackPayload) -> dict[str, Any]:
        """回到上次更新之前的那个提交。只允许在 done 之后调用。"""
        _busy_refusal()
        _desktop_refusal()
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
        target = str(state.get("from_sha") or "")
        if not target:
            raise HTTPException(
                status_code=400, detail="上次更新没有记下起始提交，回滚不了。"
            )
        # 这段字符串会被拼进 git revert 的参数里，先确认它就是一个提交 SHA：
        # 不校验的话 --force、HEAD~5 或者别的什么都会被原样传下去。
        if not updater.is_commit_sha(target):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"上次更新记下的起始提交「{target}」不像一个提交 SHA"
                    "（7–40 位十六进制）。为了不把错东西拼进 git 命令，这次不许回滚；"
                    "真要退回去，请手动 git log 找到那个提交再处理。"
                ),
            )
        try:
            job_id = updater.start_job(updater.KIND_ROLLBACK, {"target": target})
        except updater.JobBusy as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        return {"job_id": job_id}

    return router
