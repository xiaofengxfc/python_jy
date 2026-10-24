"""备用行情只有永续：交割合约不能拿同名永续的 K 线冒充。

Gate 的 usdt / btc 两路都是永续。交割合约的 ccxt 符号是
``BTC/USDT:USDT-250328``，切掉到期日之后正好等于同名永续，于是图上会画出永续
行情、界面一个字都不说：贴水/升水与到期日全被抹掉，用户以为看的是自己那张合约。
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from btrader.core.types import MarketType
from btrader.exchange import mirror
from btrader.exchange.rest import ExchangeError
from btrader.web import app as web_app

DELIVERY = "BTC/USDT:USDT-250328"
PERP = "BTC/USDT:USDT"


def test_a_delivery_symbol_is_recognised() -> None:
    assert mirror.is_delivery(DELIVERY)
    assert mirror.is_delivery("BTC/USD:BTC-250328")
    assert not mirror.is_delivery(PERP)
    assert not mirror.is_delivery("BTC/USDT")


def test_a_delivery_contract_is_refused_instead_of_mapped_to_the_perp() -> None:
    # 永续写法照旧
    assert mirror.contract_for(MarketType.USDM, PERP) == "BTC_USDT"
    assert mirror.contract_for(MarketType.COINM, "BTC/USD:BTC") == "BTC_USD"

    with pytest.raises(ExchangeError) as caught:
        mirror.contract_for(MarketType.USDM, DELIVERY)
    assert "交割" in str(caught.value)


def test_the_chart_reports_the_primary_failure_instead_of_perp_rows() -> None:
    """图表的回落路径：备用源拒绝之后要把币安那条原始错误说出来。"""
    with pytest.raises(HTTPException) as caught:
        web_app._mirror_candles(
            MarketType.USDM, DELIVERY, "1h", 100, None, RuntimeError("币安不可用")
        )
    detail = str(caught.value.detail)
    assert "币安不可用" in detail
    assert "交割" in detail
