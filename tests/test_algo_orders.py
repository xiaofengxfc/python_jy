"""算法单（条件单 / 止损止盈）看得见也撤得掉。

币安把触发类订单放在独立的算法单端点上，返回 algoId / algoType: CONDITIONAL，
fetch_open_orders 里一条都看不到。以前三处都只读 fetch_open_orders，于是：
挂上去的止损在账户面板上不存在；重挂返回 cleared: 0、交易所侧旧单原样留着，
连挂四次就是四张止损；DELETE /api/orders/{id} 拿算法单号去普通撤单接口，
交易所回 -2011；平仓前的 cancel_symbol_orders 也看不见它们，
「平仓前撤掉 reduceOnly 止损」这条保证是假的。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

import ccxt
import pytest
from fastapi import HTTPException

from btrader.core.config import AppSettings, RiskSettings
from btrader.core.credentials import Credentials
from btrader.core.storage import Database
from btrader.core.types import MarketType, Network
from btrader.engine.account import AccountService
from btrader.engine.risk import RiskManager
from btrader.engine.trading import OrderRequest, TradingService
from btrader.exchange.rest import BinanceREST, ExchangeError
from btrader.web import app as web_app

SYMBOL = "BTC/USDT:USDT"
VENUE_SYMBOL = "BTCUSDT"
UNKNOWN_ORDER = 'binance {"code":-2011,"msg":"Unknown order sent."}'


class FakeBinanceClient:
    """ccxt 客户端的替身：普通挂单和算法单各存一份，谁在哪一边是这门课的重点。"""

    def __init__(self) -> None:
        self.has: dict = {}
        self.normal: list[dict] = []
        self.algo: list[dict] = []
        self.calls: list[tuple] = []
        self.last_price = 100.0
        self._seq = 0
        # ccxt 载入后就有 markets，算法单回来的 symbol 是交易所代码，靠它翻回
        # ccxt 的写法（BTCUSDT -> BTC/USDT:USDT）。
        self.markets: dict = {
            # 合约客户端里 BTCUSDT 这个 id 同时挂着现货市场，而且排在前面：
            # 触发单必须认合约那一份，否则保护单和持仓的交易对对不上号。
            "BTC/USDT": {"id": VENUE_SYMBOL, "symbol": "BTC/USDT"},
            SYMBOL: {
                "id": VENUE_SYMBOL,
                "symbol": SYMBOL,
                "contract": True,
                "swap": True,
                "contractSize": 1.0,
                "limits": {"amount": {"min": 0.001}},
            },
        }

    # ----------------------------------------------------- markets / precision
    def load_markets(self) -> dict:
        return self.markets

    def market(self, symbol: str) -> dict:
        return self.markets[symbol]

    def amount_to_precision(self, symbol: str, amount: float) -> str:
        return f"{float(amount):.3f}"

    def price_to_precision(self, symbol: str, price: float) -> str:
        return f"{float(price):.2f}"

    # ------------------------------------------------------------------ reads
    def fetch_ticker(self, symbol: str) -> dict:
        return {"last": self.last_price}

    def fetch_balance(self) -> dict:
        total = {"USDT": 10_000.0}
        return {"free": dict(total), "used": {}, "total": total}

    def fetch_positions(self, symbols=None) -> list[dict]:
        return [
            {
                "symbol": SYMBOL,
                "contracts": 0.5,
                "side": "long",
                "entryPrice": 100.0,
                "markPrice": self.last_price,
                "unrealizedPnl": 0.0,
            }
        ]

    def fetch_open_orders(self, symbol=None) -> list[dict]:
        self.calls.append(("fetch_open_orders", symbol))
        return [dict(row) for row in self.normal]

    # ----------------------------------------------------------------- writes
    def create_order(self, symbol, type, side, amount, price, params=None) -> dict:
        params = dict(params or {})
        self.calls.append(("create_order", symbol, type, side, params))
        self._seq += 1
        if "stopLossPrice" in params or "takeProfitPrice" in params:
            # 触发单走算法单端点：号是 algoId，普通挂单接口里查不到它。
            take_profit = "takeProfitPrice" in params
            trigger = params.get("takeProfitPrice") or params.get("stopLossPrice")
            algo_id = str(800_000 + self._seq)
            row = {
                "algoId": algo_id,
                "clientAlgoId": f"c{algo_id}",
                "algoType": "CONDITIONAL",
                "orderType": "TAKE_PROFIT_MARKET" if take_profit else "STOP_MARKET",
                "symbol": VENUE_SYMBOL,
                "side": side.upper(),
                "positionSide": "BOTH",
                "quantity": str(amount),
                "price": "0",
                "triggerPrice": str(trigger),
                "reduceOnly": bool(params.get("reduceOnly", True)),
                "algoStatus": "NEW",
                "createTime": 1_700_000_000_000,
            }
            self.algo.append(row)
            return {
                "id": algo_id,
                "clientOrderId": row["clientAlgoId"],
                "symbol": symbol,
                "side": side,
                "type": "take_profit_market" if take_profit else "stop_market",
                "amount": amount,
                "price": None,
                "status": "open",
                "info": dict(row),
            }
        order = {
            "id": str(self._seq),
            "symbol": symbol,
            "side": side,
            "type": type,
            "amount": amount,
            "price": price,
            "filled": 0.0,
            "status": "open",
            "timestamp": 1_700_000_000_000,
        }
        self.normal.append(order)
        return dict(order)

    def cancel_order(self, order_id, symbol=None, params=None) -> dict:
        self.calls.append(("cancel_order", str(order_id)))
        for row in list(self.normal):
            if row["id"] == str(order_id):
                self.normal.remove(row)
                return {**row, "status": "canceled"}
        raise ccxt.OrderNotFound(UNKNOWN_ORDER)

    def set_leverage(self, symbol, leverage) -> dict:
        return {}

    # ------------------------------- 算法单原始端点（ccxt 没有统一封装）
    def fapiPrivateGetOpenAlgoOrders(self, params=None) -> dict:
        self.calls.append(("open_algo", (params or {}).get("symbol")))
        wanted = (params or {}).get("symbol")
        rows = [dict(row) for row in self.algo if not wanted or row["symbol"] == wanted]
        return {"total": len(rows), "orders": rows}

    def fapiPrivateDeleteAlgoOrder(self, params=None) -> dict:
        algo_id = str((params or {}).get("algoId") or "")
        self.calls.append(("delete_algo", algo_id))
        for row in list(self.algo):
            if row["algoId"] == algo_id:
                self.algo.remove(row)
                return {"algoId": algo_id, "success": True, "code": 200, "msg": "success"}
        raise ccxt.OrderNotFound(UNKNOWN_ORDER)


def _rest(client: FakeBinanceClient) -> BinanceREST:
    rest = BinanceREST(Network.TESTNET, Credentials("key", "secret"))
    rest._clients[MarketType.USDM] = client
    rest._clients[MarketType.SPOT] = client
    return rest


def _db(tmp_path) -> Database:
    return Database(tmp_path / "algo.db")


@pytest.fixture()
def venue() -> FakeBinanceClient:
    return FakeBinanceClient()


# ------------------------------------------------------------- 账户面板看得见
def test_the_account_panel_sees_the_stop_that_was_just_armed(tmp_path, venue) -> None:
    """挂上去的止损必须在 /api/account 里出现，不然用户以为没挂上。"""
    rest = _rest(venue)
    db = _db(tmp_path)
    try:
        rest.create_stop_market(MarketType.USDM, SYMBOL, "sell", 0.5, 95.0)
        account = AccountService(
            rest_for=lambda market: rest,
            chart_rest_for=lambda market: rest,
            db=db,
            network=Network.TESTNET,
        )
        snap = account.snapshot(MarketType.USDM, refresh=True)
    finally:
        db.close()

    assert [row["id"] for row in snap.orders] == ["800001"], snap.orders
    assert snap.orders[0]["type"] == "stop_market"
    assert snap.orders[0]["trigger"] == pytest.approx(95.0)
    assert snap.orders[0]["symbol"] == SYMBOL
    assert snap.protections == [
        {"symbol": SYMBOL, "stop_loss": 95.0, "take_profit": 0.0, "orders": ["800001"]}
    ]


def test_a_limit_order_and_a_stop_show_up_side_by_side(tmp_path, venue) -> None:
    """普通挂单和算法单是两条链路，面板上要同时看得到。"""
    rest = _rest(venue)
    db = _db(tmp_path)
    try:
        venue.create_order(SYMBOL, "limit", "buy", 0.5, 90.0, {})
        rest.create_take_profit_market(MarketType.USDM, SYMBOL, "sell", 0.5, 105.0)
        account = AccountService(
            rest_for=lambda market: rest,
            chart_rest_for=lambda market: rest,
            db=db,
            network=Network.TESTNET,
        )
        snap = account.snapshot(MarketType.USDM, refresh=True)
    finally:
        db.close()

    kinds = sorted(row["type"] for row in snap.orders)
    assert kinds == ["limit", "take_profit_market"]
    assert snap.protections[0]["take_profit"] == pytest.approx(105.0)


# ----------------------------------------------------------------- 撤得掉
def test_re_arming_clears_the_previous_stop_on_the_venue(venue) -> None:
    """重挂保护单必须真的撤掉旧单：以前 cleared: 0，四张止损并存。"""
    rest = _rest(venue)
    rest.create_stop_market(MarketType.USDM, SYMBOL, "sell", 0.5, 95.0)

    assert rest.cancel_protective_orders(MarketType.USDM, SYMBOL) == 1
    assert venue.algo == [], "交易所侧旧止损必须消失"

    rest.create_stop_market(MarketType.USDM, SYMBOL, "sell", 0.5, 96.0)
    assert len(venue.algo) == 1


def test_the_protection_cancel_leaves_resting_limit_orders_alone(venue) -> None:
    """只撤该交易对的止损止盈，不碰挂着的限价单。"""
    rest = _rest(venue)
    venue.create_order(SYMBOL, "limit", "buy", 0.5, 90.0, {})
    rest.create_stop_market(MarketType.USDM, SYMBOL, "sell", 0.5, 95.0)

    assert rest.cancel_protective_orders(MarketType.USDM, SYMBOL) == 1
    assert [row["id"] for row in venue.normal] == ["1"]


def test_a_conditional_order_is_cancelled_by_its_algo_id(venue) -> None:
    """DELETE /api/orders/{id} 拿算法单号去普通撤单接口只会得到 -2011。"""
    rest = _rest(venue)
    placed = rest.create_stop_market(MarketType.USDM, SYMBOL, "sell", 0.5, 95.0)

    canceled = rest.cancel_order(MarketType.USDM, placed["id"], SYMBOL)

    assert canceled["id"] == placed["id"]
    assert canceled["status"] == "canceled"
    assert canceled["type"] == "stop_market"
    assert venue.algo == []


def test_a_cancel_of_an_unknown_id_still_reports_an_unknown_order(venue) -> None:
    """两条链路都没有这张单，才说「没有这个挂单」。"""
    rest = _rest(venue)

    with pytest.raises(ExchangeError) as caught:
        rest.cancel_order(MarketType.USDM, "123456789", SYMBOL)

    assert "-2011" in str(caught.value)


def test_closing_a_position_clears_the_algo_stop_first(tmp_path, venue) -> None:
    """平仓前撤掉 reduceOnly 止损：留着它会被回来的价格再触发一次，反向开仓。"""
    rest = _rest(venue)
    db = _db(tmp_path)
    try:
        settings = AppSettings(
            network=Network.TESTNET.value,
            risk=RiskSettings(
                max_order_quote=0,
                max_position_quote=0,
                max_leverage=20,
                max_daily_trades=0,
                max_daily_loss=0,
                max_consecutive_losses=0,
            ),
        )
        trading = TradingService(
            settings=settings,
            rest=rest,
            risk=RiskManager(settings.risk),
            db=db,
            notifier=None,
        )
        rest.create_stop_market(MarketType.USDM, SYMBOL, "sell", 0.5, 95.0)
        request = OrderRequest(
            symbol=SYMBOL,
            side="sell",
            order_type="market",
            amount=0.5,
            price=100.0,
            reduce_only=True,
            market_type=MarketType.USDM,
        )
        trading.submit(request)
    finally:
        db.close()

    assert venue.algo == [], "平仓前必须把算法单止损撤掉"


# -------------------------------------------------------------- HTTP 一层
@dataclass
class _State:
    ctx: object
    locks: dict
    account_lock: threading.RLock


class _Ctx:
    def __init__(self, rest: BinanceREST, db: Database) -> None:
        self.network = Network.TESTNET
        self.market_type = MarketType.USDM
        self.rest = rest
        settings = AppSettings(network=Network.TESTNET.value)
        self.trading = TradingService(
            settings=settings,
            rest=rest,
            risk=RiskManager(settings.risk),
            db=db,
            notifier=None,
        )
        self.account = AccountService(
            rest_for=lambda market: rest,
            chart_rest_for=lambda market: rest,
            db=db,
            network=Network.TESTNET,
        )

    def rest_for(self, market: MarketType) -> BinanceREST:
        return self.rest

    def quote_rest(self, market: MarketType) -> BinanceREST:
        return self.rest


def _endpoint(app, path: str, method: str):
    for route in app.routes:
        if getattr(route, "path", "") == path and method in getattr(route, "methods", ()):
            return route.endpoint
    raise AssertionError(f"missing route {method} {path}")


def test_the_delete_route_cancels_a_conditional_order(tmp_path, monkeypatch, venue) -> None:
    """用户点「撤单」拿到的必须是 200，而不是 404/503。"""
    rest = _rest(venue)
    db = _db(tmp_path)
    state = _State(
        ctx=_Ctx(rest, db),
        locks={market: threading.RLock() for market in MarketType},
        account_lock=threading.RLock(),
    )
    monkeypatch.setattr(web_app, "_state", state, raising=False)
    app = web_app.create_app()
    try:
        placed = rest.create_stop_market(MarketType.USDM, SYMBOL, "sell", 0.5, 95.0)
        endpoint = _endpoint(app, "/api/orders/{order_id}", "DELETE")
        result = endpoint(placed["id"], symbol=SYMBOL)
    finally:
        db.close()

    assert result["ok"] is True
    assert result["order"]["status"] == "canceled"
    assert venue.algo == []