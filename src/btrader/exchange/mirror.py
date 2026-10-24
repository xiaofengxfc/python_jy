"""备用合约行情：Gate.io 公开接口。

币安合约域名（``fapi.binance.com`` / ``dapi.binance.com``）在部分网络
会被 DNS 污染或丢包，界面就只剩「无法查看合约 K 线」。现货有
``data-api.binance.vision`` 这个公共镜像，合约没有，所以在币安合约不可达时
图表改用 Gate.io 永续公开 K 线。

三条边界：

* 只用于图表和回测取数，不用于下单、仓位和盈亏计算。
* 返回的数据带 ``source``，界面必须显示来源，否则会把 Gate 的价格当成币安。
* 只在币安失败后才试，且不掩盖原因：两条错误都返回。

Gate 的公开行情不需要鉴权，K 线接口是
``/api/v4/futures/{usdt|btc}/candlesticks?contract=BTC_USDT&interval=1h&limit=400``，
返回 ``[{"t": 秒, "o","h","l","c","v"}]``（数值是字符串）。
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request

from ..core.types import MarketType
from .rest import ExchangeError

log = logging.getLogger(__name__)

BASE_URL = "https://api.gateio.ws/api/v4"
TIMEOUT = 8.0

# Gate 的周期名与项目内部一一对应。这里只列真正支持的那些。
_INTERVALS = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1h",
    "4h": "4h",
    "8h": "8h",
    "1d": "1d",
    "1w": "7d",
}

_SOURCE_LABEL = "Gate.io 永续"


def source_label() -> str:
    return _SOURCE_LABEL


def supports(market: MarketType, timeframe: str) -> bool:
    return market.is_futures and timeframe in _INTERVALS


_DELIVERY = re.compile(r"-\d{6}$")


def is_delivery(symbol: str) -> bool:
    """交割合约：结算币段后面还挂着到期日（BTC/USDT:USDT-250328）。"""
    return bool(_DELIVERY.search(str(symbol or "").partition(":")[2]))


def contract_for(market: MarketType, symbol: str) -> str:
    """``BTC/USDT`` → ``BTC_USDT``。反向量合约 ``BTC/USD:BTC`` → ``BTC_USD``。"""
    if is_delivery(symbol):
        # Gate 的 usdt/btc 两路都是**永续**。交割合约切掉到期日之后正好等于同名永续，于是图上画的是永续行情、界面什么都不说：
        # 贴水 / 升水与到期日全被抹掉。这里直接拒，让调用方把「币安不可用」原样说出来。
        raise ExchangeError(f"备用行情只有永续合约，{symbol} 是交割合约，没有对应行情")
    base = symbol.split(":")[0].replace("/", "_")
    if market is MarketType.COINM:
        # 反向量合约以币计价；Binance 写作 BTC/USD:BTC。
        return base if base.endswith("_USD") else f"{symbol.split('/')[0]}_USD"
    return base


def fetch_ohlcv(
    market: MarketType,
    symbol: str,
    timeframe: str,
    *,
    since: int | None = None,
    limit: int = 400,
) -> list[list[float]]:
    """Return ``[[ms, open, high, low, close, volume], ...]`` ascending by time."""
    if not supports(market, timeframe):
        raise ExchangeError(f"备用行情不支持 {market.value} {timeframe}")
    contract = contract_for(market, symbol)
    path = "usdt" if market is MarketType.USDM else "btc"
    params = [
        f"contract={urllib.parse.quote(contract)}",
        f"interval={_INTERVALS[timeframe]}",
        f"limit={max(1, min(int(limit), 1000))}",
    ]
    if since:
        params.append(f"from={int(since) // 1000}")
    url = f"{BASE_URL}/futures/{path}/candlesticks?{'&'.join(params)}"
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise ExchangeError(f"备用行情 HTTP {exc.code}：{contract}") from exc
    except Exception as exc:
        raise ExchangeError(f"备用行情不可用：{exc}") from exc

    rows: list[list[float]] = []
    for item in payload if isinstance(payload, list) else []:
        try:
            rows.append(
                [
                    int(item["t"]) * 1000,
                    float(item["o"]),
                    float(item["h"]),
                    float(item["l"]),
                    float(item["c"]),
                    float(item.get("v") or 0.0),
                ]
            )
        except (KeyError, TypeError, ValueError):
            continue
    rows.sort(key=lambda row: row[0])
    if since:
        rows = [row for row in rows if row[0] >= int(since)]
    return rows[-limit:]
