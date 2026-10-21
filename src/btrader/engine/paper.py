"""Paper trading broker: fills orders against live prices without real money."""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass, field
from typing import Callable

from ..core.models import now_ms

log = logging.getLogger(__name__)


@dataclass
class PaperPosition:
    symbol: str
    contracts: float = 0.0
    entry_price: float = 0.0
    # Booked result of *this* position only, net of every fee it has paid so
    # far. A brand-new position starts at 0 and never inherits the pair, but
    # the commission of getting in is deducted the moment it is paid.
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
    # Stop-loss / take-profit guards, one per symbol.  They live on the
    # account (instead of on the order) so they survive a restart and keep
    # working after the entry order is long closed.
    protections: dict[str, dict] = field(default_factory=dict)
    # Standalone conditional orders (a stop entry, or an exit armed before the
    # position exists). They wait for a trigger price and then become a normal
    # market/limit order; unlike ``protections`` they are not tied to a
    # position's size and can be placed while a limit entry is still resting.
    conditional: list[dict] = field(default_factory=list)
    # Cumulative realized PnL per pair.  It lives on the account, not on the
    # position, because a position is dropped from the snapshot (and from every
    # API payload) the moment it goes flat: a booked result has to outlive that,
    # or every closed round silently disappears from the panel and from the
    # total as soon as the app restarts.
    realized_by_symbol: dict[str, float] = field(default_factory=dict)

    @property
    def equity(self) -> float:
        return self.cash

    @property
    def realized_pnl(self) -> float:
        """Booked result of the whole account, closed rounds included."""
        return sum(self.realized_by_symbol.values())

    def position_for(self, symbol: str) -> PaperPosition:
        return self.positions.setdefault(symbol, PaperPosition(symbol))


def _replay_realized(
    trades: list[dict],
) -> tuple[dict[str, float], dict[str, float]]:
    """Per-pair booked result and opening-side fees, rebuilt from the log.

    Every fill is classified exactly like :meth:`PaperBroker._fill` does it —
    by replaying the signed size — so a fill that only opens a position is
    recognised as such even though it books no price move.  Returns
    ``(closing PnL, opening fees)``: closing PnL is what the trade recorded
    for the closed part (already net of that fill's fee), opening fees is the
    commission paid by the fills that added exposure.
    """
    booked: dict[str, float] = {}
    open_fees: dict[str, float] = {}
    sizes: dict[str, float] = {}
    for trade in sorted(trades, key=lambda item: item.get("timestamp") or 0):
        symbol = str(trade.get("symbol") or "")
        if not symbol:
            continue
        amount = float(trade.get("amount") or 0.0)
        signed = amount if trade.get("side") == "buy" else -amount
        held = sizes.get(symbol, 0.0)
        if held != 0 and (held > 0) != (signed > 0):
            booked[symbol] = booked.get(symbol, 0.0) + float(
                trade.get("realized") or 0.0
            )
        else:
            open_fees[symbol] = open_fees.get(symbol, 0.0) + float(
                trade.get("fee") or 0.0
            )
        sizes[symbol] = held + signed
    return booked, open_fees


def _replay_positions(trades: list[dict]) -> dict[str, dict[str, float]]:
    """Rebuild every pair's *open* leg from the trade log.

    Returns ``{symbol: {size, tally, fees}}`` for the leg that is still open:
    the numbers :meth:`PaperBroker._fill` keeps on the position object (the
    price move of what this leg closed minus every commission it paid).
    Used to repair a snapshot whose position figure was saved under older
    semantics, where it held the pair's whole history instead of this leg's.
    """
    book: dict[str, dict[str, float]] = {}
    for trade in sorted(trades, key=lambda item: item.get("timestamp") or 0):
        symbol = str(trade.get("symbol") or "")
        if not symbol:
            continue
        state = book.setdefault(
            symbol,
            {"size": 0.0, "entry": 0.0, "tally": 0.0, "fees": 0.0},
        )
        amount = float(trade.get("amount") or 0.0)
        signed = amount if trade.get("side") == "buy" else -amount
        fee = float(trade.get("fee") or 0.0)
        fill = float(trade.get("price") or 0.0)
        old = state["size"]
        new = old + signed
        if abs(old) < 1e-12:
            # A new leg starts its own tally here.
            state["tally"] = 0.0
            state["fees"] = 0.0
        closed = 0.0
        price_pnl = 0.0
        if old != 0 and (old > 0) != (signed > 0):
            closed = min(abs(signed), abs(old))
            direction = 1.0 if old > 0 else -1.0
            price_pnl = (fill - state["entry"]) * closed * direction
        opened = max(abs(signed) - closed, 0.0)
        state["fees"] += fee
        state["tally"] += price_pnl - fee
        if old != 0 and new != 0 and (old > 0) != (new > 0):
            state["tally"] = -fee * (opened / abs(signed))
        if abs(new) < 1e-12:
            state["entry"] = 0.0
        elif old == 0 or (old > 0) != (new > 0):
            state["entry"] = fill
        elif (old > 0) == (signed > 0):
            state["entry"] = (state["entry"] * old + fill * signed) / new
        state["size"] = new
    return book


def _log_covers_the_account(account: PaperAccount, trades: list[dict]) -> bool:
    """Whether the trade log can account for the whole cash balance.

    Only a fill moves paper cash, so a log that replays to the stored cash
    must contain every fill the account ever made.  That is what makes it safe
    to recompute the ledger from it: if a fill were missing, the replayed cash
    would not match.  Rounds older than a truncated (500-row) log fail this
    check, and then the saved ledger is kept because it knows more than the log.
    """
    flows = 0.0
    for trade in trades:
        amount = float(trade.get("amount") or 0.0)
        signed = amount if trade.get("side") == "buy" else -amount
        flows -= signed * float(trade.get("price") or 0.0)
        flows -= float(trade.get("fee") or 0.0)
    return abs(account.starting_balance + flows - account.cash) <= 1e-6

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
        # Last traded price per symbol, used to fill resting limit orders.
        self._marks: dict[str, float] = {}
        # Called with every order this broker *created on its own* from a
        # conditional order. ``TradingService`` records the handle so the
        # background reconciler, which books and persists whatever ``mark``
        # returns, cannot record the same fill a second time. Orders placed
        # through ``place_order`` are not reported; their caller handles them.
        self._on_order: Callable[[dict], None] | None = None

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
        market: str = "spot",
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> dict:
        with self._lock:
            order_id = f"paper-{uuid.uuid4().hex[:12]}"
            ts = now_ms()
            order = {
                "id": order_id,
                "symbol": symbol,
                "market": market,
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
                "allowShort": bool(allow_short),
                "paper": True,
                "realized": 0.0,
            }
            if stop_loss or take_profit:
                self._set_protection(order, stop_loss, take_profit)
            if order_type == "market" and price and price > 0:
                if not allow_short and side == "sell":
                    held = self.account.position_for(symbol).contracts
                    if amount > held + 1e-12:
                        order["status"] = "rejected"
                        order["rejectReason"] = "spot_no_short"
                        self.account.orders.append(order)
                        return dict(order)
                order["realized"] = self._fill(order, price)
            self.account.orders.append(order)
            return dict(order)

    # ------------------------------------------------------------ protection
    def _set_protection(self, order: dict, stop_loss: float | None, take_profit: float | None) -> None:
        """Attach SL/TP guards to the account for this order's symbol.

        The guard is stored per symbol rather than per order: an exit must keep
        working even after the entry order has been closed (a market entry
        closes its order the moment it fills), and it must survive a restart.

        ``side`` records which direction the guard protects. It is derived from
        the entry order (buy -> long, sell -> short) because a resting limit
        entry has no position yet; the exit itself still follows the real
        position, so a reversed entry can never fire the wrong way.
        """
        symbol = order["symbol"]
        existing = self.account.protections.get(symbol, {})
        side = "long" if order["side"] == "buy" else "short"
        # A new entry in the opposite direction replaces the old guard rather
        # than inheriting stop levels that belong to the other position.
        base = existing if existing.get("side", side) == side else {}
        sl = float(stop_loss) if stop_loss and stop_loss > 0 else base.get("stopLoss", 0.0)
        tp = float(take_profit) if take_profit and take_profit > 0 else base.get("takeProfit", 0.0)
        if sl <= 0 and tp <= 0:
            self.account.protections.pop(symbol, None)
            return
        self.account.protections[symbol] = {
            "symbol": symbol,
            "side": side,
            "stopLoss": sl,
            "takeProfit": tp,
            "createdAt": now_ms(),
            "order": order["id"],
        }
        order["stopLoss"] = sl
        order["takeProfit"] = tp

    def set_protection(
        self,
        symbol: str,
        stop_loss: float | None,
        take_profit: float | None,
        side: str | None = None,
    ) -> dict:
        """Arm or update a guard for a symbol that already has a position.

        Unlike :meth:`_set_protection` this is not tied to an order, so the UI
        can add a stop to a position that was opened without one.
        """
        with self._lock:
            position = self.account.positions.get(symbol)
            position_side = ""
            if position and abs(position.contracts) > 1e-12:
                position_side = "long" if position.contracts > 0 else "short"
            resolved = position_side or (side or "")
            if resolved not in ("long", "short"):
                resolved = "long"
            sl = float(stop_loss) if stop_loss and stop_loss > 0 else 0.0
            tp = float(take_profit) if take_profit and take_profit > 0 else 0.0
            if sl <= 0 and tp <= 0:
                self.account.protections.pop(symbol, None)
                return {}
            guard = {
                "symbol": symbol,
                "side": resolved,
                "stopLoss": sl,
                "takeProfit": tp,
                "createdAt": now_ms(),
                "order": "",
            }
            self.account.protections[symbol] = guard
            return dict(guard)

    def protections(self) -> dict[str, dict]:
        with self._lock:
            return {symbol: dict(guard) for symbol, guard in self.account.protections.items()}

    def clear_protection(self, symbol: str) -> bool:
        """Disarm the SL/TP guard for a symbol. Returns whether one existed."""
        with self._lock:
            return self.account.protections.pop(symbol, None) is not None

    def _check_protections(self, symbol: str, price: float) -> list[dict]:
        """Market-close the position when a guard is crossed. Caller holds the lock."""
        guard = self.account.protections.get(symbol)
        if not guard:
            return []
        position = self.account.positions.get(symbol)
        if not position or abs(position.contracts) < 1e-12:
            # No position yet: a limit entry may still be resting. Keep the
            # guard armed so the stop applies the moment that order fills;
            # only drop it once the symbol has neither position nor open order.
            pending = any(
                order["symbol"] == symbol and order["status"] == "open"
                for order in self.account.orders
            )
            if not pending:
                self.account.protections.pop(symbol, None)
            return []

        long = position.contracts > 0
        # A guard belongs to one direction. If the position reversed without
        # passing through an entry order (a resting limit that filled across
        # zero, say), the old levels would fire on the new position in the
        # wrong direction — a long's stop sits *below* the price and therefore
        # looks already crossed to a short. Disarm instead of closing.
        guard_side = str(guard.get("side") or "")
        if guard_side in ("long", "short") and (guard_side == "long") != long:
            self.account.protections.pop(symbol, None)
            log.info("纸面保护单方向已失效，撤销 %s（%s → %s）", symbol, guard_side, position.side)
            return []
        stop = float(guard.get("stopLoss") or 0.0)
        take = float(guard.get("takeProfit") or 0.0)
        reason = ""
        if long:
            if stop and price <= stop:
                reason = "stop_loss"
            elif take and price >= take:
                reason = "take_profit"
        else:
            if stop and price >= stop:
                reason = "stop_loss"
            elif take and price <= take:
                reason = "take_profit"
        if not reason:
            return []

        qty = abs(position.contracts)
        # The exit belongs to the same venue as the entry, so a futures guard
        # is not reported as a spot fill in the trade list.
        market = "spot"
        for filled in reversed(self.account.orders):
            if filled["symbol"] == symbol and filled.get("market"):
                market = filled["market"]
                break
        order = {
            "id": f"paper-{uuid.uuid4().hex[:12]}",
            "symbol": symbol,
            "market": market,
            "side": "sell" if long else "buy",
            "type": "market",
            "amount": qty,
            "price": 0.0,
            "filled": 0.0,
            "remaining": qty,
            "cost": 0.0,
            "status": "open",
            "timestamp": now_ms(),
            "reduceOnly": True,
            "allowShort": True,
            "paper": True,
            "protection": reason,
            "triggerPrice": price,
        }
        order["realized"] = self._fill(order, price)
        self.account.orders.append(order)
        self.account.protections.pop(symbol, None)
        log.info("纸面保护单触发 %s %s @ %s", symbol, reason, price)
        return [dict(order)]

    def try_fill(self, symbol: str, high: float, low: float) -> list[dict]:
        """Fill resting limit orders that the bar's range touched."""
        filled: list[dict] = []
        with self._lock:
            for order in self.account.orders:
                if order["status"] != "open" or order["symbol"] != symbol:
                    continue
                limit = order["price"]
                if order["side"] == "buy" and low <= limit:
                    if self._can_fill(order, limit):
                        self._fill(order, limit)
                        filled.append(dict(order))
                elif order["side"] == "sell" and high >= limit:
                    if self._can_fill(order, limit):
                        self._fill(order, limit)
                        filled.append(dict(order))
        return filled

    def mark(self, symbol: str, price: float) -> list[dict]:
        """Fill resting limit orders against the latest traded price.

        The web UI has no candle feed of its own, so a background poll feeds
        the last price in here: a buy fills once the market trades at or below
        its limit, a sell once it trades at or above. Without this a resting
        order could never fill and the only exit was to cancel it.

        Returns every order that closed, including a market exit produced by a
        crossed stop-loss / take-profit guard.
        """
        if price <= 0:
            return []
        filled: list[dict] = []
        with self._lock:
            self._marks[symbol] = price
            for order in self.account.orders:
                if order["status"] != "open" or order["symbol"] != symbol:
                    continue
                limit = order["price"]
                if limit <= 0:
                    continue
                touched = (
                    price <= limit if order["side"] == "buy" else price >= limit
                )
                if touched and self._can_fill(order, limit):
                    self._fill(order, limit)
                    filled.append(dict(order))
            filled.extend(self._check_protections(symbol, price))
            filled.extend(self._check_conditional(symbol, price))
        return filled

    def check_protections(self, symbol: str, price: float) -> list[dict]:
        """Public entry point for the background loop: trigger SL/TP exits."""
        if price <= 0:
            return []
        with self._lock:
            return self._check_protections(symbol, price)

    # -------------------------------------------------------- conditional orders
    def place_conditional(
        self,
        symbol: str,
        side: str,
        amount: float,
        trigger: float,
        *,
        direction: str = "below",
        order_type: str = "market",
        price: float | None = None,
        market: str = "spot",
        allow_short: bool = True,
        reduce_only: bool = False,
    ) -> dict:
        """Arm a standalone stop/limit order that fires on a price cross.

        This is what lets a user protect a resting limit entry, or place an
        exit before the position exists. It is deliberately separate from
        ``protections``: a guard sizes itself from the live position, while a
        conditional order carries its own fixed size and side.
        """
        if amount <= 0:
            raise ValueError("条件单数量必须大于 0")
        if trigger <= 0:
            raise ValueError("条件单触发价必须大于 0")
        if direction not in ("below", "above"):
            raise ValueError("触发方向必须是 below 或 above")
        if order_type not in ("market", "limit"):
            raise ValueError("条件单类型必须是 market 或 limit")
        if order_type == "limit" and not (price and price > 0):
            raise ValueError("限价条件单需要填写触发后的委托价")
        with self._lock:
            item = {
                "id": f"paper-cond-{uuid.uuid4().hex[:10]}",
                "symbol": symbol,
                "market": market,
                "side": side,
                "type": order_type,
                "amount": float(amount),
                "price": float(price) if price else 0.0,
                "trigger": float(trigger),
                "direction": direction,
                "status": "open",
                "createdAt": now_ms(),
                "allowShort": bool(allow_short),
                "reduceOnly": bool(reduce_only),
                "paper": True,
            }
            self.account.conditional.append(item)
            return dict(item)

    def set_order_observer(self, callback: Callable[[dict], None] | None) -> None:
        """Install (or replace) the observer for self-created fills.

        Pass ``None`` to detach. Only fills created by a triggered conditional
        order are reported; arming one notifies nobody.
        """
        self._on_order = callback

    def _emit(self, order: dict) -> dict:
        """Hand a self-created fill to the observer, never letting it throw.

        The observer lives in another service; a bug there must not make order
        placement fail, so it is isolated exactly like a log handler would be.
        """
        callback = self._on_order
        if callback is None:
            return order
        try:
            callback(dict(order))
        except Exception:  # pragma: no cover - observation is best effort
            log.warning("记录条件单成交失败 %s", order.get("id"), exc_info=True)
        return order

    def open_conditional(self, symbol: str | None = None) -> list[dict]:
        with self._lock:
            return [
                dict(item)
                for item in self.account.conditional
                if item["status"] == "open"
                and (symbol is None or item["symbol"] == symbol)
            ]

    def conditional_orders(self) -> list[dict]:
        with self._lock:
            return [dict(item) for item in self.account.conditional]

    def cancel_conditional(self, order_id: str) -> dict | None:
        with self._lock:
            for item in self.account.conditional:
                if item["id"] == order_id and item["status"] == "open":
                    item["status"] = "canceled"
                    item["canceledAt"] = now_ms()
                    return dict(item)
        return None

    def _check_conditional(self, symbol: str, price: float) -> list[dict]:
        """Fire conditional orders the price has crossed. Caller holds the lock.

        Returns the *orders* the triggers produced, not the condition records:
        the caller persists and books fills from the returned orders, and a
        triggered condition that produced no order would otherwise vanish.
        """
        fired: list[dict] = []
        for item in self.account.conditional:
            if item["status"] != "open" or item["symbol"] != symbol:
                continue
            trigger = float(item.get("trigger") or 0.0)
            if trigger <= 0:
                continue
            crossed = (
                price <= trigger if item.get("direction") == "below" else price >= trigger
            )
            if not crossed:
                continue
            item["triggeredAt"] = now_ms()
            item["triggerPrice"] = price
            try:
                order = self.place_order(
                    symbol=symbol,
                    side=item["side"],
                    order_type=item["type"],
                    amount=float(item["amount"]),
                    price=float(item["price"]) or price,
                    reduce_only=bool(item.get("reduceOnly")),
                    allow_short=bool(item.get("allowShort", True)),
                    market=item.get("market") or "spot",
                )
            except Exception:  # a bad order must not wedge the reconciler
                log.warning("条件单触发失败 %s", symbol, exc_info=True)
                item["status"] = "rejected"
                continue
            item["orderId"] = order["id"]
            item["result"] = order
            item["status"] = "rejected" if order.get("status") == "rejected" else "triggered"
            order["conditional"] = item["id"]
            fired.append(order)
            self._emit(order)
            log.info("纸面条件单触发 %s %s @ %s", symbol, item["id"], price)
        return fired

    def mark_price(self, symbol: str) -> float:
        with self._lock:
            return self._marks.get(symbol, 0.0)

    def open_orders(self, symbol: str | None = None) -> list[dict]:
        with self._lock:
            return [
                dict(order)
                for order in self.account.orders
                if order["status"] == "open"
                and (symbol is None or order["symbol"] == symbol)
            ]

    def _can_fill(self, order: dict, price: float) -> bool:
        """Reject a resting order that can no longer be honoured.

        Spot sells are capped by the coin actually held (a resting sell does
        not reserve inventory), buys by the cash on hand. Leaving the order
        open instead of driving the balance negative matches what an exchange
        would do when the funds were spent elsewhere.
        """
        qty = order["amount"]
        account = self.account
        if order["side"] == "buy":
            return account.cash + 1e-9 >= qty * price
        if order.get("allowShort"):
            return True
        held = account.positions.get(order["symbol"])
        return (held.contracts if held else 0.0) + 1e-12 >= qty

    def cancel(self, order_id: str) -> dict | None:
        """Cancel a resting order; return its final state, or None if unknown."""
        with self._lock:
            for order in self.account.orders:
                if order["id"] == order_id and order["status"] == "open":
                    order["status"] = "canceled"
                    order["canceledAt"] = now_ms()
                    return dict(order)
        return None

    def _fill(self, order: dict, price: float) -> float:
        """Fill an order and return the realized PnL booked by this fill.

        Two tallies are maintained on purpose:

        * the account ledger (``account.realized_by_symbol``) books the whole
          fee of this fill, including the commission of an order that only
          opens a position.  A commission is a realized cost the moment it
          leaves the account, and booking it is what keeps the totals honest:
          ``权益 - 初始 == 已实现 + 浮动`` for every account state.
        * the position tally (``position.realized_pnl``) only records what
          *this* position closed, net of the closed size's fee share.  It is
          reset whenever a new position is opened, so a fresh entry reads 0
          instead of inheriting the pair's previous round.
        """
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

        closed = 0.0
        price_pnl = 0.0
        if old != 0 and (old > 0) != (signed > 0):
            closed = min(abs(signed), abs(old))
            direction = 1.0 if old > 0 else -1.0
            price_pnl = (fill - position.entry_price) * closed * direction
        symbol = order["symbol"]
        opened = max(abs(signed) - closed, 0.0)
        # A brand-new position starts its own tally. The pair's history stays
        # in the account ledger, but the row must never show a previous round's
        # result as if it were this position's own 已实现盈亏.
        if abs(old) < 1e-12:
            position.realized_pnl = 0.0
            position.fees = 0.0
        # Account ledger: every fee is booked the moment it is paid, so the
        # totals reconcile with the equity (已实现 + 浮动 == 权益变化).
        realized = price_pnl - fee
        self.account.realized_by_symbol[symbol] = (
            self.account.realized_by_symbol.get(symbol, 0.0) + realized
        )
        # Position tally, same rule: the price move of anything closed minus
        # the whole commission of this fill. Booking the entry commission here
        # too is what makes 已实现盈亏 a net figure — a round trip that ends
        # where it started must report the two commissions it cost, not 0.
        position.realized_pnl += price_pnl - fee
        if old != 0 and new != 0 and (old > 0) != (new > 0):
            # Flipped through zero: the other side is a brand-new position and
            # must not open with the closed side's result on it. It still pays
            # the commission of the size it just opened.
            position.realized_pnl = -fee * (opened / abs(signed))
        if abs(new) < 1e-12:
            position.contracts = 0.0
            position.entry_price = 0.0
            # The tally is kept for the record; a flat position is not shown
            # anywhere, and the next entry resets it before anything renders.
        elif old == 0 or (old > 0) != (new > 0):
            # A fresh side (first entry, or a flip through zero) starts here.
            position.entry_price = fill
            position.contracts = new
        elif (old > 0) == (signed > 0):
            # Adding in the same direction: weighted average of the two lots.
            position.entry_price = (position.entry_price * old + fill * signed) / new
            position.contracts = new
        else:
            # Reducing without crossing zero: the average cost stays put, only
            # the size changes. Averaging the exit price in here used to drag
            # the entry towards the exit on every partial close, which made the
            # panel's 浮动盈亏 wrong and disagreed with the booked result.
            position.contracts = new
        position.fees += fee

        order["filled"] = qty
        order["remaining"] = 0.0
        order["cost"] = qty * fill
        order["average"] = fill
        order["status"] = "closed"
        order["filledAt"] = now_ms()
        order["realized"] = realized
        order["closedQty"] = closed
        self.account.trades.append(
            {
                "id": f"paper-trade-{uuid.uuid4().hex[:10]}",
                "order": order["id"],
                "symbol": order["symbol"],
                "market": order.get("market") or "spot",
                "type": order.get("type") or "market",
                "side": side,
                "price": fill,
                "amount": qty,
                "cost": qty * fill,
                "fee": fee,
                "realized": realized,
                "closedQty": closed,
                "timestamp": now_ms(),
                "paper": True,
            }
        )
        return realized

    # ------------------------------------------------------------------ state
    def position_quote(self, symbol: str, price: float) -> float:
        position = self.account.positions.get(symbol)
        if not position or not position.contracts:
            return 0.0
        return abs(position.contracts) * price

    def realized_pnl(self) -> float:
        """Cumulative realized PnL of the account, closed rounds included."""
        with self._lock:
            return self.account.realized_pnl

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
            self._marks.clear()

    # --------------------------------------------------------------- snapshot
    def snapshot(self) -> dict:
        """Serializable state so a restart resumes where it stopped.

        The whole point of paper trading is to measure a strategy over days;
        starting from 10,000 every time the app opens throws that away.
        """
        with self._lock:
            account = self.account
            return {
                "version": 1,
                "quote_asset": account.quote_asset,
                "starting_balance": account.starting_balance,
                "cash": account.cash,
                "realized_by_symbol": dict(account.realized_by_symbol),
                "positions": [
                    {
                        "symbol": position.symbol,
                        "contracts": position.contracts,
                        "entry_price": position.entry_price,
                        "realized_pnl": position.realized_pnl,
                        "fees": position.fees,
                    }
                    for position in account.positions.values()
                    if abs(position.contracts) > 1e-12
                ],
                "protections": {symbol: dict(guard) for symbol, guard in account.protections.items()},
                "conditional": [dict(item) for item in account.conditional[-200:]],
                "orders": [dict(order) for order in account.orders[-500:]],
                "trades": [dict(trade) for trade in account.trades[-500:]],
                "marks": dict(self._marks),
            }

    def restore(self, data: dict | None) -> bool:
        """Load a snapshot produced by :meth:`snapshot`. Returns success.

        The snapshot is all-or-nothing: a truncated or foreign payload must
        leave the current account untouched rather than restore it with a
        zero cash balance.
        """
        if not data or not isinstance(data, dict):
            return False
        try:
            if data.get("cash") is None or data.get("starting_balance") is None:
                raise ValueError("缺少现金字段")
            account = PaperAccount(
                quote_asset=str(data.get("quote_asset") or "USDT"),
                starting_balance=float(data["starting_balance"]),
                cash=float(data["cash"]),
            )
            for item in data.get("positions") or []:
                if not isinstance(item, dict) or not item.get("symbol"):
                    raise ValueError("持仓条目缺少交易对")
                position = PaperPosition(
                    symbol=str(item["symbol"]),
                    contracts=float(item.get("contracts") or 0.0),
                    entry_price=float(item.get("entry_price") or 0.0),
                    realized_pnl=float(item.get("realized_pnl") or 0.0),
                    fees=float(item.get("fees") or 0.0),
                )
                if abs(position.contracts) > 1e-12:
                    account.positions[position.symbol] = position
            trades = [
                item
                for item in (data.get("trades") or [])
                if isinstance(item, dict) and item.get("symbol")
            ]
            booked, open_fees = _replay_realized(trades)
            rebuilt = {
                symbol: booked.get(symbol, 0.0) - open_fees.get(symbol, 0.0)
                for symbol in set(booked) | set(open_fees)
            }
            ledger = data.get("realized_by_symbol")
            if isinstance(ledger, dict):
                account.realized_by_symbol = {
                    str(symbol): float(value or 0.0) for symbol, value in ledger.items()
                }
            # Recompute whenever the log can be trusted to hold the whole
            # history.  Two saved shapes need it: a snapshot with no ledger at
            # all (every closed round missing) and one saved before commissions
            # were booked on the opening side (short by exactly those fees).
            complete = _log_covers_the_account(account, trades)
            if complete or not account.realized_by_symbol:
                account.realized_by_symbol = dict(rebuilt)
            if complete:
                # The position tally used to hold the pair's cumulative
                # figure, so a previously saved row showed an earlier round as
                # if it belonged to the position that is open now. Rebuild the
                # tally of the still-open leg from the fills themselves.
                legs = _replay_positions(trades)
                for symbol, position in account.positions.items():
                    leg = legs.get(symbol)
                    if not leg:
                        continue
                    if abs(leg["size"] - position.contracts) > 1e-9:
                        continue
                    position.realized_pnl = leg["tally"]
                    position.fees = leg["fees"]
            for symbol, guard in (data.get("protections") or {}).items():
                if isinstance(guard, dict):
                    account.protections[str(symbol)] = dict(guard)
            account.conditional = [
                dict(item)
                for item in data.get("conditional") or []
                if isinstance(item, dict)
            ]
            account.orders = [dict(order) for order in data.get("orders") or []]
            account.trades = [dict(trade) for trade in data.get("trades") or []]
            marks = {
                str(symbol): float(price)
                for symbol, price in (data.get("marks") or {}).items()
            }
        except (TypeError, ValueError, AttributeError):
            log.warning("纸面账户快照损坏，忽略", exc_info=True)
            return False

        with self._lock:
            self.account = account
            self._marks = marks
        log.info(
            "纸面账户已恢复：现金 %.2f，持仓 %d，挂单 %d",
            account.cash,
            len(account.positions),
            len([order for order in account.orders if order.get("status") == "open"]),
        )
        return True
