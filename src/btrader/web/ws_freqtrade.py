"""WebSocket proxy for the Freqtrade RPC server.

The browser cannot attach the JWT to a WebSocket handshake, so this endpoint
logs in on the server side and forwards Freqtrade's status/entry/exit/protection
messages unchanged. Everything stays on localhost; the token never reaches the
page.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable
from urllib.parse import quote

import websockets
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

log = logging.getLogger(__name__)


def build_freqtrade_ws_router(get_state: Callable[[], Any]) -> APIRouter:
    router = APIRouter()

    @router.websocket("/ws/ft")
    async def freqtrade_socket(websocket: WebSocket) -> None:
        await websocket.accept()
        state = get_state()
        bridge = state.ctx.freqtrade
        if not bridge.running:
            await websocket.send_json(
                {"type": "error", "data": {"message": "Freqtrade 未启动"}}
            )
            await websocket.close(code=1013)
            return

        try:
            token = await asyncio.to_thread(bridge.client().login)
            url = (
                f"ws://127.0.0.1:{bridge.settings.trade_port}"
                f"/api/v1/message/ws?token={quote(token, safe='')}"
            )
            async with websockets.connect(
                url, ping_interval=20, ping_timeout=20, open_timeout=10
            ) as upstream:
                await websocket.send_json(
                    {"type": "connected", "data": {"trade_port": bridge.settings.trade_port}}
                )

                async def browser_to_bot() -> None:
                    while True:
                        message = await websocket.receive_text()
                        await upstream.send(message)

                async def bot_to_browser() -> None:
                    async for message in upstream:
                        await websocket.send_text(
                            message if isinstance(message, str) else message.decode("utf-8")
                        )

                tasks = [
                    asyncio.create_task(browser_to_bot()),
                    asyncio.create_task(bot_to_browser()),
                ]
                done, pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                for task in done:
                    exc = task.exception()
                    if exc and not isinstance(exc, WebSocketDisconnect):
                        log.debug("Freqtrade WebSocket 代理结束: %s", exc)
        except WebSocketDisconnect:
            return
        except Exception as exc:
            log.warning("Freqtrade WebSocket 连接失败: %s", exc)
            try:
                await websocket.send_json(
                    {"type": "error", "data": {"message": str(exc)}}
                )
            except Exception:
                pass
        finally:
            try:
                await websocket.close()
            except Exception:
                pass

    return router
