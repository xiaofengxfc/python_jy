import { useEffect, useRef } from "react";
import { cssVar, useThemeRevision } from "../hooks/useThemeRevision";

export interface Point {
  x: number;
  y: number;
}

export interface Series {
  name: string;
  color: string;
  points: Point[];
}

interface Props {
  series: Series[];
  height?: number;
  valueFormat?: (value: number) => string;
  xFormat?: (value: number) => string;
  baseline?: number;
  empty?: string;
}

const AXIS_FALLBACK = "#9a9aa3";
const GRID_FALLBACK = "#f0f0f2";

/** Lightweight multi-series line chart. Canvas keeps thousands of equity
 * points smooth, and the component owns its own resize handling. */
export default function LineChart({
  series,
  height = 240,
  valueFormat = (value) => value.toFixed(2),
  xFormat = (value) => new Date(value).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" }),
  baseline,
  empty = "暂无数据",
}: Props) {
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const themeRevision = useThemeRevision();

  useEffect(() => {
    const wrap = wrapRef.current;
    const canvas = canvasRef.current;
    if (!wrap || !canvas) return;

    const paint = () => {
      // Resolved per paint so a theme switch does not leave pale gridlines behind.
      const AXIS = cssVar("--text-3", AXIS_FALLBACK);
      const GRID = cssVar("--border", GRID_FALLBACK);
      const BASELINE = cssVar("--border-strong", "#d4d4da");
      const rect = wrap.getBoundingClientRect();
      const width = Math.max(200, Math.floor(rect.width));
      const boxHeight = height;
      const dpr = window.devicePixelRatio || 1;
      canvas.width = width * dpr;
      canvas.height = boxHeight * dpr;
      canvas.style.width = `${width}px`;
      canvas.style.height = `${boxHeight}px`;

      const ctx = canvas.getContext("2d");
      if (!ctx) return;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, width, boxHeight);

      const drawable = series.filter((item) => item.points.length > 0);
      if (!drawable.length) {
        ctx.fillStyle = AXIS;
        ctx.font = "12px Inter, sans-serif";
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText(empty, width / 2, boxHeight / 2);
        return;
      }

      const padding = { top: 10, right: 64, bottom: 20, left: 6 };
      const plotW = width - padding.left - padding.right;
      const plotH = boxHeight - padding.top - padding.bottom;

      const xs = drawable.flatMap((item) => item.points.map((point) => point.x));
      const ys = drawable.flatMap((item) => item.points.map((point) => point.y));
      let minX = Math.min(...xs);
      let maxX = Math.max(...xs);
      let minY = Math.min(...ys, baseline ?? Number.POSITIVE_INFINITY);
      let maxY = Math.max(...ys, baseline ?? Number.NEGATIVE_INFINITY);
      if (maxX === minX) maxX = minX + 1;
      if (maxY === minY) {
        maxY += Math.abs(maxY) * 0.01 || 1;
        minY -= Math.abs(minY) * 0.01 || 1;
      } else {
        const pad = (maxY - minY) * 0.08;
        minY -= pad;
        maxY += pad;
      }

      const xOf = (value: number) => padding.left + ((value - minX) / (maxX - minX)) * plotW;
      const yOf = (value: number) => padding.top + (1 - (value - minY) / (maxY - minY)) * plotH;

      ctx.font = "10px Inter, sans-serif";
      ctx.textBaseline = "middle";
      ctx.textAlign = "left";
      for (let i = 0; i <= 4; i += 1) {
        const y = padding.top + (plotH * i) / 4;
        const value = maxY - ((maxY - minY) * i) / 4;
        ctx.strokeStyle = GRID;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(padding.left, Math.round(y) + 0.5);
        ctx.lineTo(padding.left + plotW, Math.round(y) + 0.5);
        ctx.stroke();
        ctx.fillStyle = AXIS;
        ctx.fillText(valueFormat(value), padding.left + plotW + 8, y);
      }

      if (baseline !== undefined) {
        const y = yOf(baseline);
        ctx.strokeStyle = BASELINE;
        ctx.setLineDash([4, 3]);
        ctx.beginPath();
        ctx.moveTo(padding.left, y);
        ctx.lineTo(padding.left + plotW, y);
        ctx.stroke();
        ctx.setLineDash([]);
      }

      for (const item of drawable) {
        ctx.strokeStyle = item.color;
        ctx.lineWidth = 1.6;
        ctx.lineJoin = "round";
        ctx.beginPath();
        item.points.forEach((point, index) => {
          const x = xOf(point.x);
          const y = yOf(point.y);
          if (index === 0) ctx.moveTo(x, y);
          else ctx.lineTo(x, y);
        });
        ctx.stroke();
      }

      ctx.fillStyle = AXIS;
      ctx.textAlign = "center";
      const ticks = Math.min(4, Math.max(2, Math.floor(plotW / 110)));
      for (let i = 0; i < ticks; i += 1) {
        const value = minX + ((maxX - minX) * i) / Math.max(1, ticks - 1);
        ctx.fillText(
          xFormat(value),
          Math.min(padding.left + plotW - 20, Math.max(padding.left + 20, xOf(value))),
          boxHeight - 8,
        );
      }
    };

    paint();
    const observer = new ResizeObserver(paint);
    observer.observe(wrap);
    return () => observer.disconnect();
  }, [series, height, valueFormat, xFormat, baseline, empty, themeRevision]);

  return (
    <div ref={wrapRef} style={{ width: "100%" }}>
      <canvas ref={canvasRef} style={{ display: "block" }} />
    </div>
  );
}

export function Legend({ series }: { series: Series[] }) {
  return (
    <div className="legend">
      {series.map((item) => (
        <span key={item.name} className="legend-item">
          <i style={{ background: item.color }} />
          {item.name}
        </span>
      ))}
    </div>
  );
}
