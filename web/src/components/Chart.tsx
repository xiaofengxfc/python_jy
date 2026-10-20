import { useCallback, useEffect, useRef, useState } from "react";
import type { PointerEvent as ReactPointerEvent } from "react";
import type { AILevel, Candle } from "../types";
import { fmtPrice, shortTime } from "../format";
import { cssVar, useThemeRevision } from "../hooks/useThemeRevision";
import type { Drawing, DrawingPoint, DrawingTool } from "./drawings";
import { drawingLabel, lineColor } from "./drawings";
import { newDrawingId } from "./drawingsStore";

/** Levels the AI named, drawn as labelled horizontal lines over the candles.
 * They are validated against the traded range on the backend, so anything that
 * arrives here belongs inside the visible price band. */
/** Indicators the user can switch on. Several may be active at once: overlays
 * draw on the price panel, oscillators each get a panel below it. */
export type Indicator =
  | "ma"
  | "ema"
  | "boll"
  | "macd"
  | "rsi"
  | "kdj"
  | "wr"
  | "cci"
  | "obv"
  | "atr"
  | "dmi";

export interface IndicatorParam {
  /** Stable key used in the stored config. */
  key: string;
  label: string;
  min: number;
  max: number;
  step: number;
  /** Value used the first time the indicator is switched on. */
  value: number;
}

export interface IndicatorSpec {
  label: string;
  kind: "main" | "sub";
  params: IndicatorParam[];
}

export type IndicatorParams = Record<string, number>;

/** One switched-on indicator with the user's parameters. */
export interface IndicatorConfig {
  name: Indicator;
  params: IndicatorParams;
}

export const INDICATOR_SPECS: Record<Indicator, IndicatorSpec> = {
  ma: {
    label: "MA",
    kind: "main",
    params: [
      { key: "fast", label: "快", min: 1, max: 400, step: 1, value: 7 },
      { key: "mid", label: "中", min: 1, max: 400, step: 1, value: 25 },
      { key: "slow", label: "慢", min: 1, max: 400, step: 1, value: 60 },
    ],
  },
  ema: {
    label: "EMA",
    kind: "main",
    params: [
      { key: "fast", label: "快", min: 1, max: 400, step: 1, value: 12 },
      { key: "slow", label: "慢", min: 1, max: 400, step: 1, value: 26 },
    ],
  },
  boll: {
    label: "BOLL",
    kind: "main",
    params: [
      { key: "period", label: "周期", min: 2, max: 400, step: 1, value: 20 },
      { key: "mult", label: "倍数", min: 0.5, max: 5, step: 0.1, value: 2 },
    ],
  },
  macd: {
    label: "MACD",
    kind: "sub",
    params: [
      { key: "fast", label: "快", min: 1, max: 200, step: 1, value: 12 },
      { key: "slow", label: "慢", min: 2, max: 400, step: 1, value: 26 },
      { key: "signal", label: "信号", min: 1, max: 100, step: 1, value: 9 },
    ],
  },
  rsi: {
    label: "RSI",
    kind: "sub",
    params: [{ key: "period", label: "周期", min: 2, max: 200, step: 1, value: 14 }],
  },
  kdj: {
    label: "KDJ",
    kind: "sub",
    params: [
      { key: "period", label: "周期", min: 2, max: 200, step: 1, value: 9 },
      { key: "smooth", label: "平滑", min: 1, max: 20, step: 1, value: 3 },
    ],
  },
  wr: {
    label: "WR",
    kind: "sub",
    params: [{ key: "period", label: "周期", min: 2, max: 200, step: 1, value: 14 }],
  },
  cci: {
    label: "CCI",
    kind: "sub",
    params: [{ key: "period", label: "周期", min: 2, max: 200, step: 1, value: 20 }],
  },
  obv: {
    label: "OBV",
    kind: "sub",
    params: [{ key: "period", label: "均线", min: 2, max: 200, step: 1, value: 30 }],
  },
  atr: {
    label: "ATR",
    kind: "sub",
    params: [{ key: "period", label: "周期", min: 2, max: 200, step: 1, value: 14 }],
  },
  dmi: {
    label: "DMI",
    kind: "sub",
    params: [
      { key: "period", label: "周期", min: 2, max: 200, step: 1, value: 14 },
      { key: "adx", label: "ADX", min: 2, max: 200, step: 1, value: 14 },
    ],
  },
};

export const MAIN_INDICATORS: Indicator[] = ["ma", "ema", "boll"];
export const SUB_INDICATORS: Indicator[] = ["macd", "rsi", "kdj", "wr", "cci", "obv", "atr", "dmi"];

/** Default parameters for one indicator, as a fresh object. */
export function defaultParams(name: Indicator): IndicatorParams {
  const out: IndicatorParams = {};
  INDICATOR_SPECS[name].params.forEach((param) => {
    out[param.key] = param.value;
  });
  return out;
}

/** Clamp a user-typed value into the parameter's allowed range. */
export function clampParam(name: Indicator, key: string, value: number): number {
  const spec = INDICATOR_SPECS[name].params.find((param) => param.key === key);
  if (!spec || !Number.isFinite(value)) return spec ? spec.value : value;
  return Math.min(spec.max, Math.max(spec.min, value));
}

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
  /** Every indicator the user switched on, with its parameters; empty means a
   * bare chart. */
  indicators: IndicatorConfig[];
  /** Asks for the window before the oldest candle. Called once the view is
   * scrolled all the way to the left edge. */
  onLoadOlder?: () => void;
  /** A history request is in flight; shown as a hint and used to rate-limit. */
  olderLoading?: boolean;
  /** Support/resistance/stop lines the AI proposed for this symbol. Kept for
   * backwards compatibility; the trading page now feeds those same levels in
   * through ``drawings`` so they can be edited like any manual line. */
  aiLevels?: AILevel[];
  /** The trader's drawings plus AI levels converted to editable lines. */
  drawings?: Drawing[];
  /** Replaces the whole list; the parent owns persistence, so every drag goes
   * through this callback instead of mutating local state. */
  onDrawingsChange?: (next: Drawing[]) => void;
  /** Active tool. ``select`` is pointer/pan mode. */
  drawingTool?: DrawingTool;
  selectedId?: string | null;
  onSelect?: (id: string | null) => void;
  /** Called once a line has been placed, so the parent can drop back to select. */
  onToolDone?: () => void;
  /** Without this, drawings are hidden but never deleted (e.g. AI lines). */
  drawingsVisible?: boolean;
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
    ema12: cssVar("--accent", "#2563eb"),
    ema26: cssVar("--down", "#e5484d"),
    boll: cssVar("--text-2", "#5b5b5b"),
    signal: cssVar("--accent", "#2563eb"),
    macd: cssVar("--text-2", "#5b5b5b"),
    kdjK: cssVar("--accent", "#2563eb"),
    kdjD: cssVar("--warn", "#b45309"),
    kdjJ: cssVar("--down", "#e5484d"),
    hover: cssVar("--bg-hover", "#f4f4f4"),
    // AI levels get their own hues so they never read as one of the indicator
    // lines: support sits with the up colour, resistance with the down one.
    aiSupport: cssVar("--up", "#16a34a"),
    aiResistance: cssVar("--down", "#e11d48"),
    aiStop: cssVar("--warn", "#b45309"),
  };
}

const MIN_BARS = 40;
/** Hard floor for zooming in: a handful of candles fill the plot. */
const MIN_ZOOM_BARS = 3;
/** Ceiling for zooming out. The chart asks for more history once the oldest
 * loaded bar comes into view, so this is a comfort limit, not a data limit. */
const MAX_ZOOM_BARS = 1000;
/** One wheel notch (deltaY ~100) changes the visible range by this factor.
 * 1.6 felt sluggish — reaching a useful zoom-out took many flicks — so a
 * notch now roughly doubles or halves the window, like Binance's chart. */
const ZOOM_FACTOR = 2.0;
/** Narrowest a single candle may become. Zooming out stops here: below ~4px
 * per bar the series collapses into a dark smear and stops conveying shape. */
const MIN_BAR_PX = 4;
/** Below this spacing the candles are drawn edge to edge and snapped to whole
 * pixels. That is what Binance does when a series is compressed: a continuous
 * band, not a moiré of 2px bodies separated by sub-pixel gaps. */
const DENSE_STEP = 5;
/** Widest gap between two candles, matching the spacing Binance uses at full
 * zoom-in. Without it a handful of bars stretch into solid blocks. */
const MAX_BAR_STEP = 22;
/** Cap on the coloured body itself. */
const MAX_BAR_BODY = 15;
/** How far one wheel event may travel: a fast flick must still land somewhere
 * sensible, but too low a cap makes trackpads feel unresponsive. */
const MAX_WHEEL_STEPS = 6;
const MIN_WHEEL_STEPS = 0.5;
/** Wicks are only clipped once the full high/low range is this many times
 * wider than the range of candle bodies. Ordinary markets routinely run 2–3×
 * (one doji among a tight cluster of bodies), so the threshold sits well above
 * that: real data keeps its true scale, and only a simulated or corrupted
 * series (where a single bar spans 30% around a body of a few hundred USDT)
 * gets its axis narrowed. */
const SPIKE_RATIO = 4;

/** Geometry of the price panel, captured by ``draw()`` so pointer maths uses
 * the exact numbers the candles were painted with (and never its own copy). */
interface ChartGeometry {
  left: number;
  right: number;
  top: number;
  priceH: number;
  min: number;
  max: number;
  step: number;
  plotX: number;
  /** Index of the first candle in the slice and how many are drawn. */
  start: number;
  barCount: number;
}

/** Index of the candle whose open time is closest to ``t``. Binary search:
 * drawings can be hours or weeks away from the visible window. */
function indexOfTime(candles: Candle[], t: number): number {
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

/** Candle open time ``delta`` bars away from ``t``, clamped to the series. A
 * trend line dragged sideways keeps its shape instead of drifting in price. */
function shiftTime(candles: Candle[], t: number, delta: number): number {
  if (!candles.length) return t;
  const index = indexOfTime(candles, t) + delta;
  const clamped = Math.min(candles.length - 1, Math.max(0, index));
  return candles[clamped]!.t;
}

/** Shortest distance from a point to a line segment, in pixels. */
function distanceToSegment(
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

export default function Chart({
  candles,
  seriesKey,
  dataKey,
  timeframe,
  lastPrice,
  loading,
  error,
  indicators,
  onLoadOlder,
  olderLoading = false,
  aiLevels = [],
  drawings = [],
  onDrawingsChange,
  drawingTool = "select",
  selectedId = null,
  onSelect,
  onToolDone,
  drawingsVisible = true,
}: Props) {
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const themeRevision = useThemeRevision();
  const [offset, setOffset] = useState(0);
  // Width of the drawing area in CSS pixels, tracked so the zoom-out limit can
  // scale with the window instead of a fixed bar count.
  const [plotWidth, setPlotWidth] = useState(0);
  // null means "as many bars as the width comfortably fits"; a number is the
  // user's zoom level. Kept across quote updates so zooming does not reset.
  const [zoomBars, setZoomBars] = useState<number | null>(null);
  const [hover, setHover] = useState<{ x: number; y: number } | null>(null);
  const drag = useRef<{ x: number; start: number } | null>(null);
  /** Geometry of the last paint; pointer maths must match what was drawn. */
  const geomRef = useRef<ChartGeometry | null>(null);
  /** The rubber-band line while a trend line is being dragged out. */
  const [preview, setPreview] = useState<{ a: DrawingPoint; b: DrawingPoint } | null>(null);
  const trendDraft = useRef<{ a: DrawingPoint; startX: number; startY: number } | null>(null);
  const lineDrag = useRef<
    | { mode: "move-hline"; id: string; startY: number; startPrice: number }
    | {
        mode: "move-trend";
        id: string;
        startX: number;
        startY: number;
        a: DrawingPoint;
        b: DrawingPoint;
      }
    | { mode: "move-endpoint"; id: string; which: "a" | "b" }
    | null
  >(null);
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

  // Zooming out stops while bars are still readable: at ~3px each a series
  // turns into one dark smear. The limit follows the window, so a wide chart
  // can show more history than a narrow one. 86px is the same gutter draw()
  // uses (16px wrap padding + 8px left pad + 62px price axis).
  const zoomCeiling = Math.max(
    MIN_ZOOM_BARS,
    plotWidth > 0 ? Math.floor((plotWidth - 86) / MIN_BAR_PX) : MAX_ZOOM_BARS,
  );

  const maxBars = Math.max(candles.length, MIN_ZOOM_BARS);
  const fitBars = zoomBars === null ? autoBars() : Math.round(zoomBars);
  const visibleBars = Math.min(maxBars, zoomCeiling, Math.max(MIN_ZOOM_BARS, fitBars));
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

    const paint = () => {
      // Keep the zoom-out limit in step with the panel width.
      const measured = Math.floor(wrap.getBoundingClientRect().width);
      setPlotWidth((current) => (current === measured ? current : measured));
      geomRef.current = draw(
        canvas,
        wrap,
        candles,
        timeframe,
        lastPrice,
        offset,
        visibleBars,
        hover,
        indicators,
        colors(),
        aiLevels,
        drawings,
        selectedId,
        preview,
        drawingsVisible,
      );
    };
    paint();

    const observer = new ResizeObserver(paint);
    observer.observe(wrap);
    return () => observer.disconnect();
  }, [
    candles,
    timeframe,
    lastPrice,
    offset,
    visibleBars,
    hover,
    indicators,
    aiLevels,
    drawings,
    selectedId,
    preview,
    drawingsVisible,
    themeRevision,
  ]);

  /** Canvas-local pointer position (draw() measures from the canvas box). */
  const localPoint = (event: ReactPointerEvent<HTMLCanvasElement>) => {
    const rect = event.currentTarget.getBoundingClientRect();
    return { x: event.clientX - rect.left, y: event.clientY - rect.top };
  };

  /** Keep receiving moves after the pointer leaves the canvas. Synthetic
   * pointers (automation, some touch emulation) have no capture target, so a
   * failed capture must not abort the drag. */
  const capture = (event: ReactPointerEvent<HTMLCanvasElement>) => {
    try {
      event.currentTarget.setPointerCapture(event.pointerId);
    } catch {
      // Nothing to capture; the drag still works while the pointer stays put.
    }
  };

  /** Screen y → price, null outside the price panel. */
  const priceAt = (geom: ChartGeometry, y: number): number | null => {
    const ratio = (y - geom.top) / geom.priceH;
    if (!Number.isFinite(ratio) || ratio < 0 || ratio > 1) return null;
    return geom.max - ratio * (geom.max - geom.min);
  };

  /** Screen x → nearest visible candle open time, null when there is no data. */
  const barAt = (geom: ChartGeometry, x: number): number | null => {
    const raw = Math.round((x - geom.plotX) / Math.max(0.0001, geom.step) - 0.5);
    const local = Math.min(geom.barCount - 1, Math.max(0, raw));
    return candles[geom.start + local]?.t ?? null;
  };

  const yOfPrice = (geom: ChartGeometry, price: number): number =>
    geom.top + geom.priceH * (1 - (price - geom.min) / (geom.max - geom.min || 1));

  const xOfTime = (geom: ChartGeometry, t: number): number =>
    geom.plotX + (indexOfTime(candles, t) - geom.start + 0.5) * geom.step;

  /** Topmost drawing within the grab tolerance of a canvas point. Hidden lines
   * are not hit-testable: the user cannot grab what they cannot see. */
  const hitTest = (x: number, y: number): Drawing | null => {
    const geom = geomRef.current;
    if (!geom || !drawingsVisible) return null;
    for (let index = drawings.length - 1; index >= 0; index -= 1) {
      const item = drawings[index]!;
      if (item.kind === "hline") {
        const lineY = yOfPrice(geom, item.price);
        if (
          lineY >= geom.top - 6 &&
          lineY <= geom.top + geom.priceH + 6 &&
          Math.abs(y - lineY) <= 6
        ) {
          return item;
        }
        continue;
      }
      const ax = xOfTime(geom, item.a.t);
      const ay = yOfPrice(geom, item.a.price);
      const bx = xOfTime(geom, item.b.t);
      const by = yOfPrice(geom, item.b.price);
      if (Math.hypot(x - ax, y - ay) <= 9 || Math.hypot(x - bx, y - by) <= 9) return item;
      if (distanceToSegment(x, y, ax, ay, bx, by) <= 6) return item;
    }
    return null;
  };

  const onPointerDown = (event: ReactPointerEvent<HTMLCanvasElement>) => {
    if (event.button !== 0) return;
    const geom = geomRef.current;
    if (!geom) return;
    const { x, y } = localPoint(event);

    if (drawingTool === "hline") {
      // Only the price panel places lines: a click on the price axis or the
      // time strip must not create a level at a price nobody chose.
      if (x < geom.left || x > geom.right) return;
      const price = priceAt(geom, y);
      if (price == null || !onDrawingsChange) return;
      const item: Drawing = {
        id: newDrawingId("hline"),
        kind: "hline",
        price,
        source: "manual",
      };
      onDrawingsChange([...drawings, item]);
      onSelect?.(item.id);
      onToolDone?.();
      return;
    }

    if (drawingTool === "trend") {
      if (x < geom.left || x > geom.right) return;
      const t = barAt(geom, x);
      const price = priceAt(geom, y);
      if (t == null || price == null) return;
      // Remember where the pointer actually went down: the "did the user drag
      // at all?" test must use that point, not the snapped bar centre (which
      // can be up to half a bar away and would turn a plain click into a
      // zero-length trend line).
      trendDraft.current = { a: { t, price }, startX: x, startY: y };
      setPreview({ a: { t, price }, b: { t, price } });
      capture(event);
      return;
    }

    // Select mode: editing an existing line must win over panning the chart.
    if (selectedId && onDrawingsChange) {
      const selected = drawings.find((item) => item.id === selectedId);
      if (selected?.kind === "hline" && Math.abs(y - yOfPrice(geom, selected.price)) <= 6) {
        lineDrag.current = {
          mode: "move-hline",
          id: selected.id,
          startY: y,
          startPrice: selected.price,
        };
        capture(event);
        return;
      }
      if (selected?.kind === "trend") {
        const ax = xOfTime(geom, selected.a.t);
        const ay = yOfPrice(geom, selected.a.price);
        const bx = xOfTime(geom, selected.b.t);
        const by = yOfPrice(geom, selected.b.price);
        const nearA = Math.hypot(x - ax, y - ay);
        const nearB = Math.hypot(x - bx, y - by);
        if (Math.min(nearA, nearB) <= 9) {
          lineDrag.current = {
            mode: "move-endpoint",
            id: selected.id,
            which: nearA <= nearB ? "a" : "b",
          };
          capture(event);
          return;
        }
        if (distanceToSegment(x, y, ax, ay, bx, by) <= 6) {
          lineDrag.current = {
            mode: "move-trend",
            id: selected.id,
            startX: x,
            startY: y,
            a: { ...selected.a },
            b: { ...selected.b },
          };
          capture(event);
          return;
        }
      }
    }

    const hit = hitTest(x, y);
    if (hit && onDrawingsChange) {
      onSelect?.(hit.id);
      if (hit.kind === "hline") {
        lineDrag.current = {
          mode: "move-hline",
          id: hit.id,
          startY: y,
          startPrice: hit.price,
        };
      } else {
        lineDrag.current = {
          mode: "move-trend",
          id: hit.id,
          startX: x,
          startY: y,
          a: { ...hit.a },
          b: { ...hit.b },
        };
      }
      capture(event);
      return;
    }

    onSelect?.(null);
    drag.current = { x: event.clientX, start: offset };
    capture(event);
  };

  const onPointerMove = (event: ReactPointerEvent<HTMLCanvasElement>) => {
    const rect = event.currentTarget.getBoundingClientRect();
    const x = event.clientX - rect.left;
    const y = event.clientY - rect.top;
    setHover({ x, y });
    const geom = geomRef.current;
    if (!geom) return;

    // A pointer whose buttons were released outside the canvas must not keep
    // dragging: without this the line follows the cursor until the next click.
    if (event.buttons === 0 && (drag.current || lineDrag.current || trendDraft.current)) {
      abortDrag(event);
      return;
    }

    if (trendDraft.current) {
      const t = barAt(geom, x);
      const price = priceAt(geom, y);
      if (t != null && price != null) {
        setPreview({ a: trendDraft.current.a, b: { t, price } });
      }
      return;
    }

    const state = lineDrag.current;
    if (state && onDrawingsChange) {
      const pricePerPixel = (geom.max - geom.min) / Math.max(1, geom.priceH);
      if (state.mode === "move-hline") {
        const price = Math.max(0, state.startPrice + (state.startY - y) * pricePerPixel);
        onDrawingsChange(
          drawings.map((item) =>
            item.id === state.id && item.kind === "hline" ? { ...item, price } : item,
          ),
        );
      } else if (state.mode === "move-trend") {
        const deltaBars = Math.round((x - state.startX) / Math.max(0.0001, geom.step));
        const deltaPrice = (state.startY - y) * pricePerPixel;
        onDrawingsChange(
          drawings.map((item) => {
            if (item.id !== state.id || item.kind !== "trend") return item;
            return {
              ...item,
              a: {
                t: shiftTime(candles, state.a.t, deltaBars),
                price: Math.max(0, state.a.price + deltaPrice),
              },
              b: {
                t: shiftTime(candles, state.b.t, deltaBars),
                price: Math.max(0, state.b.price + deltaPrice),
              },
            };
          }),
        );
      } else {
        const t = barAt(geom, x);
        const price = priceAt(geom, y);
        if (t != null && price != null) {
          onDrawingsChange(
            drawings.map((item) =>
              item.id === state.id && item.kind === "trend"
                ? { ...item, [state.which]: { t, price: Math.max(0, price) } }
                : item,
            ),
          );
        }
      }
      return;
    }

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
    if (trendDraft.current) {
      const geom = geomRef.current;
      const rect = event.currentTarget.getBoundingClientRect();
      const x = event.clientX - rect.left;
      const y = event.clientY - rect.top;
      const draft = trendDraft.current;
      const start = draft.a;
      trendDraft.current = null;
      setPreview(null);
      // A plain click (no drag) must not leave a zero-length line behind. The
      // distance is measured from where the pointer went down, not from the
      // snapped bar centre: at full zoom those differ by up to 11px.
      const moved = Math.hypot(x - draft.startX, y - draft.startY);
      if (geom && onDrawingsChange && moved >= 8) {
        const t = barAt(geom, x);
        const price = priceAt(geom, y);
        if (t != null && price != null) {
          const item: Drawing = {
            id: newDrawingId("trend"),
            kind: "trend",
            a: start,
            b: { t, price },
            source: "manual",
          };
          onDrawingsChange([...drawings, item]);
          onSelect?.(item.id);
          onToolDone?.();
        }
      }
    }
    lineDrag.current = null;
    if (drag.current || event.currentTarget.hasPointerCapture(event.pointerId)) {
      try {
        event.currentTarget.releasePointerCapture(event.pointerId);
      } catch {
        // Pointer capture can already be gone after a cancel; nothing to do.
      }
    }
    drag.current = null;
  };

  /** Abandon an in-flight gesture without committing anything. A cancelled
   * pointer (browser gesture, OS touch handling) is not a user click. */
  const abortDrag = (event: ReactPointerEvent<HTMLCanvasElement>) => {
    trendDraft.current = null;
    setPreview(null);
    lineDrag.current = null;
    drag.current = null;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      try {
        event.currentTarget.releasePointerCapture(event.pointerId);
      } catch {
        // Already released; nothing to do.
      }
    }
  };

  // Delete removes the selected line; Escape abandons a half-drawn trend line,
  // the selection, and an armed drawing tool. Ignored while an input has focus
  // so typing a price is never mistaken for a chart shortcut.
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      const tag = target?.tagName?.toLowerCase() ?? "";
      if (tag === "input" || tag === "textarea" || tag === "select" || target?.isContentEditable) {
        return;
      }
      if ((event.key === "Delete" || event.key === "Backspace") && selectedId) {
        event.preventDefault();
        // Filter the *full* list: when drawings are hidden the render list is
        // empty, and filtering that would wipe every line instead of one.
        onDrawingsChange?.(drawings.filter((item) => item.id !== selectedId));
        onSelect?.(null);
      } else if (event.key === "Escape") {
        trendDraft.current = null;
        setPreview(null);
        lineDrag.current = null;
        onSelect?.(null);
        if (drawingTool !== "select") onToolDone?.();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [drawings, selectedId, onDrawingsChange, onSelect, drawingTool, onToolDone]);

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
      // stretch a handful of bars across the plot) and by the readable-width
      // ceiling, so the series never collapses into a single line.
      const ceiling = Math.min(maxBars, zoomCeiling);
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
    [candles.length, visibleBars, offset, maxBars, zoomCeiling, onLoadOlder, olderLoading],
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
        onPointerCancel={abortDrag}
        onPointerLeave={(event) => {
          setHover(null);
          // While a pointer is captured the canvas still receives the moves;
          // clearing state on a captured leave would silently drop the drag.
          if (event.currentTarget.hasPointerCapture(event.pointerId)) return;
          drag.current = null;
          lineDrag.current = null;
          if (trendDraft.current) {
            trendDraft.current = null;
            setPreview(null);
          }
        }}
      />
      {!empty && (
        <div className="chart-legend">
          {indicators.map((config) => (
            <span className="legend-item" key={config.name}>
              {legendText(config)}
            </span>
          ))}
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
  indicators: IndicatorConfig[],
  COLORS: ReturnType<typeof colors>,
  aiLevels: AILevel[] = [],
  drawings: Drawing[] = [],
  selectedId: string | null = null,
  preview: { a: DrawingPoint; b: DrawingPoint } | null = null,
  drawingsVisible = true,
): ChartGeometry | null {
  const rect = wrap.getBoundingClientRect();
  // Never inflate the canvas past its container: a hard 240px floor made the
  // canvas wider than the chart panel on narrow windows, so it overflowed and
  // was clipped by the card. Measure what we actually have — even a short
  // panel gets a short canvas that fits, instead of a 160px one that is cut.
  const width = Math.max(140, Math.floor(rect.width) - 16);
  const height = Math.max(80, Math.floor(rect.height) - 8);
  const dpr = window.devicePixelRatio || 1;

  canvas.width = width * dpr;
  canvas.height = height * dpr;
  canvas.style.width = `${width}px`;
  canvas.style.height = `${height}px`;

  const ctx = canvas.getContext("2d");
  if (!ctx) return null;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);
  if (!candles.length) return null;

  const visible = Math.min(candles.length, Math.max(MIN_ZOOM_BARS, visibleBars));
  const end = Math.max(visible, candles.length - offset);
  const start = Math.max(0, end - visible);
  const slice = candles.slice(start, end);

  const padding = { top: 12, right: 62, bottom: 22, left: 8 };
  const active = indicators;
  /** Parameters of one switched-on indicator, or null when it is off. */
  const cfg = (name: Indicator): IndicatorParams | null =>
    active.find((item) => item.name === name)?.params ?? null;
  // Several panels can be on at once, so give each an equal slice of the
  // height. When that would squeeze the candles too far, shrink the panels
  // rather than the price chart: the bars are what the trader is reading.
  const subs = active.filter((item) => INDICATOR_SPECS[item.name].kind === "sub");
  const SUB_GAP = 10;
  const MIN_PRICE_H = 140;
  const SUB_WANT_H = 62;
  let subH = subs.length ? SUB_WANT_H : 0;
  if (subs.length) {
    const room = height - padding.top - padding.bottom - 10;
    const needed = subs.length * (subH + SUB_GAP);
    if (room - needed < MIN_PRICE_H) {
      subH = Math.max(34, Math.floor((room - MIN_PRICE_H) / subs.length) - SUB_GAP);
    }
  }
  const subTotal = subs.length * (subH + SUB_GAP);
  const volumeH = Math.round((height - padding.top - padding.bottom - subTotal) * 0.16);
  const priceH = Math.max(
    60,
    height - padding.top - padding.bottom - volumeH - 10 - subTotal,
  );
  const left = padding.left;
  const right = width - padding.right;
  const plotW = right - left;

  const highs = slice.map((candle) => candle.h);
  const lows = slice.map((candle) => candle.l);
  let max = Math.max(...highs);
  let min = Math.min(...lows);

  const closes = candles.map((candle) => candle.c);
  const referencePrice = lastPrice || slice[slice.length - 1]!.c;
  const bollCfg = cfg("boll");
  const boll = bollCfg ? bands(closes, bollCfg.period ?? 20, bollCfg.mult ?? 2) : null;
  const emaCfg = cfg("ema");
  const emaFast = emaCfg ? ema(closes, emaCfg.fast ?? 12) : null;
  const emaSlow = emaCfg ? ema(closes, emaCfg.slow ?? 26) : null;
  if (boll) {
    for (let i = start; i < end; i += 1) {
      const upper = boll.upper[i];
      const lower = boll.lower[i];
      if (upper != null) max = Math.max(max, upper);
      if (lower != null) min = Math.min(min, lower);
    }
  }
  // EMA values sit inside the candle range almost always, but include them
  // whenever visible so a fast trend cannot clip its own line at the edge.
  for (const series of [emaFast, emaSlow]) {
    if (!series) continue;
    for (let i = start; i < end; i += 1) {
      const value = series[i];
      if (value == null) continue;
      max = Math.max(max, value);
      min = Math.min(min, value);
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

  // Binance/TradingView geometry: bar spacing has a ceiling, so a short
  // history (three weekly bars on a freshly listed symbol) keeps a normal
  // candle size instead of being blown up into blocks. Like TradingView, the
  // series is right-anchored: the live bar sits next to the price axis and the
  // empty space falls on the left, which is where older history will load.
  const step = Math.min(plotW / slice.length, MAX_BAR_STEP);
  const plotX = right - step * slice.length;
  // Zoomed far out, the gap is dropped and each bar starts on a whole pixel:
  // a 0.8px gap at 2px bodies is what made the compressed chart look dirty.
  const dense = step < DENSE_STEP;
  const bodyW = dense
    ? Math.max(1, Math.round(step))
    : Math.max(1, Math.min(step * 0.72, MAX_BAR_BODY));
  const bodyLeft = (center: number) =>
    dense ? Math.round(center) - Math.floor(bodyW / 2) : center - bodyW / 2;

  // Everything pointer maths needs, captured from this exact paint. Drawings
  // and pan/drag hit-testing read it instead of recomputing the scale, so a
  // line can never be drawn on one price and grabbed on another.
  const geometry: ChartGeometry = {
    left,
    right,
    top: padding.top,
    priceH,
    min,
    max,
    step,
    plotX,
    start,
    barCount: slice.length,
  };

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
    const x = plotX + index * step + step / 2;
    const barH = (candle.v / volMax) * volumeH;
    ctx.fillStyle = candle.c >= candle.o ? COLORS.upVolume : COLORS.downVolume;
    ctx.fillRect(bodyLeft(x), volTop + volumeH - barH, bodyW, barH);
  });

  // Clip the candles to the price panel: after the wick squeeze above, a spike
  // would otherwise be drawn straight through the volume panel.
  ctx.save();
  ctx.beginPath();
  ctx.rect(left, padding.top, plotW, priceH + 1);
  ctx.clip();

  slice.forEach((candle, index) => {
    const x = plotX + index * step + step / 2;
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
    ctx.fillRect(
      bodyLeft(x),
      Math.min(openY, closeY),
      bodyW,
      Math.max(1, Math.abs(closeY - openY)),
    );
  });

  const maCfg = cfg("ma");
  if (maCfg) {
    drawMA(ctx, slice, step, plotX, yOf, maCfg.fast ?? 7, COLORS.ma7);
    drawMA(ctx, slice, step, plotX, yOf, maCfg.mid ?? 25, COLORS.ma25);
    drawMA(ctx, slice, step, plotX, yOf, maCfg.slow ?? 60, COLORS.ema26);
  }
  if (emaFast && emaSlow) {
    drawLine(ctx, emaFast, start, end, slice.length, step, plotX, yOf, COLORS.ema12, 1.2);
    drawLine(ctx, emaSlow, start, end, slice.length, step, plotX, yOf, COLORS.ema26, 1.2);
  }

  if (boll) {
    drawLine(ctx, boll.upper, start, end, slice.length, step, plotX, yOf, COLORS.boll, 1);
    drawLine(ctx, boll.middle, start, end, slice.length, step, plotX, yOf, COLORS.boll, 1, [3, 3]);
    drawLine(ctx, boll.lower, start, end, slice.length, step, plotX, yOf, COLORS.boll, 1);
  }
  ctx.restore();

  // The AI's levels sit on top of everything in the price panel: they are the
  // conclusion the trader is reading, so a candle body must never hide one.
  drawAILevels(ctx, aiLevels, yOf, {
    left,
    right,
    min,
    max,
    top: padding.top,
    bottom: padding.top + priceH,
    colors: COLORS,
  });

  // The trader's own lines (and the AI levels once converted) go above the
  // built-in indicators: they are annotations, not data.
  if (drawingsVisible && drawings.length) {
    drawDrawings(ctx, drawings, candles, geometry, COLORS, selectedId);
  }
  if (drawingsVisible && preview) {
    drawTrendPreview(ctx, preview, candles, geometry, COLORS);
  }

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
  // The axis tick loop leaves textAlign centred; the tag starts at its box's
  // left edge, so set it explicitly instead of inheriting a stale alignment.
  ctx.textAlign = "left";
  ctx.fillText(tag, right + 9, refY);

  ctx.fillStyle = COLORS.axis;
  ctx.textAlign = "center";
  const ticks = Math.min(5, slice.length);
  for (let i = 0; i < ticks; i += 1) {
    const index = Math.floor((slice.length - 1) * (i / Math.max(1, ticks - 1)));
    const candle = slice[index]!;
    const x = plotX + index * step + step / 2;
    ctx.fillText(
      shortTime(candle.t, timeframe),
      Math.min(right - 20, Math.max(left + 20, x)),
      height - 8,
    );
  }

  // Each active sub-indicator gets its own panel, stacked downwards.
  subs.forEach((config, index) => {
    drawSubChart(ctx, {
      indicator: config.name as SubIndicator,
      params: config.params,
      candles,
      start,
      end,
      top: padding.top + priceH + 10 + volumeH + SUB_GAP + index * (subH + SUB_GAP),
      height: subH,
      left: plotX,
      right,
      step,
      COLORS,
    });
  });

  // Crosshair is drawn last so it sits above every indicator.
  if (hover && hover.x >= left && hover.x <= right && hover.y >= padding.top && hover.y <= volTop + volumeH) {
    const index = Math.min(
      slice.length - 1,
      Math.max(0, Math.floor((hover.x - plotX) / step)),
    );
    const candle = slice[index]!;
    const snapX = plotX + index * step + step / 2;

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

  return geometry;
}

function bands(values: number[], period: number, mult = 2) {
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
    upper[i] = mean + sd * mult;
    lower[i] = mean - sd * mult;
  }
  return { middle, upper, lower };
}

function drawAILevels(
  ctx: CanvasRenderingContext2D,
  levels: AILevel[],
  yOf: (value: number) => number,
  box: {
    left: number;
    right: number;
    min: number;
    max: number;
    top: number;
    bottom: number;
    colors: ReturnType<typeof colors>;
  },
) {
  if (!levels.length) return;
  const colour = (kind: AILevel["kind"]) =>
    kind === "support"
      ? box.colors.aiSupport
      : kind === "resistance"
        ? box.colors.aiResistance
        : box.colors.aiStop;

  // Labels are only drawn when they do not collide: three supports stacked in a
  // 20px band would otherwise overprint each other into a black smear.
  const placed: number[] = [];
  for (const level of levels) {
    // Off-screen levels are skipped rather than clamped to the edge: a line
    // pinned to the border would read as a real level at that price.
    if (level.price < box.min || level.price > box.max) continue;
    const y = Math.round(yOf(level.price)) + 0.5;
    if (y < box.top || y > box.bottom) continue;

    ctx.strokeStyle = colour(level.kind);
    ctx.lineWidth = 1;
    ctx.setLineDash(level.kind === "stop" ? [5, 3] : [2, 4]);
    ctx.beginPath();
    ctx.moveTo(box.left, y);
    ctx.lineTo(box.right, y);
    ctx.stroke();
    ctx.setLineDash([]);

    if (placed.some((used) => Math.abs(used - y) < 13)) continue;
    placed.push(y);

    const text = level.label
      ? `${level.label} ${fmtPrice(level.price)}`
      : fmtPrice(level.price);
    ctx.font = "10px Inter, sans-serif";
    const textW = ctx.measureText(text).width + 8;
    ctx.fillStyle = colour(level.kind);
    ctx.globalAlpha = 0.92;
    ctx.fillRect(box.left + 2, y - 8, Math.min(textW, box.right - box.left - 4), 14);
    ctx.globalAlpha = 1;
    ctx.fillStyle = box.colors.tagText;
    ctx.textAlign = "left";
    ctx.fillText(text, box.left + 6, y + 2);
  }
  ctx.textAlign = "center";
}

/** Draw one drawing: a horizontal level or a two-point trend line.
 *
 * Everything is clipped to the price panel. A support drawn for a price that is
 * currently off-screen simply disappears — clamping it to the border would
 * advertise a level at the wrong price. */
function drawDrawings(
  ctx: CanvasRenderingContext2D,
  drawings: Drawing[],
  candles: Candle[],
  geom: ChartGeometry,
  COLORS: ReturnType<typeof colors>,
  selectedId: string | null,
) {
  const yOf = (price: number) =>
    geom.top + geom.priceH * (1 - (price - geom.min) / (geom.max - geom.min || 1));
  const xOf = (t: number) =>
    geom.plotX + (indexOfTime(candles, t) - geom.start + 0.5) * geom.step;

  ctx.save();
  ctx.beginPath();
  ctx.rect(geom.left, geom.top, geom.right - geom.left, geom.priceH);
  ctx.clip();
  ctx.font = "10px Inter, sans-serif";
  ctx.textBaseline = "middle";
  ctx.lineJoin = "round";
  // Two levels a few points apart would print their price chips on top of each
  // other; the line still shows, only the label is skipped.
  const chipRows: number[] = [];

  for (const item of drawings) {
    const selected = item.id === selectedId;
    const colour = lineColor(item);
    ctx.strokeStyle = colour;
    ctx.lineWidth = selected ? 2.2 : 1.4;

    if (item.kind === "hline") {
      const y = yOf(item.price);
      if (y < geom.top - 2 || y > geom.top + geom.priceH + 2) continue;
      ctx.setLineDash([7, 4]);
      ctx.beginPath();
      ctx.moveTo(geom.left, y);
      ctx.lineTo(geom.right, y);
      ctx.stroke();
      ctx.setLineDash([]);

      const text = drawingLabel(item)
        ? `${drawingLabel(item)} ${fmtPrice(item.price)}`
        : fmtPrice(item.price);
      const chipW = Math.min(ctx.measureText(text).width + 8, geom.right - geom.left - 4);
      const crowded = chipRows.some((used) => Math.abs(used - y) < 13);
      if (!crowded) {
        chipRows.push(y);
        ctx.fillStyle = colour;
        ctx.globalAlpha = 0.92;
        ctx.fillRect(geom.left + 2, y - 8, chipW, 14);
        ctx.globalAlpha = 1;
        ctx.fillStyle = COLORS.tagText;
        ctx.textAlign = "left";
        ctx.fillText(text, geom.left + 6, y + 2);
      }
      if (selected) {
        ctx.fillStyle = COLORS.tagText;
        ctx.beginPath();
        ctx.arc(geom.right - 5, y, 4.5, 0, Math.PI * 2);
        ctx.fill();
        ctx.strokeStyle = colour;
        ctx.lineWidth = 2;
        ctx.stroke();
      }
      continue;
    }

    const ax = xOf(item.a.t);
    const ay = yOf(item.a.price);
    const bx = xOf(item.b.t);
    const by = yOf(item.b.price);
    ctx.setLineDash([]);
    ctx.beginPath();
    ctx.moveTo(ax, ay);
    ctx.lineTo(bx, by);
    ctx.stroke();

    const label = drawingLabel(item);
    if (label) {
      ctx.fillStyle = colour;
      ctx.textAlign = "left";
      ctx.fillText(label, Math.min(ax, bx) + 6, Math.min(ay, by) - 9);
    }

    if (selected) {
      ctx.fillStyle = COLORS.tagText;
      for (const [hx, hy] of [
        [ax, ay],
        [bx, by],
      ] as const) {
        ctx.beginPath();
        ctx.arc(hx, hy, 4.5, 0, Math.PI * 2);
        ctx.fill();
        ctx.strokeStyle = colour;
        ctx.lineWidth = 2;
        ctx.stroke();
      }
    }
  }

  ctx.restore();
  ctx.setLineDash([]);
  // Restore the left alignment the rest of the price panel expects (the
  // reference-price tag draws from its box's left edge).
  ctx.textAlign = "left";
}

/** The rubber-band line while a trend is being drawn: dashed, no handles. */
function drawTrendPreview(
  ctx: CanvasRenderingContext2D,
  preview: { a: DrawingPoint; b: DrawingPoint },
  candles: Candle[],
  geom: ChartGeometry,
  COLORS: ReturnType<typeof colors>,
) {
  const yOf = (price: number) =>
    geom.top + geom.priceH * (1 - (price - geom.min) / (geom.max - geom.min || 1));
  const xOf = (t: number) =>
    geom.plotX + (indexOfTime(candles, t) - geom.start + 0.5) * geom.step;
  ctx.save();
  ctx.beginPath();
  ctx.rect(geom.left, geom.top, geom.right - geom.left, geom.priceH);
  ctx.clip();
  ctx.strokeStyle = COLORS.crosshair;
  ctx.lineWidth = 1.2;
  ctx.setLineDash([4, 4]);
  ctx.beginPath();
  ctx.moveTo(xOf(preview.a.t), yOf(preview.a.price));
  ctx.lineTo(xOf(preview.b.t), yOf(preview.b.price));
  ctx.stroke();
  ctx.restore();
  ctx.setLineDash([]);
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

/** Indicators that get their own panel below the candles. */
type SubIndicator = Exclude<Indicator, "ma" | "ema" | "boll">;

interface SubOptions {
  indicator: SubIndicator;
  params: IndicatorParams;
  candles: Candle[];
  start: number;
  end: number;
  top: number;
  height: number;
  left: number;
  right: number;
  step: number;
  COLORS: ReturnType<typeof colors>;
}

/** Lowest and highest value a series takes inside the visible window, used by
 * the panels whose scale has no natural bounds. */
function extent(values: (number | null)[], start: number, end: number) {
  let low = Infinity;
  let high = -Infinity;
  for (let i = start; i < end; i += 1) {
    const value = values[i];
    if (value == null) continue;
    if (value < low) low = value;
    if (value > high) high = value;
  }
  if (!Number.isFinite(low) || !Number.isFinite(high)) return { low: 0, high: 1 };
  if (high - low < 1e-9) return { low: low - 1, high: high + 1 };
  return { low, high };
}

/** Horizontal reference lines, drawn dashed and faded. */
function guideLines(
  ctx: CanvasRenderingContext2D,
  levels: number[],
  y: (value: number) => number,
  left: number,
  right: number,
  color: string,
) {
  ctx.strokeStyle = color;
  ctx.setLineDash([2, 3]);
  levels.forEach((level) => {
    ctx.beginPath();
    ctx.moveTo(left, Math.round(y(level)) + 0.5);
    ctx.lineTo(right, Math.round(y(level)) + 0.5);
    ctx.stroke();
  });
  ctx.setLineDash([]);
}

/** Williams %R: where the close sits inside the period's range, -100..0. */
function wr(candles: Candle[], period: number): (number | null)[] {
  const out: (number | null)[] = new Array(candles.length).fill(null);
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
function cci(candles: Candle[], period: number): (number | null)[] {
  const typical = candles.map((candle) => (candle.h + candle.l + candle.c) / 3);
  const out: (number | null)[] = new Array(candles.length).fill(null);
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

/** On-balance volume: a running total that adds volume on up bars and removes
 * it on down bars, plus its simple moving average. */
function obv(candles: Candle[], period: number) {
  const line: number[] = new Array(candles.length).fill(0);
  for (let i = 1; i < candles.length; i += 1) {
    const previous = candles[i - 1]!.c;
    const current = candles[i]!.c;
    line[i] =
      line[i - 1]! +
      (current > previous ? candles[i]!.v : current < previous ? -candles[i]!.v : 0);
  }
  const average: (number | null)[] = new Array(candles.length).fill(null);
  for (let i = period - 1; i < line.length; i += 1) {
    let sum = 0;
    for (let j = i - period + 1; j <= i; j += 1) sum += line[j]!;
    average[i] = sum / period;
  }
  return { line, average };
}

/** Average true range, Wilder smoothed. */
function atr(candles: Candle[], period: number): (number | null)[] {
  const out: (number | null)[] = new Array(candles.length).fill(null);
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
function dmi(candles: Candle[], period: number, adxPeriod: number) {
  const plus: (number | null)[] = new Array(candles.length).fill(null);
  const minus: (number | null)[] = new Array(candles.length).fill(null);
  const adx: (number | null)[] = new Array(candles.length).fill(null);
  const dx: (number | null)[] = new Array(candles.length).fill(null);
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

/** One-line description of a switched-on indicator, parameters included. */
function legendText(config: IndicatorConfig): string {
  const values = INDICATOR_SPECS[config.name].params.map(
    (param) => config.params[param.key] ?? param.value,
  );
  return `${INDICATOR_SPECS[config.name].label}(${values.join(",")})`;
}

/** KDJ(9,3,3): stochastic oscillator with the J line derived from K and D. */
function kdj(candles: Candle[], period = 9, smooth = 3) {
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

function drawSubChart(ctx: CanvasRenderingContext2D, options: SubOptions) {
  const { indicator, params, candles, start, end, top, height, left, right, step, COLORS } =
    options;
  ctx.strokeStyle = COLORS.grid;
  ctx.beginPath();
  ctx.moveTo(left, Math.round(top) + 0.5);
  ctx.lineTo(right, Math.round(top) + 0.5);
  ctx.stroke();

  const value = (key: string, fallback: number) => params[key] ?? fallback;
  const closes = candles.map((candle) => candle.c);

  ctx.font = "9px Inter, sans-serif";
  ctx.textAlign = "left";
  ctx.fillStyle = COLORS.axis;
  ctx.fillText(legendText({ name: indicator, params }), left + 2, top + 9);

  // A panel the series has no data for (a 5-bar history asked for RSI(14))
  // stays as a header with a hint instead of a misleading flat line.
  if (candles.length < 2) return;

  if (indicator === "kdj") {
    const series = kdj(candles, value("period", 9), value("smooth", 3));
    const y = (level: number) => top + height - ((level + 20) / 140) * (height - 4);
    guideLines(ctx, [0, 50, 100], y, left, right, COLORS.grid);
    drawLine(ctx, series.k, start, end, series.k.length, step, left, y, COLORS.kdjK, 1.2);
    drawLine(ctx, series.d, start, end, series.d.length, step, left, y, COLORS.kdjD, 1.2);
    drawLine(ctx, series.j, start, end, series.j.length, step, left, y, COLORS.kdjJ, 1);
    return;
  }

  if (indicator === "rsi") {
    const values = rsi(closes, value("period", 14));
    const y = (level: number) => top + height - (level / 100) * (height - 4);
    guideLines(ctx, [30, 50, 70], y, left, right, COLORS.grid);
    drawLine(ctx, values, start, end, values.length, step, left, y, COLORS.signal, 1.2);
    return;
  }

  if (indicator === "wr") {
    const values = wr(candles, value("period", 14));
    const y = (level: number) => top + height - ((level + 100) / 100) * (height - 4);
    guideLines(ctx, [-80, -50, -20], y, left, right, COLORS.grid);
    drawLine(ctx, values, start, end, values.length, step, left, y, COLORS.signal, 1.2);
    return;
  }

  if (indicator === "cci") {
    const values = cci(candles, value("period", 20));
    // CCI is unbounded, so each window gets its own scale around the ±100
    // trigger lines, which stay on screen whenever the data allows it.
    const range = extent(values, start, end);
    const span = Math.max(Math.abs(range.low), Math.abs(range.high), 100);
    const y = (level: number) =>
      top + height / 2 - (level / span) * (height / 2 - 4);
    [100, 0, -100].forEach((level) => {
      guideLines(ctx, [level], y, left, right, COLORS.grid);
    });
    drawLine(ctx, values, start, end, values.length, step, left, y, COLORS.signal, 1.2);
    return;
  }

  if (indicator === "atr") {
    const values = atr(candles, value("period", 14));
    const range = extent(values, start, end);
    const span = range.high - range.low;
    const y = (level: number) => top + height - ((level - range.low) / span) * (height - 6);
    drawLine(ctx, values, start, end, values.length, step, left, y, COLORS.signal, 1.2);
    return;
  }

  if (indicator === "dmi") {
    const series = dmi(candles, value("period", 14), value("adx", 14));
    const y = (level: number) => top + height - (level / 100) * (height - 4);
    guideLines(ctx, [20, 50], y, left, right, COLORS.grid);
    drawLine(ctx, series.plus, start, end, series.plus.length, step, left, y, COLORS.kdjK, 1.2);
    drawLine(ctx, series.minus, start, end, series.minus.length, step, left, y, COLORS.kdjJ, 1.2);
    drawLine(ctx, series.adx, start, end, series.adx.length, step, left, y, COLORS.kdjD, 1);
    return;
  }

  if (indicator === "obv") {
    const series = obv(candles, value("period", 30));
    const range = extent(series.line, start, end);
    const span = range.high - range.low;
    const y = (level: number) => top + height - ((level - range.low) / span) * (height - 6);
    drawLine(ctx, series.line, start, end, series.line.length, step, left, y, COLORS.signal, 1.2);
    drawLine(
      ctx,
      series.average,
      start,
      end,
      series.average.length,
      step,
      left,
      y,
      COLORS.ma25,
      1,
    );
    return;
  }

  // MACD: DIF and DEA on a symmetric scale with the histogram behind them.
  const fast = ema(closes, value("fast", 12));
  const slow = ema(closes, value("slow", 26));
  const dif = fast.map((item, index) => item - slow[index]!);
  const dea = ema(dif, value("signal", 9));
  const hist = dif.map((item, index) => item - dea[index]!);

  let limit = 1e-9;
  for (let i = start; i < end; i += 1) {
    limit = Math.max(limit, Math.abs(dif[i]!), Math.abs(dea[i]!), Math.abs(hist[i]!));
  }
  const mid = top + height / 2;
  const y = (level: number) => mid - (level / limit) * (height / 2 - 4);

  ctx.strokeStyle = COLORS.grid;
  ctx.beginPath();
  ctx.moveTo(left, mid);
  ctx.lineTo(right, mid);
  ctx.stroke();

  for (let i = start; i < end; i += 1) {
    const x = left + (i - start) * step + step / 2;
    const item = hist[i]!;
    ctx.fillStyle = item >= 0 ? COLORS.upVolume : COLORS.downVolume;
    const y0 = y(0);
    const y1 = y(item);
    ctx.fillRect(
      x - Math.max(1, step * 0.3),
      Math.min(y0, y1),
      Math.max(1, step * 0.6),
      Math.max(1, Math.abs(y1 - y0)),
    );
  }

  drawLine(ctx, dif, start, end, dif.length, step, left, y, COLORS.macd, 1.2);
  drawLine(ctx, dea, start, end, dea.length, step, left, y, COLORS.signal, 1);
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
