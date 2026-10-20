/** Chart drawings: horizontal levels and trend lines.
 *
 * Types only plus small pure helpers — the canvas rendering and pointer
 * handling live in ``Chart.tsx`` because they must share that component's exact
 * geometry (plotX / step / yOf / scroll offset). Keep this module free of React
 * so both the chart and the persistence layer can import it.
 */

/** What a click on the chart does. ``select`` is the idle/pointer mode. */
export type DrawingTool = "select" | "hline" | "trend";

/** One anchored point: a candle open time in ms and a price. */
export interface DrawingPoint {
  t: number;
  price: number;
}

export type DrawingKind = "hline" | "trend";

interface DrawingBase {
  id: string;
  label?: string;
  /** Explicit stroke colour; when absent ``lineColor`` picks the default. */
  color?: string;
  /** ``ai`` lines came from an analysis and may be replaced on the next run;
   * ``manual`` lines are the trader's own and are only deleted by the user. */
  source?: "manual" | "ai";
}

export interface HLineDrawing extends DrawingBase {
  kind: "hline";
  price: number;
  aiKind?: "support" | "resistance" | "stop";
}

export interface TrendDrawing extends DrawingBase {
  kind: "trend";
  a: DrawingPoint;
  b: DrawingPoint;
}

export type Drawing = HLineDrawing | TrendDrawing;

const MANUAL_COLOR = "#4c7dd8";
const AI_COLORS: Record<NonNullable<HLineDrawing["aiKind"]>, string> = {
  support: "#2f9e6f",
  resistance: "#d5514f",
  stop: "#c08a2b",
};

/** Stroke colour for a drawing: the user's override, then the AI meaning, then
 * the neutral manual colour. */
export function lineColor(drawing: Drawing): string {
  if (drawing.color) return drawing.color;
  if (drawing.kind === "hline" && drawing.aiKind) return AI_COLORS[drawing.aiKind];
  return MANUAL_COLOR;
}

/** Default label shown next to a line when the user has not named it. */
export function drawingLabel(drawing: Drawing): string {
  if (drawing.label) return drawing.label;
  if (drawing.kind === "hline" && drawing.aiKind) {
    return drawing.aiKind === "support"
      ? "支撑"
      : drawing.aiKind === "resistance"
        ? "阻力"
        : "止损";
  }
  return "";
}

/** Human-readable kind, used by the toolbar/legend. */
export function drawingKindLabel(drawing: Drawing): string {
  return drawing.kind === "hline" ? "水平线" : "趋势线";
}
