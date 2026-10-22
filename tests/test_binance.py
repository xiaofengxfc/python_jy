"""Binance-specific behaviour: credential isolation and error translation."""

from __future__ import annotations

import ccxt
import pytest

from btrader.core.credentials import CredentialStore, Credentials
from btrader.core.types import MarketType, Network
from btrader.exchange.rest import _binance_code, _message


class FakeKeyring:
    def __init__(self) -> None:
        self.data: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, user: str) -> str | None:
        return self.data.get((service, user))

    def set_password(self, service: str, user: str, value: str) -> None:
        self.data[(service, user)] = value

    def delete_password(self, service: str, user: str) -> None:
        self.data.pop((service, user), None)


@pytest.fixture()
def store() -> CredentialStore:
    return CredentialStore(backend=FakeKeyring())


def test_futures_markets_do_not_share_a_credential_slot(store: CredentialStore) -> None:
    store.save(Network.MAINNET, MarketType.USDM, Credentials("USDM_KEY_1111", "USDM_SECRET"))
    store.save(Network.MAINNET, MarketType.COINM, Credentials("COINM_KEY_2222", "COINM_SECRET"))

    usdm = store.load(Network.MAINNET, MarketType.USDM)
    coinm = store.load(Network.MAINNET, MarketType.COINM)

    assert usdm.api_key == "USDM_KEY_1111"
    assert coinm.api_key == "COINM_KEY_2222"


def test_spot_and_futures_keys_stay_separate(store: CredentialStore) -> None:
    store.save(Network.MAINNET, MarketType.SPOT, Credentials("SPOT_KEY_AAAA", "SPOT_SECRET"))
    store.save(Network.MAINNET, MarketType.USDM, Credentials("USDM_KEY_BBBB", "USDM_SECRET"))

    assert store.load(Network.MAINNET, MarketType.SPOT).api_key == "SPOT_KEY_AAAA"
    assert store.load(Network.MAINNET, MarketType.USDM).api_key == "USDM_KEY_BBBB"


def test_legacy_shared_futures_key_is_adopted(store: CredentialStore) -> None:
    # A key saved by an older build lived in the shared "futures" slot.
    store._backend.set_password("biTrader-credentials:mainnet:futures", "api_key", "OLD_KEY")
    store._backend.set_password("biTrader-credentials:mainnet:futures", "api_secret", "OLD_SECRET")

    for market in (MarketType.USDM, MarketType.COINM):
        creds = store.load(Network.MAINNET, market)
        assert creds.api_key == "OLD_KEY", f"{market.value} 应当继承旧槽位的密钥"


def test_delete_clears_legacy_slot_too(store: CredentialStore) -> None:
    store._backend.set_password("biTrader-credentials:mainnet:futures", "api_key", "OLD_KEY")
    store._backend.set_password("biTrader-credentials:mainnet:futures", "api_secret", "OLD_SECRET")
    store.save(Network.MAINNET, MarketType.USDM, Credentials("NEW_KEY", "NEW_SECRET"))

    store.delete(Network.MAINNET, MarketType.USDM)

    assert not store.load(Network.MAINNET, MarketType.USDM).is_set


def test_market_switch_drops_the_previous_markets_key(tmp_path) -> None:
    """A spot key must not be sent to a futures endpoint (Binance answers -2015)."""
    from pathlib import Path

    from btrader import paths

    original = paths.config_file
    paths.config_file = lambda: Path(tmp_path) / "config.json"  # type: ignore[assignment]
    try:
        from btrader.context import AppContext

        ctx = AppContext()
        ctx.credentials = CredentialStore(backend=FakeKeyring())
        ctx.save_credentials("SPOT_KEY_AAAA", "SPOT_SECRET")
        assert ctx.creds.api_key == "SPOT_KEY_AAAA"

        ctx.set_market_type(MarketType.USDM)

        assert ctx.creds.is_set is False, "切到合约后不应继续使用现货密钥"
        assert ctx.rest.has_credentials is False
    finally:
        paths.config_file = original  # type: ignore[assignment]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('binance {"code":-2015,"msg":"Invalid API-key."}', "-2015"),
        ('binance {"code":-2008,"msg":"Invalid Api-Key ID."}', "-2008"),
        ("binance {'code': -1021, 'msg': 'x'}", "-1021"),
        ("network timeout", None),
    ],
)
def test_binance_code_is_extracted(text: str, expected: str | None) -> None:
    assert _binance_code(RuntimeError(text)) == expected


def test_invalid_key_message_explains_ip_whitelist_and_permissions() -> None:
    exc = ccxt.AuthenticationError(
        'binance {"code":-2015,"msg":"Invalid API-key, IP, or permissions for action."}'
    )
    message = _message(exc)
    assert "IP" in message
    assert "权限" in message
    assert "-2015" in message


def test_unknown_code_falls_back_to_generic_authentication_text() -> None:
    exc = ccxt.AuthenticationError('binance {"code":-99999,"msg":"mystery"}')
    assert _message(exc).startswith("认证失败")


class _RecordingClient:
    """A ccxt stand-in that records what the wrapper sends."""

    def __init__(self, open_orders=None) -> None:
        self.calls: list[tuple] = []
        self.open_orders = open_orders if open_orders is not None else []
        self.canceled: list[str] = []

    def load_markets(self):
        return {}

    def price_to_precision(self, symbol, price):
        return f"{price:.2f}"

    def create_order(self, symbol, type, side, amount, price, params=None):
        self.calls.append((symbol, type, side, amount, price, params))
        return {"id": "order-1", "status": "open"}

    def fetch_open_orders(self, symbol):
        return self.open_orders

    def cancel_order(self, order_id, symbol, params=None):
        self.canceled.append(order_id)
        return {"id": order_id}


def _rest_with(client) -> BinanceREST:
    from btrader.exchange.rest import BinanceREST

    rest = BinanceREST(Network.MAINNET, Credentials("key", "secret"))
    rest._clients[MarketType.USDM] = client
    rest._clients[MarketType.SPOT] = client
    return rest


def test_stop_market_uses_stop_loss_price_and_reduce_only() -> None:
    """`triggerPrice` alone stays a MARKET order; `stopLossPrice` sets the type."""
    client = _RecordingClient()
    rest = _rest_with(client)
    rest.create_stop_market(MarketType.USDM, "BTC/USDT:USDT", "sell", 0.5, 80_000.0)

    _symbol, order_type, side, amount, price, params = client.calls[0]
    assert order_type == "market"
    assert side == "sell"
    assert amount == 0.5
    assert price is None, "a stop-market order must not carry a limit price"
    assert params["stopLossPrice"] == pytest.approx(80_000.0)
    assert params["reduceOnly"] is True
    assert "takeProfitPrice" not in params


def test_take_profit_market_sends_only_the_target_price() -> None:
    client = _RecordingClient()
    rest = _rest_with(client)
    rest.create_take_profit_market(
        MarketType.USDM, "BTC/USDT:USDT", "sell", 0.5, 90_000.0
    )

    _symbol, _type, _side, _amount, _price, params = client.calls[0]
    assert params["takeProfitPrice"] == pytest.approx(90_000.0)
    assert "stopLossPrice" not in params, "a second trigger would make it a stop"
    assert params["reduceOnly"] is True


def test_spot_has_no_venue_side_conditional_orders() -> None:
    from btrader.exchange.rest import ExchangeError

    rest = _rest_with(_RecordingClient())
    with pytest.raises(ExchangeError, match="现货"):
        rest.create_stop_market(MarketType.SPOT, "BTC/USDT", "sell", 0.5, 80_000.0)


def test_cancel_symbol_orders_survives_a_vanished_order() -> None:
    """A stop already filled must not block closing the rest of the symbol."""

    class _Flaky(_RecordingClient):
        def cancel_order(self, order_id, symbol, params=None):
            self.canceled.append(order_id)
            if order_id == "gone":
                raise RuntimeError("Unknown order sent")
            return {"id": order_id}

    client = _Flaky([{"id": "live"}, {"id": "gone"}, {"id": "live-2"}])
    rest = _rest_with(client)
    assert rest.cancel_symbol_orders(MarketType.USDM, "BTC/USDT:USDT") == 2
    assert client.canceled == ["live", "gone", "live-2"]


# ----------------------------------------------------- 测试环境（模拟盘 / 测试网）
class _FakeClient:
    """记录测试环境怎么被打开的替身。"""

    def __init__(self) -> None:
        self.demo: bool | None = None
        self.sandbox = False
        self.has = {"sandbox": True}

    def enable_demo_trading(self, on: bool) -> None:
        self.demo = bool(on)

    def set_sandbox_mode(self, on: bool) -> None:
        raise AssertionError("合约不该走测试网：币安已经停用了")


def test_futures_testnet_uses_demo_trading_not_sandbox() -> None:
    """币安停用合约测试网，ccxt 直接拒 set_sandbox_mode。

    报价、下单都要能通，所以合约必须切到模拟盘（demo-fapi / demo-dapi）。
    """
    from btrader.exchange.rest import BinanceREST

    client = _FakeClient()
    BinanceREST._apply_test_environment(client, MarketType.USDM)

    assert client.demo is True


def test_spot_testnet_still_uses_sandbox_mode() -> None:
    from btrader.exchange.rest import BinanceREST

    class SpotClient(_FakeClient):
        def set_sandbox_mode(self, on: bool) -> None:
            self.sandbox = bool(on)

    client = SpotClient()
    BinanceREST._apply_test_environment(client, MarketType.SPOT)

    assert client.sandbox is True
    assert client.demo is None


def test_retired_futures_testnet_error_is_explained_in_chinese() -> None:
    """ccxt 的英文弃用公告要翻成中文，并指向模拟盘。"""
    exc = ccxt.ExchangeError(
        "binanceusdm testnet/sandbox mode is not supported for futures anymore, "
        "please check the deprecation announcement"
    )

    text = _message(exc)

    assert "模拟盘" in text
    assert "testnet/sandbox" not in text


def test_credentials_are_kept_per_platform(store: CredentialStore) -> None:
    """换平台不能读到上一个平台的 Key。"""
    store.save(Network.MAINNET, MarketType.SPOT, Credentials("BINANCE_KEY", "S"), "binance")
    store.save(Network.MAINNET, MarketType.SPOT, Credentials("OTHER_KEY", "S"), "okx")

    assert store.load(Network.MAINNET, MarketType.SPOT, "binance").api_key == "BINANCE_KEY"
    assert store.load(Network.MAINNET, MarketType.SPOT, "okx").api_key == "OTHER_KEY"