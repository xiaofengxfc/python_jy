"""提交前复核要看见「此刻」的持仓，不是面板缓存里的那一眼。

``AccountService`` 的快照是给面板轮询用的（交易对 3 秒、全钱包权益 10 秒）。
AI 交易的提交前复核吃这层缓存，就等于「模型调用期间成交了 0.5 个币」这件事在
复核眼里根本没发生：它照着旧快照放行，然后拿错误的仓位去下单。
"""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pandas as pd
import pytest

from btrader.core.config import AppSettings
from btrader.core.storage import Database
from btrader.core.types import MarketType, Network
from btrader.engine.account import AccountService
from btrader.web import routes

from fakes import FakeVenue

SYMBOL = "BTC/USDT:USDT"


def decision_json(**overrides) -> str:
    payload = {
        "action": "open_long",
        "symbol": SYMBOL,
        "quote": 25.0,
        "price": 100.0,
        "stop_loss": 98.0,
        "take_profit": 104.0,
        "confidence": 0.8,
        "ttl_sec": 120,
        "rationale": "均线多头",
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


def make_frame(bars: int = 30) -> pd.DataFrame:
    rows = []
    price = 100.0
    for index in range(bars):
        price += 0.4
        rows.append(
            {
                "timestamp": 1_700_000_000_000 + index * 900_000,
                "open": price - 0.2,
                "high": price + 0.6,
                "low": price - 0.6,
                "close": price,
                "volume": 10.0 + index,
            }
        )
    return pd.DataFrame(rows)


class FakeLLM:
    def __init__(self, answer: str = "") -> None:
        self.answer = answer or decision_json()

    def ask(self, prompt: str, **kwargs) -> dict:
        return {"ok": True, "text": self.answer}


class FakeTrading:
    def __init__(self) -> None:
        self.requests: list = []

    def submit(self, req):
        self.requests.append(req)

        class Result:
            order = {"id": "order-1"}

        return Result()


class FakeRisk:
    """风控替身：不计分、不拦单。这一组测的是账户快照这一路。"""

    settings = SimpleNamespace(kill_switch=False)
    state = SimpleNamespace(halted_reason="")

    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed

    def check_order(self, quote, current_position_quote, added_quote, **kwargs):
        risk = self

        class Verdict:
            allowed = risk.allowed
            reason = ""

        return Verdict()


class _StubStrategy:
    key = "ma_cross"

    def validate_params(self, params):
        return dict(params or {})


class _StubRegistry:
    keys = ["ma_cross"]

    def get(self, key):
        return _StubStrategy()


class _StubRest:
    has_credentials = True


def _service(tmp_path, venue: FakeVenue) -> tuple[AccountService, Database]:
    venue._last.setdefault(SYMBOL, 100.0)
    db = Database(tmp_path / "account.db")
    service = AccountService(
        rest_for=lambda market: venue,
        chart_rest_for=lambda market: venue,
        db=db,
        network=Network.TESTNET,
    )
    return service, db


def _context(service: AccountService, venue: FakeVenue) -> SimpleNamespace:
    settings = AppSettings()
    settings.ai.enabled = True
    settings.ai_auto.symbol = SYMBOL
    settings.ai_auto.timeframe = "15m"
    risk = FakeRisk()
    return SimpleNamespace(
        market_type=MarketType.USDM,
        network=Network.TESTNET,
        account=service,
        settings=settings,
        strategies=_StubRegistry(),
        rest_for=lambda market: _StubRest(),
        market_rest_for=lambda market: venue,
        ai=SimpleNamespace(),
        trading=SimpleNamespace(risk=risk),
    )


def _state(service: AccountService, venue: FakeVenue) -> SimpleNamespace:
    return SimpleNamespace(
        ctx=_context(service, venue),
        locks={market: threading.RLock() for market in MarketType},
    )


# ------------------------------------------------------- 绕开缓存的读取路径
def test_live_snapshot_sees_a_fill_the_panel_cache_still_hides(tmp_path):
    """缓存里是 0 仓，交易所里已经是 0.5：复核要看后者。"""
    venue = FakeVenue()
    service, db = _service(tmp_path, venue)
    try:
        assert service.snapshot(MarketType.USDM, SYMBOL).positions == []

        # 模型调用的那几十秒里成交了半张
        venue.create_order(MarketType.USDM, SYMBOL, "market", "buy", 0.5, 100.0)

        # 面板照旧吃缓存：这正是「复核看不见成交」的来源
        assert service.snapshot(MarketType.USDM, SYMBOL).positions == []

        # 复核那条路径现读
        live = service.live_snapshot(MarketType.USDM, SYMBOL)
        assert len(live.positions) == 1
        assert live.positions[0]["contracts"] == pytest.approx(0.5)
    finally:
        db.close()


def test_the_default_read_stays_cached(tmp_path):
    """既有默认行为不变：面板不会因为这条新路径开始每 2 秒打一次交易所。"""
    venue = FakeVenue()
    service, db = _service(tmp_path, venue)
    try:
        service.snapshot(MarketType.USDM, SYMBOL)
        venue.create_order(MarketType.USDM, SYMBOL, "market", "buy", 0.5, 100.0)
        assert service.snapshot(MarketType.USDM, SYMBOL).positions == []
        assert service.snapshot(MarketType.USDM, SYMBOL, refresh=True).positions != []
    finally:
        db.close()


# ------------------------------------------------- AI 交易那一路的接线
def test_the_ai_portfolio_view_is_live_while_the_panels_stay_cached(tmp_path):
    """同一时刻：面板（/api/account 那条路）还是旧数，AI 的组合视图已是新的。"""
    venue = FakeVenue()
    service, db = _service(tmp_path, venue)
    state = _state(service, venue)
    try:
        assert routes._ai_portfolio(state, SYMBOL)["position_quote"] == 0.0
        venue.create_order(MarketType.USDM, SYMBOL, "market", "buy", 0.5, 100.0)

        # 面板视角（缓存，顶部侧栏和资产页读的就是这一份）
        panel = service.snapshot(MarketType.USDM, SYMBOL)
        assert panel.positions == []
        # AI 交易的视角：0.5 个 × 100
        live = routes._ai_portfolio(state, SYMBOL)
        assert live["position_quote"] == pytest.approx(50.0)
    finally:
        db.close()


def test_a_fill_only_the_fresh_read_sees_blocks_the_entry(tmp_path):
    """端到端复现：模型调用期间成交，复核必须判「持仓变了」而不是放行。"""
    venue = FakeVenue()
    service, db = _service(tmp_path, venue)
    state = _state(service, venue)
    try:
        # 走真实接线：控制器建出来的实例，portfolio_provider 是它自己那一个
        trader = routes.AITraderController()._build(
            state,
            routes.AITraderStartPayload(
                market="usdm", symbol=SYMBOL, strategy="ma_cross", confirm_live=True, depth="quick"
            ),
        )
        # 其余 provider 换成测试替身：这一条只验证账户快照这一路
        trader.frame_provider = make_frame
        trader.signal_provider = lambda: 0.6
        trader.news_provider = None
        llm = FakeLLM()
        answer = llm.ask
        trader.llm = llm
        trading = FakeTrading()
        trader.trading = trading
        trader.clock = lambda: 1_800_000_000.0

        def during_the_model_call(prompt, **kwargs):
            # 模型调用的那几十秒里成交了半个币
            venue.create_order(MarketType.USDM, SYMBOL, "market", "buy", 0.5, 100.0)
            return answer(prompt, **kwargs)

        llm.ask = during_the_model_call

        record = trader.decide_once()

        assert record is not None and record["outcome"] == "reject", record
        # 断言到具体那句：不能因为「读不到持仓」之类的别的拒绝而碰巧通过
        assert "持仓在模型调用期间变了" in record["reason"], record["reason"]
        assert trading.requests == []
    finally:
        db.close()
