"""Paper trading broker: fills orders against live prices without real money."""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass, field

from ..core.models import now_ms

log = logging.getLogger(__name__)


@dataclass
class PaperPosition:
    symbol: str
    contracts: float = 0.0
    entry_price: float = 0.0
    realized_pnl: float = 0.0
    fees: float = 0.0

    @property
    def side(self) -> str:
        if self.contracts > 0:
            return "long"
        if self.contracts < 0:
            return "short"
        return "flat"

    def unrealized(self, price: float) -> float:
        return (price - self.entry_price) * self.contracts if self.contracts else 0.0


@dataclass
class PaperAccount:
    quote_asset: str = "USDT"
    starting_balance: float = 10_000.0
    cash: float = 10_000.0
    positions: dict[str, PaperPosition] = field(default_factory=dict)
    orders: list[dict] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)

    @property
    def equity(self) -> float:
        return self.cash

    def position_for(self, symbol: str) -> PaperPosition:
        return self.positions.setdefault(symbol, PaperPosition(symbol))


class PaperBroker:
    """Minimal market/limit simulator.

    Market orders fill immediately at the supplied price plus slippage. Limit
    orders rest until ``try_fill`` is called with a matching price.
    """

    def __init__(
        self,
        starting_balance: float = 10_000.0,
        fee_rate: float = 0.0004,
        slippage_bps: float = 1.0,
        quote_asset: str = "USDT",
    ) -> None:
        self.fee_rate = fee_rate
        self.slippage_bps = slippage_bps
        self.account = PaperAccount(
            quote_asset=quote_asset,
            starting_balance=starting_balance,
            cash=starting_balance,
        )
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ order
    def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        amount: float,
        price: float | None = None,
        reduce_only: bool = False,
        allow_short: bool = True,
    ) -> dict:
        with self._lock:
            order_id = f"paper-{uuid.uuid4().hex[:12]}"
            ts = now_ms()
            order = {
                "id": order_id,
                "symbol": symbol,
                "side": side,
                "type": order_type,
                "amount": float(amount),
                "price": float(price) if price else 0.0,
                "filled": 0.0,
                "remaining": float(amount),
                "cost": 0.0,
                "status": "open",
                "timestamp": ts,
                "reduceOnly": reduce_only,
                "paper": True,
            }
            if order_type == "market" and price and price > 0:
                if not allow_short and side == "sell":
                    held = self.account.position_for(symbol).contracts
                    if amount > held + 1e-12:
                        order["status"] = "rejected"
                        order["rejectReason"] = "spot_no_short"
                        self.account.orders.append(order)
                        return dict(order)
                self._fill(order, price)
            self.account.orders.append(order)
            return dict(order)

    def try_fill(self, symbol: str, high: float, low: float) -> list[dict]:
        """Fill resting limit orders that the bar's range touched."""
        filled: list[dict] = []
        with self._lock:
            for order in self.account.orders:
                if order["status"] != "open" or order["symbol"] != symbol:
                    continue
                limit = order["price"]
                if order["side"] == "buy" and low <= limit:
                    self._fill(order, limit)
                    filled.append(dict(order))
                elif order["side"] == "sell" and high >= limit:
                    self._fill(order, limit)
                    filled.append(dict(order))
        return filled

    def cancel(self, order_id: str) -> bool:
        with self._lock:
            for order in self.account.orders:
                if order["id"] == order_id and order["status"] == "open":
                    order["status"] = "canceled"
                    return True
        return False

    def _fill(self, order: dict, price: float) -> None:
        qty = order["amount"]
        side = order["side"]
        fill = price
        if order["type"] == "market":
            delta = price * self.slippage_bps / 10_000.0
            fill = price + delta if side == "buy" else price - delta

        position = self.account.position_for(order["symbol"])
        signed = qty if side == "buy" else -qty
        old = position.contracts
        new = old + signed
        fee = abs(signed) * fill * self.fee_rate
        self.account.cash -= signed * fill + fee

        if old != 0 and (old > 0) != (signed > 0):
            closed = min(abs(signed), abs(old))
            direction = 1.0 if old > 0 else -1.0
            realized = (fill - position.entry_price) * closed * direction - fee
            position.realized_pnl += realized
        if abs(new) < 1e-12:
            position.contracts = 0.0
            position.entry_price = 0.0
        elif old == 0 or (old > 0) != (new > 0):
            position.entry_price = fill
            position.contracts = new
        else:
            position.entry_price = (position.entry_price * old + fill * signed) / new
            position.contracts = new
        position.fees += fee

        order["filled"] = qty
        order["remaining"] = 0.0
        order["cost"] = qty * fill
        order["average"] = fill
        order["status"] = "closed"
        order["filledAt"] = now_ms()
        self.account.trades.append(
            {
                "id": f"paper-trade-{uuid.uuid4().hex[:10]}",
                "order": order["id"],
                "symbol": order["symbol"],
                "side": side,
                "price": fill,
                "amount": qty,
                "cost": qty * fill,
                "fee": fee,
                "timestamp": now_ms(),
                "paper": True,
            }
        )

    # ------------------------------------------------------------------ state
    def position_quote(self, symbol: str, price: float) -> float:
        position = self.account.positions.get(symbol)
        if not position or not position.contracts:
            return 0.0
        return abs(position.contracts) * price

    def realized_pnl(self) -> float:
        return sum(p.realized_pnl for p in self.account.positions.values())

    def unrealized_pnl(self, prices: dict[str, float]) -> float:
        total = 0.0
        for symbol, position in self.account.positions.items():
            px = prices.get(symbol)
            if px:
                total += position.unrealized(px)
        return total

    def reset(self, balance: float | None = None) -> None:
        with self._lock:
            balance = self.account.starting_balance if balance is None else balance
            self.account = PaperAccount(
                quote_asset=self.account.quote_asset,
                starting_balance=balance,
                cash=balance,
            )
