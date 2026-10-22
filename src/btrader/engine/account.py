"""Exchange account snapshots: the one source of account truth.

Every page — trading, portfolio, auto trading, AI trading — reads the *same*
venue account. This module is the single place that turns signed exchange
calls into the payload shape the UI renders.

Two things live here on purpose:

* replay_ledger() re-derives per-pair realized PnL and the open leg from the
  trade log. The venue reports fills, not cost basis, so a weighted-average
  book is the only way to answer "what did this pair actually make".
* AccountService caches one snapshot per market. The panels poll every few
  seconds; signing a balance + positions + open-orders request per poll would
  burn the request weight budget for no benefit.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..core.storage import Database
from ..core.types import STOP_TYPES, TAKE_PROFIT_TYPES, MarketType, Network

log = logging.getLogger(__name__)

# Equity baseline for "总盈亏": written once per account so a fresh install
# starts from its own first-seen equity instead of a made-up starting balance.
BASELINE_KEY = "account.baseline.v1"


# How many stored fills the ledger replay reads. Long enough to cover a busy
# day of scalping, short enough that the replay stays microseconds.
LEDGER_TRADE_LIMIT = 500
# Spot wallets list one row per coin; beyond this the table is noise.
SPOT_POSITION_LIMIT = 60
# Bulk ticker calls go in batches so one rejected symbol cannot blank the rest.
PRICE_BATCH = 100


def quote_asset_for(market: MarketType) -> str:
    """The wallet currency a market settles in.

    USD-M margin is USDT; coin-M margin is the base coin itself (BTC for
    BTCUSD), so its balance and PnL are denominated in that coin.
    """
    return "BTC" if market is MarketType.COINM else "USDT"


def replay_ledger(trades: list[dict]) -> dict[str, dict[str, float]]:
    """Rebuild each pair's open leg and realized PnL from the fill log.

    Returns {symbol: {size, entry, realized, fees}}. "realized" is the
    cumulative *net* result of every closed round on that pair (commissions
    included), "entry" is the weighted average cost of the leg still open.

    This is deliberately the same accounting the old paper broker used: an
    entry fee is a cost booked immediately, a closing fill books the price
    move minus its own commission. Without it the panel can only show what
    the venue chose to report, which for spot is nothing at all.
    """
    book: dict[str, dict[str, float]] = {}
    for trade in sorted(trades, key=lambda item: item.get("timestamp") or 0):
        symbol = str(trade.get("symbol") or "")
        if not symbol:
            continue
        state = book.setdefault(
            symbol, {"size": 0.0, "entry": 0.0, "realized": 0.0, "fees": 0.0}
        )
        amount = abs(float(trade.get("amount") or 0.0))
        if amount <= 0:
            continue
        signed = amount if trade.get("side") == "buy" else -amount
        fee = abs(float(trade.get("fee") or 0.0))
        fill = float(trade.get("price") or 0.0)
        old = state["size"]
        new = old + signed
        if old != 0 and (old > 0) != (signed > 0):
            closed = min(abs(signed), abs(old))
            direction = 1.0 if old > 0 else -1.0
            state["realized"] += (fill - state["entry"]) * closed * direction
        state["fees"] += fee
        # Every commission is a cost, whichever side of the round it belongs to.
        state["realized"] -= fee
        if abs(new) < 1e-12:
            state["entry"] = 0.0
        elif old == 0 or (old > 0) != (new > 0):
            state["entry"] = fill
        elif (old > 0) == (signed > 0):
            state["entry"] = (state["entry"] * old + fill * signed) / new
        state["size"] = new
    return book


@dataclass
class Snapshot:
    """One market's account view, already shaped for the UI."""

    market: MarketType
    quote_asset: str
    cash: float = 0.0
    equity: float = 0.0
    baseline: float = 0.0
    positions: list[dict[str, Any]] = field(default_factory=list)
    orders: list[dict[str, Any]] = field(default_factory=list)
    trades: list[dict[str, Any]] = field(default_factory=list)
    protections: list[dict[str, Any]] = field(default_factory=list)
    realized_by_symbol: dict[str, float] = field(default_factory=dict)
    has_credentials: bool = False
    error: str = ""
    updated_at: float = 0.0

    @property
    def realized_pnl(self) -> float:
        return sum(self.realized_by_symbol.values())

    def as_payload(self) -> dict[str, Any]:
        """One snapshot, two callers: the trading panel and the 资产 page."""
        return {
            "quote_asset": self.quote_asset,
            "cash": self.cash,
            "starting_balance": self.baseline,
            "equity": self.equity,
            "realized_pnl": self.realized_pnl,
            "realized_pnl_by_symbol": dict(self.realized_by_symbol),
            "positions": list(self.positions),
            "orders": list(self.orders),
            "trades": list(self.trades),
            "protections": list(self.protections),
            "has_credentials": self.has_credentials,
            "error": self.error,
            "updated_at": self.updated_at,
        }


class AccountService:
    """Reads the venue account and caches it for the polling panels."""

    def __init__(
        self,
        *,
        rest_for: Callable[[MarketType], Any],
        chart_rest_for: Callable[[MarketType], Any],
        db: Database,
        network: Network,
        ttl: float = 3.0,
        wallet_ttl: float = 10.0,
    ) -> None:
        self._rest_for = rest_for
        self._chart_rest_for = chart_rest_for
        self.db = db
        self.network = network
        self.ttl = ttl
        # Equity is a whole-wallet figure and pricing every coin is the
        # expensive part, so it is refreshed on a slower beat than a pair read.
        self.wallet_ttl = wallet_ttl
        self._lock = threading.RLock()
        self._cache: dict[tuple[MarketType, str], Snapshot] = {}

    # ------------------------------------------------------------------ public
    def invalidate(self) -> None:
        """Drop every cached snapshot (network, key or proxy changed)."""
        with self._lock:
            self._cache.clear()

    def snapshot(
        self,
        market: MarketType,
        symbol: str | None = None,
        *,
        refresh: bool = False,
    ) -> Snapshot:
        """Account numbers for one market, optionally narrowed to one pair."""
        if symbol:
            pair = self._read(market, symbol, self.ttl, refresh)
            if pair.has_credentials:
                # A pair read prices one coin; the account is the whole
                # wallet. Borrowing the wallet's totals keeps the sidebar and
                # the 资产 page quoting the same equity.
                wallet = self._read(market, None, self.wallet_ttl, False)
                if not wallet.error:
                    pair.equity = wallet.equity
                    pair.cash = wallet.cash
                    pair.baseline = wallet.baseline
                    pair.realized_by_symbol = wallet.realized_by_symbol
            return pair
        return self._read(market, None, self.wallet_ttl, refresh)

    def _read(
        self, market: MarketType, symbol: str | None, ttl: float, refresh: bool
    ) -> Snapshot:
        key = (market, symbol or "")
        with self._lock:
            cached = self._cache.get(key)
        fresh = cached is not None and (time.time() - cached.updated_at) < ttl
        if cached is not None and fresh and not refresh and not cached.error:
            return cached
        snap = self._load(market, symbol)
        with self._lock:
            # A failed read must not erase the last good numbers: the panels
            # would flash zeros and look like an empty account.
            if snap.error and cached is not None and cached.positions:
                cached.error = snap.error
                return cached
            self._cache[key] = snap
        return snap

    def refresh_all(self, markets: list[MarketType] | None = None) -> None:
        """Refresh every market the panels might show (background thread)."""
        for market in markets or list(MarketType):
            try:
                self.snapshot(market, refresh=True)
            except Exception:  # the poller must survive any venue hiccup
                log.debug("刷新账户快照失败 %s", market.value, exc_info=True)

    # ------------------------------------------------------------------- build
    def _load(self, market: MarketType, symbol: str | None) -> Snapshot:
        snap = Snapshot(
            market=market,
            quote_asset=quote_asset_for(market),
            updated_at=time.time(),
        )
        rest = self._rest_for(market)
        snap.has_credentials = bool(getattr(rest, "has_credentials", False))
        if not snap.has_credentials:
            snap.error = "该市场尚未配置 API Key"
            return snap

        try:
            balance = rest.fetch_balance(market)
        except Exception as exc:
            snap.error = f"读取余额失败：{exc}"
            log.warning("读取账户余额失败 %s：%s", market.value, exc)
            return snap

        ledger = self._ledger()
        snap.realized_by_symbol = {
            name: round(state["realized"], 8) for name, state in ledger.items()
        }

        if market.is_futures:
            snap.positions = self._futures_positions(rest, market, symbol, ledger, snap)
        else:
            snap.positions = self._spot_positions(balance, symbol, ledger, snap)

        snap.cash = self._cash(balance, snap.quote_asset)
        holdings = sum(float(item.get("quote_value") or 0.0) for item in snap.positions)
        unrealized = sum(float(item.get("unrealized") or 0.0) for item in snap.positions)
        total = self._total(balance, snap.quote_asset)
        if market.is_futures:
            # Wallet balance plus open PnL: margin locked in a position is
            # still the trader's money.
            snap.equity = total + unrealized
        else:
            snap.equity = total + holdings
        if symbol is None and len(snap.positions) > SPOT_POSITION_LIMIT:
            # A testnet wallet can hold hundreds of dust coins. Equity above is
            # already the whole truth; the payload is capped so the tables stay
            # readable. A single-pair read is never capped.
            snap.positions = snap.positions[:SPOT_POSITION_LIMIT]

        try:
            snap.orders = self._open_orders(rest, market, symbol)
        except Exception as exc:
            log.warning("读取挂单失败 %s：%s", market.value, exc)
        snap.protections = self._protections(snap.orders)
        snap.trades = self._recent_trades()
        # Only a whole-wallet read may set the reference point: a pair read
        # prices one coin and would freeze a partial equity as "the start".
        snap.baseline = (
            self._baseline(market, snap.equity)
            if symbol is None
            else self._stored_baseline(market)
        )
        return snap

    # -------------------------------------------------------------- positions
    def _futures_positions(
        self,
        rest: Any,
        market: MarketType,
        symbol: str | None,
        ledger: dict[str, dict[str, float]],
        snap: Snapshot,
    ) -> list[dict[str, Any]]:
        try:
            raw = rest.fetch_positions(market, [symbol] if symbol else None)
        except Exception as exc:
            snap.error = f"读取持仓失败：{exc}"
            log.warning("读取持仓失败 %s：%s", market.value, exc)
            return []
        spec = None
        if symbol:
            try:
                spec = rest.contract_spec(market, symbol)
            except Exception:
                spec = None
        out: list[dict[str, Any]] = []
        for item in raw:
            contracts = float(item.get("contracts") or 0.0)
            if abs(contracts) <= 1e-12:
                continue
            pair = str(item.get("symbol") or "")
            short = item.get("side") == "short"
            signed = -abs(contracts) if short else abs(contracts)
            entry = float(item.get("entryPrice") or 0.0)
            mark = float(item.get("markPrice") or 0.0)
            unrealized = item.get("unrealizedPnl")
            if unrealized is None:
                unrealized = (mark - entry) * signed if mark and entry else 0.0
            notional = abs(contracts) * (mark or entry)
            if spec is not None and pair == symbol:
                try:
                    notional = spec.notional(abs(contracts), mark or entry)
                except Exception:
                    pass
            state = ledger.get(pair) or {}
            out.append(
                {
                    "symbol": pair,
                    "contracts": signed,
                    "side": "short" if short else "long",
                    "entry_price": entry,
                    "last": mark or entry,
                    "unrealized": float(unrealized or 0.0),
                    "quote_value": float(notional),
                    "realized_pnl": float(state.get("realized") or 0.0),
                    "fees": float(state.get("fees") or 0.0),
                }
            )
        return out

    def _spot_positions(
        self,
        balance: Any,
        symbol: str | None,
        ledger: dict[str, dict[str, float]],
        snap: Snapshot,
    ) -> list[dict[str, Any]]:
        """Spot holdings, priced in one bulk ticker call.

        Balance "total" (not "free") is used: coins locked in a resting sell
        order are still held. Assets the venue does not quote against the
        wallet currency (testnet junk, delisted coins) are skipped rather than
        priced at zero, which would silently shrink equity.
        """
        totals = balance.get("total") if isinstance(balance, dict) else None
        if not isinstance(totals, dict):
            return []
        held: dict[str, float] = {}
        for asset, amount in totals.items():
            try:
                value = float(amount or 0.0)
            except (TypeError, ValueError):
                continue
            if value > 0:
                held[str(asset).upper()] = value
        quote = snap.quote_asset
        held.pop(quote, None)
        wanted = {f"{asset}/{quote}": asset for asset in held}
        if symbol:
            # The trading panel only asks about one pair; keep the bulk call to
            # that pair instead of pricing the whole wallet on every poll.
            base = symbol.split("/")[0].upper()
            wanted = {name: asset for name, asset in wanted.items() if asset == base}
        if not wanted:
            return []
        prices: dict[str, float] = {}
        chart = self._chart_rest_for(MarketType.SPOT)
        # Only ask for pairs the venue lists. A testnet wallet carries assets
        # with no market at all, and one unknown symbol makes a per-symbol call
        # fail, which would price nothing instead of most things.
        try:
            listed = chart.markets(MarketType.SPOT)
            if listed:
                wanted = {name: asset for name, asset in wanted.items() if name in listed}
        except Exception as exc:
            log.debug("读取市场列表失败：%s", exc)
        if wanted:
            # One request for the whole wallet beats a call per pair: the
            # all-symbols ticker is a single, cheap endpoint.
            try:
                everything = chart.fetch_tickers(MarketType.SPOT)
            except Exception as exc:
                log.debug("读取全部行情失败：%s", exc)
                everything = {}
            if everything:
                for name in wanted:
                    ticker = everything.get(name) or {}
                    last = float(ticker.get("last") or 0.0)
                    if last > 0:
                        prices[name] = last
            else:
                for start_at in range(0, len(wanted), PRICE_BATCH):
                    batch = list(wanted)[start_at : start_at + PRICE_BATCH]
                    try:
                        tickers = chart.fetch_tickers(MarketType.SPOT, batch)
                    except Exception as exc:
                        log.debug("批量取价失败（%d 个）：%s", len(batch), exc)
                        continue
                    for name, ticker in (tickers or {}).items():
                        last = float((ticker or {}).get("last") or 0.0)
                        if last > 0:
                            prices[name] = last
        out: list[dict[str, Any]] = []
        for name, asset in wanted.items():
            amount = held.get(asset, 0.0)
            last = prices.get(name, 0.0)
            if amount <= 0 or last <= 0:
                continue
            state = ledger.get(name) or {}
            entry = float(state.get("entry") or 0.0)
            out.append(
                {
                    "symbol": name,
                    "contracts": amount,
                    "side": "long",
                    "entry_price": entry,
                    "last": last,
                    "unrealized": (last - entry) * amount if entry else 0.0,
                    "quote_value": amount * last,
                    "realized_pnl": float(state.get("realized") or 0.0),
                    "fees": float(state.get("fees") or 0.0),
                }
            )
        return sorted(out, key=lambda item: item["quote_value"], reverse=True)

    # ----------------------------------------------------------------- orders
    def _open_orders(
        self, rest: Any, market: MarketType, symbol: str | None
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for order in rest.fetch_open_orders(market, symbol):
            out.append(
                {
                    "id": str(order.get("id") or ""),
                    "symbol": order.get("symbol") or "",
                    "market": market.value,
                    "side": order.get("side") or "buy",
                    "type": order.get("type") or "limit",
                    "amount": float(order.get("amount") or 0.0),
                    "price": float(order.get("price") or 0.0),
                    "trigger": float(
                        order.get("triggerPrice") or order.get("stopPrice") or 0.0
                    ),
                    "filled": float(order.get("filled") or 0.0),
                    "reduce_only": bool(order.get("reduceOnly")),
                    "status": "open",
                    "timestamp": int(order.get("timestamp") or 0),
                }
            )
        return out

    def _protections(self, orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Group the venue's stop / take-profit orders into per-symbol guards."""
        guards: dict[str, dict[str, Any]] = {}
        for order in orders:
            kind = str(order.get("type") or "").lower()
            is_stop = kind in STOP_TYPES
            is_profit = kind in TAKE_PROFIT_TYPES
            if not (is_stop or is_profit):
                continue
            level = float(order.get("trigger") or order.get("price") or 0.0)
            if level <= 0:
                continue
            pair = str(order.get("symbol") or "")
            guard = guards.setdefault(
                pair,
                {"symbol": pair, "stop_loss": 0.0, "take_profit": 0.0, "orders": []},
            )
            if is_stop:
                guard["stop_loss"] = level
            else:
                guard["take_profit"] = level
            guard["orders"].append(order.get("id"))
        return list(guards.values())

    # ------------------------------------------------------------ trade ledger
    def _recent_trades(self) -> list[dict[str, Any]]:
        try:
            rows = self.db.recent_trades(limit=60, mode="live")
        except Exception:
            log.debug("读取成交历史失败", exc_info=True)
            return []
        return [
            {
                "id": row["id"],
                "symbol": row["symbol"],
                "side": row["side"],
                "price": row["price"],
                "amount": row["amount"],
                "cost": row["cost"],
                "fee": row["fee"],
                "timestamp": row["timestamp"],
            }
            for row in rows
        ]

    def _ledger(self) -> dict[str, dict[str, float]]:
        try:
            rows = self.db.recent_trades(limit=LEDGER_TRADE_LIMIT, mode="live")
        except Exception:
            log.debug("读取成交账本失败", exc_info=True)
            return {}
        return replay_ledger(
            [
                {
                    "symbol": row["symbol"],
                    "side": row["side"],
                    "price": row["price"],
                    "amount": row["amount"],
                    "fee": row["fee"],
                    "timestamp": row["timestamp"],
                }
                for row in rows
            ]
        )

    # ------------------------------------------------------------------ wallet
    @staticmethod
    def _cash(balance: Any, asset: str) -> float:
        free = balance.get("free") if isinstance(balance, dict) else None
        return _as_float(free.get(asset)) if isinstance(free, dict) else 0.0

    @staticmethod
    def _total(balance: Any, asset: str) -> float:
        total = balance.get("total") if isinstance(balance, dict) else None
        return _as_float(total.get(asset)) if isinstance(total, dict) else 0.0

    # ---------------------------------------------------------------- baseline
    def _stored_baseline(self, market: MarketType) -> float:
        """The equity this account started at, or 0 when never recorded."""
        key = f"{BASELINE_KEY}:{self.network.value}:{market.value}"
        try:
            raw = self.db.get_value(key)
            if raw:
                return float(json.loads(raw))
        except Exception:
            log.debug("读取权益基线失败", exc_info=True)
        return 0.0

    def _baseline(self, market: MarketType, equity: float) -> float:
        """First-seen equity for this account, so "总盈亏" has a reference."""
        key = f"{BASELINE_KEY}:{self.network.value}:{market.value}"
        known = self._stored_baseline(market)
        if known:
            return known
        if equity <= 0:
            return 0.0
        try:
            self.db.set_value(key, json.dumps(float(equity)))
        except Exception:  # pragma: no cover - storage is best effort
            log.debug("写入权益基线失败", exc_info=True)
        return float(equity)


def _as_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
