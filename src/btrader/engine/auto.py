"""Automated strategy runner (paper + live).

The runner polls closed candles, evaluates the strategy and rebalances toward
the target position. It runs in its own thread so the UI stays responsive and
all orders go through ``TradingService`` (risk checks included).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import pandas as pd
from PySide6.QtCore import QObject, Signal

from ..core.types import MarketType, RunMode, timeframe_seconds
from ..strategy.base import Strategy, clamp_target
from .trading import OrderRejected, OrderRequest, TradingService

log = logging.getLogger(__name__)


@dataclass
class AutoTraderConfig:
    symbol: str
    market_type: MarketType
    timeframe: str = "15m"
    mode: RunMode = RunMode.PAPER
    strategy_key: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    position_pct: float = 1.0
    leverage: float = 1.0
    poll_seconds: float = 10.0
    kline_limit: int = 500


class AutoTrader(QObject):
    """Signal-emitting wrapper around a background polling thread."""

    log_event = Signal(str, str)          # level, message
    signal_changed = Signal(float, str)   # target, reason
    state_changed = Signal(bool)          # running

    def __init__(
        self,
        config: AutoTraderConfig,
        strategy: Strategy,
        trading: TradingService,
        klines,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
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
        self.state_changed.emit(True)
        self.log_event.emit("INFO", f"自动交易已启动（{self.config.mode.label}）")

    def stop(self) -> None:
        if not self.running:
            return
        self._stop.set()
        self._thread.join(timeout=5)
        self._thread = None
        self.state_changed.emit(False)
        self.log_event.emit("INFO", "自动交易已停止")

    # ------------------------------------------------------------------ loop
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:  # never let the loop die silently
                log.exception("自动交易循环异常")
                self.log_event.emit("ERROR", f"循环异常：{exc}")
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
        self.log_event.emit(
            "INFO",
            f"新收盘 K 线 {pd.Timestamp(last_ts, unit='ms'):%m-%d %H:%M} "
            f"策略目标 {target:+.0%}（当前 {self._current_target:+.0%}）",
        )
        if target == self._current_target:
            return
        self._current_target = target
        self.signal_changed.emit(target, f"收盘价 {price:,.4f}")

        self._rebalance(target, price)

    def _refresh_equity(self, price: float) -> None:
        if self.config.mode is RunMode.PAPER:
            account = self.trading.paper.account
            position = account.positions.get(self.config.symbol)
            held = position.contracts if position else 0.0
            self._position_quote = abs(held) * price
            # Cash alone understates equity once a position is open.
            self._equity = account.cash + held * price
        else:
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

        if self.config.mode is RunMode.PAPER:
            position = self.trading.paper.account.positions.get(self.config.symbol)
            current_qty = position.contracts if position else 0.0
        else:
            current_qty = self._live_position_qty(price)

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
            mode=self.config.mode,
        )
        try:
            result = self.trading.submit(request)
        except OrderRejected as exc:
            self.log_event.emit("WARN", f"下单被拒绝：{exc}")
            return
        except Exception as exc:
            self.log_event.emit("ERROR", f"下单失败：{exc}")
            return

        action = "买入" if side == "buy" else "卖出"
        self.log_event.emit(
            "TRADE",
            f"{action} {abs(delta):.6f} {self.config.symbol} @ {price:,.4f} "
            f"(目标仓位 {target:+.0%}) → {result.order.get('status', '')}",
        )

    def _live_position_qty(self, price: float) -> float:
        """Current live position expressed in base coins."""
        spec = self.trading.rest.contract_spec(self.config.market_type, self.config.symbol)
        if self.config.market_type is MarketType.SPOT:
            return self._live_spot_balance(spec)
        try:
            positions = self.trading.rest.fetch_positions(
                self.config.market_type, [self.config.symbol]
            )
        except Exception:
            return 0.0
        total = 0.0
        for position in positions:
            contracts = float(position.get("contracts") or 0)
            if position.get("side") == "short":
                contracts = -contracts
            mark = float(position.get("markPrice") or price or 0)
            total += spec.contracts_to_base(contracts, mark)
        return total

    def _live_spot_balance(self, spec) -> float:
        try:
            balance = self.trading.rest.fetch_balance(self.config.market_type)
        except Exception:
            return 0.0
        asset = spec.base or self.config.symbol.split("/")[0]
        return float((balance.get("total") or {}).get(asset) or 0.0)
