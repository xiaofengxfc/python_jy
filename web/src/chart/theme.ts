/** 主题：CSS 设计变量 → lightweight-charts 的选项。
 *
 * 库不会自己读 CSS 变量，颜色要显式下发。
 * 所以映射集中在这一层，切换主题时用 applyOptions 整包刷新。
 */

import {
  ColorType,
  CrosshairMode,
  LineStyle,
  TickMarkType,
  type ChartOptions,
  type DeepPartial,
  type Time,
} from "lightweight-charts";
import { cssVar } from "../hooks/useThemeRevision";

export interface Palette {
  background: string;
  grid: string;
  axis: string;
  up: string;
  down: string;
  upVolume: string;
  downVolume: string;
  crosshair: string;
  tagBg: string;
  tagText: string;
  maFast: string;
  maMid: string;
  maSlow: string;
  emaFast: string;
  emaSlow: string;
  boll: string;
  signal: string;
  macd: string;
  kdjK: string;
  kdjD: string;
  kdjJ: string;
  /** 持仓开仓均价线。 */
  entry: string;
}

/** Reads a px design token and hands lightweight-charts a number, which is what
 * the library wants for fontSize (it does not parse CSS lengths). Falls back
 * when the token is missing or the page is not laid out yet. */
export function cssPx(name: string, fallback: number): number {
  const value = Number.parseFloat(cssVar(name, ""));
  return Number.isFinite(value) && value > 0 ? value : fallback;
}

export function chartPalette(): Palette {
  return {
    background: cssVar("--bg", "#ffffff"),
    grid: cssVar("--border", "#f0f0f2"),
    axis: cssVar("--text-3", "#9a9aa3"),
    up: cssVar("--up", "#16a34a"),
    down: cssVar("--down", "#e11d48"),
    upVolume: cssVar("--up-weak", "#d8f0e2"),
    downVolume: cssVar("--down-weak", "#fbdde4"),
    crosshair: cssVar("--text-3", "#9a9aa3"),
    tagBg: cssVar("--ink", "#141414"),
    tagText: cssVar("--bg", "#ffffff"),
    maFast: cssVar("--accent", "#2563eb"),
    maMid: cssVar("--warn", "#b45309"),
    maSlow: cssVar("--down", "#e11d48"),
    emaFast: cssVar("--accent", "#2563eb"),
    emaSlow: cssVar("--down", "#e5484d"),
    boll: cssVar("--text-2", "#5b5b5b"),
    signal: cssVar("--accent", "#2563eb"),
    macd: cssVar("--text-2", "#5b5b5b"),
    kdjK: cssVar("--accent", "#2563eb"),
    kdjD: cssVar("--warn", "#b45309"),
    kdjJ: cssVar("--down", "#e5484d"),
    entry: cssVar("--warn", "#b45309"),
  };
}

function pad2(value: number): string {
  return value < 10 ? "0" + value : String(value);
}

/** 库里的时间走 UTC 语义，但交易者看的是本机时间（与旧图、与后端日志
 * 一致），所以刻度与十字光标都自己格式化成本地时间。 */
function toLocal(time: Time): Date | null {
  if (typeof time !== "number") return null;
  return new Date(time * 1000);
}

export function tickMark(time: Time, type: TickMarkType): string {
  const date = toLocal(time);
  if (!date) return "";
  switch (type) {
    case TickMarkType.Year:
      return String(date.getFullYear());
    case TickMarkType.Month:
      return date.getMonth() + 1 + "月";
    case TickMarkType.DayOfMonth:
      return pad2(date.getMonth() + 1) + "/" + pad2(date.getDate());
    case TickMarkType.Time:
      return pad2(date.getHours()) + ":" + pad2(date.getMinutes());
    case TickMarkType.TimeWithSeconds:
      return pad2(date.getHours()) + ":" + pad2(date.getMinutes()) + ":" + pad2(date.getSeconds());
    default:
      return pad2(date.getMonth() + 1) + "/" + pad2(date.getDate());
  }
}

function fullStamp(time: Time): string {
  const date = toLocal(time);
  if (!date) return "";
  return (
    date.getFullYear() +
    "-" +
    pad2(date.getMonth() + 1) +
    "-" +
    pad2(date.getDate()) +
    " " +
    pad2(date.getHours()) +
    ":" +
    pad2(date.getMinutes())
  );
}

export function chartOptions(
  palette: Palette,
  timeframe: string,
): DeepPartial<ChartOptions> {
  return {
    autoSize: true,
    layout: {
      background: { type: ColorType.Solid, color: palette.background },
      textColor: palette.axis,
      fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif",
      fontSize: cssPx("--fs-12", 12),
      attributionLogo: false,
    },
    grid: {
      vertLines: { color: palette.grid },
      horzLines: { color: palette.grid },
    },
    crosshair: {
      mode: CrosshairMode.Normal,
      vertLine: {
        color: palette.crosshair,
        width: 1,
        style: LineStyle.Dashed,
        labelBackgroundColor: palette.tagBg,
      },
      horzLine: {
        color: palette.crosshair,
        width: 1,
        style: LineStyle.Dashed,
        labelBackgroundColor: palette.tagBg,
      },
    },
    rightPriceScale: {
      borderColor: palette.grid,
      // 底部留出成交量条的位置，K 线与量柱不互相压。
      scaleMargins: { top: 0.06, bottom: 0.24 },
    },
    timeScale: {
      borderColor: palette.grid,
      rightOffset: 4,
      barSpacing: 7,
      minBarSpacing: 0.4,
      // 往右能滚到最后一根之后，往左一直滚到最老的一根为止。
      fixLeftEdge: false,
      fixRightEdge: false,
      lockVisibleTimeRangeOnResize: true,
      secondsVisible: timeframe === "1m",
      tickMarkFormatter: tickMark,
    },
    localization: {
      locale: "zh-CN",
      timeFormatter: fullStamp,
    },
    handleScroll: {
      mouseWheel: true,
      pressedMouseMove: true,
      horzTouchDrag: true,
      vertTouchDrag: false,
    },
    handleScale: {
      axisPressedMouseMove: true,
      mouseWheel: true,
      pinch: true,
    },
  };
}
