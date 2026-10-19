import type { Candle } from "./types";

export interface FoldResult {
  /** Updated series, or the same reference when the tick changed nothing. */
  candles: Candle[];
  /** A new bar was opened, so the series grew by one. */
  appended: boolean;
  /** The tick skipped at least one whole bar: history is stale and should be
   * re-fetched instead of guessing what happened in between. */
  gap: boolean;
}

/**
 * Fold one live quote into the tail of a candle series.
 *
 * The exchange REST snapshot is the source of truth for history; this only
 * keeps the right-most bar in step with the socket, which is what makes the
 * chart look live.
 *
 * ``volume`` is the traded size behind this tick, in base units. A ticker
 * carries no per-bar increment, so quotes arrive with 0 and only the public
 * trade feed moves the bar's volume.
 */
export function foldTick(
  candles: Candle[],
  barMs: number,
  price: number,
  whenMs: number,
  volume = 0,
): FoldResult {
  if (!(barMs > 0) || !(price > 0) || !Number.isFinite(price)) {
    return { candles, appended: false, gap: false };
  }
  const gain = Number.isFinite(volume) && volume > 0 ? volume : 0;
  const last = candles[candles.length - 1];
  if (!last) {
    return {
      candles: [
        { t: Math.floor(whenMs / barMs) * barMs, o: price, h: price, l: price, c: price, v: gain },
      ],
      appended: true,
      gap: false,
    };
  }

  const barStart = Math.floor(whenMs / barMs) * barMs;
  const next = candles.slice();

  if (barStart > last.t) {
    // A fresh bar starts with this tick. If more than one interval elapsed the
    // socket slept through history we never saw, so ask for a real snapshot.
    next.push({ t: barStart, o: price, h: price, l: price, c: price, v: gain });
    return { candles: next, appended: true, gap: barStart - last.t > barMs };
  }
  if (barStart < last.t) {
    // A late frame from the previous bar; the tail is already ahead of it.
    return { candles, appended: false, gap: false };
  }

  const high = Math.max(last.h, price);
  const low = Math.min(last.l, price);
  const barVolume = last.v + gain;
  if (last.h === high && last.l === low && last.c === price && last.v === barVolume) {
    return { candles, appended: false, gap: false };
  }

  next[next.length - 1] = { ...last, h: high, l: low, c: price, v: barVolume };
  return { candles: next, appended: false, gap: false };
}
