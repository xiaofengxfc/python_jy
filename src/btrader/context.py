"""Application context: owns long-lived services shared by all UI pages."""

from __future__ import annotations

import logging

from . import paths
from .core.config import AppSettings, ConfigStore
from .core.credentials import CredentialStore, Credentials
from .core.storage import Database
from .core.types import MarketType, Network
from .engine.account import AccountService
from .engine.risk import RiskManager
from .engine.trading import TradingService
from .exchange.klines import KlineStore
from .exchange.rest import DEFAULT_PLATFORM, SUPPORTED_PLATFORMS, BinanceREST

from .llm import LLMService
from .news import NewsService
from .notify import Notifier
from .strategy.registry import StrategyRegistry

log = logging.getLogger(__name__)


class AppContext:
    def __init__(self) -> None:
        self.config = ConfigStore()
        self.settings: AppSettings = self.config.settings
        self.db = Database()

        self.network = Network(self.settings.network)
        self.market_type = MarketType(self.settings.market_type)
        self.platform = self.settings.platform or DEFAULT_PLATFORM

        self.credentials = CredentialStore()
        self.creds: Credentials = self.credentials.load(
            self.network, self.market_type, self.platform
        )

        self.rest = BinanceREST(
            self.network, self.creds, proxy=self.settings.proxy, platform=self.platform
        )
        # Charting reads Binance's read-only public feed so candles look like
        # the real market even while orders go to the test network. Credentials
        # are deliberately withheld: a charting client can never sign anything.
        self.market_rest = BinanceREST(
            self.network,
            Credentials(),
            market_data=True,
            proxy=self.settings.proxy,
            platform=self.platform,
        )
        self.risk = RiskManager(self.settings.risk, self.db)
        # Outbound WeChat push. Constructed even when disabled so the settings
        # page can report whether the OpenClaw CLI is actually reachable.
        self.notifier = Notifier(self.settings.notify)
        # Optional LLM assistant. Built even when disabled so the settings page
        # can report the provider, the key and today's usage.
        self.ai = LLMService(self.settings.ai)
        # Both services hold their nested settings by reference; bind them to
        # the live objects now that both exist.
        self._attach_settings()
        self.trading = TradingService(
            self.settings, self.rest, self.risk, self.db,
            notifier=self.notifier,
            # An order carries its own market type; the service resolves that
            # market's client here instead of borrowing the global one.
            rest_for=self.rest_for,
        )
        # The single source of account truth: exchange balances, positions,
        # open orders and the stored fill ledger, cached for the polling panels.
        self.account = AccountService(
            rest_for=self.rest_for,
            chart_rest_for=self.market_rest_for,
            db=self.db,
            network=self.network,
        )

        # Keyless feed: announcements plus two RSS outlets, no token to manage.
        self.news = NewsService()

        self.klines = KlineStore(self.market_rest, paths.kline_cache_dir(), source="mainnet")

        self.strategies = StrategyRegistry()
        self.strategies.discover([paths.user_strategies_dir()])

        self._market_rest: dict[MarketType, BinanceREST] = {self.market_type: self.rest}
        self._data_rest: dict[MarketType, BinanceREST] = {self.market_type: self.market_rest}

    # ------------------------------------------------------------------ market
    def rest_for(self, market: MarketType) -> BinanceREST:
        """A REST client for any market type (public data works unauthenticated)."""
        # 当前市场永远用 self.rest：切换市场、保存凭证都只更新那一个对象。
        # 以前这里会另建一个客户端存进缓存，保存新 key 之后缓存那份还是旧的，
        # 账户就会一直报「尚未配置 API Key」。
        if market == self.market_type:
            self._market_rest[market] = self.rest
            return self.rest
        if market not in self._market_rest:
            # Each market authorises against its own key slot: sending a spot
            # key to fapi/dapi makes Binance answer -2015.
            creds = self.creds if market == self.market_type else self.credentials.load(
                self.network, market, self.platform
            )
            self._market_rest[market] = BinanceREST(
                self.network, creds, proxy=self.settings.proxy, platform=self.platform
            )
        return self._market_rest[market]

    def market_rest_for(self, market: MarketType) -> BinanceREST:
        """The charting client: read-only candles, tickers and order books.

        Always the real venue feed, whatever the trading network is: spot goes
        to Binance's public mirror, futures to fapi/dapi directly. A testnet
        session trades against the sandbox but must not be shown simulated
        prices, which would silently disagree with any other chart.
        """
        if market not in self._data_rest:
            base = self._market_rest.get(market) or self.rest_for(market)
            self._data_rest[market] = BinanceREST(
                base.network,
                Credentials(),
                market_data=True,
                proxy=self.settings.proxy,
                platform=self.platform,
            )
        return self._data_rest[market]

    def quote_rest(self, market: MarketType) -> BinanceREST:
        """Where an order price comes from: the venue that will execute it.

        Quoting from the charting feed would send a testnet order with a
        mainnet-only price, so orders and the close route always read the
        trading client. The chart keeps using market_rest_for() and is allowed
        to disagree — it shows the real market, the testnet shows its own.
        """
        return self.rest_for(market)

    def set_network(self, network: Network, *, persist: bool = True) -> None:
        self.network = network
        self.settings.network = network.value
        self.creds = self.credentials.load(network, self.market_type, self.platform)
        self.rest.network = network
        self.rest.credentials = self.creds
        self.rest.reset()
        self.market_rest.network = network
        self.market_rest.reset()
        self._market_rest = {self.market_type: self.rest}
        self._data_rest = {self.market_type: self.market_rest}
        self.account.network = network
        self.account.invalidate()
        if persist:
            self.config.save()

    def set_market_type(self, market: MarketType, *, persist: bool = True) -> None:
        self.market_type = market
        self.settings.market_type = market.value
        # Credentials are stored per margined type: a spot key cannot authorise
        # a futures endpoint (Binance answers -2015). Load the slot that belongs
        # to this market instead of carrying the previous key across.
        creds = self.credentials.load(self.network, market, self.platform)
        self.creds = creds
        self.rest.credentials = creds
        self.rest.reset()
        # 当前市场的客户端就是 self.rest，缓存里也放了它，别留空表让人另建一份。
        self._market_rest = {market: self.rest}
        self._data_rest = {self.market_type: self.market_rest}
        self.account.invalidate()
        if persist:
            self.config.save()

    def set_platform(self, platform: str, *, persist: bool = True) -> None:
        """切换交易所。凭据按平台分开存，切过去就取那一份。"""
        target = platform if platform in SUPPORTED_PLATFORMS else DEFAULT_PLATFORM
        if target == self.platform:
            return
        self.platform = target
        self.settings.platform = target
        self.creds = self.credentials.load(self.network, self.market_type, target)
        self.rest.platform = target
        self.rest.credentials = self.creds
        self.rest.reset()
        self.market_rest.platform = target
        self.market_rest.reset()
        self._market_rest = {self.market_type: self.rest}
        self._data_rest = {self.market_type: self.market_rest}
        self.account.invalidate()
        if persist:
            self.config.save()

    def save_credentials(self, api_key: str, api_secret: str) -> bool:
        creds = Credentials(api_key.strip(), api_secret.strip())
        ok = self.credentials.save(self.network, self.market_type, creds, self.platform)
        if ok:
            self.creds = creds
            self.rest.credentials = creds
            self.rest.reset()
            # 缓存里那份客户端也是 self.rest，但显式对齐一次，免得以后有人又
            # 往缓存里塞别的实例。
            self._market_rest = {self.market_type: self.rest}
            # A fresh key can mean a different account: drop the cached view.
            self.account.invalidate()
        return ok

    def save_settings(self) -> None:
        self.config.save()
        self._attach_settings()

    def apply_proxy(self, proxy: str) -> None:
        """Switch the outbound proxy and drop every cached exchange client.

        ccxt reads ``proxies`` once per client, so a changed proxy only takes
        effect after the clients are rebuilt; the caches hold the old ones.
        """
        cleaned = (proxy or "").strip()
        if cleaned == self.settings.proxy:
            return
        self.settings.proxy = cleaned
        self.rest.proxy = cleaned
        self.rest.reset()
        self._market_rest = {self.market_type: self.rest}
        self.market_rest.proxy = cleaned
        self.market_rest.reset()
        self._data_rest = {self.market_type: self.market_rest}
        self.account.invalidate()

    def reload_settings(self) -> None:
        """Re-read the config file into the live settings object.

        The web layer edits ``self.settings`` in place and persists it, so this
        is only needed when the file was changed by someone else — it keeps the
        two from silently diverging rather than replacing the live object.
        """
        loaded = ConfigStore().settings
        for name in AppSettings.__dataclass_fields__:
            setattr(self.settings, name, getattr(loaded, name))
        self._attach_settings()

    def _attach_settings(self) -> None:
        """Point every service at the *current* nested settings objects.

        ``self.settings.risk`` / ``.notify`` are replaced wholesale when the
        config is reloaded, while the services captured the old instances at
        construction. Re-binding here is what makes a saved breaker limit or a
        new WeChat target take effect without rebuilding the services.
        """
        self.risk.settings = self.settings.risk
        self.notifier.settings = self.settings.notify
        self.ai.settings = self.settings.ai

    def shutdown(self) -> None:
        try:
            self.notifier.stop()
        except Exception:  # pragma: no cover - best effort on exit
            log.debug("停止微信通知线程失败", exc_info=True)
        try:
            self.db.close()
        except Exception:  # pragma: no cover - best effort on exit
            log.debug("关闭数据库失败", exc_info=True)
