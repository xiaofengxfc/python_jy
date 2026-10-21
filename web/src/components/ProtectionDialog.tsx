import { useMemo, useState } from "react";
import { api, query } from "../api";
import { fmt, fmtPrice, parseNumber } from "../format";
import type { PaperPosition, PaperProtection } from "../types";

interface Props {
  position: PaperPosition;
  /** The guard currently armed for this pair, if any. */
  protection?: PaperProtection | null;
  /** Paper guards are local; an exchange position cannot be protected by them. */
  mode?: "paper" | "live";
  onClose: () => void;
  /** Reload the account so the table shows the new levels. */
  onSaved?: () => void;
}

/** Percent moves offered as one-click levels, from the current price. */
const STEPS = [1, 2, 3, 5, 10];

/** Price decimals that stay readable from BTC down to a sub-dollar alt. */
function decimalsFor(price: number): number {
  if (price >= 1000) return 1;
  if (price >= 10) return 2;
  if (price >= 1) return 4;
  return 6;
}

/** Dialog that arms, moves, or disarms the stop-loss / take-profit guard of one
 * position.
 *
 * It exists because the order ticket could only protect the pair being traded:
 * a position opened earlier (or one whose stop has to be trailed as the trade
 * develops) had no place to be adjusted from. The levels are sent to
 * `PUT /api/positions/protection`, the same call the ticket uses, so both
 * paths keep exactly one guard per pair.
 */
export default function ProtectionDialog({
  position,
  protection,
  mode = "paper",
  onClose,
  onSaved,
}: Props) {
  const long = position.contracts > 0;
  const held = Math.abs(position.contracts);
  const mark = position.last && position.last > 0 ? position.last : position.entry_price;
  const decimals = decimalsFor(mark || 1);
  const [stop, setStop] = useState(
    protection && protection.stopLoss > 0 ? String(protection.stopLoss) : "",
  );
  const [target, setTarget] = useState(
    protection && protection.takeProfit > 0 ? String(protection.takeProfit) : "",
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const stopValue = parseNumber(stop);
  const targetValue = parseNumber(target);

  /** A level that sits on the wrong side of the market would fire on the next
   * tick, so it is rejected here as well as on the server. */
  const problem = useMemo(() => {
    if (stopValue > 0 && ((long && stopValue >= mark) || (!long && stopValue <= mark))) {
      return long ? "多单止损价必须低于最新价。" : "空单止损价必须高于最新价。";
    }
    if (targetValue > 0 && ((long && targetValue <= mark) || (!long && targetValue >= mark))) {
      return long ? "多单止盈价必须高于最新价。" : "空单止盈价必须低于最新价。";
    }
    return "";
  }, [long, mark, stopValue, targetValue]);

  const distance = (value: number) => {
    if (!(value > 0) || !(mark > 0)) return "—";
    const pct = ((value - mark) / mark) * 100;
    return `${pct >= 0 ? "+" : ""}${pct.toFixed(2)}%`;
  };

  const level = (pct: number, kind: "stop" | "target") => {
    if (!(mark > 0)) return "";
    const sign = kind === "stop" ? (long ? -1 : 1) : long ? 1 : -1;
    const value = mark * (1 + (sign * pct) / 100);
    return String(Number(value.toFixed(decimals)));
  };

  const apply = async () => {
    if (!stopValue && !targetValue) {
      setError("请填写止损价或止盈价；要取消全部保护请点“撤销保护”。");
      return;
    }
    if (problem) {
      setError(problem);
      return;
    }
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await api("/api/positions/protection", {
        method: "PUT",
        body: JSON.stringify({
          symbol: position.symbol,
          stop_loss: stopValue,
          take_profit: targetValue,
        }),
      });
      setNotice("已保存：最新价触到该价位即市价平仓。");
      onSaved?.();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(false);
    }
  };

  const clear = async () => {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await api(`/api/positions/protection?${query({ symbol: position.symbol })}`, {
        method: "DELETE",
      });
      setStop("");
      setTarget("");
      setNotice("已撤销这笔持仓的止损 / 止盈。");
      onSaved?.();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(false);
    }
  };

  const live = mode === "live";

  return (
    <div
      className="modal-mask"
      role="presentation"
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        className="modal modal-wide"
        role="dialog"
        aria-modal="true"
        aria-label={`${position.symbol} 的止损止盈`}
      >
        <div className="modal-title">{position.symbol} · 止损 / 止盈</div>

        <div className="detail-grid">
          <span>方向 / 数量</span>
          <b>
            <b className={long ? "up" : "down"}>{long ? "多头" : "空头"}</b>{" "}
            <b className="num">{fmt(held, 6)}</b>
          </b>
          <span>开仓价</span>
          <b className="num">{position.entry_price ? fmtPrice(position.entry_price) : "—"}</b>
          <span>最新价</span>
          <b className="num">{position.last ? fmtPrice(position.last) : "—"}</b>
          <span>浮动盈亏</span>
          <b className={`num ${(position.unrealized ?? 0) >= 0 ? "up" : "down"}`}>
            {position.unrealized === undefined ? "—" : fmt(position.unrealized, 2)}
          </b>
        </div>

        {live ? (
          <p className="modal-text">
            实盘持仓的保护单在交易所侧，本地代持的止损止盈不会作用到真实仓位，请在交易所页面设置。
          </p>
        ) : (
          <>
            <div className="field">
              <label className="field-label" htmlFor="dialog-stop">
                <span>止损价</span>
                <em>{long ? "低于最新价" : "高于最新价"}，触到即市价平仓</em>
              </label>
              <input
                id="dialog-stop"
                className="input"
                type="text"
                inputMode="decimal"
                autoComplete="off"
                placeholder={long ? "止损价（低于最新价）" : "止损价（高于最新价）"}
                value={stop}
                onChange={(event) => setStop(event.target.value)}
              />
              <div className="chip-row">
                <span className="faint">距现价</span>
                {STEPS.map((pct) => (
                  <button
                    key={`stop-${pct}`}
                    type="button"
                    className="chip"
                    onClick={() => setStop(level(pct, "stop"))}
                  >
                    {long ? "-" : "+"}
                    {pct}%
                  </button>
                ))}
                <span className="spacer" />
                <b className="num down">{distance(stopValue)}</b>
              </div>
            </div>

            <div className="field">
              <label className="field-label" htmlFor="dialog-target">
                <span>止盈价</span>
                <em>{long ? "高于最新价" : "低于最新价"}，触到即市价平仓</em>
              </label>
              <input
                id="dialog-target"
                className="input"
                type="text"
                inputMode="decimal"
                autoComplete="off"
                placeholder={long ? "止盈价（高于最新价）" : "止盈价（低于最新价）"}
                value={target}
                onChange={(event) => setTarget(event.target.value)}
              />
              <div className="chip-row">
                <span className="faint">距现价</span>
                {STEPS.map((pct) => (
                  <button
                    key={`target-${pct}`}
                    type="button"
                    className="chip"
                    onClick={() => setTarget(level(pct, "target"))}
                  >
                    {long ? "+" : "-"}
                    {pct}%
                  </button>
                ))}
                <span className="spacer" />
                <b className="num up">{distance(targetValue)}</b>
              </div>
            </div>

            {problem && <div className="flash err">{problem}</div>}
            {error && !problem && <div className="flash err">{error}</div>}
            {notice && <div className="flash ok">{notice}</div>}
          </>
        )}

        <div className="modal-actions">
          {!live && (
            <button
              type="button"
              className="btn ghost"
              disabled={busy}
              onClick={() => void clear()}
            >
              撤销保护
            </button>
          )}
          <button type="button" className="btn ghost" onClick={onClose}>
            {live ? "关闭" : "取消"}
          </button>
          {!live && (
            <button
              type="button"
              className="btn primary"
              disabled={busy || Boolean(problem)}
              onClick={() => void apply()}
            >
              {busy ? "保存中…" : "保存"}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
