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
        # One entry per market. A USDⓈ-M key and a COIN-M key are separate on
        # Binance, and sharing a single "futures" slot meant saving one market
        # silently overwrote the other, which then failed with -2015.
        return f"{SERVICE_NAME}:{network.value}:{market.value}"

    @staticmethod
    def _legacy_service(network: Network, market: MarketType) -> str | None:
        """Older builds kept spot and every futures key in one shared slot."""
        suffix = "futures" if market.is_futures else "spot"
        service = f"{SERVICE_NAME}:{network.value}:{suffix}"
        return service

    def _read(self, service: str) -> Credentials:
        try:
            key = self._backend.get_password(service, "api_key") or ""
            secret = self._backend.get_password(service, "api_secret") or ""
        except Exception as exc:  # keyring backends can raise many things
            log.warning("读取系统密钥环失败: %s", exc)
            return Credentials()
        return Credentials(key, secret)

    def _write(self, service: str, creds: Credentials) -> bool:
        try:
            self._backend.set_password(service, "api_key", creds.api_key)
            self._backend.set_password(service, "api_secret", creds.api_secret)
            return True
        except Exception as exc:
            log.warning("写入系统密钥环失败: %s", exc)
            return False

    def load(self, network: Network, market: MarketType) -> Credentials:
        if self._backend is None:
            return Credentials()
        service = self._service(network, market)
        creds = self._read(service)
        if creds.is_set:
            return creds
        legacy = self._legacy_service(network, market)
        if not legacy or legacy == service:
            return creds
        # Adopt the shared legacy key once so upgrading users keep working.
        # Copying (not moving) lets both futures markets keep the same key
        # until they are saved separately.
        creds = self._read(legacy)
        if creds.is_set:
            self._write(service, creds)
        return creds

    def save(self, network: Network, market: MarketType, creds: Credentials) -> bool:
        if self._backend is None:
            return False
        return self._write(self._service(network, market), creds)

    def delete(self, network: Network, market: MarketType) -> None:
        if self._backend is None:
            return
        services = [self._service(network, market)]
        legacy = self._legacy_service(network, market)
        if legacy and legacy not in services:
            # Also drop the shared legacy slot, otherwise the key would come
            # straight back on the next load and "cleared" would be a lie.
            services.append(legacy)
        for service in services:
            for name in ("api_key", "api_secret"):
                try:
                    self._backend.delete_password(service, name)
                except Exception:
                    pass

    # ------------------------------------------------------------------- news
    def load_news(self) -> str:
        """CryptoPanic token; a single account-wide key, not per market."""
        if self._backend is None:
            return ""
        try:
            return self._backend.get_password(f"{SERVICE_NAME}:news", "token") or ""
        except Exception as exc:
            log.warning("读取新闻 Token 失败: %s", exc)
            return ""

    def save_news(self, token: str) -> bool:
        if self._backend is None:
            return False
        try:
            self._backend.set_password(f"{SERVICE_NAME}:news", "token", token)
            return True
        except Exception as exc:
            log.warning("写入新闻 Token 失败: %s", exc)
            return False

    def delete_news(self) -> None:
        if self._backend is None:
            return
        try:
            self._backend.delete_password(f"{SERVICE_NAME}:news", "token")
        except Exception:
            pass
