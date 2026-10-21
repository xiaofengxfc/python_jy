"""The Freqtrade WebSocket facade must fail clearly when the sidecar is down."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from btrader.web.ws_freqtrade import build_freqtrade_ws_router


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.closed = False

    async def accept(self) -> None:
        pass

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)

    async def close(self, code: int = 1000) -> None:
        self.closed = True


def test_ws_reports_engine_not_running():
    freqtrade = SimpleNamespace(running=False)
    state = SimpleNamespace(ctx=SimpleNamespace(freqtrade=freqtrade))
    router = build_freqtrade_ws_router(lambda: state)
    endpoint = router.routes[0].endpoint
    socket = FakeWebSocket()

    asyncio.run(endpoint(socket))

    assert socket.sent[0]["type"] == "error"
    assert "未启动" in socket.sent[0]["data"]["message"]
    assert socket.closed
