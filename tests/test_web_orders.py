"""Order gating in the web layer.

Every order now leaves the building, so the switch has to be guarded: a signed
account, and — on mainnet only — an explicit acknowledgement. The rules live in
a plain function so they can be checked without an HTTP client.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from btrader.core.types import MarketType, Network
from btrader.web.app import _require_live_ready


def _detail(exc: HTTPException) -> str:
    return str(exc.detail)


def test_order_needs_credentials():
    with pytest.raises(HTTPException) as caught:
        _require_live_ready(
            signed=False,
            confirm_live=True,
            network=Network.TESTNET,
            stop_loss=None,
            take_profit=None,
        )
    assert caught.value.status_code == 400
    assert "API Key" in _detail(caught.value)


def test_mainnet_order_needs_explicit_confirmation():
    with pytest.raises(HTTPException) as caught:
        _require_live_ready(
            signed=True,
            confirm_live=False,
            network=Network.MAINNET,
            stop_loss=None,
            take_profit=None,
        )
    assert caught.value.status_code == 400
    assert "二次确认" in _detail(caught.value)


def test_testnet_order_needs_no_confirmation():
    """Rehearsing must not cost two clicks: the testnet has fake money."""
    _require_live_ready(
        signed=True,
        confirm_live=False,
        network=Network.TESTNET,
        stop_loss=None,
        take_profit=None,
    )


def test_futures_order_accepts_protection():
    """Futures stops are placed venue-side, so they do not block an entry."""
    _require_live_ready(
        signed=True,
        confirm_live=True,
        network=Network.MAINNET,
        market=MarketType.USDM,
        stop_loss=90.0,
        take_profit=110.0,
    )


def test_spot_order_refuses_protection():
    """Binance spot has no reduce-only stop; arming nothing would be worse."""
    for kwargs in ({"stop_loss": 90.0}, {"take_profit": 110.0}):
        with pytest.raises(HTTPException) as caught:
            _require_live_ready(
                signed=True,
                confirm_live=True,
                network=Network.TESTNET,
                market=MarketType.SPOT,
                stop_loss=kwargs.get("stop_loss"),
                take_profit=kwargs.get("take_profit"),
            )
        assert caught.value.status_code == 400
        assert "现货" in _detail(caught.value)


def test_order_passes_when_signed_and_confirmed():
    _require_live_ready(
        signed=True,
        confirm_live=True,
        network=Network.MAINNET,
        stop_loss=None,
        take_profit=None,
    )
