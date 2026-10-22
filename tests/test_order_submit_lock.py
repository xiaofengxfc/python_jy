"""提交段的两条规矩：按市场取客户端、同一时刻只发一张单。

两件都是审查里定下来的设计取舍：

* 跨市场下单不再借全局市场。以前 ``POST /api/orders`` 在 account_lock 里先把
  ``ctx.market_type`` 借成请求的市场、用完再还原，并发读设置能看到另一个市场。
* 账户锁统一到提交段。以前只有 web 路由拿锁，AutoTrader 和 AI 交易的 submit
  完全不拿，两路同时动资金时靠路由 409 兜着。

锁的范围也是断言的一部分：只包提交那一下，模型调用和 K 线拉取必须在锁外，
否则一次慢模型就把下单框卡住。
"""

from __future__ import annotations

import json
import threading
import time

import pytest

import btrader.web.app as web_app
from btrader.context import AppContext
from btrader.core.credentials import Credentials
from btrader.core.config import AppSettings, RiskSettings
from btrader.core.storage import Database
from btrader.core.types import MarketType, OrderSide, OrderType
from btrader.engine.account import AccountService
from btrader.engine.ai_trader import AITrader, AITraderConfig
from btrader.engine.auto import AutoTrader, AutoTraderConfig
from btrader.engine.risk import RiskManager
from btrader.engine.trading import OrderRequest, TradingService
from btrader.strategy.base import Strategy
from btrader.web.app import OrderPayload
from btrader.web.routes import build_router

import numpy as np
import pandas as pd

from fakes import FakeVenue
from helpers import make_candles

FUTURES = "BTC/USDT:USDT"
SPOT = "BTC/USDT"


# ------------------------------------------------------------------- doubles
class _Venue(FakeVenue):
    """替身交易所：能给 create_order 加个闸门，用来把提交卡在半路。"""

    def __init__(self, span: float = 0.0) -> None:
        super().__init__()
        self.credentials = Credentials()
        self.span = span
        self.entered = threading.Event()
        self.release = threading.Event()
        self._inflight = 0
        self.max_inflight = 0

    def reset(self) -> None:  # ccxt 客户端重建；替身没有可重建的东西
        return None

    def create_order(self, market, symbol, order_type, side, amount, price=None, params=None):
        self.entered.set()
        self._inflight += 1
        self.max_inflight = max(self.max_inflight, self._inflight)
        try:
            if self.span:
                time.sleep(self.span)
            else:
                self.release.wait(5.0)
            return super().create_order(market, symbol, order_type, side, amount, price, params)
        finally:
            self._inflight -= 1


class _Fixed(Strategy):
    key = "fixed"
    name = "fixed"
    allow_short = True

    def __init__(self, target: float) -> None:
        super().__init__()
        self.target = target

    def generate(self, df, params):
        return pd.DataFrame({"target": np.full(len(df), self.target)}, index=df.index)


class _Klines:
    """K 线替身：可选的闸门用来证明拉 K 线的时候没占着账户锁。"""

    def __init__(self, frame: pd.DataFrame, gate: bool = False) -> None:
        self.frame = frame
        self.entered = threading.Event()
        self.release = threading.Event()
        self.gate = gate

    def fetch(self, *args, **kwargs):
        if self.gate:
            self.entered.set()
            self.release.wait(5.0)
        return self.frame.copy()


class _State:
    """下单路由用到的进程级状态，account_lock 与 app.py 一样走交易服务那把。"""

    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.locks = {market: threading.RLock() for market in MarketType}
        self.loading = {market: threading.Lock() for market in MarketType}
        self.symbols: dict = {}
        self.account_lock = ctx.trading.account_lock
        self.equity_history: list = []
        self.equity_stamp = 0.0
        self.stream = None
        self.stop_reconcile = threading.Event()


def _open_caps(risk: RiskSettings) -> None:
    """把风控上限清零：这些用例关心的是锁和市场，不是仓位限额。"""
    risk.max_order_quote = 0.0
    risk.max_order_pct = 0.0
    risk.max_position_quote = 0.0
    risk.max_daily_loss = 0.0
    risk.max_daily_trades = 0
    risk.max_consecutive_losses = 0
    risk.require_confirmation = False


def _context(tmp_path, monkeypatch, venue: _Venue) -> AppContext:
    """测试网 + 合约起步，交易所全走替身，配置落在 tmp_path。"""
    monkeypatch.setenv("BITRADER_DATA_DIR", str(tmp_path))
    (tmp_path / "config.json").write_text(
        json.dumps({"network": "testnet", "market_type": MarketType.USDM.value}),
        encoding="utf-8",
    )
    ctx = AppContext()
    ctx.rest = venue
    ctx._market_rest = {market: venue for market in MarketType}
    ctx._data_rest = {market: venue for market in MarketType}
    ctx.trading = TradingService(
        ctx.settings, venue, ctx.risk, ctx.db, notifier=None, rest_for=ctx.rest_for
    )
    ctx.account = AccountService(
        rest_for=lambda market: venue,
        chart_rest_for=lambda market: venue,
        db=ctx.db,
        network=ctx.network,
    )
    _open_caps(ctx.settings.risk)
    return ctx


def _serve(monkeypatch, ctx: AppContext) -> _State:
    state = _State(ctx)
    monkeypatch.setattr(web_app, "_state", state, raising=False)
    return state


def _endpoint(container, path: str, method: str):
    for route in container.routes:
        if getattr(route, "path", "") == path and method in getattr(route, "methods", ()):
            return route.endpoint
    raise AssertionError(f"missing route {method} {path}")


def _lock_is_free(ctx: AppContext, timeout: float = 0.2) -> bool:
    """账户锁此刻能不能被另一个调用方拿到。"""
    lock = ctx.trading.account_lock
    if lock.acquire(timeout=timeout):
        lock.release()
        return True
    return False


def _saved_market(path) -> str:
    return json.loads(path.read_text(encoding="utf-8")).get("market_type", "")


# ------------------------------------------------ 一）不借全局市场
def test_a_cross_market_order_never_shows_up_in_a_settings_read(tmp_path, monkeypatch):
    """在合约页面下一张现货单：订单在途那一刻，并发读到的市场仍然是合约。"""
    venue = _Venue()
    venue._last[SPOT] = 100.0
    ctx = _context(tmp_path, monkeypatch, venue)
    _serve(monkeypatch, ctx)
    app = web_app.create_app()
    place = _endpoint(app, "/api/orders", "POST")
    read_settings = _endpoint(build_router(web_app.get_state), "/api/settings", "GET")
    failures: list[str] = []

    def order() -> None:
        try:
            place(
                OrderPayload(
                    market=MarketType.SPOT.value,
                    symbol=SPOT,
                    side="buy",
                    order_type="market",
                    size=1.0,
                )
            )
        except Exception as exc:  # pragma: no cover - 线程里的失败要带出来
            failures.append(repr(exc))

    worker = threading.Thread(target=order)
    worker.start()
    try:
        assert venue.entered.wait(5.0), "下单没有走到交易所，没法证明中途的设置读"
        # 订单正卡在 create_order 里：这时读设置，看到的必须是原来的市场
        seen = read_settings()
    finally:
        venue.release.set()
        worker.join(10.0)

    assert failures == []
    assert seen["market"] == MarketType.USDM.value
    assert seen["settings"]["market_type"] == MarketType.USDM.value
    # 收尾也得干净：全局市场、落盘的配置都没被借走
    assert ctx.market_type is MarketType.USDM
    assert ctx.settings.market_type == MarketType.USDM.value
    assert _saved_market(tmp_path / "config.json") == MarketType.USDM.value
    # 订单本身还得落在请求的市场，否则就是把功能改坏了
    assert venue.fills[-1]["market"] == MarketType.SPOT.value


def test_an_order_is_signed_with_its_own_markets_client(tmp_path):
    """提交时按 OrderRequest.market_type 取客户端，不是借当前市场那一个。"""
    settings = AppSettings()
    _open_caps(settings.risk)
    futures = FakeVenue()
    futures._last[FUTURES] = 100.0
    spot = FakeVenue()
    spot._last[SPOT] = 100.0
    db = Database(tmp_path / "clients.db")
    try:
        trading = TradingService(
            settings, futures, RiskManager(settings.risk), db,
            rest_for=lambda market: spot if market is MarketType.SPOT else futures,
        )
        order = OrderRequest(
            symbol=SPOT,
            side=OrderSide.BUY.value,
            order_type=OrderType.MARKET.value,
            amount=0.5,
            price=100.0,
            market_type=MarketType.SPOT,
        )
        trading.submit(order)
    finally:
        db.close()

    assert [item["market"] for item in spot.fills] == [MarketType.SPOT.value]
    assert futures.fills == [], "现货单被发到了当前市场的客户端上"


# ------------------------------------------------ 二）提交段串行
def test_manual_and_engine_submits_never_overlap(tmp_path, monkeypatch):
    """手动下单和自动交易的提交同时进来：两张单必须一前一后，不许重叠。"""
    venue = _Venue(span=0.25)
    venue._last[FUTURES] = 100.0
    ctx = _context(tmp_path, monkeypatch, venue)
    _serve(monkeypatch, ctx)
    app = web_app.create_app()
    place = _endpoint(app, "/api/orders", "POST")
    auto = AutoTrader(
        AutoTraderConfig(
            symbol=FUTURES,
            market_type=MarketType.USDM,
            timeframe="1h",
            position_pct=0.5,
            leverage=1.0,
        ),
        _Fixed(1.0),
        ctx.trading,
        _Klines(make_candles(120, price=100.0, drift=0.0, volatility=0.0)),
    )
    failures: list[str] = []
    barrier = threading.Barrier(2)

    def manual() -> None:
        barrier.wait()
        try:
            place(
                OrderPayload(
                    market=MarketType.USDM.value,
                    symbol=FUTURES,
                    side="buy",
                    order_type="market",
                    size=0.1,
                )
            )
        except Exception as exc:  # pragma: no cover
            failures.append(f"manual: {exc!r}")

    def engine() -> None:
        barrier.wait()
        try:
            auto._tick()
        except Exception as exc:  # pragma: no cover
            failures.append(f"auto: {exc!r}")

    threads = [threading.Thread(target=manual), threading.Thread(target=engine)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20.0)

    assert failures == []
    assert len(venue.fills) == 2, "两路都该发出自己的单，不能有一路被丢掉"
    assert venue.max_inflight == 1, "两次 submit 重叠了：提交段没有串行"


# ------------------------------------------------ 锁的范围
def test_the_account_lock_covers_the_order_but_not_the_kline_fetch(tmp_path, monkeypatch):
    """拉 K 线在锁外，提交在锁内：一次慢下载不该把下单框堵住。"""
    venue = _Venue()
    venue._last[FUTURES] = 100.0
    ctx = _context(tmp_path, monkeypatch, venue)
    _serve(monkeypatch, ctx)
    klines = _Klines(
        make_candles(120, price=100.0, drift=0.0, volatility=0.0), gate=True
    )
    auto = AutoTrader(
        AutoTraderConfig(
            symbol=FUTURES,
            market_type=MarketType.USDM,
            timeframe="1h",
            position_pct=0.5,
            leverage=1.0,
        ),
        _Fixed(1.0),
        ctx.trading,
        klines,
    )
    worker = threading.Thread(target=auto._tick)
    worker.start()
    try:
        assert klines.entered.wait(5.0), "引擎没有去拉 K 线"
        assert _lock_is_free(ctx), "拉 K 线的时候账户锁被占住了"
        klines.release.set()
        assert venue.entered.wait(5.0), "引擎没有走到下单"
        assert not _lock_is_free(ctx), "提交段没有占住账户锁"
    finally:
        klines.release.set()
        venue.release.set()
        worker.join(10.0)

    assert len(venue.fills) == 1


def test_the_model_call_stays_outside_the_account_lock(tmp_path, monkeypatch):
    """AI 交易问模型的那几秒不占锁，放行后仍然照常下单。"""
    venue = _Venue()
    venue._last[FUTURES] = 100.0
    ctx = _context(tmp_path, monkeypatch, venue)
    _serve(monkeypatch, ctx)
    frame = make_candles(120, price=100.0, drift=0.0, volatility=0.0)
    answer = json.dumps(
        {
            "action": "open_long",
            "symbol": FUTURES,
            "quote": 10.0,
            "price": 100.0,
            "stop_loss": 98.0,
            "take_profit": 104.0,
            "confidence": 0.9,
            "ttl_sec": 120,
            "rationale": "冒烟",
        },
        ensure_ascii=False,
    )

    class SlowLLM:
        def __init__(self) -> None:
            self.entered = threading.Event()
            self.release = threading.Event()
            self.calls = 0

        def ask(self, prompt, **kwargs):
            self.calls += 1
            self.entered.set()
            self.release.wait(5.0)
            return {"ok": True, "text": answer}

    llm = SlowLLM()
    trader = AITrader(
        AITraderConfig(
            enabled=True,
            confirm_live=True,
            require_stop=False,
            network="testnet",
            market_type=MarketType.USDM.value,
            symbol=FUTURES,
            poll_sec=3600.0,
            max_order_quote=50.0,
            max_position_quote=200.0,
        ),
        trading=ctx.trading,
        llm=llm,
        frame_provider=lambda: frame,
        portfolio_provider=lambda: {"equity": 1000.0, "position_quote": 0.0, "price": 100.0},
        audit_path=tmp_path / "audit.jsonl",
        clock=lambda: 1_800_000_000.0,
    )
    worker = threading.Thread(target=trader.decide_once)
    worker.start()
    try:
        assert llm.entered.wait(5.0), "模型没被调用"
        assert _lock_is_free(ctx), "模型调用期间账户锁被占住了"
    finally:
        llm.release.set()
        venue.release.set()
        worker.join(10.0)

    assert (trader.last_decision or {}).get("outcome") == "order"
    assert len(venue.fills) == 1