import { useCallback, useEffect, useRef, useState } from "react";
import type { PointerEvent as ReactPointerEvent } from "react";
import type { Candle } from "../types";
import { fmtPrice, shortTime } from "../format";
import { cssVar, useThemeRevision } from "../hooks/useThemeRevision";

export type Indicator = "none" | "boll" | "macd" | "rsi";

interface Props {
  candles: Candle[];
  /** Identifies the series (market + symbol) so a live tail update does not
   * look like a new instrument and reset the user's zoom/scroll. */
  seriesKey: string;
  /** Identifies the *data* on screen: series plus the timeframe it was loaded
   * for. While a timeframe switch is in flight the old candles are still
   * passed down, so keying history detection on `seriesKey` alone would label
   * minutes-old bars as the new period's oldest bar. */
  dataKey?: string;
  timeframe: string;
  lastPrice: number;
  loading: boolean;
  error: string;
  indicator: Indicator;
  /** Asks for the window before the oldest candle. Called once the view is
   * scrolled all the way to the left edge. */
  onLoadOlder?: () => void;
  /** A history request is in flight; shown as a hint and used to rate-limit. */
  olderLoading?: boolean;
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
/** Hard floor for zooming in: a handful of candles fill the plot. */
const MIN_ZOOM_BARS = 3;
/** Ceiling for zooming out. The chart asks for more history once the oldest
 * loaded bar comes into view, so this is a comfort limit, not a data limit. */
const MAX_ZOOM_BARS = 1000;
/** One wheel notch (deltaY ~100) changes the visible range by this factor. */
const ZOOM_FACTOR = 1.6;
/** How far one wheel event may travel: a fast flick must still land somewhere
 * sensible, but too low a cap makes trackpads feel unresponsive. */
const MAX_WHEEL_STEPS = 6;
const MIN_WHEEL_STEPS = 0.5;
/** Wicks are treated as spikes worth clipping once the full high/low range is
 * this many times wider than the range of candle bodies. Below it the real
 * price action still sets the scale. */
const SPIKE_RATIO = 1.8;

export default function Chart({
  candles,
  seriesKey,
  dataKey,
  timeframe,
  lastPrice,
  loading,
  error,
  indicator,
  onLoadOlder,
  olderLoading = false,
}: Props) {
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const themeRevision = useThemeRevision();
  const [offset, setOffset] = useState(0);
  // null means "as many bars as the width comfortably fits"; a number is the
  // user's zoom level. Kept across quote updates so zooming does not reset.
  const [zoomBars, setZoomBars] = useState<number | null>(null);
  const [hover, setHover] = useState<{ x: number; y: number } | null>(null);
  const drag = useRef<{ x: number; start: number } | null>(null);
  // The oldest bar and series seen so far, used to tell a prepended history
  // block apart from a new bar at the tail. Keyed on the *loaded data*, not on
  // the requested timeframe: during a switch the old candles are still on
  // screen, and labelling them as the new period would make the arriving
  // series look like prepended history and scroll the view hours back.
  const dataId = dataKey ?? seriesKey;
  const seenHead = useRef<{ key: string; firstT: number }>({ key: "", firstT: 0 });
  const olderAsk = useRef(0);

  // A new symbol/timeframe must not inherit a scroll position from the old one.
  // Deliberately keyed on the instrument, not on `candles`: the last bar is
  // rewritten on every tick, and resetting there would fight the user's zoom.
  useEffect(() => {
    setOffset(0);
    setHover(null);
    setZoomBars(null);
  }, [dataId, timeframe]);

  // Prepending older candles grows the series at the front, so the scroll
  // offset must grow by the same amount or the view would jump forward.
  useEffect(() => {
    const firstT = candles[0]?.t ?? 0;
    const previous = seenHead.current;
    if (previous.key === dataId && previous.firstT && firstT && firstT < previous.firstT) {
      const added = candles.findIndex((bar) => bar.t >= previous.firstT);
      if (added > 0) setOffset((value) => value + added);
    }
    seenHead.current = { key: dataId, firstT };
  }, [candles, dataId]);

  const autoBars = useCallback(() => {
    const width = wrapRef.current?.clientWidth ?? 640;
    const fit = Math.max(MIN_BARS, Math.floor(width / 8));
    // Never ask for more bars than the series holds: a week-old symbol on a
    // long timeframe may only have a dozen candles, and drawing 40 slots from
    // 13 bars leaves bars piled in the left corner.
    return Math.max(MIN_ZOOM_BARS, Math.min(fit, Math.max(candles.length, MIN_ZOOM_BARS)));
  }, [candles.length]);

  const maxBars = Math.max(candles.length, MIN_ZOOM_BARS);
  const visibleBars =
    zoomBars === null
      ? autoBars()
      : Math.min(maxBars, Math.max(MIN_ZOOM_BARS, Math.round(zoomBars)));
  /** Bars actually on screen: the plot is drawn from this, so pointer maths
   * (and therefore dragging) must use it too, not the zoom intent. */
  const shownBars = Math.min(candles.length, visibleBars);

  // Zooming in can leave the scroll offset past the end of the data.
  useEffect(() => {
    setOffset((value) => Math.min(value, Math.max(0, candles.length - visibleBars)));
  }, [visibleBars, candles.length]);

  useEffect(() => {
    const wrap = wrapRef.current;
    const canvas = canvasRef.current;
    if (!wrap || !canvas) return;

    const paint = () =>
      draw(
        canvas,
        wrap,
        candles,
        timeframe,
        lastPrice,
        offset,
        visibleBars,
        hover,
        indicator,
        colors(),
      );
    paint();

    const observer = new ResizeObserver(paint);
    observer.observe(wrap);
    return () => observer.disconnect();
  }, [candles, timeframe, lastPrice, offset, visibleBars, hover, indicator, themeRevision]);

  const onPointerDown = (event: ReactPointerEvent<HTMLCanvasElement>) => {
    if (event.button !== 0) return;
    drag.current = { x: event.clientX, start: offset };
    event.currentTarget.setPointerCapture(event.pointerId);
  };

  const onPointerMove = (event: ReactPointerEvent<HTMLCanvasElement>) => {
    const rect = event.currentTarget.getBoundingClientRect();
    setHover({ x: event.clientX - rect.left, y: event.clientY - rect.top });
    if (!drag.current) return;
    // Match the plot width used by draw(): 8px left pad + 62px price axis.
    // Use the bars actually on screen, not the zoom intent: when the series is
    // shorter than the requested window, dragging would otherwise move several
    // bars per pixel.
    const step = Math.max(2, (rect.width - 86) / Math.max(1, shownBars));
    const shift = Math.round((event.clientX - drag.current.x) / step);
    const maxOffset = Math.max(0, candles.length - shownBars);
    const wanted = drag.current.start + shift;
    if (wanted > maxOffset && onLoadOlder) {
      // Dragging past the oldest bar: keep the view pinned and fetch the
      // previous window instead of letting the user pull against a wall.
      if (!olderLoading && Date.now() - olderAsk.current > 800) {
        olderAsk.current = Date.now();
        onLoadOlder();
      }
    }
    setOffset(Math.min(maxOffset, Math.max(0, wanted)));
  };

  const endDrag = (event: ReactPointerEvent<HTMLCanvasElement>) => {
    if (drag.current) event.currentTarget.releasePointerCapture(event.pointerId);
    drag.current = null;
  };

  /** Wheel zooms around the pointer. Registered natively below so the page
   * behind the chart does not scroll at the same time (React's onWheel is a
   * passive listener and cannot call preventDefault). */
  const onWheel = useCallback(
    (event: WheelEvent) => {
      if (!candles.length) return;
      event.preventDefault();
      // deltaMode 1 is lines (Firefox), 2 is pages; normalise both to pixels so
      // one notch means the same thing on every browser.
      const unit =
        event.deltaMode === 1
          ? event.deltaY * 16
          : event.deltaMode === 2
            ? event.deltaY * 400
            : event.deltaY;
      if (!unit) return;
      const rect = (event.currentTarget as HTMLElement).getBoundingClientRect();
      const plot = Math.max(1, rect.width - 86);
      const fraction = Math.min(1, Math.max(0, (event.clientX - rect.left - 8) / plot));
      // One notch (~100px) is a 1.6x change. Sub-notch trackpad deltas still
      // register (0.5 floor) so a slow scroll is not swallowed.
      const steps = Math.min(MAX_WHEEL_STEPS, Math.max(MIN_WHEEL_STEPS, Math.abs(unit) / 100));
      const factor = Math.pow(ZOOM_FACTOR, steps);
      const desired = Math.round(unit < 0 ? visibleBars / factor : visibleBars * factor);
      // Zooming out is capped by the data actually held (a wider window would
      // stretch a handful of bars across the plot) and by the comfort ceiling.
      const ceiling = Math.min(maxBars, MAX_ZOOM_BARS);
      const capped = Math.min(ceiling, Math.max(MIN_ZOOM_BARS, desired));

      // Zooming out past the loaded window keeps working: fetch the previous
      // page and remember the wider intent, so the chart expands as soon as
      // the history lands instead of stopping at the data edge. This tests the
      // *intent*, not the drawn width: the drawn width is capped by the data
      // already held, so capping first would make this never true.
      const wantsMore = unit > 0 && desired > candles.length;
      if (wantsMore && onLoadOlder && !olderLoading && Date.now() - olderAsk.current > 300) {
        olderAsk.current = Date.now();
        onLoadOlder();
      }

      const nextBars = Math.min(maxBars, capped);
      if (nextBars === visibleBars) {
        if (wantsMore) setZoomBars(capped);
        return;
      }

      const total = candles.length;
      const end = Math.max(visibleBars, total - offset);
      const start = Math.max(0, end - visibleBars);
      const anchor = start + fraction * Math.max(0, visibleBars - 1);
      let nextEnd = Math.round(anchor + (1 - fraction) * Math.max(0, nextBars - 1));
      nextEnd = Math.min(total, Math.max(nextBars, nextEnd));
      setZoomBars(nextBars);
      setOffset(Math.max(0, total - nextEnd));
    },
    [candles.length, visibleBars, offset, maxBars, onLoadOlder, olderLoading],
  );

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const handler = (event: WheelEvent) => onWheel(event);
    canvas.addEventListener("wheel", handler, { passive: false });
    return () => canvas.removeEventListener("wheel", handler);
  }, [onWheel]);

  const empty = candles.length === 0;
  const tail = candles[candles.length - 1];

  return (
    <div className="chart-wrap" ref={wrapRef}>
      <canvas
        ref={canvasRef}
        className="chart-canvas"
        data-bars={visibleBars}
        data-last-close={tail ? tail.c : ""}
        data-last-open={tail ? tail.t : ""}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
        onPointerLeave={() => {
          setHover(null);
          drag.current = null;
        }}
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
  visibleBars: number,
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

  const visible = Math.min(candles.length, Math.max(MIN_ZOOM_BARS, visibleBars));
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
  const referencePrice = lastPrice || slice[slice.length - 1]!.c;
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

  // Testnet candles carry deep wicks (a daily bar can span 30% around a body
  // of a few hundred USDT). Scaling to the full high/low range then squashes
  // every body into a one-pixel line on the longer timeframes, which reads as
  // "the candles got tiny". When the wicks dwarf the bodies, narrow the axis
  // to the bodies and clip the overhanging wicks at the plot edge.
  const bodyMax = Math.max(...slice.map((candle) => Math.max(candle.o, candle.c)));
  const bodyMin = Math.min(...slice.map((candle) => Math.min(candle.o, candle.c)));
  const bodyRange = bodyMax - bodyMin;
  if (bodyRange > 0 && max - min > bodyRange * SPIKE_RATIO) {
    const margin = Math.max(bodyRange * 0.35, (max - min) * 0.02);
    max = bodyMax + margin;
    min = bodyMin - margin;
    // Keep the live price and any visible BOLL band in frame even when they
    // sit outside the body range.
    if (referencePrice > max) max = referencePrice + margin;
    if (referencePrice < min) min = referencePrice - margin;
    for (let i = start; i < end; i += 1) {
      const upper = boll?.upper[i];
      const lower = boll?.lower[i];
      if (upper != null && upper > max) max = upper + margin;
      if (lower != null && lower < min) min = lower - margin;
    }
  }
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

  // Clip the candles to the price panel: after the wick squeeze above, a spike
  // would otherwise be drawn straight through the volume panel.
  ctx.save();
  ctx.beginPath();
  ctx.rect(left, padding.top, plotW, priceH + 1);
  ctx.clip();

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
  ctx.restore();

  const reference = referencePrice;
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
      shortTime(candle.t, timeframe),
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

    const timeText = shortTime(candle.t, timeframe);
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
