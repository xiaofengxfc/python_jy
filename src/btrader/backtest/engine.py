"""Event-driven backtester.

The engine walks candles bar by bar, evaluates the strategy at each close and
executes the resulting target position at the next bar's open. This mirrors how
a real session works (signal on close, fill on the following open) and keeps
results free of look-ahead bias.

Target semantics: 1.0 = fully long of allocated capital, 0.0 = flat,
-1.0 = fully short (futures only).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np
import pandas as pd

from ..core.types import timeframe_seconds
from ..strategy.base import Strategy, clamp_target

log = logging.getLogger(__name__)


@dataclass
class BacktestConfig:
    initial_capital: float = 10_000.0
    fee_rate: float = 0.0004          # taker fee per side (0.04%)
    slippage_bps: float = 1.0         # 1 bp = 0.01%
    position_pct: float = 1.0         # fraction of equity deployed
    leverage: float = 1.0
    allow_short: bool = True
    stop_loss_pct: float = 0.0        # 0 disables
    take_profit_pct: float = 0.0      # 0 disables
    timeframe: str = "1h"
    # 强平：只在合约、杠杆 > 1、liquidate 开时生效。维持保证金率 0.5% 是币安
    # BTCUSDT 永续的档位；别的交易对档位不同，要准就自己传。
    maintenance_margin_rate: float = 0.005
    liquidate: bool = True
    # 资金费：拿不到历史就不扣，但在 warnings 里说清楚，别让人以为算了。
    funding: bool = True
    # 最小下单量与步长，单位是**基础币**（引擎里 qty 的量纲）。None = 不设限。
    min_amount: float | None = None
    amount_step: float | None = None
    # 调用方已经知道、要连同结果一起报给用户的说明（比如币本位的最小量按张、
    # 这里没折算）。引擎把它们并进 BacktestResult.warnings，不影响任何数字。
    notes: list[str] = field(default_factory=list)

    @property
    def bars_per_year(self) -> float:
        return 365 * 24 * 3600 / timeframe_seconds(self.timeframe)

    @property
    def deploy(self) -> float:
        return max(0.0, self.position_pct) * max(1.0, self.leverage)


@dataclass
class TradeRecord:
    entry_time: int
    exit_time: int
    side: str
    entry_price: float
    exit_price: float
    qty: float
    pnl: float
    pnl_pct: float
    bars: int
    exit_reason: str = "signal"
    fee: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "entry_time": self.entry_time,
            "exit_time": self.exit_time,
            "side": self.side,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "qty": self.qty,
            "pnl": self.pnl,
            "pnl_pct": self.pnl_pct,
            "bars": self.bars,
            "exit_reason": self.exit_reason,
            "fee": self.fee,
        }


@dataclass
class BacktestResult:
    equity: pd.DataFrame               # timestamp, equity, drawdown, position, target, close
    trades: list[TradeRecord] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)
    # 这一段回测没能算进去的成本，或者发生了什么极端情况（账户亏光）。
    warnings: list[str] = field(default_factory=list)

    @property
    def trade_frame(self) -> pd.DataFrame:
        if not self.trades:
            return pd.DataFrame(columns=list(TradeRecord.__annotations__))
        return pd.DataFrame([t.as_dict() for t in self.trades])


def _slipped(price: float, side: str, bps: float) -> float:
    """Apply slippage against the trader."""
    delta = price * bps / 10_000.0
    return price + delta if side == "buy" else price - delta


def run_backtest(
    df: pd.DataFrame,
    strategy: Strategy,
    params: dict[str, Any],
    config: BacktestConfig,
    funding_rates: dict[int, float] | None = None,
) -> BacktestResult:
    """Run one backtest on ``df`` (columns: timestamp, open, high, low, close).

    ``funding_rates`` maps a settlement timestamp (ms) to that settlement rate.
    Missing history is not an error: nothing is charged and ``warnings`` says so.
    """
    if df.empty:
        return BacktestResult(pd.DataFrame(), [], {}, dict(params))

    frame = strategy.generate(df, params)
    if "target" not in frame:
        raise ValueError("策略必须返回包含 'target' 列的 DataFrame")
    raw = frame["target"].reindex(df.index).to_numpy(dtype=float)
    raw = np.nan_to_num(raw, nan=0.0)

    allow_short = config.allow_short and strategy.meta.allow_short
    targets = np.array([clamp_target(v, allow_short) for v in raw])

    ts = df["timestamp"].to_numpy(dtype=np.int64)
    opens = df["open"].to_numpy(dtype=float)
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    closes = df["close"].to_numpy(dtype=float)
    n = len(df)

    fee_rate = config.fee_rate
    slip = config.slippage_bps
    deploy = config.deploy

    cash = float(config.initial_capital)
    qty = 0.0
    entry_price = 0.0
    entry_time = 0
    entry_index = 0
    entry_fee = 0.0

    equity_arr = np.empty(n, dtype=float)
    pos_arr = np.zeros(n, dtype=float)
    trades: list[TradeRecord] = []
    total_fees = 0.0
    total_funding = 0.0
    skipped_small = 0
    warnings: list[str] = list(getattr(config, "notes", ()) or ())

    # 资金费：把结算时刻对齐到 K 线，落在 (上一根, 这一根] 里的费率算在这一根上。
    funding_schedule = _funding_by_bar(ts, funding_rates) if config.funding else {}
    if config.funding and not funding_schedule:
        warnings.append(
            "这一段没有资金费率数据，回测没有扣资金费——真实持仓是会付的，多空都算。"
        )

    pending: float | None = None
    current_target = 0.0  # target behind the currently held position

    for i in range(n):
        # 1) Fill the target computed at the previous close, at this open.
        #    Only rebalance when the *target* changed: otherwise equity drift
        #    would cause tiny, fee-burning adjustments on every bar.
        if pending is not None:
            if pending != current_target:
                equity_at_open = cash + qty * opens[i]
                desired = pending * equity_at_open * deploy / opens[i] if opens[i] > 0 else 0.0
                delta = _quantize(desired - qty, config)
                if delta is None:
                    # 交易所不收这么小的单：这一笔跳过，但账要记下来
                    skipped_small += 1
                    delta = 0.0
                notional = abs(delta) * opens[i]
                if notional > max(1e-9, equity_at_open * 1e-6):
                    side = "buy" if delta > 0 else "sell"
                    fill = _slipped(opens[i], side, slip)
                    fee = abs(delta) * fill * fee_rate
                    total_fees += fee
                    cash -= delta * fill + fee

                    old_qty = qty
                    new_qty = old_qty + delta
                    closed_qty = 0.0
                    if old_qty != 0.0 and (old_qty > 0) != (delta > 0):
                        closed_qty = min(abs(delta), abs(old_qty))
                        # Share of this fill that closes the old position. When
                        # the fill both closes and opens (a flip) the fee must be
                        # split between the two positions.
                        fill_close_share = closed_qty / abs(delta)
                        fee_for_close = fee * fill_close_share
                        entry_fee_for_close = entry_fee * (closed_qty / abs(old_qty))
                        trades.append(
                            _make_trade(
                                entry_price=entry_price,
                                exit_price=fill,
                                signed_qty=math.copysign(closed_qty, old_qty),
                                entry_time=entry_time,
                                exit_time=int(ts[i]),
                                entry_index=entry_index,
                                exit_index=i,
                                fee=entry_fee_for_close + fee_for_close,
                                reason="signal",
                            )
                        )
                        entry_fee -= entry_fee_for_close
                        remaining_fee = fee - fee_for_close
                    else:
                        remaining_fee = fee

                    if old_qty == 0.0:
                        entry_price, entry_time, entry_index = fill, int(ts[i]), i
                        entry_fee = fee
                    elif abs(new_qty) <= 1e-12 or (new_qty > 0) != (old_qty > 0):
                        # Flipped or fully closed: the remainder opens a new position.
                        if abs(new_qty) > 1e-12:
                            entry_price, entry_time, entry_index = fill, int(ts[i]), i
                            entry_fee = remaining_fee
                        else:
                            entry_price, entry_time, entry_index, entry_fee = 0.0, 0, 0, 0.0
                    elif closed_qty > 0:
                        # Partial close; the fee for the remaining part stays booked.
                        # The units that stay keep their original cost basis:
                        # averaging the exit price into entry_price would book the
                        # closed part's result twice — once in the trade appended
                        # above, and again on the next exit.
                        entry_fee += remaining_fee
                    else:
                        entry_price = _weighted_entry(entry_price, old_qty, fill, delta)
                        entry_fee += fee

                    qty = 0.0 if abs(new_qty) <= 1e-12 else new_qty
                current_target = pending
            pending = None

        # 2) Intrabar protective exits for whatever is currently held.
        if qty != 0.0:
            reason = _check_risk_exit(
                qty, entry_price, highs[i], lows[i],
                config.stop_loss_pct, config.take_profit_pct,
            )
            if reason is not None:
                exit_px = _risk_exit_price(qty, entry_price, config, reason, opens[i])
                exit_px = _slipped(exit_px, "sell" if qty > 0 else "buy", slip)
                fee = abs(qty) * exit_px * fee_rate
                total_fees += fee
                cash += qty * exit_px - fee
                trades.append(
                    _make_trade(
                        entry_price, exit_px, qty, entry_time, int(ts[i]),
                        entry_index, i, entry_fee + fee, reason,
                    )
                )
                qty = 0.0
                entry_price = 0.0
                entry_fee = 0.0
                # Do not re-enter on the same signal that got stopped out; wait
                # for the strategy to change its mind.
                current_target = targets[i]
            elif config.liquidate and config.leverage > 1.0 and qty != 0.0:
                # 强平排在看门狗之后：止损离得更近，真实里也是先打止损。
                liq_px = _liquidation_price(qty, entry_price, config)
                fill_px = _liquidation_fill(qty, liq_px, opens[i], highs[i], lows[i])
                if fill_px is not None:
                    exit_px = _slipped(fill_px, "sell" if qty > 0 else "buy", slip)
                    fee = abs(qty) * exit_px * fee_rate
                    total_fees += fee
                    cash += qty * exit_px - fee
                    if cash < 0.0:
                        # 亏光为止：保证金账户不会欠钱，现金停在 0
                        warnings.append(
                            f"第 {i} 根强平后账户亏光（{cash:.2f}），现金归零。"
                        )
                        cash = 0.0
                    trades.append(
                        _make_trade(
                            entry_price, exit_px, qty, entry_time, int(ts[i]),
                            entry_index, i, entry_fee + fee, "liquidation",
                        )
                    )
                    qty = 0.0
                    entry_price = 0.0
                    entry_fee = 0.0
                    # 同一根不许再进场：等策略换目标
                    current_target = targets[i]

        # 3) 资金费：这一根里有结算、这一根收盘时还持仓，就按收盘价结一次。
        if qty != 0.0 and funding_schedule:
            rate = funding_schedule.get(i)
            if rate:
                payment = qty * closes[i] * rate   # 空头 qty 为负，自然收钱
                cash -= payment
                total_funding += payment

        # 4) Mark to market at the close.
        equity = cash + qty * closes[i]
        equity_arr[i] = equity
        pos_arr[i] = qty * closes[i] / equity if equity > 0 else 0.0

        # 5) New signal from this close; executed at the next open.
        pending = targets[i] if i < n - 1 else None

    # Close the final position at the last close for clean accounting.
    if qty != 0.0:
        exit_px = _slipped(closes[-1], "sell" if qty > 0 else "buy", slip)
        fee = abs(qty) * exit_px * fee_rate
        total_fees += fee
        cash += qty * exit_px - fee
        trades.append(
            _make_trade(
                entry_price, exit_px, qty, entry_time, int(ts[-1]),
                entry_index, n - 1, entry_fee + fee, "end_of_data",
            )
        )
        equity_arr[-1] = cash

    equity_df = pd.DataFrame(
        {
            "timestamp": ts,
            "equity": equity_arr,
            "position": pos_arr,
            "target": targets,
            "close": closes,
        }
    )
    equity_df["drawdown"] = equity_df["equity"] / equity_df["equity"].cummax() - 1.0

    metrics = compute_metrics(equity_df, trades, config)
    metrics["fee_paid"] = total_fees
    metrics["funding_paid"] = float(total_funding)
    metrics["liquidations"] = float(sum(1 for t in trades if t.exit_reason == "liquidation"))
    if skipped_small:
        warnings.append(
            f"有 {skipped_small} 笔调仓小于最小下单量（{config.min_amount:g}），已跳过。"
        )
    return BacktestResult(equity_df, trades, metrics, dict(params), warnings)


def _weighted_entry(entry_price: float, old_qty: float, fill: float, delta: float) -> float:
    new_qty = old_qty + delta
    if abs(new_qty) < 1e-12 or old_qty == 0:
        return fill
    return (entry_price * old_qty + fill * delta) / new_qty


def _make_trade(
    entry_price: float,
    exit_price: float,
    signed_qty: float,
    entry_time: int,
    exit_time: int,
    entry_index: int,
    exit_index: int,
    fee: float,
    reason: str,
) -> TradeRecord:
    pnl = (exit_price - entry_price) * signed_qty - fee
    base = abs(entry_price * signed_qty) or 1.0
    return TradeRecord(
        entry_time=int(entry_time),
        exit_time=int(exit_time),
        side="long" if signed_qty > 0 else "short",
        entry_price=float(entry_price),
        exit_price=float(exit_price),
        qty=float(signed_qty),
        pnl=float(pnl),
        pnl_pct=float(pnl / base),
        bars=int(exit_index - entry_index),
        exit_reason=reason,
        fee=float(fee),
    )


def _check_risk_exit(
    qty: float, entry: float, high: float, low: float, sl_pct: float, tp_pct: float
) -> str | None:
    if entry <= 0:
        return None
    # Stop loss is checked first: when both levels are inside one bar we assume
    # the worse outcome (conservative).
    if qty > 0:
        if sl_pct > 0 and low <= entry * (1 - sl_pct):
            return "stop_loss"
        if tp_pct > 0 and high >= entry * (1 + tp_pct):
            return "take_profit"
    else:
        if sl_pct > 0 and high >= entry * (1 + sl_pct):
            return "stop_loss"
        if tp_pct > 0 and low <= entry * (1 - tp_pct):
            return "take_profit"
    return None


def _risk_exit_price(
    qty: float, entry: float, config: BacktestConfig, reason: str, bar_open: float
) -> float:
    """止损/止盈的成交价：开盘就跳过触发价时按开盘价，不再按理论价。

    跳空穿过止损时，那根 K 线的第一笔成交就在开盘价上，按 98 成交等于凭空
    少亏一截。跳空穿过止盈同理（往好的方向也要照实算）。
    """
    if reason == "stop_loss":
        level = entry * (1 - config.stop_loss_pct) if qty > 0 else entry * (1 + config.stop_loss_pct)
        return min(level, bar_open) if qty > 0 else max(level, bar_open)
    level = entry * (1 + config.take_profit_pct) if qty > 0 else entry * (1 - config.take_profit_pct)
    return max(level, bar_open) if qty > 0 else min(level, bar_open)


def _liquidation_price(qty: float, entry: float, config: BacktestConfig) -> float:
    """强平价：多 entry*(1 - 1/L + m)，空 entry*(1 + 1/L - m)。"""
    lev = max(1.0, float(config.leverage))
    margin = max(0.0, float(config.maintenance_margin_rate))
    if qty > 0:
        return max(0.0, entry * (1.0 - 1.0 / lev + margin))
    return entry * (1.0 + 1.0 / lev - margin)


def _liquidation_fill(
    qty: float, liq_price: float, bar_open: float, high: float, low: float
) -> float | None:
    """这一根有没有打穿强平价；打穿了返回成交价，没打穿返回 None。

    开盘就在强平价之外（跳空）时按开盘价成交——强平单是市价单，成交在第一笔
    能成交的价上，不是那个算出来的理论价。
    """
    if qty > 0:
        if bar_open <= liq_price:
            return bar_open
        return liq_price if low <= liq_price else None
    if bar_open >= liq_price:
        return bar_open
    return liq_price if high >= liq_price else None


def _funding_by_bar(ts, rates: dict[int, float] | None) -> dict[int, float]:
    """结算时刻 -> 归到哪一根 K 线上（落在 (上一根, 这一根] 的都算这一根）。

    结算时刻是整点的，K 线也是，所以一次结算只会落进一根。第一根之前的结算
    不算：那会儿还没有持仓。
    """
    out: dict[int, float] = {}
    if not rates:
        return out
    stamps = sorted(int(key) for key in rates)
    cursor = 0
    for i in range(1, len(ts)):
        previous, current = int(ts[i - 1]), int(ts[i])
        total = 0.0
        while cursor < len(stamps) and stamps[cursor] <= previous:
            cursor += 1
        while cursor < len(stamps) and stamps[cursor] <= current:
            total += float(rates[stamps[cursor]])
            cursor += 1
        if total:
            out[i] = total
    return out


def _quantize(delta: float, config: BacktestConfig) -> float | None:
    """按交易所的步长与最小量收一次调仓量；太小下不出去就返回 None。

    向零截断：宁可少下一点，也不要下出交易所不收的数量。数量单位是基础币，
    和引擎里 qty 一致。两样都没配就原样返回（老行为一个字节都不变）。
    """
    step = config.amount_step
    floor = config.min_amount
    if (step is None or step <= 0) and floor is None:
        return delta
    qty = delta
    if step and step > 0 and qty:
        steps = math.trunc(abs(qty) / step + 1e-9)
        qty = math.copysign(steps * step, qty)
    if floor is not None and floor > 0 and abs(qty) < floor - 1e-12:
        return None
    return qty

def compute_metrics(
    equity: pd.DataFrame, trades: list[TradeRecord], config: BacktestConfig
) -> dict[str, float]:
    if equity.empty:
        return {}
    values = equity["equity"].to_numpy(dtype=float)
    initial = float(config.initial_capital)
    final = float(values[-1])
    total_return = final / initial - 1.0 if initial else 0.0

    bars = len(values)
    years = bars / config.bars_per_year if config.bars_per_year else 0.0
    if years > 0 and final > 0 and initial > 0:
        annual = (final / initial) ** (1 / years) - 1.0
    else:
        annual = 0.0

    returns = pd.Series(values).pct_change().dropna()
    if len(returns) > 1 and returns.std(ddof=0) > 0:
        sharpe = float(returns.mean() / returns.std(ddof=0) * math.sqrt(config.bars_per_year))
    else:
        sharpe = 0.0
    downside = returns[returns < 0]
    if len(downside) > 1 and downside.std(ddof=0) > 0:
        sortino = float(returns.mean() / downside.std(ddof=0) * math.sqrt(config.bars_per_year))
    else:
        sortino = 0.0

    running_max = np.maximum.accumulate(values)
    max_dd = float((values / running_max - 1.0).min())

    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    gross_profit = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses))
    win_rate = len(wins) / len(trades) if trades else 0.0
    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    else:
        profit_factor = 999.0 if gross_profit > 0 else 0.0

    return {
        "initial_capital": initial,
        "final_equity": final,
        "total_return": float(total_return),
        "annual_return": float(annual),
        "max_drawdown": max_dd,
        "sharpe": sharpe,
        "sortino": sortino,
        "calmar": float(annual / abs(max_dd)) if max_dd < 0 else 0.0,
        "trades": float(len(trades)),
        "win_rate": float(win_rate),
        "profit_factor": float(profit_factor),
        "avg_trade": float(np.mean([t.pnl for t in trades])) if trades else 0.0,
        "avg_bars": float(np.mean([t.bars for t in trades])) if trades else 0.0,
        "exposure": float(np.mean(np.abs(equity["position"].to_numpy()))),
    }


# --------------------------------------------------------------------- grid
def expand_grid(grid: dict[str, list[Any]]) -> list[dict[str, Any]]:
    if not grid:
        return [{}]
    keys = list(grid)
    combos: list[list[Any]] = [[]]
    for key in keys:
        values = grid[key] or []
        if not values:
            return []
        combos = [combo + [value] for combo in combos for value in values]
    return [dict(zip(keys, combo)) for combo in combos]


def grid_search(
    df: pd.DataFrame,
    strategy: Strategy,
    grid: dict[str, list[Any]],
    config: BacktestConfig,
    metric: str = "sharpe",
    top_n: int = 50,
    progress: Callable[[int, int, dict[str, Any]], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    funding_rates: dict[int, float] | None = None,
) -> pd.DataFrame:
    """Brute-force parameter search; returns rows sorted by ``metric`` desc."""
    combos = expand_grid(grid)
    total = len(combos)
    rows: list[dict[str, Any]] = []

    for index, params in enumerate(combos, start=1):
        if should_stop and should_stop():
            break
        clean = strategy.validate_params(params)
        try:
            result = run_backtest(df, strategy, clean, config, funding_rates)
        except Exception as exc:  # a bad parameter set must not abort the sweep
            log.warning("参数组合失败 %s: %s", clean, exc)
            if progress:
                progress(index, total, clean)
            continue
        row = dict(clean)
        row.update({f"m_{k}": v for k, v in result.metrics.items()})
        rows.append(row)
        if progress:
            progress(index, total, clean)

    if not rows:
        return pd.DataFrame()

    frame = pd.DataFrame(rows)
    sort_key = f"m_{metric}"
    if sort_key in frame:
        frame = frame.sort_values(sort_key, ascending=False)
    return frame.head(top_n).reset_index(drop=True)
