"""AI 自主交易核心的离线测试。

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

def test_halt_releases_the_trade_lease_for_other_controllers(tmp_path):
    """熔断不是把整个账户锁死：停下来的引擎不该继续占着交易权。

    之前只有 stop()/kill() 释放租约，halt() 不释放。后果是一次熔断之后，
    自动交易和 AI 量化再 acquire 只会拿到「交易权已被占用」，只能等有人手动
    stop 那个已经停下来的引擎。
    """
    trader, _llm, _trading, _risk = make_trader(tmp_path)
    lease = trader.lease
    ok, _message = trader.start()
    assert ok
    assert lease.owner

    trader.halt("人工熔断")

    assert trader.state == "halted"
    assert lease.owner == "", "熔断后必须当场把交易权还回去"
    # 熔断的引擎仍然拒绝自己启动：拦新开仓的是 state 和 AIGuard，不是租约。
    # （此刻 poller 还在空转，先撞上「上一轮线程仍在退出」那道检查，同样是拒绝。）
    ok, _message = trader.start()
    assert not ok
    assert lease.owner == "", "被拒的启动不能顺手把交易权抓回来"
    # 另一路（这里用同一把租约的第二个实例代表 auto/quant）马上能拿到。
    other = AITrader(
        trader.config, trading=FakeTrading(), llm=FakeLLM(),
        frame_provider=make_frame, audit_path=tmp_path / "other.jsonl",
        clock=lambda: 1_800_000_000.0, lease=lease,
    )
    ok, message = other.start()
    assert ok, message
    other.stop()
    trader.stop()  # 熔断实例的 poller 仍在空转（step 直接返回），收尾停掉


def test_failed_thread_start_rolls_back_state_and_lease(tmp_path, monkeypatch):
    """线程起不来就是没跑：state 要回 stopped，交易权也要还回去。

    之前 start() 先把 state 写成 running 再去起线程，线程抛异常时既不回滚
    state 也不释放租约：界面显示在跑、实际没有 poller，而交易权被一个死引擎
    占着，别的控制器再也拿不到。
    """
    from btrader.engine import ai_trader as ai_trader_module

    class BoomThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

        def is_alive(self):
            return False

    class FakeThreading:
        Thread = BoomThread

    trader, _llm, _trading, _risk = make_trader(tmp_path)
    monkeypatch.setattr(ai_trader_module, "threading", FakeThreading)

    with pytest.raises(RuntimeError):
        trader.start()

    assert trader.state == "stopped", "线程没起来就不能显示在跑"
    assert trader._thread is None
    assert trader.lease.owner == "", "启动失败必须把交易权还回去"
    assert trader.lease.acquire("auto")
    trader.lease.release("auto")


def test_start_has_no_live_enabled_gate(tmp_path):
    """「允许实盘」这道总闸删了：没有它照样能启动，凭据和 confirm_live 才是闸。

    ``AITraderConfig`` 上已经没有 live_enabled 这个字段了，所以这里连传都不传；
    真传了会 TypeError（字段删干净了，不是留着不读）。
    """
    trader = make_trader(tmp_path, market_type="usdm")[0]

    ok, message = trader.start()

    assert ok, message
    trader.stop()

    with pytest.raises(TypeError):
        make_trader(tmp_path, live_enabled=True)


def test_status_payload_no_longer_has_live_enabled(tmp_path):
    """老前端可能还读 status.config.live_enabled：删掉就是删掉，不留个假值。"""
    trader = make_trader(tmp_path, market_type="usdm")[0]

    payload = json.loads(json.dumps(trader.status()))

    assert "live_enabled" not in payload["config"]
    assert set(payload["config"]) == {
        "network",
        "symbol",
        "strategy",
        "market_type",
        "timeframe",
        "depth",
        "max_order_quote",
        "max_position_quote",
    }


def test_start_requires_arming_conditions(tmp_path):
    """测试网和主网共用一条路径，只差那句确认。"""
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
    trader._stop_event.set()  # 相当于这几秒里按了停止
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


def test_a_nan_quote_never_reaches_the_order(tmp_path):
    """NaN 和任何数比大小都是假：金额、单笔上限、持仓上限三道检查全都判不出来。

    一条 quote=NaN 的决策会一路走到下单，提交 amount=NaN，还把 NaN 写进审计 JSONL——
    那不是合法 JSON，前端 JSON.parse 整块表格都会失败。
    """
    answer = json.dumps(
        {
            "action": "open_long",
            "symbol": "BTC/USDT",
            "quote": float("nan"),
            "price": 100.0,
            "stop_loss": 98.0,
            "confidence": 0.8,
        }
    )
    trader, _llm, trading, _risk = make_trader(tmp_path, answer=answer)

    record = trader.decide_once()

    assert record is not None and record["outcome"] == "reject"
    assert "有限数值" in record["reason"]
    assert trading.requests == []

    def no_constants(token: str) -> float:
        raise ValueError(f"非法 JSON 常量：{token}")

    for line in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines():
        # 审计行必须是标准 JSON：NaN 会让前端 JSON.parse 整块表格失败
        json.loads(line, parse_constant=no_constants)


def test_a_take_profit_on_the_wrong_side_is_rejected(tmp_path):
    """止损方向查了，止盈没查：多单止盈放在现价下方会被立刻触发。

    交易所要么拒单（止盈挂不上，持仓裸奔），要么当场按市价了结。
    """
    trader, _llm, trading, _risk = make_trader(
        tmp_path, answer=decision_json(take_profit=96.0)
    )

    record = trader.decide_once()

    assert record is not None and record["outcome"] == "reject"
    assert "止盈" in record["reason"]
    assert trading.requests == []


def test_a_stop_on_the_wrong_side_is_caught_without_a_model_price(tmp_path):
    """模型不给 price 时方向校验曾经整段跳过：多单止损 102、行情 100，挂上就成交。

    下单时 _submit 就是拿行情价当参考价的，校验得用同一个价。
    """
    answer = json.dumps(
        {
            "action": "open_long",
            "symbol": "BTC/USDT",
            "quote": 25.0,
            "stop_loss": 102.0,
            "take_profit": 110.0,
            "confidence": 0.9,
        }
    )
    trader, _llm, trading, _risk = make_trader(
        tmp_path, answer=answer, require_stop=True
    )

    record = trader.decide_once()

    assert record is not None and record["outcome"] == "reject"
    assert "多单止损" in record["reason"]
    assert trading.requests == []


def test_a_close_without_a_positive_quote_is_not_sent_as_an_empty_order(tmp_path):
    """平仓没查金额：quote=0/负数会变成 amount=0 / amount<0 的平仓单。

    交易所一拒，仓还在，审计里却写着 order。
    """
    for quote in (0.0, -25.0):
        trader, _llm, trading, _risk = make_trader(
            tmp_path,
            answer=decision_json(action="close_long", quote=quote, stop_loss=None),
            portfolio={"equity": 1000.0, "position_quote": 400.0, "price": 100.0},
        )

        record = trader.decide_once()

        assert record is not None and record["outcome"] == "reject", quote
        assert "平仓金额" in record["reason"]
        assert trading.requests == []
