"""AI 交易控制器的状态、暂停：停掉之后还要答得出来。

两条都在这轮全功能实测里撞到：

1. ``POST /api/ai/trader/stop`` 之后 ``GET /api/ai/trader/status`` 把 config、
   counters、last_decision、events 全清成空——刚跑完一轮的人看到「今日决策 0」
   会以为计数归零，也看不到刚才发生了什么。而当日决策次数在控制器自己的
   ``TradeBudget`` 里，事件在审计里，本来都还在。
2. ``POST /api/ai/trader/pause`` 在 stopped 状态下返回 200（paused 仍 false），
   传 ``{"paused": "yes"}`` 这种非布尔也 200——pydantic 把真值字符串悄悄转成了 True。
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from btrader.web.routes import AITraderController, AITraderPausePayload, build_router

SYMBOL = "BTC/USDT:USDT"


class _FakeTrader:
    """``AITrader`` 的最小替身：控制器只用得上 start / stop / pause / status。

    ``counters`` 里故意写 99：停掉之后报的必须是控制器 ``TradeBudget`` 的真数，
    不是这份快照里的数。
    """

    def __init__(self) -> None:
        self.stopped = False
        self.paused = False
        self.decided = False

    def start(self):
        return True, ""

    def stop(self) -> None:
        self.stopped = True

    def pause(self, paused: bool = True) -> None:
        self.paused = bool(paused)

    def decide_once(self) -> dict:
        self.decided = True
        return {"kind": "decision", "outcome": "order", "seq": 2}

    def status(self, since: int = 0) -> dict:
        events = [
            {"seq": 1, "kind": "decision", "outcome": "order"},
            {"seq": 2, "kind": "decision", "outcome": "reject"},
        ]
        if since:
            events = [item for item in events if item["seq"] > since]
        return {
            "state": "stopped" if self.stopped else "running",
            "paused": self.paused,
            "halted_reason": "",
            "config": {
                "network": "testnet",
                "symbol": SYMBOL,
                "strategy": "ma_cross",
                "market_type": "usdm",
                "timeframe": "15m",
                "depth": "quick",
                "max_order_quote": 100.0,
                "max_position_quote": 500.0,
            },
            "counters": {
                "decisions_today": 99,
                "orders_last_hour": 99,
                "max_decisions_per_day": 96,
                "max_orders_per_hour": 12,
            },
            "last_decision": {"seq": 2, "action": "open_long", "symbol": SYMBOL},
            "events": events,
            "seq": 2,
        }


def _endpoint(router, path: str, method: str):
    for route in router.routes:
        if getattr(route, "path", "") == path and method in getattr(route, "methods", ()):
            return route.endpoint
    raise AssertionError(f"missing route {method} {path}")


def _controller(router) -> AITraderController:
    """路由闭包里那个控制器单例（接口暴露的就是它）。"""
    endpoint = _endpoint(router, "/api/ai/trader/stop", "POST")
    for cell in endpoint.__closure__ or ():
        try:
            value = cell.cell_contents
        except ValueError:  # pragma: no cover - 空 cell 不会出现
            continue
        if isinstance(value, AITraderController):
            return value
    raise AssertionError("找不到 AI 交易控制器")


@pytest.fixture()
def ai_app():
    """``build_router`` 建出来的那份路由：控制器单例就活在它的闭包里。"""
    state = SimpleNamespace(locks={}, account_lock=threading.RLock())
    return build_router(lambda: state)


def _count_decisions(ctl: AITraderController, times: int = 1) -> None:
    """记几次决策。和真引擎一样先 roll 一次：``TradeBudget`` 当天第一次 roll 会清零。"""
    ctl.budget.roll(time.time())
    for _ in range(times):
        ctl.budget.note_decision()


def _running(ai_app, monkeypatch) -> tuple[AITraderController, _FakeTrader]:
    ctl = _controller(ai_app)
    fake = _FakeTrader()
    monkeypatch.setattr(ctl, "_build", lambda state, payload: fake)
    ctl.start(None, None)
    return ctl, fake


# ------------------------------------------------------- 停掉之后状态不能空
def test_status_after_stop_still_reports_the_run_that_just_ended(ai_app, monkeypatch):
    ctl, _fake = _running(ai_app, monkeypatch)
    _count_decisions(ctl, 2)

    stopped = ctl.stop()

    assert stopped["state"] == "stopped"
    # 配置：最后一次运行的那一份
    assert stopped["config"]["symbol"] == SYMBOL
    assert stopped["config"]["strategy"] == "ma_cross"
    # 计数：控制器预算里的真数（不是快照里的 99）
    assert stopped["counters"]["decisions_today"] == 2
    assert stopped["counters"]["max_decisions_per_day"] == 96
    assert stopped["counters"]["max_orders_per_hour"] == 12
    # 刚才发生了什么
    assert stopped["last_decision"]["action"] == "open_long"
    assert [item["seq"] for item in stopped["events"]] == [1, 2]


def test_the_status_route_reports_the_same_after_a_stop(ai_app, monkeypatch):
    ctl, _fake = _running(ai_app, monkeypatch)
    _count_decisions(ctl)
    stop = _endpoint(ai_app, "/api/ai/trader/stop", "POST")
    status = _endpoint(ai_app, "/api/ai/trader/status", "GET")

    stop()
    payload = status()

    assert payload["state"] == "stopped"
    assert payload["counters"]["decisions_today"] == 1
    assert payload["config"]["symbol"] == SYMBOL
    assert payload["last_decision"]["action"] == "open_long"
    assert payload["events"], payload


def test_since_still_filters_after_a_stop(ai_app, monkeypatch):
    ctl, _fake = _running(ai_app, monkeypatch)
    ctl.stop()

    assert [item["seq"] for item in ctl.status(since=1)["events"]] == [2]
    assert ctl.status(since=2)["events"] == []


def test_a_one_off_decision_leaves_a_reportable_status(ai_app, monkeypatch):
    """「立即决策一次」跑完也要留痕：暂停/停止之后看不到刚才那一条最坑。"""
    ctl = _controller(ai_app)
    fake = _FakeTrader()
    monkeypatch.setattr(ctl, "_build", lambda state, payload: fake)
    _count_decisions(ctl)

    ctl.decide(None, None)
    payload = ctl.stop()

    assert fake.decided is True
    assert payload["state"] == "stopped"
    assert payload["config"]["symbol"] == SYMBOL
    assert payload["last_decision"]["action"] == "open_long"
    assert payload["counters"]["decisions_today"] == 1


# ------------------------------------------------------------------ pause
def test_pausing_a_stopped_controller_is_a_400(ai_app, monkeypatch):
    pause = _endpoint(ai_app, "/api/ai/trader/pause", "POST")

    with pytest.raises(HTTPException) as caught:
        pause(AITraderPausePayload(paused=True))

    assert caught.value.status_code == 400
    assert "没有在运行" in str(caught.value.detail)


def test_pause_only_accepts_a_real_boolean(ai_app, monkeypatch):
    """``{"paused": "yes"}`` 不是布尔：pydantic 会悄悄转成 True，界面传错也成功。"""
    pause = _endpoint(ai_app, "/api/ai/trader/pause", "POST")

    with pytest.raises(HTTPException) as caught:
        pause(AITraderPausePayload(paused="yes"))

    assert caught.value.status_code == 400
    assert "true" in str(caught.value.detail)


def test_pausing_a_running_trader_takes_effect(ai_app, monkeypatch):
    ctl, fake = _running(ai_app, monkeypatch)
    pause = _endpoint(ai_app, "/api/ai/trader/pause", "POST")

    payload = pause(AITraderPausePayload(paused=True))

    assert payload["status"]["paused"] is True
    assert fake.paused is True
