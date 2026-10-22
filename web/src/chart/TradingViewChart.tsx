/** TradingView 图表，用 lightweight-charts v5。
 *
 * 行情来自本机后端（币安 REST 为主，Gate 永续备用）。这一层只管画：
 * K 线、成交量、主图和副图指标、历史分页、实时尾部、画线、AI 价位、
 * 成交标记、持仓均价。
 *
 * 坐标换算全部走库的 API（priceToCoordinate / logicalToCoordinate）。
 * 自己算坐标会和库的缩放对不上。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  CandlestickSeries,
  HistogramSeries,
  LineSeries,
  LineStyle,
  createChart,
  createSeriesMarkers,
  type HistogramData,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type LineData,
  type Logical,
  type LogicalRange,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import { useThemeRevision } from "../hooks/useThemeRevision";
import type { AILevel, Candle } from "../types";
import { fmtPrice } from "../format";
import {
  drawingLabel,
  lineColor,
  type Drawing,
  type DrawingPoint,
  type DrawingTool,
} from "../components/drawings";
import { newDrawingId } from "../components/drawingsStore";
import {
  barData,
  barTimeAt,
  distanceToSegment,
  toTime,
  histogramData,
  indexOfTime,
  lineData,
  volumeData,
} from "./adapter";
import {
  atr,
  bands,
  cci,
  dmi,
  ema,
  kdj,
  macd,
  obv,
  rsi,
  sma,
  wr,
} from "./indicators";
import {
  DrawingPrimitive,
  drawingSegments,
  type PrimitiveContext,
  type PrimitiveStyle,
} from "./primitives";
import {
  INDICATOR_SPECS,
  legendText,
  type IndicatorConfig,
  type IndicatorParams,
} from "./specs";
import { chartOptions, chartPalette, type Palette } from "./theme";

export type { Indicator, IndicatorConfig, IndicatorParams } from "./specs";
export { INDICATOR_SPECS, clampParam, defaultParams } from "./specs";

/** 一个成交点，画成买卖箭头。时间会被吸附到它所在那根 K 线。 */
export interface ChartMarker {
  time: number;
  side: "buy" | "sell";
  label: string;
}

interface Props {
  candles: Candle[];
  /** Identifies the series (market + symbol) so a live tail update does not
   * look like a new instrument and reset the user's zoom/scroll. */
  seriesKey: string;
  /** Identifies the *data* on screen: series plus the timeframe it was loaded
   * for. While a timeframe switch is in flight the old candles are still
   * passed down, so keying history detection on seriesKey alone would label
   * minutes-old bars as the new period's oldest bar. */
  dataKey?: string;
  timeframe: string;
  /** 最新价。图上最后一根 K 线的收价已经由后端/行情流对齐，这里保留是为了
   * 与旧组件保持同一份 props 契约。 */
  lastPrice?: number;
  loading: boolean;
  error: string;
  /** Every indicator the user switched on, with its parameters. */
  indicators: IndicatorConfig[];
  /** Asks for the window before the oldest candle. */
  onLoadOlder?: () => void;
  olderLoading?: boolean;
  /** 已废弃：AI 价位现在通过 drawings 传进来（那样才能拖动）。 */
  aiLevels?: AILevel[];
  drawings?: Drawing[];
  onDrawingsChange?: (next: Drawing[]) => void;
  drawingTool?: DrawingTool;
  selectedId?: string | null;
  onSelect?: (id: string | null) => void;
  onToolDone?: () => void;
  drawingsVisible?: boolean;
  /** 成交买卖点。 */
  markers?: ChartMarker[];
  /** 当前持仓均价，画成一条只读虚线。 */
  entryPrice?: number | null;
  /** 价格小数位（来自 /api/symbols 的 price_precision）。 */
  pricePrecision?: number;
}

const DRAFT_ID = "__draft";

/** 拖拽中的临时状态。所有位移都以「指针事件的屏幕坐标」为锚，
 * 松手时才回写父组件，避免拖一下写 N 次 localStorage。 */
type DragState =
  | { mode: "hline"; id: string; startY: number; startPrice: number }
  | {
      mode: "trend";
      id: string;
      startX: number;
      startY: number;
      a: DrawingPoint;
      b: DrawingPoint;
    }
  | { mode: "endpoint"; id: string; which: "a" | "b" }
  | { mode: "draft"; a: DrawingPoint; startX: number; startY: number };

interface IndicatorEntry {
  paneIndex: number;
  paint: (candles: Candle[], palette: Palette) => void;
}

const SCROLL_ON = {
  handleScroll: {
    mouseWheel: true,
    pressedMouseMove: true,
    horzTouchDrag: true,
    vertTouchDrag: false,
  },
  handleScale: { axisPressedMouseMove: true, mouseWheel: true, pinch: true },
} as const;

const SCROLL_OFF = { handleScroll: false, handleScale: false } as const;

export default function TradingViewChart({
  candles,
  seriesKey,
  dataKey,
  timeframe,
  loading,
  error,
  indicators,
  onLoadOlder,
  olderLoading = false,
  drawings = [],
  onDrawingsChange,
  drawingTool = "select",
  selectedId = null,
  onSelect,
  onToolDone,
  drawingsVisible = true,
  markers = [],
  entryPrice = null,
  pricePrecision = 2,
}: Props) {
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const hostRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volumeRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const markersRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null);
  const entryLineRef = useRef<IPriceLine | null>(null);
  const indicatorRef = useRef<IndicatorEntry[]>([]);
  const primitiveRef = useRef(new Map<string, DrawingPrimitive>());
  /** What the series currently holds, so a tick can be applied incrementally
   * instead of re-sending 400 bars. */
  const prevDataRef = useRef<{ key: string; first: number; length: number } | null>(null);
  const candlesRef = useRef<Candle[]>(candles);
  const dragRef = useRef<DragState | null>(null);
  /** 拖拽中的临时画线（先本地显示，松手才提交）。 */
  const draftRef = useRef<Drawing | null>(null);
  /** 主图指标线（MA/EMA/BOLL）：它们和蜡烛同在 pane 0，拆 pane 清不掉。 */
  const mainSeriesRef = useRef<Array<ISeriesApi<"Line">>>([]);
  const askOlderRef = useRef(0);
  const [chartReady, setChartReady] = useState(false);
  /** 拖拽/绘制中的临时画线：先本地显示，松手才提交给父组件。 */
  const [draft, setDraft] = useState<Drawing | null>(null);
  const themeRevision = useThemeRevision();
  const palette = useMemo(() => chartPalette(), [themeRevision]);

  candlesRef.current = candles;

  /** 事件回调里的 props 快照：原生监听器只挂一次，不能闭包住旧值。 */
  const live = useRef({
    drawings,
    drawingTool,
    selectedId,
    drawingsVisible,
    onDrawingsChange,
    onSelect,
    onToolDone,
    onLoadOlder,
    olderLoading,
    timeframe,
  });
  live.current = {
    drawings,
    drawingTool,
    selectedId,
    drawingsVisible,
    onDrawingsChange,
    onSelect,
    onToolDone,
    onLoadOlder,
    olderLoading,
    timeframe,
  };

  /* --------------------------------------------------------- 坐标换算 */
  const priceToY = useCallback((price: number): number | null => {
    const series = candleRef.current;
    if (!series) return null;
    const coordinate = series.priceToCoordinate(price);
    return coordinate == null ? null : Number(coordinate);
  }, []);

  const timeToX = useCallback((time: number): number | null => {
    const chart = chartRef.current;
    const list = candlesRef.current;
    if (!chart || !list.length) return null;
    const index = indexOfTime(list, time);
    const coordinate = chart.timeScale().logicalToCoordinate(index as unknown as Logical);
    return coordinate == null ? null : Number(coordinate);
  }, []);

  const context = useMemo<PrimitiveContext>(
    () => ({ timeToX, priceToY }),
    [priceToY, timeToX],
  );
  const contextRef = useRef(context);
  contextRef.current = context;

  /** 屏幕坐标 → 价格／K 线开盘时间。 */
  const priceAt = useCallback((y: number): number | null => {
    const series = candleRef.current;
    if (!series) return null;
    const price = series.coordinateToPrice(y);
    return price == null ? null : Number(price);
  }, []);

  const timeAt = useCallback((x: number): number | null => {
    const chart = chartRef.current;
    const list = candlesRef.current;
    if (!chart || !list.length) return null;
    const logical = chart.timeScale().coordinateToLogical(x);
    if (logical == null) return null;
    const index = Math.min(list.length - 1, Math.max(0, Math.round(Number(logical))));
    return list[index]?.t ?? null;
  }, []);

  /** 把某个时间按 K 线根数整体平移（趋势线横拖时保持两端价差不漂）。 */
  const shiftBars = useCallback((time: number, delta: number): number => {
    const list = candlesRef.current;
    if (!list.length) return time;
    const index = indexOfTime(list, time) + delta;
    const clamped = Math.min(list.length - 1, Math.max(0, index));
    return list[clamped]?.t ?? time;
  }, []);

  /* ------------------------------------------------------------ 建图 */
  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const flat = chartPalette();
    const chart = createChart(host, chartOptions(flat, live.current.timeframe));
    const series = chart.addSeries(CandlestickSeries, {
      upColor: flat.up,
      downColor: flat.down,
      borderUpColor: flat.up,
      borderDownColor: flat.down,
      wickUpColor: flat.up,
      wickDownColor: flat.down,
      priceLineVisible: true,
      lastValueVisible: true,
    });
    const volume = chart.addSeries(HistogramSeries, {
      priceScaleId: "volume",
      priceFormat: { type: "volume" },
      priceLineVisible: false,
      lastValueVisible: false,
    });
    chart.priceScale("volume").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });

    chartRef.current = chart;
    candleRef.current = series;
    volumeRef.current = volume;
    setChartReady(true);

    return () => {
      primitiveRef.current.clear();
      indicatorRef.current = [];
      markersRef.current = null;
      entryLineRef.current = null;
      candleRef.current = null;
      volumeRef.current = null;
      chartRef.current = null;
      prevDataRef.current = null;
      setChartReady(false);
      chart.remove();
    };
  }, []);

  /* ------------------------------------------------------ 主题与精度 */
  useEffect(() => {
    const chart = chartRef.current;
    const series = candleRef.current;
    const volume = volumeRef.current;
    if (!chart || !series || !volume) return;
    chart.applyOptions(chartOptions(palette, timeframe));
    series.applyOptions({
      upColor: palette.up,
      downColor: palette.down,
      borderUpColor: palette.up,
      borderDownColor: palette.down,
      wickUpColor: palette.up,
      wickDownColor: palette.down,
      priceFormat: {
        type: "price",
        precision: pricePrecision,
        minMove: Number((10 ** -pricePrecision).toFixed(pricePrecision)),
      },
    });
    // 成交量柱的颜色写在数据里，换主题要重下发一次。
    volume.setData(volumeData(candlesRef.current, palette));
  }, [chartReady, palette, timeframe, pricePrecision]);

  /* --------------------------------------------------------- K 线数据 */
  useEffect(() => {
    const chart = chartRef.current;
    const series = candleRef.current;
    const volume = volumeRef.current;
    if (!chart || !series || !volume) return;

    const key = dataKey ?? seriesKey;
    const first = candles[0]?.t ?? 0;
    const previous = prevDataRef.current;
    const bars = barData(candles);
    const volumes = volumeData(candles, palette);

    const sameSeries = previous !== null && previous.key === key;
    let fullReset = !sameSeries;
    if (sameSeries && candles.length >= previous.length && first === previous.first) {
      // 实时行情只会动尾部（或用一条新 bar 追加），只把尾巴喂进去。
      // 下标必须按 candles 算：barData 会丢掉重复 bar，用过滤后的数组会错位一格。
      const from = Math.max(0, previous.length - 1);
      try {
        for (let index = from; index < candles.length; index += 1) {
          const candle = candles[index];
          if (!candle) continue;
          const time = toTime(candle.t);
          series.update({
            time,
            open: candle.o,
            high: candle.h,
            low: candle.l,
            close: candle.c,
          });
          volume.update({
            time,
            value: candle.v,
            color: candle.c >= candle.o ? palette.up : palette.down,
          });
        }
      } catch {
        // 库只接受时间单调不减的 update（数据重复或倒退会被拒）。真遇上了
        // 就整表重设——画得慢一点，总比留一张卡死的图强。
        series.setData(bars);
        volume.setData(volumes);
        fullReset = true;
      }
    } else if (previous && previous.key === key && candles.length > previous.length && first < previous.first) {
      // 历史分页：整表重设，并把可视范围右移「新增的根数」，
      // 用户停在哪根 K 线就还停在哪根，不会跳到最左边。
      fullReset = true;
      const added = Math.max(0, candles.findIndex((candle) => candle.t >= previous.first));
      const range = chart.timeScale().getVisibleLogicalRange();
      series.setData(bars);
      volume.setData(volumes);
      if (range) {
        chart.timeScale().setVisibleLogicalRange({
          from: (range.from + added) as Logical,
          to: (range.to + added) as Logical,
        });
      }
    } else {
      series.setData(bars);
      volume.setData(volumes);
      fullReset = true;
    }

    prevDataRef.current = { key, first, length: candles.length };
    // 新数据落地时库会把视野铺满整段行情（逻辑坐标 0 出现在左边界），
    // 那不算用户往回拖。给它一点时间，别一换币种就自动多拉一屏历史。
    if (fullReset) askOlderRef.current = Date.now() + 800;
    // 指标线跟着同一份 K 线走，数值不能落后一根。
    indicatorRef.current.forEach((entry) => entry.paint(candles, palette));
  }, [chartReady, candles, dataKey, seriesKey, palette]);

  /* ------------------------------------------------------ 无限历史 */
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart || !chartReady) return;
    const handler = (range: LogicalRange | null) => {
      if (!range) return;
      if (range.from > 12) return;
      const now = Date.now();
      // 用户往回拖会连着触发很多帧，别把后端问穿。askOlderRef 既是节流时间戳，
      // 也用于整表重设后的短暂屏蔽（见上面的数据 effect）。
      if (now < askOlderRef.current) return;
      if (live.current.olderLoading) return;
      askOlderRef.current = now + 400;
      live.current.onLoadOlder?.();
    };
    chart.timeScale().subscribeVisibleLogicalRangeChange(handler);
    return () => chart.timeScale().unsubscribeVisibleLogicalRangeChange(handler);
  }, [chartReady]);

  /* ---------------------------------------------------------- 指标 */
  const indicatorSignature = useMemo(
    () =>
      indicators
        .map((config) =>
          config.name +
          ":" +
          INDICATOR_SPECS[config.name].params
            .map((param) => String(config.params[param.key] ?? param.value))
            .join(","),
        )
        .join("|"),
    [indicators],
  );

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart || !chartReady) return;

    // 拆掉上一轮：先摘主图上的指标线（它们与蜡烛同处 pane 0，删 pane 带不走），
    // 再把副图 pane 从末尾删掉——删 pane 会连带清掉它里面的序列。
    mainSeriesRef.current.forEach((item) => {
      try {
        chart.removeSeries(item);
      } catch {
        // 已经随 pane 一起销毁的情况，忽略即可。
      }
    });
    mainSeriesRef.current = [];
    while (chart.panes().length > 1) chart.removePane(chart.panes().length - 1);
    indicatorRef.current = [];

    const precision = pricePrecision;
    const priceFormat = {
      type: "price" as const,
      precision,
      minMove: Number((10 ** -precision).toFixed(precision)),
    };
    const entries: IndicatorEntry[] = [];

    const addLine = (
      paneIndex: number,
      options: {
        color: string;
        lineWidth?: number;
        dashed?: boolean;
        sub?: boolean;
      },
      compute: (candles: Candle[]) => (number | null)[] | number[],
      guides: number[] = [],
    ) => {
      const series = chart.addSeries(
        LineSeries,
        {
          color: options.color,
          lineWidth: (options.lineWidth ?? 1) as 1 | 2 | 3 | 4,
          lineStyle: options.dashed ? LineStyle.Dashed : LineStyle.Solid,
          priceLineVisible: false,
          lastValueVisible: false,
          crosshairMarkerVisible: false,
          priceFormat: options.sub
            ? { type: "price", precision: 2, minMove: 0.01 }
            : priceFormat,
        },
        paneIndex,
      );
      guides.forEach((price) => {
        series.createPriceLine({
          price,
          color: palette.grid,
          lineWidth: 1,
          lineStyle: LineStyle.Dotted,
          axisLabelVisible: false,
          title: "",
        });
      });
      entries.push({
        paneIndex,
        paint: (bars) => {
          series.setData(lineData(bars, compute(bars)) as LineData<UTCTimestamp>[]);
        },
      });
      return series;
    };

    const addHistogram = (
      paneIndex: number,
      compute: (candles: Candle[]) => (number | null)[],
      colors: { up: string; down: string },
    ) => {
      const series = chart.addSeries(
        HistogramSeries,
        {
          priceLineVisible: false,
          lastValueVisible: false,
          priceFormat: { type: "price", precision: 2, minMove: 0.01 },
        },
        paneIndex,
      );
      entries.push({
        paneIndex,
        paint: (bars) => {
          series.setData(histogramData(bars, compute(bars), colors) as HistogramData<UTCTimestamp>[]);
        },
      });
      return series;
    };

    const value = (params: IndicatorParams, key: string, fallback: number) =>
      params[key] ?? fallback;
    const closesOf = (bars: Candle[]) => bars.map((candle) => candle.c);

    // 主图指标挂在 pane 0，与蜡烛同一个面板。
    for (const config of indicators) {
      if (INDICATOR_SPECS[config.name].kind !== "main") continue;
      if (config.name === "ma") {
        const plan: [string, string][] = [
          [String(value(config.params, "fast", 7)), palette.maFast],
          [String(value(config.params, "mid", 25)), palette.maMid],
          [String(value(config.params, "slow", 60)), palette.maSlow],
        ];
        plan.forEach(([period, color]) => {
          const span = Number(period);
          mainSeriesRef.current.push(
            addLine(0, { color, lineWidth: 1 }, (bars) => sma(closesOf(bars), span)),
          );
        });
      } else if (config.name === "ema") {
        const fast = value(config.params, "fast", 12);
        const slow = value(config.params, "slow", 26);
        mainSeriesRef.current.push(
          addLine(0, { color: palette.emaFast, lineWidth: 1 }, (bars) =>
            ema(closesOf(bars), fast),
          ),
        );
        mainSeriesRef.current.push(
          addLine(0, { color: palette.emaSlow, lineWidth: 1 }, (bars) =>
            ema(closesOf(bars), slow),
          ),
        );
      } else {
        const period = value(config.params, "period", 20);
        const mult = value(config.params, "mult", 2);
        mainSeriesRef.current.push(
          addLine(0, { color: palette.boll, lineWidth: 1 }, (bars) =>
            bands(closesOf(bars), period, mult).upper,
          ),
        );
        mainSeriesRef.current.push(
          addLine(0, { color: palette.boll, lineWidth: 1, dashed: true }, (bars) =>
            bands(closesOf(bars), period, mult).middle,
          ),
        );
        mainSeriesRef.current.push(
          addLine(0, { color: palette.boll, lineWidth: 1 }, (bars) =>
            bands(closesOf(bars), period, mult).lower,
          ),
        );
      }
    }

    // 副图指标各占一个 pane，顺序与用户开启的顺序一致。
    for (const config of indicators) {
      if (INDICATOR_SPECS[config.name].kind !== "sub") continue;
      chart.addPane();
      const paneIndex = chart.panes().length - 1;
      chart.panes()[paneIndex]?.setStretchFactor(0.24);

      if (config.name === "macd") {
        const fast = value(config.params, "fast", 12);
        const slow = value(config.params, "slow", 26);
        const signal = value(config.params, "signal", 9);
        addHistogram(
          paneIndex,
          (bars) => macd(closesOf(bars), fast, slow, signal).hist,
          { up: palette.upVolume, down: palette.downVolume },
        );
        addLine(paneIndex, { color: palette.macd, lineWidth: 1, sub: true }, (bars) =>
          macd(closesOf(bars), fast, slow, signal).dif,
        );
        addLine(paneIndex, { color: palette.signal, lineWidth: 1, sub: true }, (bars) =>
          macd(closesOf(bars), fast, slow, signal).dea,
        );
      } else if (config.name === "rsi") {
        const period = value(config.params, "period", 14);
        addLine(
          paneIndex,
          { color: palette.signal, lineWidth: 1, sub: true },
          (bars) => rsi(closesOf(bars), period),
          [30, 50, 70],
        );
      } else if (config.name === "kdj") {
        const period = value(config.params, "period", 9);
        const smooth = value(config.params, "smooth", 3);
        addLine(
          paneIndex,
          { color: palette.kdjK, lineWidth: 1, sub: true },
          (bars) => kdj(bars, period, smooth).k,
          [0, 50, 100],
        );
        addLine(paneIndex, { color: palette.kdjD, lineWidth: 1, sub: true }, (bars) =>
          kdj(bars, period, smooth).d,
        );
        addLine(paneIndex, { color: palette.kdjJ, lineWidth: 1, sub: true }, (bars) =>
          kdj(bars, period, smooth).j,
        );
      } else if (config.name === "wr") {
        const period = value(config.params, "period", 14);
        addLine(
          paneIndex,
          { color: palette.signal, lineWidth: 1, sub: true },
          (bars) => wr(bars, period),
          [-80, -50, -20],
        );
      } else if (config.name === "cci") {
        const period = value(config.params, "period", 20);
        addLine(
          paneIndex,
          { color: palette.signal, lineWidth: 1, sub: true },
          (bars) => cci(bars, period),
          [100, 0, -100],
        );
      } else if (config.name === "obv") {
        const period = value(config.params, "period", 30);
        addLine(paneIndex, { color: palette.signal, lineWidth: 1, sub: true }, (bars) =>
          obv(bars, period).line,
        );
        addLine(paneIndex, { color: palette.maMid, lineWidth: 1, sub: true }, (bars) =>
          obv(bars, period).average,
        );
      } else if (config.name === "atr") {
        const period = value(config.params, "period", 14);
        addLine(paneIndex, { color: palette.signal, lineWidth: 1, sub: true }, (bars) =>
          atr(bars, period),
        );
      } else {
        const period = value(config.params, "period", 14);
        const adxPeriod = value(config.params, "adx", 14);
        addLine(
          paneIndex,
          { color: palette.kdjK, lineWidth: 1, sub: true },
          (bars) => dmi(bars, period, adxPeriod).plus,
          [20, 50],
        );
        addLine(paneIndex, { color: palette.kdjJ, lineWidth: 1, sub: true }, (bars) =>
          dmi(bars, period, adxPeriod).minus,
        );
        addLine(paneIndex, { color: palette.kdjD, lineWidth: 1, sub: true }, (bars) =>
          dmi(bars, period, adxPeriod).adx,
        );
      }
    }

    indicatorRef.current = entries;
    entries.forEach((entry) => entry.paint(candlesRef.current, palette));
  }, [chartReady, indicatorSignature, indicators, palette, pricePrecision]);

  /* ------------------------------------------------------ 成交标记 */
  useEffect(() => {
    const series = candleRef.current;
    if (!series || !chartReady) return;
    if (!markersRef.current) markersRef.current = createSeriesMarkers(series, []);
    const list: SeriesMarker<Time>[] = [];
    const bars = candlesRef.current;
    for (const marker of markers) {
      const barTime = barTimeAt(bars, marker.time);
      if (barTime == null) continue;
      list.push({
        time: Math.floor(barTime / 1000) as UTCTimestamp,
        position: marker.side === "buy" ? "belowBar" : "aboveBar",
        shape: marker.side === "buy" ? "arrowUp" : "arrowDown",
        color: marker.side === "buy" ? palette.up : palette.down,
        text: marker.label,
      });
    }
    markersRef.current.setMarkers(list);
  }, [chartReady, markers, candles, palette]);

  /* ---------------------------------------------------- 持仓均价线 */
  useEffect(() => {
    const series = candleRef.current;
    if (!series || !chartReady) return;
    if (entryLineRef.current) {
      series.removePriceLine(entryLineRef.current);
      entryLineRef.current = null;
    }
    if (entryPrice && entryPrice > 0) {
      entryLineRef.current = series.createPriceLine({
        price: entryPrice,
        color: palette.entry,
        lineWidth: 1,
        lineStyle: LineStyle.Dashed,
        axisLabelVisible: true,
        title: "开仓均价",
      });
    }
  }, [chartReady, entryPrice, palette]);

  /* ---------------------------------------------------------- 画线 */
  const visibleDrawings = useMemo(() => {
    if (!draft) return drawings;
    const index = drawings.findIndex((item) => item.id === draft.id);
    if (index < 0) return [...drawings, draft];
    const next = drawings.slice();
    next[index] = draft;
    return next;
  }, [drawings, draft]);

  useEffect(() => {
    const series = candleRef.current;
    if (!series || !chartReady) return;
    const attached = primitiveRef.current;
    const wanted = drawingsVisible ? visibleDrawings : [];
    const keep = new Set(wanted.map((item) => item.id));

    for (const [id, primitive] of [...attached]) {
      if (keep.has(id)) continue;
      try {
        series.detachPrimitive(primitive);
      } catch {
        // 图元可能已随序列销毁，忽略。
      }
      attached.delete(id);
    }

    wanted.forEach((item) => {
      const style: PrimitiveStyle = {
        color: lineColor(item),
        chipText:
          item.kind === "hline"
            ? (drawingLabel(item) ? drawingLabel(item) + " " : "") + fmtPrice(item.price)
            : drawingLabel(item),
        textColor: palette.tagText,
        dashed: item.kind === "hline" || item.id === DRAFT_ID,
        lineWidth: item.id === selectedId ? 2.2 : 1.4,
        selected: item.id === selectedId && item.id !== DRAFT_ID,
      };
      const existing = attached.get(item.id);
      if (existing) {
        existing.update(item, style, context);
        return;
      }
      const primitive = new DrawingPrimitive(item, style, context);
      series.attachPrimitive(primitive);
      attached.set(item.id, primitive);
    });
  }, [chartReady, visibleDrawings, drawingsVisible, selectedId, palette, context]);

  /* ------------------------------------------------------ 指针交互 */
  useEffect(() => {
    const wrap = wrapRef.current;
    const host = hostRef.current;
    if (!wrap || !host || !chartReady) return;

    const point = (event: PointerEvent) => {
      const rect = host.getBoundingClientRect();
      return { x: event.clientX - rect.left, y: event.clientY - rect.top };
    };

    /** 命中测试：后画的线优先，隐藏时不可抓（看不见的东西不该能拖走）。 */
    /** 指针是否落在 K 线绘图区（右边是价格轴，底部是时间轴）。 */
    const insidePlot = (x: number, y: number): boolean => {
      const chart = chartRef.current;
      const host = hostRef.current;
      if (!chart || !host) return false;
      const axis = chart.priceScale("right").width();
      return x >= 0 && x <= host.clientWidth - axis && y >= 0 && y <= chart.paneSize(0).height;
    };

    const drawingAt = (x: number, y: number): Drawing | null => {
      const state = live.current;
      if (!state.drawingsVisible) return null;
      for (let index = state.drawings.length - 1; index >= 0; index -= 1) {
        const item = state.drawings[index];
        if (!item) continue;
        const segments = drawingSegments(item, contextRef.current);
        if (!segments) continue;
        if (item.kind === "hline") {
          if (Math.abs(y - segments.y1) <= 6) return item;
          continue;
        }
        if (
          Math.hypot(x - segments.x1, y - segments.y1) <= 9 ||
          Math.hypot(x - segments.x2, y - segments.y2) <= 9
        ) {
          return item;
        }
        if (
          distanceToSegment(x, y, segments.x1, segments.y1, segments.x2, segments.y2) <= 6
        ) {
          return item;
        }
      }
      return null;
    };

    const restoreScroll = () => {
      chartRef.current?.applyOptions(SCROLL_ON);
    };

    const finish = (commit: boolean) => {
      const pending = draftRef.current;
      dragRef.current = null;
      draftRef.current = null;
      restoreScroll();
      if (commit && pending) {
        // 新画的趋势线在草稿阶段用的是占位 id，落地时要换成一个真 id，
        // 否则第二条趋势线会顶着同一个 "__draft" 把前一条覆盖掉。
        const item: Drawing =
          pending.id === DRAFT_ID ? { ...pending, id: newDrawingId("trend") } : pending;
        const list = live.current.drawings;
        const index = list.findIndex((entry) => entry.id === item.id);
        const next =
          index >= 0
            ? list.map((entry) => (entry.id === item.id ? item : entry))
            : [...list, item];
        live.current.onDrawingsChange?.(next);
        live.current.onSelect?.(item.id);
        if (index < 0) live.current.onToolDone?.();
      }
      setDraft(null);
    };

    const onPointerDown = (event: PointerEvent) => {
      if (event.button !== 0) return;
      const chart = chartRef.current;
      if (!chart) return;
      const { x, y } = point(event);
      const state = live.current;

      if (state.drawingTool === "hline") {
        if (!insidePlot(x, y)) return;
        const price = priceAt(y);
        if (price == null || !state.onDrawingsChange) return;
        event.preventDefault();
        event.stopPropagation();
        const item: Drawing = {
          id: newDrawingId("hline"),
          kind: "hline",
          price,
          source: "manual",
        };
        state.onDrawingsChange([...state.drawings, item]);
        state.onSelect?.(item.id);
        state.onToolDone?.();
        return;
      }

      if (state.drawingTool === "trend") {
        if (!insidePlot(x, y)) return;
        const barTime = timeAt(x);
        const price = priceAt(y);
        if (barTime == null || price == null || !state.onDrawingsChange) return;
        event.preventDefault();
        event.stopPropagation();
        const origin = { t: barTime, price };
        dragRef.current = { mode: "draft", a: origin, startX: x, startY: y };
        const item: Drawing = {
          id: DRAFT_ID,
          kind: "trend",
          a: origin,
          b: { ...origin },
          source: "manual",
        };
        draftRef.current = item;
        setDraft(item);
        chart.applyOptions(SCROLL_OFF);
        return;
      }

      const hit = drawingAt(x, y);
      if (hit && state.onDrawingsChange) {
        event.preventDefault();
        event.stopPropagation();
        state.onSelect?.(hit.id);
        const segments = drawingSegments(hit, contextRef.current);
        if (hit.kind === "hline") {
          dragRef.current = { mode: "hline", id: hit.id, startY: y, startPrice: hit.price };
        } else if (segments) {
          const nearA = Math.hypot(x - segments.x1, y - segments.y1);
          const nearB = Math.hypot(x - segments.x2, y - segments.y2);
          if (Math.min(nearA, nearB) <= 9) {
            dragRef.current = {
              mode: "endpoint",
              id: hit.id,
              which: nearA <= nearB ? "a" : "b",
            };
          } else {
            dragRef.current = {
              mode: "trend",
              id: hit.id,
              startX: x,
              startY: y,
              a: { ...hit.a },
              b: { ...hit.b },
            };
          }
        }
        draftRef.current = hit;
        chart.applyOptions(SCROLL_OFF);
        return;
      }

      state.onSelect?.(null);
    };

    const onPointerMove = (event: PointerEvent) => {
      const drag = dragRef.current;
      const { x, y } = point(event);
      if (!drag) {
        // 悬停在线上时换指针样式，表示这条线能拖。
        wrap.style.cursor = drawingAt(x, y) ? "grab" : "";
        return;
      }
      if (drag.mode === "draft") {
        const barTime = timeAt(x);
        const price = priceAt(y);
        if (barTime == null || price == null) return;
        const moved = Math.hypot(x - drag.startX, y - drag.startY) > 3;
        if (!moved) return;
        const item: Drawing = {
          id: DRAFT_ID,
          kind: "trend",
          a: drag.a,
          b: { t: barTime, price },
          source: "manual",
        };
        draftRef.current = item;
        setDraft(item);
        return;
      }

      const current = draftRef.current;
      if (!current) return;

      if (drag.mode === "hline") {
        if (current.kind !== "hline") return;
        const price = priceAt(y);
        if (price == null) return;
        const item: Drawing = { ...current, price };
        draftRef.current = item;
        setDraft(item);
        return;
      }

      if (current.kind !== "trend") return;

      if (drag.mode === "endpoint") {
        const barTime = timeAt(x);
        const price = priceAt(y);
        if (barTime == null || price == null) return;
        const point2 = { t: barTime, price };
        const item: Drawing = drag.which === "a"
          ? { ...current, a: point2 }
          : { ...current, b: point2 };
        draftRef.current = item;
        setDraft(item);
        return;
      }

      // 整条平移：横按 K 线根数、纵按价格差，线的斜率保持不变。
      const price = priceAt(y);
      const anchorPrice = priceAt(drag.startY);
      const logical = chartRef.current?.timeScale().coordinateToLogical(x);
      const anchorLogical = chartRef.current?.timeScale().coordinateToLogical(drag.startX);
      if (price == null || anchorPrice == null || logical == null || anchorLogical == null) {
        return;
      }
      const deltaBars = Math.round(Number(logical) - Number(anchorLogical));
      const deltaPrice = price - anchorPrice;
      if (deltaBars === 0 && Math.abs(deltaPrice) < 1e-12) return;
      const item: Drawing = {
        ...current,
        a: { t: shiftBars(drag.a.t, deltaBars), price: drag.a.price + deltaPrice },
        b: { t: shiftBars(drag.b.t, deltaBars), price: drag.b.price + deltaPrice },
      };
      draftRef.current = item;
      setDraft(item);
    };

    const onPointerUp = (event: PointerEvent) => {
      if (!dragRef.current) return;
      event.preventDefault();
      event.stopPropagation();
      const drag = dragRef.current;
      const pending = draftRef.current;
      if (drag.mode === "draft" && pending && pending.kind === "trend") {
        // 一次点击不算一条趋势线：两个端点重合就丢弃。
        if (pending.a.t === pending.b.t && pending.a.price === pending.b.price) {
          finish(false);
          return;
        }
      }
      finish(true);
    };

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      if (!dragRef.current) return;
      dragRef.current = null;
      draftRef.current = null;
      restoreScroll();
      setDraft(null);
    };

    wrap.addEventListener("pointerdown", onPointerDown, true);
    wrap.addEventListener("pointermove", onPointerMove, true);
    wrap.addEventListener("pointerup", onPointerUp, true);
    wrap.addEventListener("pointercancel", onPointerUp, true);
    window.addEventListener("keydown", onKeyDown);
    return () => {
      wrap.removeEventListener("pointerdown", onPointerDown, true);
      wrap.removeEventListener("pointermove", onPointerMove, true);
      wrap.removeEventListener("pointerup", onPointerUp, true);
      wrap.removeEventListener("pointercancel", onPointerUp, true);
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [chartReady, priceAt, timeAt, shiftBars]);

  const empty = candles.length === 0;

  return (
    <div className="chart-wrap" ref={wrapRef}>
      <div className="chart-host" ref={hostRef} />
      {!empty && (
        <div className="chart-legend">
          {indicators.map((config) => (
            <span className="legend-item" key={config.name}>
              {legendText(config)}
            </span>
          ))}
          {olderLoading && <span className="legend-item">加载更早的 K 线…</span>}
          <button
            type="button"
            className="link-btn"
            onClick={() => chartRef.current?.timeScale().scrollToRealTime()}
          >
            回到最新
          </button>
        </div>
      )}
      {(empty || loading) && (
        <div className="chart-empty">
          {error ? "K 线加载失败：" + error : loading ? "正在加载行情…" : "暂无 K 线数据"}
        </div>
      )}
    </div>
  );
}
