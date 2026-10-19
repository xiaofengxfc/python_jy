"""Live-order gating in the web layer.

Live orders share ``POST /api/orders`` with paper ones, so the switch has to
be guarded: a signed account, an explicit acknowledgement, and no protective
exit that the exchange side cannot honour yet. The rules live in a plain
function so they can be checked without an HTTP client.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from btrader.web.app import _require_live_ready


def _detail(exc: HTTPException) -> str:
    return str(exc.detail)


def test_live_order_needs_credentials():
    with pytest.raises(HTTPException) as caught:
        _require_live_ready(
            signed=False,
            confirm_live=True,
            stop_loss=None,
            take_profit=None,
        )
    assert caught.value.status_code == 400
    assert "API Key" in _detail(caught.value)


def test_live_order_needs_explicit_confirmation():
    with pytest.raises(HTTPException) as caught:
        _require_live_ready(
            signed=True,
            confirm_live=False,
            stop_loss=None,
            take_profit=None,
        )
    assert caught.value.status_code == 400
    assert "二次确认" in _detail(caught.value)


def test_live_order_refuses_unenforceable_protection():
    """Silently dropping a stop would leave the user unhedged."""
    for kwargs in ({"stop_loss": 90.0}, {"take_profit": 110.0}):
        with pytest.raises(HTTPException) as caught:
            _require_live_ready(
                signed=True,
                confirm_live=True,
                stop_loss=kwargs.get("stop_loss"),
                take_profit=kwargs.get("take_profit"),
            )
        assert caught.value.status_code == 400
        assert "止损/止盈" in _detail(caught.value)


def test_live_order_passes_when_signed_and_confirmed():
    _require_live_ready(
        signed=True,
        confirm_live=True,
        stop_loss=None,
        take_profit=None,
    )
