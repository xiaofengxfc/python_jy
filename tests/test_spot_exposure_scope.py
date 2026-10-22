"""现货敞口的两个口径：仓位上限只算「本程序买的」，未实现盈亏只按账本数量算。

t51 实测两件事，根子是同一个混淆——把钱包里的币当成我们的持仓：

1. 仓位上限（`max_position_quote`）读的是钱包里那个币的余额。用户自己有 1 BTC
   （约 8.1 万）、上限 5000，于是**任何现货买单都 400**「持仓金额将达 81225.76，
   超过上限 5000.00」——现货一个都买不进来。合约侧本来就是按交易所持仓算的，不动。
2. `/api/account` 的现货行 `contracts` 取钱包数量，`entry_price` / `unrealized`
   取成交账本：钱包 1.00042、账本 0.00042 时，未实现盈亏按 1.00042 算，凭空多出
   一百多 USDT。

改后的口径：

- 现货敞口 = `min(本地成交账本里这一对的数量, 钱包数量)`——账本是我们买的，钱包是
  真在手上有的，取小的那个既不放宽（买得越多闸门越紧），也不会把用户自己的币算进来；
- 现货行的未实现盈亏只按**账本数量 × 账本均价**算，并带上 `unrealized_basis="ledger"`
  和 `ledger_size` 说明这是账本口径；`contracts` / `quote_value` 仍是钱包事实。
"""

from __future__ import annotations

import threading

import pytest

from btrader.core.config import AppSettings
from btrader.core.storage import Database
from btrader.core.types import MarketType, Network
from btrader.engine.account import AccountService
from btrader.engine.risk import RiskDecision, RiskManager
from btrader.engine.trading import LIVE_LEDGER_KEY, OrderRequest, TradingService

from fakes import FakeVenue

SPOT = "BTC/USDT"
FUTURES = "BTC/USDT:USDT"
PRICE = 81_000.0  # 1 BTC ≈ 8.1 万，一颗就把 5000 的上限顶穿


def _trading(tmp_path, *, wallet_btc: float, cap: float = 5_000.0, books: dict | None = None):
    venue = FakeVenue(
        balances={
            MarketType.SPOT: {"USDT": 10_000.0, "BTC": wallet_btc},
            MarketType.USDM: {"USDT": 10_000.0},
            MarketType.COINM: {"BTC": 0.2},
        }
    )
    venue._last[SPOT] = PRICE
    venue._last[FUTURES] = PRICE
    settings = AppSettings(network=Network.TESTNET.value)
    settings.risk.max_position_quote = cap
    db = Database(tmp_path / "exposure.db")
    if books is not None:
        db.set_json(LIVE_LEDGER_KEY, {"seen": [], "books": books})
    trading = TradingService(
        settings=settings,
        rest=venue,
        risk=RiskManager(settings.risk),
        db=db,
        notifier=None,
    )
    return trading, db, venue


def _spot_buy(trading: TradingService, quote: float = 10.0) -> RiskDecision:
    """打一张 10 USDT 的现货买单（数量按现价折算）。"""
    return trading.precheck(
        OrderRequest(
            symbol=SPOT,
            side="buy",
            order_type="market",
            amount=quote / PRICE,
            price=PRICE,
            market_type=MarketType.SPOT,
        )
    )


# ------------------------------------------------------- 1) 仓位上限的口径
def test_a_spot_buy_is_allowed_when_the_wallet_holds_coins_we_never_bought(tmp_path):
    """钱包里 1 BTC 是用户自己充的，不是我们的仓位：10 USDT 的买单必须放行。"""
    trading, db, _ = _trading(tmp_path, wallet_btc=1.0)
    try:
        decision = _spot_buy(trading)
        assert decision.allowed, decision.reason
    finally:
        db.close()


def test_the_cap_still_counts_what_the_program_bought(tmp_path):
    """闸门还在：账本里买了 0.1 BTC（8100），再买就被口径拦住。"""
    trading, db, _ = _trading(
        tmp_path, wallet_btc=1.0, books={SPOT: {"contracts": 0.1, "entry": 80_000.0}}
    )
    try:
        decision = _spot_buy(trading)
        assert decision.allowed is False
        assert "持仓金额将达 8110.00" in decision.reason, decision.reason
        assert "超过上限 5000.00" in decision.reason, decision.reason
    finally:
        db.close()


def test_the_exposure_never_exceeds_what_the_wallet_holds(tmp_path):
    """账本说 2 BTC，钱包只有 0.5：敞口按 0.5 算（币已经不在手上了）。"""
    loose = tmp_path / "loose"
    loose.mkdir()
    trading, db, _ = _trading(
        loose,
        wallet_btc=0.5,
        cap=50_000.0,
        books={SPOT: {"contracts": 2.0, "entry": 80_000.0}},
    )
    try:
        # 0.5 * 81000 + 10 = 40510，在 5 万以内
        assert _spot_buy(trading).allowed is True
    finally:
        db.close()

    tight = tmp_path / "tight"
    tight.mkdir()
    trading, db, _ = _trading(
        tight,
        wallet_btc=0.5,
        cap=40_000.0,
        books={SPOT: {"contracts": 2.0, "entry": 80_000.0}},
    )
    try:
        decision = _spot_buy(trading)
        assert decision.allowed is False
        # 不是 2 * 81000 = 162010，而是钱包的 40500 + 10
        assert "持仓金额将达 40510.00" in decision.reason, decision.reason
    finally:
        db.close()


def test_a_futures_buy_still_counts_the_venue_position(tmp_path):
    """合约侧一个字节不动：敞口仍来自交易所那份持仓。"""
    trading, db, venue = _trading(tmp_path, wallet_btc=0.0)
    try:
        venue.create_order(MarketType.USDM, FUTURES, "market", "buy", 1.0, PRICE)
        decision = trading.precheck(
            OrderRequest(
                symbol=FUTURES,
                side="buy",
                order_type="market",
                amount=10.0 / PRICE,
                price=PRICE,
                market_type=MarketType.USDM,
            )
        )
        assert decision.allowed is False
        assert "持仓金额将达 81010.00" in decision.reason, decision.reason
    finally:
        db.close()


# ------------------------------------------------- 2) 现货行的未实现盈亏口径
def _account(tmp_path, venue: FakeVenue) -> tuple[AccountService, Database]:
    db = Database(tmp_path / "account.db")
    service = AccountService(
        rest_for=lambda market: venue,
        chart_rest_for=lambda market: venue,
        db=db,
        network=Network.TESTNET,
    )
    return service, db


def _spot_venue(wallet_btc: float = 1.00042, last: float = PRICE) -> FakeVenue:
    venue = FakeVenue(balances={MarketType.SPOT: {"USDT": 0.0, "BTC": wallet_btc}})
    venue._last[SPOT] = last
    return venue


def _seed_buy(db: Database, amount: float, price: float, trade_id: str = "t1") -> None:
    db.save_trade(
        "binance:testnet",
        {
            "id": trade_id,
            "symbol": SPOT,
            "side": "buy",
            "price": price,
            "amount": amount,
            "cost": amount * price,
            "fee": {"cost": 0.0, "currency": "USDT"},
            "timestamp": 1,
        },
        "live",
    )


def test_a_spot_row_books_pnl_on_the_ledger_size_not_the_wallet(tmp_path):
    """钱包 1.00042、账本 0.00042：未实现盈亏只能是 0.00042 那一份。"""
    venue = _spot_venue()
    service, db = _account(tmp_path, venue)
    try:
        _seed_buy(db, 0.00042, 80_000.0)
        snap = service.snapshot(MarketType.SPOT, SPOT)
        row = snap.positions[0]
        assert row["contracts"] == pytest.approx(1.00042)  # 钱包事实
        assert row["quote_value"] == pytest.approx(1.00042 * PRICE)
        assert row["entry_price"] == pytest.approx(80_000.0)
        assert row["ledger_size"] == pytest.approx(0.00042)
        assert row["unrealized_basis"] == "ledger"
        # (81000 - 80000) * 0.00042 = 0.42，不是 1000.42
        assert row["unrealized"] == pytest.approx(0.42)
    finally:
        db.close()


def test_pnl_does_not_move_when_more_user_coins_arrive(tmp_path):
    """用户又往钱包里充 1 BTC：我的盈亏一个数都不该动。"""
    venue = _spot_venue()
    service, db = _account(tmp_path, venue)
    try:
        _seed_buy(db, 0.00042, 80_000.0)
        before = service.snapshot(MarketType.SPOT, SPOT).positions[0]

        venue.balances[MarketType.SPOT]["BTC"] = 2.00042
        after = service.snapshot(MarketType.SPOT, SPOT, refresh=True).positions[0]

        assert after["contracts"] == pytest.approx(2.00042)
        assert after["unrealized"] == pytest.approx(before["unrealized"])
        assert after["unrealized"] == pytest.approx(0.42)
    finally:
        db.close()


def test_a_wallet_only_row_reports_no_unrealized_pnl(tmp_path):
    """没有成交账本的行就是钱包里原有的币：不报浮盈浮亏。"""
    venue = _spot_venue()
    service, db = _account(tmp_path, venue)
    try:
        row = service.snapshot(MarketType.SPOT, SPOT).positions[0]
        assert row["origin"] == "wallet"
        assert row["entry_price"] == 0.0
        assert row["ledger_size"] == 0.0
        assert row["unrealized_basis"] == ""
        assert row["unrealized"] == 0.0
    finally:
        db.close()


def test_the_api_payload_carries_the_basis_fields(tmp_path):
    """`/api/account` 直接把行原样发出去：这两个字段必须过到前端。"""
    venue = _spot_venue()
    service, db = _account(tmp_path, venue)
    try:
        _seed_buy(db, 0.00042, 80_000.0)
        payload = service.snapshot(MarketType.SPOT, SPOT).as_payload()
        row = payload["positions"][0]
        assert row["unrealized_basis"] == "ledger"
        assert row["ledger_size"] == pytest.approx(0.00042)
    finally:
        db.close()

# ---------------------------------------- 3) AI 交易的仓位上限同一口径
class _AiCtx:
    """只够 `_ai_portfolio` 用：账户快照 + 风控 + 取价。"""

    def __init__(self, venue: FakeVenue, db: Database, market: MarketType) -> None:
        self.settings = AppSettings(network=Network.TESTNET.value)
        self.network = Network.TESTNET
        self.market_type = market
        self.venue = venue
        self.trading = TradingService(
            settings=self.settings,
            rest=venue,
            risk=RiskManager(self.settings.risk),
            db=db,
            notifier=None,
        )
        self.account = AccountService(
            rest_for=lambda market: venue,
            chart_rest_for=lambda market: venue,
            db=db,
            network=Network.TESTNET,
        )

    def market_rest_for(self, market: MarketType):
        return self.venue


class _AiState:
    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self.locks = {market: threading.RLock() for market in MarketType}
        self.account_lock = threading.RLock()


def test_the_ai_position_cap_counts_only_what_the_program_bought(tmp_path):
    """AI 交易同一道闸门：现货敞口按账本算，用户自己的 1 BTC 不算进来。"""
    from btrader.web.routes import _ai_portfolio

    venue = _spot_venue()
    db = Database(tmp_path / "ai.db")
    try:
        _seed_buy(db, 0.00042, 80_000.0)
        ctx = _AiCtx(venue, db, MarketType.SPOT)
        view = _ai_portfolio(_AiState(ctx), SPOT)
        # 0.00042 * 81000 = 34.02，不是钱包估值 81034.02
        assert view["position_quote"] == pytest.approx(0.00042 * PRICE, rel=1e-6)
    finally:
        db.close()


def test_the_ai_position_cap_on_futures_still_reads_the_position(tmp_path):
    """合约侧不动：敞口仍来自交易所那份持仓。"""
    from btrader.web.routes import _ai_portfolio

    venue = FakeVenue(balances={MarketType.USDM: {"USDT": 10_000.0}})
    venue._last[FUTURES] = PRICE
    venue.create_order(MarketType.USDM, FUTURES, "market", "buy", 1.0, PRICE)
    db = Database(tmp_path / "ai-futures.db")
    try:
        ctx = _AiCtx(venue, db, MarketType.USDM)
        view = _ai_portfolio(_AiState(ctx), FUTURES)
        assert view["position_quote"] == pytest.approx(PRICE)
    finally:
        db.close()
