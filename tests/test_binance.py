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
