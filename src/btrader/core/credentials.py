"""Secure API credential storage backed by the OS keyring."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .. import APP_NAME
from .types import MarketType, Network

log = logging.getLogger(__name__)

SERVICE_NAME = f"{APP_NAME}-credentials"


@dataclass(slots=True)
class Credentials:
    api_key: str = ""
    api_secret: str = ""

    @property
    def is_set(self) -> bool:
        return bool(self.api_key and self.api_secret)

    @property
    def masked_key(self) -> str:
        if not self.api_key:
            return "未配置"
        if len(self.api_key) <= 8:
            return "*" * len(self.api_key)
        return f"{self.api_key[:4]}...{self.api_key[-4:]}"


def _account_id(network: Network) -> str:
    # Spot and futures share one Binance API key per account, but testnet keys differ.
    return network.value


class CredentialStore:
    """Stores credentials in the OS keyring (Windows Credential Manager, etc.)."""

    def __init__(self, backend=None) -> None:
        if backend is not None:
            self._backend = backend
            return
        try:
            import keyring

            self._backend = keyring
        except ImportError:  # pragma: no cover - keyring is a hard dependency
            log.error("keyring 未安装，凭据将不会被持久化")
            self._backend = None

    def _service(self, network: Network, market: MarketType) -> str:
        # Historically spot keys were used for futures too; keep one entry per
        # network+margined type so users can use different keys if they want.
        suffix = "futures" if market.is_futures else "spot"
        return f"{SERVICE_NAME}:{network.value}:{suffix}"

    def load(self, network: Network, market: MarketType) -> Credentials:
        if self._backend is None:
            return Credentials()
        service = self._service(network, market)
        try:
            key = self._backend.get_password(service, "api_key") or ""
            secret = self._backend.get_password(service, "api_secret") or ""
        except Exception as exc:  # keyring backends can raise many things
            log.warning("读取系统密钥环失败: %s", exc)
            return Credentials()
        return Credentials(key, secret)

    def save(self, network: Network, market: MarketType, creds: Credentials) -> bool:
        if self._backend is None:
            return False
        service = self._service(network, market)
        try:
            self._backend.set_password(service, "api_key", creds.api_key)
            self._backend.set_password(service, "api_secret", creds.api_secret)
            return True
        except Exception as exc:
            log.warning("写入系统密钥环失败: %s", exc)
            return False

    def delete(self, network: Network, market: MarketType) -> None:
        if self._backend is None:
            return
        service = self._service(network, market)
        for name in ("api_key", "api_secret"):
            try:
                self._backend.delete_password(service, name)
            except Exception:
                pass
