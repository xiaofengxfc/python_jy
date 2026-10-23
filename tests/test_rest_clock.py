"""本机时钟偏了不该让所有签名请求一起失败。

实测：本机比币安测试网快 1.3 秒，-1021 Timestamp for this request was 1000ms
ahead of the server 把所有签名请求都拒了。界面显示的是「无法读取当前持仓，
为避免重复开仓已拒绝本次下单。」——用户看不出是时钟的问题。这个版本的
ccxt python 客户端没有 adjustForTimeDifference，所以在 fetch 这一层兜一次。
"""

from __future__ import annotations

from btrader.core.types import Network
from btrader.exchange.rest import BinanceREST, _is_clock_drift

DRIFT = 'binance code -1021 Timestamp for this request was 1000ms ahead of the server time'
OTHER = 'binance code -2015 Invalid API-key'


class _Client:
    """只实现 fetch 的替身：第一次故意报时钟错，校准后就正常。"""

    def __init__(self, errors):
        self.errors = list(errors)
        self.calls = []
        self.synced = 0

    def fetch(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.errors:
            raise self.errors.pop(0)
        return {"ok": True}

    def load_time_difference(self, params=None):
        self.synced += 1
        return 1363


def test_clock_drift_is_recognised():
    assert _is_clock_drift(RuntimeError(DRIFT)) is True
    assert _is_clock_drift(RuntimeError(OTHER)) is False


def test_a_drifting_clock_is_pulled_back_in_and_retried():
    client = _Client([RuntimeError(DRIFT)])
    BinanceREST._install_clock_guard(client)
    assert client.fetch("https://example/api", "GET", {}, None) == {"ok": True}
    assert client.synced == 1
    assert len(client.calls) == 2


def test_other_errors_are_not_retried():
    client = _Client([RuntimeError(OTHER)])
    BinanceREST._install_clock_guard(client)
    try:
        client.fetch("https://example/api", "GET", {}, None)
    except RuntimeError as exc:
        assert "-2015" in str(exc)
    else:  # pragma: no cover - 上面必须抛
        raise AssertionError("非时钟错误不该被吞掉")
    assert client.synced == 0
    assert len(client.calls) == 1


def test_the_guard_is_installed_once_per_client():
    client = _Client([])
    BinanceREST._install_clock_guard(client)
    wrapped = client.fetch
    BinanceREST._install_clock_guard(client)
    assert client.fetch is wrapped


def test_an_object_that_refuses_the_guard_is_left_alone():
    """替身可能不允许挂属性，别为此把整条路径搞挂。"""

    class _Frozen:
        __slots__ = ()

        def fetch(self, *args, **kwargs):
            return None

    BinanceREST._install_clock_guard(_Frozen())  # 不抛就算过
    assert BinanceREST(Network.TESTNET).has_credentials is False
