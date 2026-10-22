/** 后端 K 线 → TradingView（lightweight-charts）数据。
 *
 * 换算只发生在这一层：库要的是秒级 UTCTimestamp，我们手里是币安返回的
 * 毫秒开盘时间。除了单位，这里还负责两件事：
 *
 * 1. 时间必须严格递增且唯一 —— 库对 setData 有这个硬要求，而历史分页、
 *    实时折叠、Gate 兜底行情重叠时都可能给出重复或乱序的 bar；
 * 2. 让指标值、成交量与价格共用同一套下标，谁的序列都不能错位一格。
 */

import type { CandlestickData, HistogramData, LineData, UTCTimestamp } from "lightweight-charts";
import type { Candle } from "../types";
import type { ValueSeries } from "./indicators";

/** 毫秒开盘时间 → 库要求的秒级 UTC 时间戳。 */
export function toTime(ms: number): UTCTimestamp {
  return Math.floor(ms / 1000) as UTCTimestamp;
}

/** 库里的时间是「秒」，还原成毫秒方便与后端、绘图数据比对。 */
export function toMs(time: unknown): number {
  return typeof time === "number" ? time * 1000 : 0;
}

/** K 线 → 蜡烛数据。重复或倒退的时间直接丢掉：库会抛错，而一条重复 bar
 * 多半来自历史分页与实时折叠的边界，丢掉比整张图空白强。 */
export function barData(candles: Candle[]): CandlestickData<UTCTimestamp>[] {
  const out: CandlestickData<UTCTimestamp>[] = [];
  let last = Number.NEGATIVE_INFINITY;
  for (const candle of candles) {
    const time = toTime(candle.t);
    if (Number(time) <= last) continue;
    last = Number(time);
    out.push({
      time,
      open: candle.o,
      high: candle.h,
      low: candle.l,
      close: candle.c,
    });
  }
  return out;
}

/** 成交量：涨绿跌红，与蜡烛同色。 */
export function volumeData(
  candles: Candle[],
  colors: { up: string; down: string },
): HistogramData<UTCTimestamp>[] {
  const out: HistogramData<UTCTimestamp>[] = [];
  let last = Number.NEGATIVE_INFINITY;
  for (const candle of candles) {
    const time = toTime(candle.t);
    if (Number(time) <= last) continue;
    last = Number(time);
    out.push({
      time,
      value: candle.v,
      color: candle.c >= candle.o ? colors.up : colors.down,
    });
  }
  return out;
}

/** 指标序列 → 折线数据。null 的点直接不产生数据点，折线就自然断开
 * （RSI 的前 14 根、ATR 的前 14 根都属于这种情况）。 */
export function lineData(
  candles: Candle[],
  values: readonly (number | null)[],
): LineData<UTCTimestamp>[] {
  const out: LineData<UTCTimestamp>[] = [];
  let last = Number.NEGATIVE_INFINITY;
  candles.forEach((candle, index) => {
    const time = toTime(candle.t);
    if (Number(time) <= last) return;
    last = Number(time);
    const value = values[index];
    if (value == null || !Number.isFinite(value)) return;
    out.push({ time, value });
  });
  return out;
}

/** 直方图数据（MACD 的柱子）。 */
export function histogramData(
  candles: Candle[],
  values: readonly (number | null)[],
  colors: { up: string; down: string },
): HistogramData<UTCTimestamp>[] {
  const out: HistogramData<UTCTimestamp>[] = [];
  let last = Number.NEGATIVE_INFINITY;
  candles.forEach((candle, index) => {
    const time = toTime(candle.t);
    if (Number(time) <= last) return;
    last = Number(time);
    const value = values[index];
    if (value == null || !Number.isFinite(value)) return;
    out.push({ time, value, color: value >= 0 ? colors.up : colors.down });
  });
  return out;
}

/** Index of the candle whose open time is closest to t. Binary search:
 * drawings can be hours or weeks away from the visible window. */
export function indexOfTime(candles: Candle[], t: number): number {
  if (!candles.length) return 0;
  let low = 0;
  let high = candles.length - 1;
  while (low < high) {
    const mid = (low + high) >> 1;
    if (candles[mid]!.t < t) low = mid + 1;
    else high = mid;
  }
  if (low > 0 && Math.abs(candles[low - 1]!.t - t) <= Math.abs(candles[low]!.t - t)) {
    return low - 1;
  }
  return low;
}

/** 成交时间 → 它所在那根 K 线的开盘时间。买卖点标记必须落在真实存在的
 * bar 上，否则库会把标记丢掉；用整段行情推算比按周期取模更可靠
 * （周线的锚点是周一，不是 epoch 0 所在的周四）。 */
export function barTimeAt(candles: Candle[], ms: number): number | null {
  if (!candles.length) return null;
  if (ms < candles[0]!.t) return candles[0]!.t;
  const index = indexOfTime(candles, ms);
  const bar = candles[index]!;
  return bar.t;
}

/** 一段线段到点的最短距离（像素），画线的命中测试用。 */
export function distanceToSegment(
  x: number,
  y: number,
  ax: number,
  ay: number,
  bx: number,
  by: number,
): number {
  const dx = bx - ax;
  const dy = by - ay;
  const lengthSq = dx * dx + dy * dy;
  if (lengthSq === 0) return Math.hypot(x - ax, y - ay);
  const t = Math.min(1, Math.max(0, ((x - ax) * dx + (y - ay) * dy) / lengthSq));
  return Math.hypot(x - (ax + t * dx), y - (ay + t * dy));
}

export type { ValueSeries };
