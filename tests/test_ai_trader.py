"""AI 自主交易核心的离线测试（Phase 3-A）。

全部离线：FakeLLM / FakeTrading / FakeRisk，不联网、不读密钥环、不依赖网关。
审计文件用 ``tmp_path`` 隔离。
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from btrader.engine.ai_trader import (
    AIDecision,
    AIGuard,
    AITrader,
    AITraderConfig,
    TradeLease,
    parse_decision,
    validate_decision,
)


# ------------------------------------------------------------------- helpers
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


def decision_json(**overrides) -> str:
    payload = {
        "action": "open_long",
        "symbol": "BTC/USDT",
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


class FakeLLM:
    def __init__(self, answer: str = "", ok: bool = True) -> None:
        self.answer = answer
        self.ok = ok
        self.calls: list[str] = []

    def ask(self, prompt: str, **kwargs) -> dict:
        self.calls.append(prompt)
        if not self.ok:
            return {"ok": False, "error": "超时"}
        return {"ok": True, "text": self.answer}


class FakeRisk:
    def __init__(self, allowed: bool = True, reason: str = "") -> None:
        self.allowed = allowed
        self.reason = reason
        self.calls: list[tuple] = []

    def check_order(self, quote, current_position_quote, added_quote, **kwargs):
        self.calls.append((quote, current_position_quote, added_quote, kwargs))

        class Verdict:
            allowed = self.allowed
            reason = self.reason

        return Verdict()


class FakeTrading:
    def __init__(self, raises: Exception | None = None) -> None:
        self.requests: list = []
        self.raises = raises

    def submit(self, req):
        self.requests.append(req)
        if self.raises is not None:
            raise self.raises

        class Result:
            order = {"id": "order-1"}

        return Result()


def make_trader(
    tmp_path: Path,
    *,
    answer: str = "",
    llm_ok: bool = True,
    portfolio: dict | None = None,
    signal_target: float | None = None,
    **cfg,
) -> tuple[AITrader, FakeLLM, FakeTrading, FakeRisk]:
    cfg.setdefault("enabled", True)
    # These tests exercise the running machinery, so the arming switch is on.
    # Whether it *should* be on is what the gate tests below check.
    cfg.setdefault("live_enabled", True)
    cfg.setdefault("confirm_live", True)
    # These tests are about the decision machinery, not about protective
    # stops; the stop requirement has its own gate test below.
    cfg.setdefault("require_stop", False)
    # A long poll interval keeps the background thread from racing the
    # synchronous decide_once() calls these tests make.
    cfg.setdefault("poll_sec", 3600.0)
    config = AITraderConfig(**cfg)
    llm = FakeLLM(answer=answer or decision_json(), ok=llm_ok)
    trading = FakeTrading()
    risk = FakeRisk()
    view = portfolio or {"equity": 1000.0, "position_quote": 0.0, "price": 100.0}
    trader = AITrader(
        config,
        trading=trading,
        llm=llm,
        frame_provider=make_frame,
        portfolio_provider=lambda: dict(view),
        signal_provider=(lambda: signal_target) if signal_target is not None else None,
        audit_path=tmp_path / "audit.jsonl",
        clock=lambda: 1_800_000_000.0,
        risk=risk,
    )
    return trader, llm, trading, risk


def audit_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# --------------------------------------------------------------------- tests
def test_parse_decision_reads_json_block():
    text = "随便说点什么\n" + decision_json() + "\n结束"
    decision, error = parse_decision(text)
    assert error == ""
    assert decision is not None
    assert decision.action == "open_long"
    assert decision.quote == pytest.approx(25.0)
    assert decision.stop_loss == pytest.approx(98.0)


def test_parse_decision_rejects_garbage():
    decision, error = parse_decision("模型今天不想说话")
    assert decision is None
    assert "JSON" in error


def test_validate_rejects_quote_above_cap_instead_of_clamping():
    config = AITraderConfig(max_order_quote=10.0)
    decision = AIDecision(
        decision_id="d1", action="open_long", symbol="BTC/USDT", quote=50.0,
        price=100.0, stop_loss=98.0, confidence=0.9,
    )
    errors = validate_decision(decision, config)
    assert any("超过 AI 上限" in item for item in errors)


def test_validate_rejects_missing_stop_and_bad_direction():
    config = AITraderConfig()
    no_stop = AIDecision(
        decision_id="d1", action="open_long", symbol="BTC/USDT", quote=20.0,
        price=100.0, stop_loss=None, confidence=0.9,
    )
    assert any("止损" in item for item in validate_decision(no_stop, config))
    bad_stop = AIDecision(
        decision_id="d2", action="open_short", symbol="BTC/USDT", quote=20.0,
        price=100.0, stop_loss=99.0, confidence=0.9,
    )
    assert any("空单止损" in item for item in validate_decision(bad_stop, config))


def test_step_submits_only_through_trading_service(tmp_path):
    trader, _llm, trading, risk = make_trader(tmp_path)
    record = trader.decide_once()
    assert record is not None and record["outcome"] == "order"
    assert len(trading.requests) == 1
    request = trading.requests[0]
    assert request.symbol == "BTC/USDT"
    assert request.side == "buy"
    assert request.quote_amount == pytest.approx(25.0)
    assert request.stop_loss == pytest.approx(98.0)
    # INV-2：AI 订单在提交前也过了 RiskManager。
    assert risk.calls and risk.calls[0][0] == pytest.approx(25.0)


def test_model_failure_places_no_order_and_keeps_position(tmp_path):
    trader, _llm, trading, _risk = make_trader(tmp_path, llm_ok=False)
    record = trader.decide_once()
    assert record["outcome"] == "skip"
    assert "模型调用失败" in record["reason"]
    assert trading.requests == []


def test_unparseable_answer_places_no_order(tmp_path):
    trader, _llm, trading, _risk = make_trader(tmp_path, answer="今天不交易")
    record = trader.decide_once()
    assert record["outcome"] == "skip"
    assert trading.requests == []


def test_halt_blocks_entries(tmp_path):
    trader, _llm, trading, _risk = make_trader(tmp_path)
    trader.halt("人工熔断")
    record = trader.decide_once()
    assert record["outcome"] == "reject"
    assert "熔断" in record["reason"]
    assert trading.requests == []


def test_kill_switch_blocks_and_releases_lease(tmp_path):
    trader, _llm, _trading, _risk = make_trader(tmp_path)
    ok, _message = trader.start()
    assert ok
    trader.kill("急停")
    assert trader.state == "halted"
    assert trader.lease.owner == ""
    assert trader.step() is None


def test_start_requires_arming_conditions(tmp_path):
    """Testnet and mainnet share one path; only the confirmation differs."""
    unarmed = make_trader(tmp_path, live_enabled=False, market_type="usdm")[0]
    ok, message = unarmed.start()
    assert not ok and "live_enabled" in message

    unconfirmed = make_trader(tmp_path, confirm_live=False, market_type="usdm")[0]
    ok, message = unconfirmed.start()
    assert not ok and "二次确认" in message

    # Binance spot has no reduce-only stop, so a stop-requiring run cannot arm.
    spot = make_trader(tmp_path, confirm_live=True, require_stop=True)[0]
    ok, message = spot.start()
    assert not ok and "止损" in message

    # The testnet needs no acknowledgement: rehearsing is the point of it.
    testnet = make_trader(
        tmp_path, confirm_live=False, network="testnet", market_type="usdm",
    )[0]
    ok, message = testnet.start()
    assert ok, message
    testnet.stop()

    live = make_trader(
        tmp_path, confirm_live=True, market_type="usdm", max_position_quote=500.0,
    )[0]
    ok, message = live.start()
    assert ok, message
    live.stop()


def test_disabled_trader_does_not_start(tmp_path):
    trader, _llm, _trading, _risk = make_trader(tmp_path, enabled=False)
    ok, message = trader.start()
    assert not ok and "未启用" in message


def test_daily_decision_limit_stops_before_llm_call(tmp_path):
    trader, llm, _trading, _risk = make_trader(tmp_path, max_decisions_per_day=1)
    assert trader.decide_once()["outcome"] == "order"
    second = trader.decide_once()
    assert second["outcome"] == "skip"
    assert "当日决策次数" in second["reason"]
    assert len(llm.calls) == 1


def test_price_drift_skips_order(tmp_path):
    trader, _llm, trading, _risk = make_trader(
        tmp_path,
        portfolio={"equity": 1000.0, "position_quote": 0.0, "price": 200.0},
        max_price_drift_pct=0.005,
    )
    record = trader.decide_once()  # 决策价 100，行情 200：漂移 100%
    assert record["outcome"] == "reject"
    assert "漂移" in record["reason"]
    assert trading.requests == []


def test_audit_has_required_fields(tmp_path):
    trader, _llm, _trading, _risk = make_trader(tmp_path)
    trader.decide_once()
    rows = audit_rows(tmp_path / "audit.jsonl")
    assert rows
    last = rows[-1]
    for key in ("at", "decision_id", "action", "symbol", "quote", "confidence", "outcome", "reason"):
        assert key in last


def test_lease_blocks_second_trader(tmp_path):
    trader, _llm, _trading, _risk = make_trader(tmp_path)
    ok, _message = trader.start()
    assert ok
    other = AITrader(
        trader.config, trading=FakeTrading(), llm=FakeLLM(),
        frame_provider=make_frame, audit_path=tmp_path / "other.jsonl",
        clock=lambda: 1_800_000_000.0, lease=trader.lease,
    )
    ok, message = other.start()
    assert not ok and "占用" in message
    trader.stop()


def test_status_reports_events_since_seq(tmp_path):
    trader, _llm, _trading, _risk = make_trader(tmp_path)
    trader.decide_once()
    status = trader.status()
    assert status["state"] == "stopped"
    assert status["seq"] >= 1
    assert status["events"]
    assert status["counters"]["decisions_today"] == 1
    assert trader.status(since=status["seq"])["events"] == []


def test_guard_rejects_low_confidence_and_hold():
    guard = AIGuard(AITraderConfig(min_confidence=0.7))
    hold = AIDecision(decision_id="h", action="hold", symbol="BTC/USDT", confidence=0.9)
    assert not guard.check(
        hold, now=0.0, position_quote=0.0, equity=1000.0, decisions_today=0,
        orders_last_hour=0, last_order_at=0.0, market_price=100.0,
    ).ok
    weak = AIDecision(
        decision_id="w", action="open_long", symbol="BTC/USDT", quote=10.0,
        price=100.0, stop_loss=98.0, confidence=0.3, ttl_sec=60.0,
    )
    verdict = guard.check(
        weak, now=0.0, position_quote=0.0, equity=1000.0, decisions_today=0,
        orders_last_hour=0, last_order_at=0.0, market_price=100.0,
    )
    assert not verdict.ok and "置信度" in verdict.reason


def test_trade_lease_is_exclusive():
    lease = TradeLease()
    assert lease.acquire("ai")
    assert not lease.acquire("auto")
    lease.release("ai")


def test_strategy_signal_is_in_prompt_and_allows_same_direction(tmp_path):
    trader, llm, trading, _risk = make_trader(tmp_path, signal_target=0.6)
    record = trader.decide_once()
    assert record is not None and record["outcome"] == "order"
    assert "策略信号" in llm.calls[0]
    assert "+60%" in llm.calls[0]
    assert len(trading.requests) == 1


def test_strategy_signal_vetoes_opposite_entry(tmp_path):
    trader, _llm, trading, _risk = make_trader(
        tmp_path,
        answer=decision_json(
            action="open_short", stop_loss=102.0, take_profit=96.0
        ),
        signal_target=0.6,
    )
    record = trader.decide_once()
    assert record is not None and record["outcome"] == "reject"
    assert "反向" in record["reason"]
    assert trading.requests == []


def test_zero_strategy_signal_skips_before_calling_model(tmp_path):
    trader, llm, trading, _risk = make_trader(tmp_path, signal_target=0.0)
    record = trader.decide_once()
    assert record is not None and record["outcome"] == "skip"
    assert "策略无信号" in record["reason"]
    assert llm.calls == []
    assert trading.requests == []

def test_closing_a_short_buys_and_closing_a_long_sells(tmp_path):
    """平空必须是买、平多是卖：方向写错时卖单会把空头做得更大。"""
    short = make_trader(
        tmp_path,
        answer=decision_json(action="close_short", quote=400.0, stop_loss=None),
        portfolio={"equity": 1000.0, "position_quote": -400.0, "price": 100.0},
        market_type="usdm",
    )[0]
    record = short.decide_once()
    assert record is not None
    assert short.trading.requests[-1].side == "buy"
    assert short.trading.requests[-1].reduce_only is True

    long_only = make_trader(
        tmp_path,
        answer=decision_json(action="close_long", quote=400.0, stop_loss=None),
        portfolio={"equity": 1000.0, "position_quote": 400.0, "price": 100.0},
    )[0]
    long_only.decide_once()
    assert long_only.trading.requests[-1].side == "sell"
    assert long_only.trading.requests[-1].reduce_only is True


def test_stop_between_decision_and_submit_blocks_the_order(tmp_path):
    """停止信号在模型调用期间到达时，这一单不能再发出去。"""
    trader, _llm, trading, _risk = make_trader(tmp_path)
    trader._stop_event.set()  # 相当于用户在这几秒里按了停止
    record = trader.decide_once()
    assert record is not None and record["outcome"] == "error"
    assert trading.requests == []


def test_status_config_carries_the_strategy_key(tmp_path):
    trader, _llm, _trading, _risk = make_trader(tmp_path, strategy_key="ma_cross")
    assert trader.status()["config"]["strategy"] == "ma_cross"


def test_start_refuses_while_the_previous_poller_is_still_exiting(tmp_path):
    class StuckThread:
        def is_alive(self):
            return True

    trader, _llm, _trading, _risk = make_trader(tmp_path)
    trader._thread = StuckThread()
    ok, message = trader.start()
    assert not ok and "仍在退出" in message
