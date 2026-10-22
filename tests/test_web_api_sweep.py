"""接口扫描里定下来的状态码：错的交易对、错的订单号、空的删除目标。

这些都不是「服务暂时不可用」，却都曾回 503 或直接静默成功。测的是映射本身，
不发网络请求。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from fastapi import HTTPException

from btrader.strategy.base import IntParam, Strategy
from btrader.strategy.candidates import CandidateStore
from btrader.web.app import _cancel_http_error, _unknown_symbol, _venue_http_error
from btrader.web.routes import require_credential_market
from btrader.web.routes_lab import CandidateGeneratePayload, LabController


class VenueError(RuntimeError):
    """Stand-in for exchange.rest.ExchangeError."""


# --------------------------------------------------------------------- symbols
def test_unknown_symbol_markers_are_recognised():
    assert _unknown_symbol(VenueError("binanceusdm does not have market symbol FAKE/USDT"))
    assert _unknown_symbol(VenueError("binance Invalid symbol."))
    assert _unknown_symbol(VenueError("binanceusdm market symbol not found"))
    assert not _unknown_symbol(VenueError("Connection refused"))


def test_unknown_symbol_is_a_400_not_a_503():
    error = _venue_http_error(
        VenueError("binanceusdm does not have market symbol FAKE/USDT"),
        "无法获取最新价",
        "FAKE/USDT",
    )
    assert error.status_code == 400
    assert "FAKE/USDT" in str(error.detail)


def test_a_real_outage_stays_503():
    error = _venue_http_error(VenueError("Connection refused"), "无法获取最新价", "BTC/USDT")
    assert error.status_code == 503
    assert "无法获取最新价" in str(error.detail)


def test_mirror_fallback_still_answers_400_for_a_bad_symbol(monkeypatch):
    """两个行情源都查不到时，状态码由原始错误定：错交易对是 400，不是 503。"""
    from btrader.core.types import MarketType
    from btrader.web import app as web_app

    monkeypatch.setattr(web_app.mirror, "supports", lambda market, timeframe: True)

    def _boom(*args, **kwargs):
        raise RuntimeError("备用行情 HTTP 400：FAKE_USDT")

    monkeypatch.setattr(web_app.mirror, "fetch_ohlcv", _boom)
    with pytest.raises(HTTPException) as caught:
        web_app._mirror_candles(
            MarketType.USDM,
            "FAKE/USDT",
            "1h",
            100,
            None,
            VenueError("binanceusdm does not have market symbol FAKE/USDT"),
        )
    assert caught.value.status_code == 400
    assert "FAKE/USDT" in str(caught.value.detail)


def test_mirror_fallback_keeps_503_for_a_network_failure(monkeypatch):
    from btrader.core.types import MarketType
    from btrader.web import app as web_app

    monkeypatch.setattr(web_app.mirror, "supports", lambda market, timeframe: True)

    def _boom(*args, **kwargs):
        raise RuntimeError("备用行情 HTTP 400：FAKE_USDT")

    monkeypatch.setattr(web_app.mirror, "fetch_ohlcv", _boom)
    with pytest.raises(HTTPException) as caught:
        web_app._mirror_candles(
            MarketType.USDM,
            "BTC/USDT",
            "1h",
            100,
            None,
            VenueError("fapi.binance.com timeout"),
        )
    assert caught.value.status_code == 503


# ---------------------------------------------------------------------- cancel
def test_cancel_with_a_non_numeric_id_is_a_400():
    error = _cancel_http_error("nope-order-id", VenueError('{"code":-1102}'))
    assert error.status_code == 400
    assert "订单号" in str(error.detail)


def test_cancel_of_an_unknown_order_is_a_404():
    error = _cancel_http_error("123456789", VenueError("binanceusdm Unknown order sent."))
    assert error.status_code == 404
    assert "123456789" in str(error.detail)


def test_cancel_failure_for_another_reason_stays_503():
    error = _cancel_http_error("123456789", VenueError("timed out"))
    assert error.status_code == 503


# ----------------------------------------------------------------- credentials
def test_deleting_credentials_needs_a_market():
    for value in ("", "   "):
        with pytest.raises(HTTPException) as caught:
            require_credential_market(value)
        assert caught.value.status_code == 400
        assert "市场" in str(caught.value.detail)


def test_deleting_credentials_keeps_the_market_it_was_given():
    assert require_credential_market("usdm") == "usdm"


# ---------------------------------------------------------------------- 实验室
class _Toy(Strategy):
    key = "toy"
    name = "玩具"
    params_spec = [IntParam("period", "周期", 10, 2, 60)]

    def generate(self, df, params):
        return pd.DataFrame({"target": 0.0}, index=df.index)


class _Strategies:
    keys = ["toy"]

    def get(self, key: str):
        if key != "toy":
            raise KeyError(f"未知策略: {key}")
        return _Toy()


class _AI:
    settings = SimpleNamespace(model="fake-model")

    def ask(self, prompt: str, **kwargs) -> dict:
        return {"ok": False, "text": "", "error": "还没有填写 API Key。", "tokens": 0}


def _controller(tmp_path: Path, ai=None) -> LabController:
    ctx = SimpleNamespace(
        settings=SimpleNamespace(ai=SimpleNamespace(enabled=True)),
        ai=ai or _AI(),
        strategies=_Strategies(),
    )
    return LabController(lambda: SimpleNamespace(ctx=ctx), CandidateStore(tmp_path))


def test_generate_refuses_an_unknown_base_key(tmp_path: Path):
    controller = _controller(tmp_path)
    with pytest.raises(HTTPException) as caught:
        controller.generate(CandidateGeneratePayload(base_key="no_such_key"))
    assert caught.value.status_code == 400
    assert "no_such_key" in str(caught.value.detail)


def test_generate_reports_a_missing_api_key_as_a_400(tmp_path: Path):
    controller = _controller(tmp_path)
    with pytest.raises(HTTPException) as caught:
        controller.generate(CandidateGeneratePayload(base_key="toy", instruction="随便改改"))
    assert caught.value.status_code == 400
    assert "API Key" in str(caught.value.detail)
