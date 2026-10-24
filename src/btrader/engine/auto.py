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

# 交易所一次能给的 K 线上限。AGENTS 里定了「K 线一次拉 1000 根，不做成设置项」，
# 所以这里是常量，不是 AutoTraderConfig 上的一个可调项。
MAX_KLINES = 1000
# 预热除了策略要的根数，还要多两根：一根留给「丢掉尚未收盘的那根」，
# 一根让最后一个指标的数值落定。少这两根策略会在指标还没值的时候空转。
WARMUP_EXTRA = 2

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


def _filled(order: dict[str, Any] | None) -> float:
    """这张单成交了多少（基础币）。挂单未成交是 0。"""
    if not isinstance(order, dict):
        return 0.0
    for key in ("filled", "amount"):
        value = order.get(key)
        if value is None:
            continue
        try:
            amount = abs(float(value))
        except (TypeError, ValueError):
            continue
        if key == "amount" and str(order.get("status") or "") not in ("closed", "filled"):
            return 0.0
        return amount
    return 0.0


def _held_amount(balance: Any, asset: str) -> float:
    """钱包里这个币有多少：先看 total，没有再退回 free。

    free 只是「现在能卖的」，挂在委托里被冻结的那部分不在里面；判断「有没有仓位」
    要用 total，判断「能卖多少」才用 free。两者都有就用 total。
    """
    for block in ("total", "free"):
        raw = balance.get(block) if isinstance(balance, dict) else None
        if not isinstance(raw, dict):
            continue
        try:
            value = float(raw.get(asset) or 0.0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 0.0


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
        # Unknown until the venue answers. A made-up starting figure would size
        # the first order against money the account may not have.
        self._equity: float | None = None
        # K 线供给情况，界面通过 status 读它。预热不够时引擎在跑却什么都不做，
        # 不把这个状态说出来，用户只能看见 running=true。
        self._data_lock = threading.RLock()
        self._data_status: dict[str, Any] = {
            "required_bars": 0,
            "fetched_bars": 0,
            "max_bars": MAX_KLINES,
            "short": False,
            "message": "",
        }
        # 同一句「K 线不足」只报一次：每 10 秒一条会把日志和事件面板刷满。
        self._warned_shortfall = ""

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
        # 预热根数要按实际参数算：慢线填 200 就得 201 根，按默认值算的 22 根
        # 会让策略在指标还没有值的时候空转（目标恒为 0）。
        min_bars = self.strategy.min_bars(cfg.params)
        # 以前这里固定拉 cfg.kline_limit 根（默认 500）：慢线一填到 500 就需要
        # 503 根，永远不够，于是每轮静默 return——界面显示在跑却什么都不做，
        # 日志里一个字都没有。现在按需要的根数拉，够用了就正常出信号。
        required = min_bars + WARMUP_EXTRA
        if required > MAX_KLINES:
            # 拉也拉不到，别每 10 秒白跑一次 1000 根的请求，直接说清楚。
            self._report_shortfall(
                f"K 线不足 {required} 根（交易所单次最多 {MAX_KLINES} 根），"
                f"请把周期改小或把慢线参数调小",
                required=required,
                fetched=0,
            )
            return
        limit = self._warmup_limit(required)
        # Live runs always hit the API: the cache may be a full bar behind.
        df = self.klines.fetch(
            cfg.market_type, cfg.symbol, cfg.timeframe,
            limit=limit, use_cache=False,
        )
        bars = 0 if df is None else len(df)
        if bars < required:
            self._report_shortfall(
                f"K 线不足：只拿到 {bars} 根，需要 {required} 根，这一轮跳过"
                f"（交易对上市时间太短，或周期太长）",
                required=required,
                fetched=bars,
            )
            return

        # Drop the still-forming candle: signals use closed bars only.
        step = timeframe_seconds(cfg.timeframe) * 1000
        last_open = int(df["timestamp"].iloc[-1])
        now = int(time.time() * 1000)
        closed = df if last_open + step <= now else df.iloc[:-1]
        closed = closed.reset_index(drop=True)
        if len(closed) < min_bars + 1:
            self._report_shortfall(
                f"K 线不足：收盘后只剩 {len(closed)} 根，需要 {min_bars + 1} 根，"
                f"这一轮跳过",
                required=min_bars + 1,
                fetched=len(closed),
            )
            return
        self._clear_shortfall(required, bars)
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
        self._emit_signal(target, f"收盘价 {price:,.4f}")

        # 目标要**调仓成功之后**才算达到。以前先记 target 再下单：被拒、网络抖动、
        # 权益没读到时这一笔什么都没发生，而下一根 K 线的 changed 恒为 False，
        # 于是仓位和目标长期背离，直到策略自己再次改主意——静默且无日志。
        if self._rebalance(target, price):
            self._current_target = target

    # ------------------------------------------------------------------ warmup
    def _warmup_limit(self, required: int) -> int:
        """这一轮拉多少根 K 线：照旧起步，参数要得更多就多拉，封顶 1000。"""
        return max(1, min(MAX_KLINES, max(int(self.config.kline_limit), int(required))))

    def data_status(self) -> dict[str, Any]:
        """K 线供给：这一轮要几根、拿到几根、够不够。

        给界面看的。引擎在跑不等于在决策，预热不够时它只是每轮空转，这个字段
        是唯一能把「为什么没有信号」说出来的地方。
        """
        with self._data_lock:
            return dict(self._data_status)

    def _set_data_status(
        self, *, required: int, fetched: int, short: bool, message: str
    ) -> None:
        with self._data_lock:
            self._data_status = {
                "required_bars": int(required),
                "fetched_bars": int(fetched),
                "max_bars": MAX_KLINES,
                "short": bool(short),
                "message": message,
            }

    def _report_shortfall(self, message: str, *, required: int, fetched: int) -> None:
        """记下「数据不够」，并且只报一次。

        轮询每 10 秒一轮，同一句话重复进日志和事件面板就没人看得见了；
        参数一改、或者数据重新够用，这句话才算翻篇。
        """
        self._set_data_status(required=required, fetched=fetched, short=True, message=message)
        if message == self._warned_shortfall:
            return
        self._warned_shortfall = message
        log.warning("%s", message)
        self._emit_log("WARN", message)

    def _clear_shortfall(self, required: int, fetched: int) -> None:
        self._set_data_status(required=required, fetched=fetched, short=False, message="")
        # 恢复之后再出现同样的不足，要能再报一次。
        self._warned_shortfall = ""

    def _client(self):
        """这台引擎自己那个市场的客户端。

        不能用 ``trading.rest``：那是**全局市场**的客户端（``AppContext.rest``），
        引擎跑起来之后用户在设置页换个市场，这里就会拿另一个市场的 Key 去读余额和
        持仓——币安回 -2015，异常被吞掉，界面显示「运行中」而一笔不成交。
        ``rest_for`` 按 ``config.market_type`` 取，引擎启动时定下来的那个市场。
        """
        return self.trading.rest_for(self.config.market_type)

    def _refresh_equity(self, price: float) -> None:
        """Read the wallet straight from the venue before sizing an order.

        A stale equity from the previous candle turns a percentage-of-account
        rule into guesswork, and a wallet read is one signed call per candle.

        读不到就把 ``self._equity`` 置空并说一句：以前是裸 ``except: pass``，
        于是「读余额失败」和「余额没变」在界面上长得一模一样，引擎一直空转。
        """
        try:
            rest = self._client()
            balance = rest.fetch_balance(self.config.market_type)
            totals = balance.get("total") or {}
            spec = rest.contract_spec(self.config.market_type, self.config.symbol)
            if self.config.market_type is MarketType.COINM:
                # Coin-M wallets hold the margin coin (BTC for BTCUSD), so the
                # wallet balance has to be marked to USD before sizing.
                asset = spec.settle or spec.base or "BTC"
                value = totals.get(asset)
                self._equity = None if value is None else float(value) * price
            else:
                value = totals.get("USDT")
                # A wallet shape without the key is not a 0 balance: sizing 0
                # would sell a position the engine never meant to touch.
                self._equity = None if value is None else float(value)
            if self._equity is None:
                self._emit_log(
                    "WARN", f"账户里没有 {self.config.market_type.label} 的可用余额，本轮跳过调仓"
                )
        except Exception as exc:
            self._equity = None
            self._emit_log("WARN", f"读取账户权益失败，本轮跳过调仓：{exc}")

    def _rebalance(self, target: float, price: float) -> bool:
        """Move toward ``target``; True when the target counts as reached.

        False means "nothing happened, try again on the next candle" — the caller
        must not record the target as reached, or a rejected order would never be
        retried until the strategy changed its mind.
        """
        equity = self._equity
        if price <= 0 or equity is None:
            # Never read the wallet yet: sizing an order against an assumed
            # balance is the same mistake as assuming a flat position.
            return False
        notional = equity * self.config.position_pct * self.config.leverage
        # ``desired_qty`` is always expressed in base coins; TradingService
        # converts it to contracts for coin-margined markets.
        desired_qty = target * notional / price

        current_qty = self._live_position_qty(price)
        if current_qty is None:
            # Unknown exposure: sizing against an assumed flat position is how
            # a transient read failure turns into a doubled order.
            return False

        delta = desired_qty - current_qty
        if abs(delta) * price < 10.0:  # ignore dust
            # 尘埃以内算到位：不算失败，否则每根 K 线都重算一遍同一笔小差额。
            return True

        side = "buy" if delta > 0 else "sell"
        # Only a move to flat is a pure reduction. A flip (long -> short) also
        # has to open the other side: marking it reduce-only makes the venue
        # drop that half — or reject the whole order — and the target was
        # already recorded as reached, so the engine never retries it.
        reduce_only = desired_qty == 0 and abs(current_qty) > 1e-12
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
            return False
        except Exception as exc:
            self._emit_log("ERROR", f"下单失败：{exc}")
            return False

        action = "买入" if side == "buy" else "卖出"
        self._emit_log(
            "TRADE",
            f"{action} {abs(delta):.6f} {self.config.symbol} @ {price:,.4f} "
            f"(目标仓位 {target:+.0%}) → {result.order.get('status', '')}",
        )
        if self.config.market_type is MarketType.SPOT and _filled(result.order) > 0:
            # 现货的「持仓」就是这本账。成交同步是 5 秒一轮的，不在这里补一次，
            # 引擎下一根 K 线读到的还是成交前的数量（0），会照着旧数量再买一遍。
            try:
                self.trading.sync_symbol_fills(self.config.market_type, self.config.symbol)
            except Exception:  # 账本晚一轮更新不该让这张已经成交的单算失败
                log.debug("自动交易成交后同步账本失败 %s", self.config.symbol, exc_info=True)
        return True

    def _live_position_qty(self, price: float) -> float | None:
        """Current live position expressed in base coins, or ``None``.

        ``None`` means "unknown", and callers must skip the rebalance. Returning
        zero is indistinguishable from "flat", so one failed read
        made ``_rebalance`` treat the whole target as missing and buy it again —
        a transient network error could double a live position.
        """
        rest = self._client()
        spec = rest.contract_spec(self.config.market_type, self.config.symbol)
        if self.config.market_type is MarketType.SPOT:
            return self._live_spot_balance(spec)
        try:
            positions = rest.fetch_positions(
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
        """现货「持仓」= **本程序成交账本**里这一对的数量，钱包只当上限。

        现货没有持仓对象，钱包里那个币混着用户自己充提的部分。以前这里读
        ``balance["total"]`` 当持仓：钱包里有用户自己的 1 BTC 时，策略给出
        ``target=0``（平仓信号）会算出「应卖 0 - 1 BTC」，把用户自己的币一次卖掉；
        ``target=0.5`` 也一样永远多卖。AGENTS 第 4 节对 ``/api/positions/close``
        定了这条口径，引擎这一路当时漏了。

        账本比可用余额多说明币已被提走，按余额封顶；余额读不到就按账本数量继续
        （宁可让交易所拒这一单并留一行 WARN，也不要因为读不到余额就不平仓）。
        """
        symbol = self.config.symbol
        try:
            book = self.trading._live_ledger()["books"].get(symbol) or {}
            booked = float(book.get("contracts") or 0.0)
        except Exception as exc:
            self._emit_log("WARN", f"读取本地成交账本失败，本轮跳过调仓：{exc}")
            return None
        asset = spec.base or symbol.split("/")[0]
        try:
            balance = self._client().fetch_balance(self.config.market_type)
        except Exception as exc:
            self._emit_log(
                "WARN", f"读取实盘余额失败，按账本数量继续（可能超过可用余额）：{exc}"
            )
            return booked
        if booked == 0.0:
            return 0.0
        # 封顶用的是「钱包里这个币还有多少」，不是「现在还能卖多少」：挂在卖单里被
        # 冻结的那部分同样是仓位。只看 free 的话挂单期间 free = 0，这里回 0 等于
        # 告诉引擎「空仓」，它照着 target 再买一份——实测把一份仓位买成了两份。
        # 币真被提走了 total 也是 0，那种情况按 0 算才对。
        holding = _held_amount(balance, asset)
        capped = min(abs(booked), holding)
        return capped if booked > 0 else -capped
