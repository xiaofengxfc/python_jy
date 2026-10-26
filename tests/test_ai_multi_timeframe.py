"""多周期分析与独立决策节奏的离线测试。

覆盖三件事：
1. digest_set 把主周期与参考周期拼成一段，参考周期不列逐根明细；
2. 节拍（cadence）与主周期解耦：同一根 K 线之内按节拍可以决策多次；
3. 设置层校验：未知节奏、非法周期、参考周期等于主周期都要 400。

全部离线：不联网、不读密钥环，审计文件落在 tmp_path。
"""

from __future__ import annotations

import json

import pandas as pd
import pytest
from fastapi import HTTPException

from btrader import ai
from btrader.core.config import AIAutoSettings
from btrader.core.types import MarketType
from btrader.engine.ai_trader import AITrader, AITraderConfig
from btrader.web import routes


# ------------------------------------------------------------------- helpers
def make_frame(*, base: float = 100.0, bars: int = 30, step_ms: int = 900_000) -> pd.DataFrame:
    rows = []
    price = base
    for index in range(bars):
        price += 0.4
        rows.append(
            {
                "timestamp": 1_700_000_000_000 + index * step_ms,
                "open": price - 0.2,
                "high": price + 0.6,
                "low": price - 0.6,
                "close": price,
                "volume": 10.0 + index,
            }
        )
    return pd.DataFrame(rows)


def decision_json() -> str:
    return json.dumps(
        {
            "action": "open_long",
            "symbol": "BTC/USDT",
            "quote": 25.0,
            "price": 100.0,
            "stop_loss": 98.0,
            "take_profit": 104.0,
            "confidence": 0.8,
            "ttl_sec": 120,
            "rationale": "多周期同向",
        },
        ensure_ascii=False,
    )


class FakeLLM:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def ask(self, prompt: str, **kwargs) -> dict:
        self.prompts.append(prompt)
        return {"ok": True, "text": decision_json()}


class FakeTrading:
    def __init__(self) -> None:
        self.requests: list = []

    def submit(self, req):
        self.requests.append(req)

        class Result:
            order = {"id": f"order-{len(self.requests)}"}

        return Result()


class FakeRisk:
    def check_order(self, quote, current_position_quote, added_quote, **kwargs):
        class Verdict:
            allowed = True
            reason = ""

        return Verdict()


def make_trader(tmp_path, *, frames=None, clock=None, **cfg) -> AITrader:
    cfg.setdefault("enabled", True)
    cfg.setdefault("confirm_live", True)
    cfg.setdefault("require_stop", False)
    cfg.setdefault("poll_sec", 3600.0)
    config = AITraderConfig(**cfg)
    payload = {"equity": 1000.0, "position_quote": 0.0, "price": 100.0}
    trader = AITrader(
        config,
        trading=FakeTrading(),
        llm=FakeLLM(),
        frames_provider=(lambda: dict(frames)) if frames is not None else None,
        frame_provider=(None if frames is not None else make_frame),
        portfolio_provider=lambda: dict(payload),
        audit_path=tmp_path / "audit.jsonl",
        clock=clock or (lambda: 1_800_000_000.0),
        risk=FakeRisk(),
    )
    return trader


# -------------------------------------------------------------------- digest
def test_digest_set_puts_primary_first_and_keeps_reference_short():
    frames = {
        "15m": make_frame(bars=30),
        "4h": make_frame(bars=30, step_ms=14_400_000, base=200.0),
    }
    text = ai.digest_set(frames, primary="15m", label="BTC/USDT", reference_timeframes=("4h",))

    assert "【多周期】" in text
    assert text.index("主周期 15m") < text.index("参考周期 4h")
    # 主周期列逐根明细（每根 …），参考周期只给结论。
    primary_part, reference_part = text.split("参考周期 4h", 1)
    assert "（每根 15m）" in primary_part
    assert "（每根 4h）" not in reference_part


def test_digest_set_marks_missing_reference_as_unavailable():
    text = ai.digest_set(
        {"15m": make_frame()},
        primary="15m",
        label="BTC/USDT",
        reference_timeframes=("4h",),
    )
    assert "参考周期 4h" in text and "数据不可用" in text


def test_digest_set_without_primary_is_not_guessed():
    text = ai.digest_set({"4h": make_frame()}, primary="15m", label="BTC/USDT")
    assert "数据不足" in text


def test_digest_set_without_references_is_single_timeframe():
    text = ai.digest_set({"15m": make_frame()}, primary="15m", label="BTC/USDT")
    assert "主周期 15m" in text
    # 开头的「参考周期只用来判断…」是说明文字，不是一段摘要。
    assert "【BTC/USDT 参考周期" not in text


# ------------------------------------------------------------------- cadence
def test_fixed_cadence_seconds_and_mode(tmp_path):
    trader = make_trader(tmp_path, timeframe="1h", cadence="15m")
    assert trader._cadence_mode() == "15m"
    assert trader._cadence_seconds() == 900
    status = trader.status()
    assert status["cadence"]["mode"] == "15m"
    assert status["cadence"]["seconds"] == 900
    assert status["cadence"]["bar_seconds"] == 3600


def test_unknown_cadence_falls_back_to_bar(tmp_path):
    trader = make_trader(tmp_path, timeframe="1h", cadence="7s")
    assert trader._cadence_mode() == "bar"
    assert trader._cadence_seconds() == 3600


def test_same_slot_decides_once(tmp_path):
    trader = make_trader(tmp_path, timeframe="1h", cadence="15m")
    now = 1_800_000_000.0
    trader.clock = lambda: now
    trader.decide_once()
    assert trader.decide_once() is None


def test_next_slot_decides_again_on_the_same_candle(tmp_path):
    """节奏独立的核心：主周期 1h 一根没换，15m 节拍照样每片决策一次。"""
    trader = make_trader(tmp_path, timeframe="1h", cadence="15m")
    clock = {"now": 1_800_000_000.0}
    trader.clock = lambda: clock["now"]

    first = trader.decide_once()
    assert first is not None
    assert first["cadence"] == "15m"

    clock["now"] += 900
    second = trader.decide_once()
    assert second is not None
    assert second["bar_ts"] == first["bar_ts"]
    assert second["slot"] > first["slot"]


def test_bar_cadence_still_dedupes_per_candle(tmp_path):
    trader = make_trader(tmp_path, timeframe="1h", cadence="bar")
    clock = {"now": 1_800_000_000.0}
    trader.clock = lambda: clock["now"]
    assert trader.decide_once() is not None
    clock["now"] += 900
    # 同一根 K 线：bar 模式下不再跑。
    assert trader.decide_once() is None


def test_multi_frame_digest_reaches_the_prompt(tmp_path):
    frames = {
        "15m": make_frame(),
        "4h": make_frame(bars=30, step_ms=14_400_000, base=200.0),
    }
    trader = make_trader(tmp_path, timeframe="15m", frames=frames, timeframes=("4h",))
    record = trader.decide_once()
    assert record is not None
    prompts = "\n".join(trader.llm.prompts)
    assert "参考周期 4h" in prompts
    assert record["timeframes"] == ["4h"]


def test_record_carries_cadence_metadata(tmp_path):
    trader = make_trader(tmp_path, timeframe="1h", cadence="30m", timeframes=("4h",))
    record = trader.decide_once()
    assert record["cadence"] == "30m"
    assert record["timeframes"] == ["4h"]
    assert isinstance(record["slot"], int) and record["slot"] > 0


# -------------------------------------------------------------------- settings
def test_apply_ai_auto_accepts_valid_cadence_and_references():
    target = AIAutoSettings()
    routes._apply_ai_auto(
        target,
        {"cadence": "15m", "timeframes": ["4h", "1h", "4h"], "timeframe": "15m"},
    )
    assert target.cadence == "15m"
    assert target.timeframes == ["4h", "1h"]


def test_apply_ai_auto_rejects_unknown_cadence():
    with pytest.raises(HTTPException) as caught:
        routes._apply_ai_auto(AIAutoSettings(), {"cadence": "7s"})
    assert caught.value.status_code == 400
    assert "决策节奏" in caught.value.detail


def test_apply_ai_auto_rejects_unknown_reference():
    with pytest.raises(HTTPException) as caught:
        routes._apply_ai_auto(AIAutoSettings(), {"timeframes": ["4h", "5x"]})
    assert caught.value.status_code == 400
    assert "5x" in caught.value.detail


def test_apply_ai_auto_rejects_too_many_references():
    with pytest.raises(HTTPException) as caught:
        routes._apply_ai_auto(AIAutoSettings(), {"timeframes": ["4h", "1h", "2h"]})
    assert caught.value.status_code == 400
    assert "最多 2 个" in caught.value.detail


def test_apply_ai_auto_rejects_reference_equal_to_primary():
    with pytest.raises(HTTPException) as caught:
        routes._apply_ai_auto(
            AIAutoSettings(), {"timeframe": "1h", "timeframes": ["1h"]}
        )
    assert caught.value.status_code == 400
    assert "不能等于主周期" in caught.value.detail


def test_multi_frames_skips_broken_reference(monkeypatch):
    def fake_frame(state, ctx, market, symbol, timeframe, limit):
        if timeframe == "4h":
            raise RuntimeError("拉不到")
        return make_frame()

    monkeypatch.setattr(routes, "_market_frame", fake_frame)
    frames = routes._multi_frames(
        object(), object(), MarketType.SPOT, "BTC/USDT", "15m", ["4h", "1h"]
    )
    assert set(frames) == {"15m", "1h"}
