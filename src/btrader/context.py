"""Application context: owns long-lived services shared by all UI pages."""

from __future__ import annotations

import logging

from . import paths
from .core.config import AppSettings, ConfigStore
from .core.credentials import CredentialStore, Credentials
from .core.storage import Database
from .core.types import MarketType, Network
from .engine.paper import PaperBroker
from .engine.risk import RiskManager
from .engine.trading import TradingService
from .exchange.klines import KlineStore
from .exchange.rest import BinanceREST
from .news import NewsService
from .strategy.registry import StrategyRegistry

log = logging.getLogger(__name__)

# Where the simulated account is persisted between runs.
PAPER_STATE_KEY = "paper.account.v1"


class AppContext:
    def __init__(self) -> None:
        self.config = ConfigStore()
        self.settings: AppSettings = self.config.settings
        self.db = Database()

        self.network = Network(self.settings.network)
        self.market_type = MarketType(self.settings.market_type)

        self.credentials = CredentialStore()
        self.creds: Credentials = self.credentials.load(self.network, self.market_type)

        self.rest = BinanceREST(self.network, self.creds)
        self.paper = PaperBroker(starting_balance=10_000.0)
        # Resume the simulated account instead of handing out a fresh 10,000
        # every launch: cash, positions, resting orders and SL/TP guards.
        try:
            self.paper.restore(self.db.get_json(PAPER_STATE_KEY))
        except Exception:  # pragma: no cover - a corrupt row must not block startup
            log.warning("恢复纸面账户失败，使用初始资金", exc_info=True)
        self.risk = RiskManager(self.settings.risk)
        self.trading = TradingService(self.settings, self.rest, self.risk, self.paper, self.db)
        # The token lives in the keyring; the service reads it per request so a
        # token saved from the settings page applies without a restart.
        self.news = NewsService(self.credentials.load_news)

        self.klines = KlineStore(self.rest, paths.kline_cache_dir())

        self.strategies = StrategyRegistry()
        self.strategies.discover([paths.user_strategies_dir()])

        self._market_rest: dict[MarketType, BinanceREST] = {self.market_type: self.rest}

    # ------------------------------------------------------------------ market
    def rest_for(self, market: MarketType) -> BinanceREST:
        """A REST client for any market type (public data works unauthenticated)."""
        if market not in self._market_rest:
            # Each market authorises against its own key slot: sending a spot
            # key to fapi/dapi makes Binance answer -2015.
            creds = self.creds if market == self.market_type else self.credentials.load(
                self.network, market
            )
            self._market_rest[market] = BinanceREST(self.network, creds)
        return self._market_rest[market]

    def set_network(self, network: Network) -> None:
        self.network = network
        self.settings.network = network.value
        self.creds = self.credentials.load(network, self.market_type)
        self.rest.network = network
        self.rest.credentials = self.creds
        self.rest.reset()
        self._market_rest = {self.market_type: self.rest}
        self.config.save()

    def set_market_type(self, market: MarketType) -> None:
        self.market_type = market
        self.settings.market_type = market.value
        # Credentials are stored per margined type: a spot key cannot authorise
        # a futures endpoint (Binance answers -2015). Load the slot that belongs
        # to this market instead of carrying the previous key across.
        creds = self.credentials.load(self.network, market)
        self.creds = creds
        self.rest.credentials = creds
        self.rest.reset()
        self._market_rest = {}
        self.config.save()

    def save_credentials(self, api_key: str, api_secret: str) -> bool:
        creds = Credentials(api_key.strip(), api_secret.strip())
        ok = self.credentials.save(self.network, self.market_type, creds)
        if ok:
            self.creds = creds
            self.rest.credentials = creds
            self.rest.reset()
        return ok

    def save_settings(self) -> None:
        self.config.save()

    def shutdown(self) -> None:
        try:
            self.db.close()
        except Exception:  # pragma: no cover - best effort on exit
            log.debug("关闭数据库失败", exc_info=True)
