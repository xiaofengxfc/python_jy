/** 指标计算：纯函数，不碰 DOM、不碰 React。
 *
 * 算法是从原来手绘 canvas 的 Chart.tsx 里原样搬过来的，一个数都没改，
 * 所以换成 TradingView 图表之后，图上每一根线的数值与旧版完全一致。
 */

import type { Candle } from "../types";

/** 允许有空值的序列：前 N 根还没攒够样本时就是 null。 */
export type ValueSeries = (number | null)[];

/** 简单移动平均。 */
export function sma(values: number[], period: number): ValueSeries {
  const out: ValueSeries = new Array(values.length).fill(null);
  if (period < 1) return out;
  let sum = 0;
  for (let i = 0; i < values.length; i += 1) {
    sum += values[i]!;
    if (i >= period) sum -= values[i - period]!;
    if (i >= period - 1) out[i] = sum / period;
  }
  return out;
}

export function ema(values: number[], period: number): number[] {
  const k = 2 / (period + 1);
  const out: number[] = [];
  let prev = values[0] ?? 0;
  values.forEach((value, index) => {
    prev = index === 0 ? value : value * k + prev * (1 - k);
    out.push(prev);
  });
  return out;
}

export function rsi(values: number[], period = 14): ValueSeries {
  const out: ValueSeries = new Array(values.length).fill(null);
  if (values.length <= period) return out;
  let gain = 0;
  let loss = 0;
  for (let i = 1; i <= period; i += 1) {
    const diff = values[i]! - values[i - 1]!;
    if (diff >= 0) gain += diff;
    else loss -= diff;
  }
  gain /= period;
  loss /= period;
  out[period] = loss === 0 ? 100 : 100 - 100 / (1 + gain / loss);
  for (let i = period + 1; i < values.length; i += 1) {
    const diff = values[i]! - values[i - 1]!;
    gain = (gain * (period - 1) + Math.max(diff, 0)) / period;
    loss = (loss * (period - 1) + Math.max(-diff, 0)) / period;
    out[i] = loss === 0 ? 100 : 100 - 100 / (1 + gain / loss);
  }
  return out;
}

/** 布林带：中轨是均线，上下轨是中轨 ± 倍数 × 标准差。 */
export function bands(values: number[], period: number, mult = 2) {
  const middle: ValueSeries = new Array(values.length).fill(null);
  const upper: ValueSeries = new Array(values.length).fill(null);
  const lower: ValueSeries = new Array(values.length).fill(null);
  for (let i = period - 1; i < values.length; i += 1) {
    let sum = 0;
    for (let j = i - period + 1; j <= i; j += 1) sum += values[j]!;
    const mean = sum / period;
    let variance = 0;
    for (let j = i - period + 1; j <= i; j += 1) variance += (values[j]! - mean) ** 2;
    const sd = Math.sqrt(variance / period);
    middle[i] = mean;
    upper[i] = mean + sd * mult;
    lower[i] = mean - sd * mult;
  }
  return { middle, upper, lower };
}

/** Williams %R: where the close sits inside the period's range, -100..0. */
export function wr(candles: Candle[], period: number): ValueSeries {
  const out: ValueSeries = new Array(candles.length).fill(null);
  for (let i = period - 1; i < candles.length; i += 1) {
    let high = -Infinity;
    let low = Infinity;
    for (let j = i - period + 1; j <= i; j += 1) {
      high = Math.max(high, candles[j]!.h);
      low = Math.min(low, candles[j]!.l);
    }
    const span = high - low;
    out[i] = span > 0 ? ((high - candles[i]!.c) / span) * -100 : -50;
  }
  return out;
}

/** Commodity Channel Index around the typical price's mean deviation. */
export function cci(candles: Candle[], period: number): ValueSeries {
  const typical = candles.map((candle) => (candle.h + candle.l + candle.c) / 3);
  const out: ValueSeries = new Array(candles.length).fill(null);
  for (let i = period - 1; i < typical.length; i += 1) {
    let sum = 0;
    for (let j = i - period + 1; j <= i; j += 1) sum += typical[j]!;
    const mean = sum / period;
    let deviation = 0;
    for (let j = i - period + 1; j <= i; j += 1) deviation += Math.abs(typical[j]! - mean);
    deviation /= period;
    out[i] = deviation > 0 ? (typical[i]! - mean) / (0.015 * deviation) : 0;
  }
  return out;
}

/** On-balance volume plus its simple moving average. */
export function obv(candles: Candle[], period: number) {
  const line: number[] = new Array(candles.length).fill(0);
  for (let i = 1; i < candles.length; i += 1) {
    const previous = candles[i - 1]!.c;
    const current = candles[i]!.c;
    line[i] =
      line[i - 1]! +
      (current > previous ? candles[i]!.v : current < previous ? -candles[i]!.v : 0);
  }
  const average: ValueSeries = new Array(candles.length).fill(null);
  for (let i = period - 1; i < line.length; i += 1) {
    let sum = 0;
    for (let j = i - period + 1; j <= i; j += 1) sum += line[j]!;
    average[i] = sum / period;
  }
  return { line, average };
}

/** Average true range, Wilder smoothed. */
export function atr(candles: Candle[], period: number): ValueSeries {
  const out: ValueSeries = new Array(candles.length).fill(null);
  let total = 0;
  let value = 0;
  for (let i = 0; i < candles.length; i += 1) {
    const current = candles[i]!;
    const previous = i > 0 ? candles[i - 1]!.c : current.c;
    const tr = Math.max(
      current.h - current.l,
      Math.abs(current.h - previous),
      Math.abs(current.l - previous),
    );
    if (i < period) {
      total += tr;
      if (i === period - 1) {
        value = total / period;
        out[i] = value;
      }
      continue;
    }
    value = (value * (period - 1) + tr) / period;
    out[i] = value;
  }
  return out;
}

/** Directional movement: +DI and -DI with the Wilder ADX on top. */
export function dmi(candles: Candle[], period: number, adxPeriod: number) {
  const plus: ValueSeries = new Array(candles.length).fill(null);
  const minus: ValueSeries = new Array(candles.length).fill(null);
  const adx: ValueSeries = new Array(candles.length).fill(null);
  const dx: ValueSeries = new Array(candles.length).fill(null);
  let trSum = 0;
  let plusSum = 0;
  let minusSum = 0;
  for (let i = 1; i < candles.length; i += 1) {
    const current = candles[i]!;
    const previous = candles[i - 1]!;
    const tr = Math.max(
      current.h - current.l,
      Math.abs(current.h - previous.c),
      Math.abs(current.l - previous.c),
    );
    const up = current.h - previous.h;
    const down = previous.l - current.l;
    const plusDm = up > down && up > 0 ? up : 0;
    const minusDm = down > up && down > 0 ? down : 0;
    if (i <= period) {
      trSum += tr;
      plusSum += plusDm;
      minusSum += minusDm;
      if (i < period) continue;
    } else {
      trSum = trSum - trSum / period + tr;
      plusSum = plusSum - plusSum / period + plusDm;
      minusSum = minusSum - minusSum / period + minusDm;
    }
    const plusDi = trSum > 0 ? (plusSum / trSum) * 100 : 0;
    const minusDi = trSum > 0 ? (minusSum / trSum) * 100 : 0;
    plus[i] = plusDi;
    minus[i] = minusDi;
    const sum = plusDi + minusDi;
    dx[i] = sum > 0 ? (Math.abs(plusDi - minusDi) / sum) * 100 : 0;
  }
  let seed = 0;
  let count = 0;
  let previous: number | null = null;
  for (let i = 1; i < dx.length; i += 1) {
    const value = dx[i];
    if (value == null) continue;
    if (count < adxPeriod) {
      seed += value;
      count += 1;
      if (count === adxPeriod) {
        previous = seed / adxPeriod;
        adx[i] = previous;
      }
      continue;
    }
    previous = ((previous ?? value) * (adxPeriod - 1) + value) / adxPeriod;
    adx[i] = previous;
  }
  return { plus, minus, adx };
}

/** KDJ(9,3,3): stochastic oscillator with the J line derived from K and D. */
export function kdj(candles: Candle[], period = 9, smooth = 3) {
  const k: number[] = new Array(candles.length).fill(50);
  const d: number[] = new Array(candles.length).fill(50);
  const j: number[] = new Array(candles.length).fill(50);
  for (let i = 0; i < candles.length; i += 1) {
    const from = Math.max(0, i - period + 1);
    let low = Infinity;
    let high = -Infinity;
    for (let n = from; n <= i; n += 1) {
      low = Math.min(low, candles[n]!.l);
      high = Math.max(high, candles[n]!.h);
    }
    const span = high - low;
    const rsv = span > 0 ? ((candles[i]!.c - low) / span) * 100 : 50;
    const prevK = i === 0 ? 50 : k[i - 1]!;
    const prevD = i === 0 ? 50 : d[i - 1]!;
    k[i] = (prevK * (smooth - 1) + rsv) / smooth;
    d[i] = (prevD * (smooth - 1) + k[i]!) / smooth;
    j[i] = 3 * k[i]! - 2 * d[i]!;
  }
  return { k, d, j };
}

/** MACD: DIF, DEA and the histogram between them. */
export function macd(closes: number[], fast: number, slow: number, signal: number) {
  const fastLine = ema(closes, fast);
  const slowLine = ema(closes, slow);
  const dif = fastLine.map((item, index) => item - slowLine[index]!);
  const dea = ema(dif, signal);
  const hist = dif.map((item, index) => item - dea[index]!);
  return { dif, dea, hist };
}
