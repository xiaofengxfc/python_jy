"""Automated strategy runner.

The runner polls closed candles, evaluates the strategy and rebalances toward
the target position. It runs in its own thread so the UI stays responsive and
all orders go through ``TradingService`` (risk checks included).

The runner is deliberately free of any UI toolkit: callers subscribe with plain
callbacks via :meth:`AutoTrader.on_log` / ``on_signal`` / ``on_state``. The Qt
build wrapped those same callbacks in a ``QObject``; the web backend turns them
into a broadcast queue. Keeping this module dependency-free is what lets the
engine run headless in the desktop shell.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import pandas as pd

from ..core.types import MarketType, timeframe_seconds
from ..strategy.base import Strategy, clamp_target
from .trading import OrderRejected, OrderRequest, TradingService

log = logging.getLogger(__name__)

LogListener = Callable[[str, str], None]
SignalListener = Callable[[float, str], None]
StateListener = Callable[[bool], None]
# ``(closed_bars, target, target_changed, price)`` once per newly closed candle.
# Deliberately called on *every* new candle, not only when the target moves:
# a briefing is worth having on the quiet bars too.
CandleListener = Callable[[pd.DataFrame, float, bool, float], None]


@dataclass
class AutoTraderConfig:
    symbol: str
    market_type: MarketType
    timeframe: str = "15m"
    strategy_key: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    position_pct: float = 1.0
    leverage: float = 1.0
    poll_seconds: float = 10.0
    kline_limit: int = 500


class AutoTrader:
    """Background polling thread that exposes plain-callback hooks.

    Listeners are invoked from the worker thread. A listener that raises is
    logged and skipped: a broken consumer must never stop trading.
    """

    def __init__(
        self,
        config: AutoTraderConfig,
        strategy: Strategy,
        trading: TradingService,
        klines,
    ) -> None:
        self.config = config
        self.strategy = strategy
        self.trading = trading
        self.klines = klines
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._current_target = 0.0
        self._last_candle_ts = 0
        self._position_quote = 0.0
        self._equity = 10_000.0

        self._listener_lock = threading.RLock()
        self._log_listeners: list[LogListener] = []
        self._signal_listeners: list[SignalListener] = []
        self._state_listeners: list[StateListener] = []
        self._candle_listeners: list[CandleListener] = []

    # ------------------------------------------------------------- listeners
    def on_log(self, callback: LogListener) -> None:
        with self._listener_lock:
            self._log_listeners.append(callback)

    def on_signal(self, callback: SignalListener) -> None:
        with self._listener_lock:
            self._signal_listeners.append(callback)

    def on_state(self, callback: StateListener) -> None:
        with self._listener_lock:
            self._state_listeners.append(callback)

    def on_candle(self, callback: CandleListener) -> None:
        with self._listener_lock:
            self._candle_listeners.append(callback)

    def _emit(self, listeners, *args) -> None:
        with self._listener_lock:
            targets = list(listeners)
        for callback in targets:
            try:
                callback(*args)
            except Exception:  # a broken consumer must not break trading
                log.exception("自动交易回调异常")

    def _emit_log(self, level: str, message: str) -> None:
        log.debug("%s %s", level, message)
        self._emit(self._log_listeners, level, message)

    def _emit_signal(self, target: float, reason: str) -> None:
        self._emit(self._signal_listeners, target, reason)

    def _emit_state(self, running: bool) -> None:
        self._emit(self._state_listeners, running)

    # ------------------------------------------------------------- lifecycle
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="auto-trader", daemon=True)
        self._thread.start()
        self._emit_state(True)
        self._emit_log("INFO", f"自动交易已启动（{self.config.market_type.label}）")

    def stop(self) -> None:
        if not self.running:
            return
        self._stop.set()
        self._thread.join(timeout=5)
        if self._thread.is_alive():
            # 一轮 tick 卡在行情或下单上：保留引用，start() 会因此拒绝开新线程，
            # 旧线程拿到新 _stop 之后自己退出，不会出现两个 poller 同时调仓。
            log.warning("自动交易线程未在超时内退出，保留引用")
            self._emit_log("WARN", "交易线程仍在退出，请稍后再启动")
        else:
            self._thread = None
        self._emit_state(False)
        self._emit_log("INFO", "自动交易已停止")

    # ------------------------------------------------------------------ loop
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:  # never let the loop die silently
                log.exception("自动交易循环异常")
                self._emit_log("ERROR", f"循环异常：{exc}")
            self._stop.wait(self.config.poll_seconds)

    def _tick(self) -> None:
        cfg = self.config
        # Live runs always hit the API: the cache may be a full bar behind.
        df = self.klines.fetch(
            cfg.market_type, cfg.symbol, cfg.timeframe,
            limit=cfg.kline_limit, use_cache=False,
        )
        if df is None or len(df) < self.strategy.meta.min_bars + 2:
            return

        # Drop the still-forming candle: signals use closed bars only.
        step = timeframe_seconds(cfg.timeframe) * 1000
        last_open = int(df["timestamp"].iloc[-1])
        now = int(time.time() * 1000)
        closed = df if last_open + step <= now else df.iloc[:-1]
        closed = closed.reset_index(drop=True)
        if len(closed) < self.strategy.meta.min_bars + 1:
            return
        last_ts = int(closed["timestamp"].iloc[-1])
        if last_ts == self._last_candle_ts:
            return
        self._last_candle_ts = last_ts

        frame = self.strategy.generate(closed, self.config.params)
        raw = frame["target"].reindex(closed.index).to_numpy(dtype=float)
        # Spot cannot be sold short: clamp negative targets to flat.
        allow_short = self.config.market_type.is_futures and self.strategy.meta.allow_short
        target = clamp_target(raw[-1] if len(raw) else 0.0, allow_short)
        price = float(closed["close"].iloc[-1])

        self._refresh_equity(price)
        self._emit_log(
            "INFO",
            f"新收盘 K 线 {pd.Timestamp(last_ts, unit='ms'):%m-%d %H:%M} "
            f"策略目标 {target:+.0%}（当前 {self._current_target:+.0%}）",
        )
        changed = target != self._current_target
        # Announced before the rebalance so a listener that takes seconds (an AI
        # briefing) never sits between the signal and the order.
        self._emit(self._candle_listeners, closed, target, changed, price)
        if not changed:
            return
        self._current_target = target
        self._emit_signal(target, f"收盘价 {price:,.4f}")

        self._rebalance(target, price)

    def _refresh_equity(self, price: float) -> None:
        """Read the wallet straight from the venue before sizing an order.

        A stale equity from the previous candle turns a percentage-of-account
        rule into guesswork, and a wallet read is one signed call per candle.
        """
        try:
            balance = self.trading.rest.fetch_balance(self.config.market_type)
            totals = balance.get("total") or {}
            spec = self.trading.rest.contract_spec(
                self.config.market_type, self.config.symbol
            )
            if self.config.market_type is MarketType.COINM:
                # Coin-M wallets hold the margin coin (BTC for BTCUSD), so the
                # wallet balance has to be marked to USD before sizing.
                asset = spec.settle or spec.base or "BTC"
                self._equity = float(totals.get(asset) or 0.0) * price
            else:
                self._equity = float(totals.get("USDT") or 0.0)
        except Exception:
            pass

    def _rebalance(self, target: float, price: float) -> None:
        if price <= 0:
            return
        notional = self._equity * self.config.position_pct * self.config.leverage
        # ``desired_qty`` is always expressed in base coins; TradingService
        # converts it to contracts for coin-margined markets.
        desired_qty = target * notional / price

        current_qty = self._live_position_qty(price)
        if current_qty is None:
            # Unknown exposure: sizing against an assumed flat position is how
            # a transient read failure turns into a doubled order.
            return

        delta = desired_qty - current_qty
        if abs(delta) * price < 10.0:  # ignore dust
            return

        side = "buy" if delta > 0 else "sell"
        reduce_only = abs(desired_qty) < abs(current_qty) and (desired_qty == 0 or desired_qty * current_qty < 0)
        request = OrderRequest(
            symbol=self.config.symbol,
            side=side,
            order_type="market",
            amount=abs(delta),
            price=price,
            reduce_only=reduce_only,
            leverage=self.config.leverage,
            market_type=self.config.market_type,
        )
        try:
            result = self.trading.submit(request)
        except OrderRejected as exc:
            self._emit_log("WARN", f"下单被拒绝：{exc}")
            return
        except Exception as exc:
            self._emit_log("ERROR", f"下单失败：{exc}")
            return

        action = "买入" if side == "buy" else "卖出"
        self._emit_log(
            "TRADE",
            f"{action} {abs(delta):.6f} {self.config.symbol} @ {price:,.4f} "
            f"(目标仓位 {target:+.0%}) → {result.order.get('status', '')}",
        )

    def _live_position_qty(self, price: float) -> float | None:
        """Current live position expressed in base coins, or ``None``.

        ``None`` means "unknown", and callers must skip the rebalance. Returning
        zero is indistinguishable from "flat", so one failed read
        made ``_rebalance`` treat the whole target as missing and buy it again —
        a transient network error could double a live position.
        """
        spec = self.trading.rest.contract_spec(self.config.market_type, self.config.symbol)
        if self.config.market_type is MarketType.SPOT:
            return self._live_spot_balance(spec)
        try:
            positions = self.trading.rest.fetch_positions(
                self.config.market_type, [self.config.symbol]
            )
        except Exception as exc:
            self._emit_log("WARN", f"读取实盘持仓失败，本轮跳过调仓：{exc}")
            return None
        total = 0.0
        for position in positions:
            contracts = float(position.get("contracts") or 0)
            if position.get("side") == "short":
                contracts = -contracts
            mark = float(position.get("markPrice") or price or 0)
            total += spec.contracts_to_base(contracts, mark)
        return total

    def _live_spot_balance(self, spec) -> float | None:
        try:
            balance = self.trading.rest.fetch_balance(self.config.market_type)
        except Exception as exc:
            self._emit_log("WARN", f"读取实盘余额失败，本轮跳过调仓：{exc}")
            return None
        asset = spec.base or self.config.symbol.split("/")[0]
        return float((balance.get("total") or {}).get(asset) or 0.0)
