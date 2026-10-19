import { useCallback, useEffect, useRef, useState } from "react";
import type { PointerEvent as ReactPointerEvent, WheelEvent as ReactWheelEvent } from "react";
import type { Candle } from "../types";
import { fmtPrice, shortTime } from "../format";
import { cssVar, useThemeRevision } from "../hooks/useThemeRevision";

export type Indicator = "none" | "boll" | "macd" | "rsi";

interface Props {
  candles: Candle[];
  timeframe: string;
  lastPrice: number;
  loading: boolean;
  error: string;
  indicator: Indicator;
}

/** Colours are read from the active theme's tokens at paint time, so switching
 * to dark mode never leaves a white grid behind. */
function colors() {
  return {
    grid: cssVar("--border", "#f0f0f2"),
    axis: cssVar("--text-3", "#9a9aa3"),
    up: cssVar("--up", "#16a34a"),
    down: cssVar("--down", "#e11d48"),
    upVolume: cssVar("--up-weak", "#d8f0e2"),
    downVolume: cssVar("--down-weak", "#fbdde4"),
    crosshair: cssVar("--text-3", "#9a9aa3"),
    tagBg: cssVar("--ink", "#141414"),
    tagText: cssVar("--bg", "#ffffff"),
    ma7: cssVar("--accent", "#2563eb"),
    ma25: cssVar("--warn", "#b45309"),
    boll: cssVar("--text-2", "#5b5b5b"),
    signal: cssVar("--accent", "#2563eb"),
    macd: cssVar("--text-2", "#5b5b5b"),
    hover: cssVar("--bg-hover", "#f4f4f4"),
  };
}

const MIN_BARS = 40;

export default function Chart({ candles, timeframe, lastPrice, loading, error, indicator }: Props) {
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const themeRevision = useThemeRevision();
  const [offset, setOffset] = useState(0);
  const [hover, setHover] = useState<{ x: number; y: number } | null>(null);
  const drag = useRef<{ x: number; start: number } | null>(null);

  // A new symbol/timeframe must not inherit a scroll position from the old one.
  useEffect(() => {
    setOffset(0);
    setHover(null);
  }, [candles, timeframe]);

  useEffect(() => {
    const wrap = wrapRef.current;
    const canvas = canvasRef.current;
    if (!wrap || !canvas) return;

    const paint = () =>
      draw(canvas, wrap, candles, timeframe, lastPrice, offset, hover, indicator, colors());
    paint();

    const observer = new ResizeObserver(paint);
    observer.observe(wrap);
    return () => observer.disconnect();
  }, [candles, timeframe, lastPrice, offset, hover, indicator, themeRevision]);

  const barsInView = useCallback(() => {
    const width = wrapRef.current?.clientWidth ?? 640;
    return Math.min(Math.max(MIN_BARS, Math.floor(width / 8)), Math.max(candles.length, MIN_BARS));
  }, [candles.length]);

  const onPointerDown = (event: ReactPointerEvent<HTMLCanvasElement>) => {
    if (event.button !== 0) return;
    drag.current = { x: event.clientX, start: offset };
    event.currentTarget.setPointerCapture(event.pointerId);
  };

  const onPointerMove = (event: ReactPointerEvent<HTMLCanvasElement>) => {
    const rect = event.currentTarget.getBoundingClientRect();
    setHover({ x: event.clientX - rect.left, y: event.clientY - rect.top });
    if (!drag.current) return;
    const step = Math.max(4, (rect.width - 70) / barsInView());
    const shift = Math.round((event.clientX - drag.current.x) / step);
    const maxOffset = Math.max(0, candles.length - MIN_BARS);
    setOffset(Math.min(maxOffset, Math.max(0, drag.current.start + shift)));
  };

  const endDrag = (event: ReactPointerEvent<HTMLCanvasElement>) => {
    if (drag.current) event.currentTarget.releasePointerCapture(event.pointerId);
    drag.current = null;
  };

  const onWheel = (event: ReactWheelEvent<HTMLCanvasElement>) => {
    // Scroll up walks back in time, which is what a chart should do.
    const maxOffset = Math.max(0, candles.length - MIN_BARS);
    setOffset((value) => Math.min(maxOffset, Math.max(0, value + Math.sign(event.deltaY) * 3)));
  };

  const empty = candles.length === 0;

  return (
    <div className="chart-wrap" ref={wrapRef}>
      <canvas
        ref={canvasRef}
        className="chart-canvas"
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
        onPointerLeave={() => {
          setHover(null);
          drag.current = null;
        }}
        onWheel={onWheel}
      />
      {!empty && (
        <div className="chart-legend">
          <span className="legend-item">
            <i style={{ background: "var(--accent, #2563eb)" }} /> MA7
          </span>
          <span className="legend-item">
            <i style={{ background: "var(--warn, #b45309)" }} /> MA25
          </span>
          {indicator === "boll" && (
            <span className="legend-item">
              <i style={{ background: "var(--text-2, #5b5b5b)" }} /> BOLL(20,2)
            </span>
          )}
          {offset > 0 && (
            <button type="button" className="link-btn" onClick={() => setOffset(0)}>
              回到最新
            </button>
          )}
        </div>
      )}
      {(empty || loading) && (
        <div className="chart-empty">
          {error ? `K 线加载失败：${error}` : loading ? "正在加载行情…" : "暂无 K 线数据"}
        </div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------- indicators */

function ema(values: number[], period: number): number[] {
  const k = 2 / (period + 1);
  const out: number[] = [];
  let prev = values[0] ?? 0;
  values.forEach((value, index) => {
    prev = index === 0 ? value : value * k + prev * (1 - k);
    out.push(prev);
  });
  return out;
}

function rsi(values: number[], period = 14): (number | null)[] {
  const out: (number | null)[] = new Array(values.length).fill(null);
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

/* ------------------------------------------------------------------ paint */

function draw(
  canvas: HTMLCanvasElement,
  wrap: HTMLElement,
  candles: Candle[],
  timeframe: string,
  lastPrice: number,
  offset: number,
  hover: { x: number; y: number } | null,
  indicator: Indicator,
  COLORS: ReturnType<typeof colors>,
) {
  const rect = wrap.getBoundingClientRect();
  const width = Math.max(240, Math.floor(rect.width) - 16);
  const height = Math.max(180, Math.floor(rect.height) - 8);
  const dpr = window.devicePixelRatio || 1;

  canvas.width = width * dpr;
  canvas.height = height * dpr;
  canvas.style.width = `${width}px`;
  canvas.style.height = `${height}px`;

  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);
  if (!candles.length) return;

  const visible = Math.min(candles.length, Math.max(MIN_BARS, Math.floor(width / 8)));
  const end = Math.max(visible, candles.length - offset);
  const start = Math.max(0, end - visible);
  const slice = candles.slice(start, end);

  const padding = { top: 12, right: 62, bottom: 22, left: 8 };
  const subH = indicator === "macd" || indicator === "rsi" ? 62 : 0;
  const volumeH = Math.round((height - padding.top - padding.bottom - subH) * 0.16);
  const priceH = height - padding.top - padding.bottom - volumeH - 10 - subH;
  const left = padding.left;
  const right = width - padding.right;
  const plotW = right - left;

  const highs = slice.map((candle) => candle.h);
  const lows = slice.map((candle) => candle.l);
  let max = Math.max(...highs);
  let min = Math.min(...lows);

  const closes = candles.map((candle) => candle.c);
  const boll = indicator === "boll" ? bands(closes, 20) : null;
  if (boll) {
    for (let i = start; i < end; i += 1) {
      const upper = boll.upper[i];
      const lower = boll.lower[i];
      if (upper != null) max = Math.max(max, upper);
      if (lower != null) min = Math.min(min, lower);
    }
  }

  const pad = (max - min) * 0.08 || max * 0.001 || 1;
  max += pad;
  min -= pad;
  const range = max - min || 1;
  const yOf = (price: number) => padding.top + priceH * (1 - (price - min) / range);

  const step = plotW / slice.length;
  const bodyW = Math.max(1, Math.min(11, step * 0.62));

  ctx.font = "10px Inter, sans-serif";
  ctx.textBaseline = "middle";
  ctx.textAlign = "left";
  for (let i = 0; i <= 4; i += 1) {
    const y = padding.top + (priceH * i) / 4;
    const price = max - (range * i) / 4;
    ctx.strokeStyle = COLORS.grid;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(left, Math.round(y) + 0.5);
    ctx.lineTo(right, Math.round(y) + 0.5);
    ctx.stroke();
    ctx.fillStyle = COLORS.axis;
    ctx.fillText(fmtPrice(price), right + 8, y);
  }

  const volTop = padding.top + priceH + 10;
  const volMax = Math.max(...slice.map((candle) => candle.v), 1);
  slice.forEach((candle, index) => {
    const x = left + index * step + step / 2;
    const barH = (candle.v / volMax) * volumeH;
    ctx.fillStyle = candle.c >= candle.o ? COLORS.upVolume : COLORS.downVolume;
    ctx.fillRect(x - bodyW / 2, volTop + volumeH - barH, bodyW, barH);
  });

  slice.forEach((candle, index) => {
    const x = left + index * step + step / 2;
    const color = candle.c >= candle.o ? COLORS.up : COLORS.down;
    ctx.strokeStyle = color;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(Math.round(x) + 0.5, yOf(candle.h));
    ctx.lineTo(Math.round(x) + 0.5, yOf(candle.l));
    ctx.stroke();

    const openY = yOf(candle.o);
    const closeY = yOf(candle.c);
    ctx.fillStyle = color;
    ctx.fillRect(x - bodyW / 2, Math.min(openY, closeY), bodyW, Math.max(1, Math.abs(closeY - openY)));
  });

  drawMA(ctx, slice, step, left, yOf, 7, COLORS.ma7);
  drawMA(ctx, slice, step, left, yOf, 25, COLORS.ma25);

  if (boll) {
    drawLine(ctx, boll.upper, start, end, slice.length, step, left, yOf, COLORS.boll, 1);
    drawLine(ctx, boll.middle, start, end, slice.length, step, left, yOf, COLORS.boll, 1, [3, 3]);
    drawLine(ctx, boll.lower, start, end, slice.length, step, left, yOf, COLORS.boll, 1);
  }

  const reference = lastPrice || slice[slice.length - 1]!.c;
  const refY = yOf(reference);
  ctx.strokeStyle = COLORS.crosshair;
  ctx.setLineDash([4, 3]);
  ctx.beginPath();
  ctx.moveTo(left, refY);
  ctx.lineTo(right, refY);
  ctx.stroke();
  ctx.setLineDash([]);

  const tag = fmtPrice(reference);
  ctx.font = "10px Inter, sans-serif";
  const tagW = ctx.measureText(tag).width + 10;
  ctx.fillStyle = COLORS.tagBg;
  ctx.fillRect(right + 4, refY - 8, Math.min(tagW, padding.right - 6), 16);
  ctx.fillStyle = COLORS.tagText;
  ctx.fillText(tag, right + 9, refY);

  ctx.fillStyle = COLORS.axis;
  ctx.textAlign = "center";
  const ticks = Math.min(5, slice.length);
  for (let i = 0; i < ticks; i += 1) {
    const index = Math.floor((slice.length - 1) * (i / Math.max(1, ticks - 1)));
    const candle = slice[index]!;
    const x = left + index * step + step / 2;
    ctx.fillText(
      shortTime(candle.t, timeframe === "1d"),
      Math.min(right - 20, Math.max(left + 20, x)),
      height - 8,
    );
  }

  if (indicator === "macd" || indicator === "rsi") {
    const subTop = padding.top + priceH + 10 + volumeH + 10;
    drawSubChart(ctx, {
      indicator,
      closes: indicator === "macd" ? closes.slice(0, end) : closes.slice(0, end),
      start,
      end,
      top: subTop,
      height: subH,
      left,
      right,
      step,
      COLORS,
    });
  }

  // Crosshair is drawn last so it sits above every indicator.
  if (hover && hover.x >= left && hover.x <= right && hover.y >= padding.top && hover.y <= volTop + volumeH) {
    const index = Math.min(slice.length - 1, Math.max(0, Math.floor((hover.x - left) / step)));
    const candle = slice[index]!;
    const snapX = left + index * step + step / 2;

    ctx.strokeStyle = COLORS.crosshair;
    ctx.setLineDash([3, 3]);
    ctx.beginPath();
    ctx.moveTo(Math.round(snapX) + 0.5, padding.top);
    ctx.lineTo(Math.round(snapX) + 0.5, volTop + volumeH);
    ctx.moveTo(left, Math.round(hover.y) + 0.5);
    ctx.lineTo(right, Math.round(hover.y) + 0.5);
    ctx.stroke();
    ctx.setLineDash([]);

    const hoverPrice = max - ((hover.y - padding.top) / priceH) * range;
    const priceText = fmtPrice(hoverPrice);
    const w = ctx.measureText(priceText).width + 10;
    ctx.fillStyle = COLORS.tagBg;
    ctx.fillRect(right + 4, hover.y - 8, Math.min(w, padding.right - 6), 16);
    ctx.fillStyle = COLORS.tagText;
    ctx.textAlign = "left";
    ctx.fillText(priceText, right + 9, hover.y);

    const timeText = shortTime(candle.t, timeframe === "1d");
    const tw = ctx.measureText(timeText).width + 10;
    ctx.fillStyle = COLORS.tagBg;
    ctx.fillRect(snapX - tw / 2, height - 18, tw, 15);
    ctx.fillStyle = COLORS.tagText;
    ctx.textAlign = "center";
    ctx.fillText(timeText, snapX, height - 10);
  }
}

function bands(values: number[], period: number) {
  const middle: (number | null)[] = new Array(values.length).fill(null);
  const upper: (number | null)[] = new Array(values.length).fill(null);
  const lower: (number | null)[] = new Array(values.length).fill(null);
  for (let i = period - 1; i < values.length; i += 1) {
    let sum = 0;
    for (let j = i - period + 1; j <= i; j += 1) sum += values[j]!;
    const mean = sum / period;
    let variance = 0;
    for (let j = i - period + 1; j <= i; j += 1) variance += (values[j]! - mean) ** 2;
    const sd = Math.sqrt(variance / period);
    middle[i] = mean;
    upper[i] = mean + sd * 2;
    lower[i] = mean - sd * 2;
  }
  return { middle, upper, lower };
}

function drawLine(
  ctx: CanvasRenderingContext2D,
  values: (number | null)[],
  start: number,
  end: number,
  length: number,
  step: number,
  left: number,
  yOf: (value: number) => number,
  color: string,
  lineWidth = 1.2,
  dash: number[] = [],
) {
  ctx.strokeStyle = color;
  ctx.lineWidth = lineWidth;
  ctx.setLineDash(dash);
  ctx.beginPath();
  let started = false;
  for (let i = start; i < end; i += 1) {
    const value = values[i];
    if (value == null) continue;
    const x = left + (i - start) * step + step / 2;
    const y = yOf(value);
    if (!started) {
      ctx.moveTo(x, y);
      started = true;
    } else {
      ctx.lineTo(x, y);
    }
  }
  ctx.stroke();
  ctx.setLineDash([]);
  void length;
}

interface SubOptions {
  indicator: "macd" | "rsi";
  closes: number[];
  start: number;
  end: number;
  top: number;
  height: number;
  left: number;
  right: number;
  step: number;
  COLORS: ReturnType<typeof colors>;
}

function drawSubChart(ctx: CanvasRenderingContext2D, options: SubOptions) {
  const { indicator, closes, start, end, top, height, left, right, step, COLORS } = options;
  ctx.strokeStyle = COLORS.grid;
  ctx.beginPath();
  ctx.moveTo(left, Math.round(top) + 0.5);
  ctx.lineTo(right, Math.round(top) + 0.5);
  ctx.stroke();

  ctx.font = "9px Inter, sans-serif";
  ctx.textAlign = "left";
  ctx.fillStyle = COLORS.axis;
  ctx.fillText(indicator === "macd" ? "MACD(12,26,9)" : "RSI(14)", left + 2, top + 9);

  if (indicator === "rsi") {
    const values = rsi(closes);
    const y = (value: number) => top + height - (value / 100) * (height - 4);
    [30, 70].forEach((level) => {
      ctx.strokeStyle = COLORS.grid;
      ctx.setLineDash([2, 3]);
      ctx.beginPath();
      ctx.moveTo(left, y(level));
      ctx.lineTo(right, y(level));
      ctx.stroke();
      ctx.setLineDash([]);
    });
    ctx.beginPath();
    let started = false;
    for (let i = start; i < end; i += 1) {
      const value = values[i];
      if (value == null) continue;
      const x = left + (i - start) * step + step / 2;
      if (!started) {
        ctx.moveTo(x, y(value));
        started = true;
      } else {
        ctx.lineTo(x, y(value));
      }
    }
    ctx.strokeStyle = COLORS.signal;
    ctx.lineWidth = 1.2;
    ctx.stroke();
    return;
  }

  const fast = ema(closes, 12);
  const slow = ema(closes, 26);
  const dif = fast.map((value, index) => value - slow[index]!);
  const dea = ema(dif, 9);
  const hist = dif.map((value, index) => value - dea[index]!);

  let limit = 1e-9;
  for (let i = start; i < end; i += 1) {
    limit = Math.max(limit, Math.abs(dif[i]!), Math.abs(dea[i]!), Math.abs(hist[i]!));
  }
  const mid = top + height / 2;
  const y = (value: number) => mid - (value / limit) * (height / 2 - 4);

  ctx.strokeStyle = COLORS.grid;
  ctx.beginPath();
  ctx.moveTo(left, mid);
  ctx.lineTo(right, mid);
  ctx.stroke();

  for (let i = start; i < end; i += 1) {
    const x = left + (i - start) * step + step / 2;
    const value = hist[i]!;
    ctx.fillStyle = value >= 0 ? COLORS.upVolume : COLORS.downVolume;
    const y0 = y(0);
    const y1 = y(value);
    ctx.fillRect(x - Math.max(1, step * 0.3), Math.min(y0, y1), Math.max(1, step * 0.6), Math.max(1, Math.abs(y1 - y0)));
  }

  ctx.beginPath();
  for (let i = start; i < end; i += 1) {
    const x = left + (i - start) * step + step / 2;
    if (i === start) ctx.moveTo(x, y(dif[i]!));
    else ctx.lineTo(x, y(dif[i]!));
  }
  ctx.strokeStyle = COLORS.macd;
  ctx.lineWidth = 1.2;
  ctx.stroke();

  ctx.beginPath();
  for (let i = start; i < end; i += 1) {
    const x = left + (i - start) * step + step / 2;
    if (i === start) ctx.moveTo(x, y(dea[i]!));
    else ctx.lineTo(x, y(dea[i]!));
  }
  ctx.strokeStyle = COLORS.signal;
  ctx.stroke();
}

function drawMA(
  ctx: CanvasRenderingContext2D,
  slice: Candle[],
  step: number,
  left: number,
  yOf: (price: number) => number,
  period: number,
  color: string,
) {
  if (slice.length < period) return;
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.2;
  ctx.beginPath();
  let started = false;
  for (let i = period - 1; i < slice.length; i += 1) {
    let sum = 0;
    for (let j = i - period + 1; j <= i; j += 1) sum += slice[j]!.c;
    const x = left + i * step + step / 2;
    const y = yOf(sum / period);
    if (!started) {
      ctx.moveTo(x, y);
      started = true;
    } else {
      ctx.lineTo(x, y);
    }
  }
  ctx.stroke();
}
