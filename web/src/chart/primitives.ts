/** 画线图元：把 Drawings（水平线 / 趋势线）画到 TradingView 图上。
 *
 * lightweight-charts 不提供内置画线，只能走 series primitive：一个图元自己
 * 负责绘制（paneViews → renderer）与坐标系换算。命中测试与拖拽留在组件里做，
 * 因为那里才有「当前是哪根 K 线、鼠标落在哪个价格」的完整上下文。
 *
 * 坐标全部用 CSS 像素：渲染时 ctx.scale(横向比, 纵向比) 之后，
 * 所有数字与指针事件、与库的 coordinateToPrice 都是同一套单位。
 */

import type {
  IPrimitivePaneRenderer,
  IPrimitivePaneView,
  ISeriesPrimitive,
  PrimitivePaneViewZOrder,
  SeriesAttachedParameter,
  Time,
} from "lightweight-charts";
import type { Drawing } from "../components/drawings";

type RenderTarget = Parameters<IPrimitivePaneRenderer["draw"]>[0];

/** 价格/时间 → 屏幕像素。由组件注入，用的是它手上那份 K 线数组，因此
 * 即使某个点在当前可视范围之外也能算出坐标（库自带的 timeToCoordinate
 * 只对已加载的 bar 有效）。 */
export interface PrimitiveContext {
  timeToX: (t: number) => number | null;
  priceToY: (price: number) => number | null;
}

export interface PrimitiveStyle {
  color: string;
  /** 已经格式化好的「标签 + 价格」文本，图元不负责数字格式。 */
  chipText: string;
  textColor: string;
  dashed: boolean;
  lineWidth: number;
  selected: boolean;
}

export interface Segments {
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  /** 水平线整条铺满面板，右端点要等渲染时才知道。 */
  fullWidth: boolean;
}

function segmentsOf(drawing: Drawing, context: PrimitiveContext): Segments | null {
  if (drawing.kind === "hline") {
    const y = context.priceToY(drawing.price);
    if (y == null) return null;
    return { x1: 0, y1: y, x2: 0, y2: y, fullWidth: true };
  }
  const ax = context.timeToX(drawing.a.t);
  const ay = context.priceToY(drawing.a.price);
  const bx = context.timeToX(drawing.b.t);
  const by = context.priceToY(drawing.b.price);
  if (ax == null || ay == null || bx == null || by == null) return null;
  return { x1: ax, y1: ay, x2: bx, y2: by, fullWidth: false };
}

/** 屏幕上这条线段的坐标，命中测试与渲染共用同一份换算。 */
export function drawingSegments(
  drawing: Drawing,
  context: PrimitiveContext,
): Segments | null {
  return segmentsOf(drawing, context);
}

class DrawingRenderer implements IPrimitivePaneRenderer {
  constructor(private readonly _primitive: DrawingPrimitive) {}

  draw(target: RenderTarget): void {
    const segments = this._primitive.segments();
    if (!segments) return;
    const style = this._primitive.style;
    const kind = this._primitive.drawing.kind;

    target.useBitmapCoordinateSpace((scope) => {
      const context = scope.context;
      const ratioX = scope.horizontalPixelRatio;
      const ratioY = scope.verticalPixelRatio;
      const width = scope.mediaSize.width;

      context.save();
      // 之后一律按 CSS 像素作画：与指针坐标、与库的 API 同一单位。
      context.scale(ratioX, ratioY);
      context.lineJoin = "round";
      context.strokeStyle = style.color;
      context.lineWidth = style.lineWidth;
      context.setLineDash(style.dashed ? [7, 4] : []);
      context.beginPath();
      context.moveTo(segments.x1, segments.y1);
      context.lineTo(segments.fullWidth ? width : segments.x2, segments.y2);
      context.stroke();
      context.setLineDash([]);

      if (kind === "hline") {
        // 价格气泡贴在左边缘，和币安网页版的画线提示一个位置。
        context.font = "10px Inter, sans-serif";
        context.textBaseline = "middle";
        const textWidth = context.measureText(style.chipText).width + 8;
        const chipWidth = Math.min(textWidth, Math.max(40, width - 8));
        context.globalAlpha = 0.92;
        context.fillStyle = style.color;
        context.fillRect(4, segments.y1 - 7, chipWidth, 14);
        context.globalAlpha = 1;
        context.fillStyle = style.textColor;
        context.textAlign = "left";
        context.fillText(style.chipText, 8, segments.y1);
        if (style.selected) {
          context.beginPath();
          context.arc(width - 6, segments.y1, 4.5, 0, Math.PI * 2);
          context.fillStyle = style.textColor;
          context.fill();
          context.strokeStyle = style.color;
          context.lineWidth = 2;
          context.stroke();
        }
      } else {
        if (style.chipText) {
          context.font = "10px Inter, sans-serif";
          context.textBaseline = "middle";
          context.fillStyle = style.color;
          context.textAlign = "left";
          context.fillText(style.chipText, Math.min(segments.x1, segments.x2) + 6, Math.min(segments.y1, segments.y2) - 9);
        }
        if (style.selected) {
          const points: [number, number][] = [
            [segments.x1, segments.y1],
            [segments.x2, segments.y2],
          ];
          for (const [x, y] of points) {
            context.beginPath();
            context.arc(x, y, 4.5, 0, Math.PI * 2);
            context.fillStyle = style.textColor;
            context.fill();
            context.strokeStyle = style.color;
            context.lineWidth = 2;
            context.stroke();
          }
        }
      }
      context.restore();
    });
  }
}

class DrawingPaneView implements IPrimitivePaneView {
  private readonly _renderer: IPrimitivePaneRenderer;

  constructor(primitive: DrawingPrimitive) {
    this._renderer = new DrawingRenderer(primitive);
  }

  /** 画线是注释，永远压在 K 线之上：蜡烛的实体不许盖住一条支撑位。 */
  zOrder(): PrimitivePaneViewZOrder {
    return "top";
  }

  renderer(): IPrimitivePaneRenderer | null {
    return this._renderer;
  }
}

/** 一条画线 = 一个挂到主图 K 线序列上的图元。 */
export class DrawingPrimitive implements ISeriesPrimitive<Time> {
  private _drawing: Drawing;
  private _style: PrimitiveStyle;
  private _context: PrimitiveContext;
  private _requestUpdate: (() => void) | null = null;
  private readonly _views: readonly IPrimitivePaneView[];

  constructor(drawing: Drawing, style: PrimitiveStyle, context: PrimitiveContext) {
    this._drawing = drawing;
    this._style = style;
    this._context = context;
    this._views = [new DrawingPaneView(this)];
  }

  attached(parameter: SeriesAttachedParameter<Time>): void {
    this._requestUpdate = parameter.requestUpdate;
  }

  detached(): void {
    this._requestUpdate = null;
  }

  updateAllViews(): void {
    // 坐标是画的时候现算的，视口变了不需要缓存任何东西。
  }

  paneViews(): readonly IPrimitivePaneView[] {
    return this._views;
  }

  get drawing(): Drawing {
    return this._drawing;
  }

  get style(): PrimitiveStyle {
    return this._style;
  }

  segments(): Segments | null {
    return segmentsOf(this._drawing, this._context);
  }

  /** 复用同一个图元：只换数据与样式，不重新挂载（拖拽时每帧都会走这里）。 */
  update(drawing: Drawing, style: PrimitiveStyle, context: PrimitiveContext): void {
    this._drawing = drawing;
    this._style = style;
    this._context = context;
    this._requestUpdate?.();
  }
}
