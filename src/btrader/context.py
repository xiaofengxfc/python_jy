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
from .gateway import GatewaySupervisor
from .installer import OpenClawInstaller
from .llm import LLMService
from .news import NewsService
from .notify import WeChatNotifier
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

        self.rest = BinanceREST(self.network, self.creds, proxy=self.settings.proxy)
        # Charting reads Binance's read-only public feed so candles look like
        # the real market even while orders go to the test network. Credentials
        # are deliberately withheld: a charting client can never sign anything.
        self.market_rest = BinanceREST(
            self.network, Credentials(), market_data=True, proxy=self.settings.proxy
        )
        self.paper = PaperBroker(starting_balance=10_000.0)
        # Resume the simulated account instead of handing out a fresh 10,000
        # every launch: cash, positions, resting orders and SL/TP guards.
        try:
            self.paper.restore(self.db.get_json(PAPER_STATE_KEY))
        except Exception:  # pragma: no cover - a corrupt row must not block startup
            log.warning("恢复纸面账户失败，使用初始资金", exc_info=True)
        self.risk = RiskManager(self.settings.risk, self.db)
        # Outbound WeChat push. Constructed even when disabled so the settings
        # page can report whether the OpenClaw CLI is actually reachable.
        self.notifier = WeChatNotifier(self.settings.notify)
        # Optional LLM assistant. Built even when disabled so the settings page
        # can report the provider, the key and today's usage.
        self.ai = LLMService(self.settings.ai)
        # Both services hold their nested settings by reference; bind them to
        # the live objects now that both exist.
        self._attach_settings()
        self.trading = TradingService(
            self.settings, self.rest, self.risk, self.paper, self.db,
            notifier=self.notifier,
        )
        # Installs the CLI the notifier shells out to. Separate object because
        # it owns a process, not a message queue.
        self.installer = OpenClawInstaller()
        # Starts the gateway the notifier talks to. WeChat push fails without it,
        # and nothing else here was starting it.
        self.gateway = GatewaySupervisor()
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
        if market not in self._market_rest:
            # Each market authorises against its own key slot: sending a spot
            # key to fapi/dapi makes Binance answer -2015.
            creds = self.creds if market == self.market_type else self.credentials.load(
                self.network, market
            )
            self._market_rest[market] = BinanceREST(
                self.network, creds, proxy=self.settings.proxy
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
            )
        return self._data_rest[market]

    def quote_rest(self, market: MarketType, *, paper: bool) -> BinanceREST:
        """Where a fill price comes from.

        A paper fill is simulated, so quoting it from the same feed the chart
        draws keeps the chart, the fills and the P/L consistent. A live order
        must be priced against the venue that will execute it, otherwise a
        testnet order would be sent with a mainnet-only price.
        """
        return self.market_rest_for(market) if paper else self.rest_for(market)

    def set_network(self, network: Network) -> None:
        self.network = network
        self.settings.network = network.value
        self.creds = self.credentials.load(network, self.market_type)
        self.rest.network = network
        self.rest.credentials = self.creds
        self.rest.reset()
        self.market_rest.network = network
        self.market_rest.reset()
        self._market_rest = {self.market_type: self.rest}
        self._data_rest = {self.market_type: self.market_rest}
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
        self._data_rest = {self.market_type: self.market_rest}
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
            self.installer.shutdown()
        except Exception:  # pragma: no cover - best effort on exit
            log.debug("停止安装进程失败", exc_info=True)
        try:
            # The gateway is deliberately left running. Restarting it costs the
            # WeChat login: the ilink bot token is tied to the running plugin, so
            # a restart makes it answer ``errcode -14 session timeout`` and the
            # user has to scan the QR again. Keeping the process alive across
            # app restarts is what stops "放久一点就要重新扫码".
            self.gateway.detach()
            log.info("OpenClaw 网关保持运行（避免微信重新扫码）")
        except Exception:  # pragma: no cover - best effort on exit
            log.debug("保留 OpenClaw 网关失败", exc_info=True)
        try:
            self.notifier.stop()
        except Exception:  # pragma: no cover - best effort on exit
            log.debug("停止微信通知线程失败", exc_info=True)
        try:
            self.db.close()
        except Exception:  # pragma: no cover - best effort on exit
            log.debug("关闭数据库失败", exc_info=True)
