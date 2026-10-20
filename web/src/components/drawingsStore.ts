/** Persistence for chart drawings.
 *
 * Drawings are a view of the trader's own chart, not account state: they live
 * in the browser (one entry per market / symbol / timeframe) and never go to
 * the backend. A corrupt or half-written entry must never break the chart, so
 * every read validates shape and silently falls back to "no drawings".
 */

import type { Drawing, DrawingPoint } from "./drawings";
import type { AILevel } from "../types";

const STORAGE_PREFIX = "btrader.drawings.v1";

export function drawingStorageKey(
  market: string,
  symbol: string,
  timeframe: string,
): string {
  return `${STORAGE_PREFIX}:${market}:${symbol}:${timeframe}`;
}

function isPoint(value: unknown): value is DrawingPoint {
  if (!value || typeof value !== "object") return false;
  const point = value as Partial<DrawingPoint>;
  return Number.isFinite(point.t) && Number.isFinite(point.price);
}

/** Keep only entries that match the union the chart knows how to draw. */
function normalise(raw: unknown): Drawing[] {
  if (!Array.isArray(raw)) return [];
  const out: Drawing[] = [];
  raw.forEach((item) => {
    if (!item || typeof item !== "object") return;
    const value = item as Partial<Drawing> & { id?: unknown };
    if (typeof value.id !== "string" || !value.id) return;
    if (value.kind === "hline" && Number.isFinite(value.price)) {
      out.push({
        id: value.id,
        kind: "hline",
        price: Number(value.price),
        label: typeof value.label === "string" ? value.label : "",
        color: typeof value.color === "string" ? value.color : undefined,
        source: value.source === "ai" ? "ai" : "manual",
        aiKind:
          value.aiKind === "support" ||
          value.aiKind === "resistance" ||
          value.aiKind === "stop"
            ? value.aiKind
            : undefined,
      });
      return;
    }
    if (value.kind === "trend" && isPoint(value.a) && isPoint(value.b)) {
      out.push({
        id: value.id,
        kind: "trend",
        a: { t: Number(value.a.t), price: Number(value.a.price) },
        b: { t: Number(value.b.t), price: Number(value.b.price) },
        label: typeof value.label === "string" ? value.label : "",
        color: typeof value.color === "string" ? value.color : undefined,
        source: value.source === "ai" ? "ai" : "manual",
      });
    }
  });
  return out;
}

export function loadDrawings(
  market: string,
  symbol: string,
  timeframe: string,
): Drawing[] {
  try {
    const raw = localStorage.getItem(drawingStorageKey(market, symbol, timeframe));
    return raw ? normalise(JSON.parse(raw)) : [];
  } catch {
    return [];
  }
}

export function saveDrawings(
  market: string,
  symbol: string,
  timeframe: string,
  drawings: Drawing[],
): void {
  try {
    localStorage.setItem(
      drawingStorageKey(market, symbol, timeframe),
      JSON.stringify(drawings),
    );
  } catch {
    // A full or disabled storage must not break trading.
  }
}

let counter = 0;

export function newDrawingId(prefix = "d"): string {
  counter += 1;
  return `${prefix}-${Date.now().toString(36)}-${counter}`;
}

const AI_KIND_COLOR: Record<AILevel["kind"], string> = {
  support: "#2f9e6f",
  resistance: "#d5514f",
  stop: "#c08a2b",
};

/** Turn one analysis' price levels into editable drawings.
 *
 * The levels keep their meaning in the label, but from here on they behave like
 * every other line on the chart: draggable, renameable, deletable. The AI is a
 * source of suggestions, not a second rendering pipeline.
 */
export function levelsToDrawings(levels: AILevel[]): Drawing[] {
  return levels.map((level) => ({
    id: newDrawingId("ai"),
    kind: "hline" as const,
    price: Number(level.price),
    label: level.label || (level.kind === "support" ? "支撑" : level.kind === "resistance" ? "阻力" : "止损"),
    color: AI_KIND_COLOR[level.kind],
    source: "ai" as const,
    aiKind: level.kind,
  }));
}
