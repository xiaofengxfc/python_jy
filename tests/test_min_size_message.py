"""最小下单量的文案按市场说人话：现货不许再冒出「张」。

实测原话：
「风控拒绝：下单量 0.00000417 低于最小下单量 0.0001。（1 张 = 1 ETH）」——
现货根本没有「张」这个东西，1 币被当成 1 张说出来，用户看不懂该下多少。

改后：

- 现货（和 U 本位这种「按币」的市场）：直接说「最小下单量 0.0001 ETH」；
- 币本位（按张）：仍然说清 1 张 = 多少 USD，那条信息是真的、也是必要的。
"""

from __future__ import annotations

import pytest

from btrader.core.config import AppSettings
from btrader.core.contracts import ContractSpec
from btrader.core.storage import Database
from btrader.core.types import MarketType, Network
from btrader.engine.risk import RiskManager
from btrader.engine.trading import OrderRejected, OrderRequest, TradingService

from fakes import FakeVenue


def _trading(tmp_path, market: MarketType, spec: ContractSpec) -> tuple[TradingService, Database]:
    venue = FakeVenue(
        balances={MarketType.SPOT: {"USDT": 10_000.0, "ETH": 0.0}},
        spec_overrides={market: spec},
    )
    venue._last["ETH/USDT"] = 3_000.0
    venue._last["ETH/USD:BTC"] = 3_000.0
    settings = AppSettings(network=Network.TESTNET.value)
    db = Database(tmp_path / "min.db")
    trading = TradingService(
        settings=settings,
        rest=venue,
        risk=RiskManager(settings.risk),
        db=db,
        notifier=None,
    )
    return trading, db


ETH_SPOT = ContractSpec(
    contract_size=1.0,
    inverse=False,
    base="ETH",
    quote="USDT",
    min_amount=0.0001,
)


def test_spot_minimum_names_the_coin_and_never_says_zhang(tmp_path):
    """现货：「最小下单量 0.0001 ETH」，一个「张」字都不许有。"""
    trading, db = _trading(tmp_path, MarketType.SPOT, ETH_SPOT)
    try:
        with pytest.raises(OrderRejected) as caught:
            trading.exchange_amount(MarketType.SPOT, "ETH/USDT", 0.00000417, 3_000.0)
        message = str(caught.value)
        assert "最小下单量 0.0001 ETH" in message, message
        assert "张" not in message, message
    finally:
        db.close()


def test_spot_minimum_reaches_the_user_through_submit(tmp_path):
    """用户看到的那一句（风控调用链那一路）同样不许出现「张」。"""
    trading, db = _trading(tmp_path, MarketType.SPOT, ETH_SPOT)
    try:
        with pytest.raises(OrderRejected) as caught:
            trading.submit(
                OrderRequest(
                    symbol="ETH/USDT",
                    side="buy",
                    order_type="market",
                    amount=0.00000417,
                    price=3_000.0,
                    market_type=MarketType.SPOT,
                )
            )
        message = str(caught.value)
        assert "最小下单量 0.0001 ETH" in message, message
        assert "张" not in message, message
    finally:
        db.close()


def test_coinm_keeps_explaining_what_one_contract_is_worth(tmp_path):
    """币本位是「张」，那句话必须留着：1 张 = 100 USD。"""
    spec = ContractSpec(
        contract_size=100.0,
        inverse=True,
        base="BTC",
        quote="USD",
        settle="BTC",
        min_amount=1.0,
    )
    trading, db = _trading(tmp_path, MarketType.COINM, spec)
    try:
        with pytest.raises(OrderRejected) as caught:
            trading.exchange_amount(MarketType.COINM, "BTC/USD:BTC", 0.001, 50_000.0)
        message = str(caught.value)
        assert "最小下单量" in message, message
        assert "1 张 = 100 USD" in message, message
    finally:
        db.close()