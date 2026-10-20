"""Shared pytest fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import make_candles  # noqa: E402


@pytest.fixture
def candles() -> pd.DataFrame:
    return make_candles()


@pytest.fixture(autouse=True)
def no_gateway_probe(monkeypatch):
    """Keep the suite independent of whether a gateway runs on this machine.

    The notifier probes the gateway port before sending (fast failure instead of
    a 30 s CLI stall). That probe is real I/O, so leaving it on would make tests
    pass or fail depending on the developer's own OpenClaw state. Tests that are
    *about* the probe re-enable it explicitly.
    """
    from btrader import installer
    from btrader.notify import WeChatNotifier

    monkeypatch.setattr(WeChatNotifier, "_preflight", lambda self: None)
    # Same reasoning for the login probe: it posts to WeChat's API, so the suite
    # must not depend on this machine's token state. Patched at the source (the
    # installer helper) so the notifier's caching logic still runs.
    monkeypatch.setattr(
        installer,
        "wechat_session_state",
        lambda account="": {"state": "unknown", "detail": "", "account": account},
    )
