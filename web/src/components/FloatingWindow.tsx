import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type CSSProperties,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import "./FloatingWindow.css";

/** A rectangle in viewport coordinates: `x`/`y` is the top-left corner and
 * `w`/`h` is the size, all in CSS pixels. */
export interface FloatingWindowRect {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface FloatingWindowProps {
  /** Shown in the title bar; also the dialog's accessible name. */
  title: string;
  /** Nothing renders while this is false. Geometry survives through storage. */
  open: boolean;
  /** Called by the title-bar close button. The parent owns the open state. */
  onClose: () => void;
  /** localStorage key for position and size. One key per window: two windows
   * sharing a key would fight over the same rectangle. */
  storageKey: string;
  /** The first rectangle, before the user moves or resizes the window. Any
   * missing field falls back to a centred window that fits the viewport. */
  defaultRect?: Partial<FloatingWindowRect>;
  /** Resize limits; ignored when the viewport itself is smaller. */
  minWidth?: number;
  minHeight?: number;
  children: ReactNode;
  /** Pinned under the scrollable body, e.g. actions that must stay visible. */
  footer?: ReactNode;
}

const VIEWPORT_MARGIN = 16;
/** Below this width the window is pinned near full width instead of floating,
 * so it cannot be dragged off a phone screen. */
const NARROW_WIDTH = 700;
const NARROW_MARGIN = 8;
const DEFAULT_WIDTH = 560;
const DEFAULT_HEIGHT = 500;
const DEFAULT_MIN_WIDTH = 320;
const DEFAULT_MIN_HEIGHT = 220;
/** Clicks on these elements inside the title bar must not start a drag. */
const NO_DRAG_SELECTOR = "button, a, input, textarea, select, [data-fw-no-drag]";

type DragMode = "move" | "resize";

interface DragState {
  mode: DragMode;
  pointerId: number;
  /** Pointer position and geometry when the drag started. */
  startX: number;
  startY: number;
  rect: FloatingWindowRect;
}

interface StoredState {
  rect: FloatingWindowRect;
  collapsed: boolean;
}

function clamp(value: number, low: number, high: number): number {
  return Math.min(Math.max(low, value), high);
}

function finiteOr(value: number | undefined, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function viewportSize(): { w: number; h: number } {
  if (typeof window === "undefined") return { w: 1024, h: 768 };
  const width = window.innerWidth || document.documentElement.clientWidth || 1024;
  const height = window.innerHeight || document.documentElement.clientHeight || 768;
  return { w: Math.max(1, width), h: Math.max(1, height) };
}

/** Clamp a rectangle into the viewport with a margin on every side.
 *
 * Every path that can change the geometry ends here: restoring a rectangle
 * saved on a bigger monitor, dragging, resizing and a browser-window resize.
 * On narrow viewports the width is forced to nearly full width; on all
 * viewports the size is capped so at least the margin ring stays grabbable.
 */
function fitToViewport(
  rect: FloatingWindowRect,
  size: { w: number; h: number },
  minWidth: number,
  minHeight: number,
): FloatingWindowRect {
  const narrow = size.w < NARROW_WIDTH;
  const margin = narrow ? NARROW_MARGIN : VIEWPORT_MARGIN;
  const maxW = Math.max(160, size.w - margin * 2);
  const maxH = Math.max(96, size.h - margin * 2);
  const w = narrow ? maxW : clamp(rect.w, Math.min(minWidth, maxW), maxW);
  const h = clamp(rect.h, Math.min(minHeight, maxH), maxH);
  const x = narrow ? margin : clamp(rect.x, margin, Math.max(margin, size.w - margin - w));
  const y = clamp(rect.y, margin, Math.max(margin, size.h - margin - h));
  return { x: Math.round(x), y: Math.round(y), w: Math.round(w), h: Math.round(h) };
}

function fallbackRect(
  overrides: Partial<FloatingWindowRect> | undefined,
  size: { w: number; h: number },
): FloatingWindowRect {
  const w = finiteOr(overrides?.w, Math.min(DEFAULT_WIDTH, size.w - VIEWPORT_MARGIN * 2));
  const h = finiteOr(overrides?.h, Math.min(DEFAULT_HEIGHT, size.h - VIEWPORT_MARGIN * 2));
  const x = finiteOr(overrides?.x, (size.w - w) / 2);
  // Slightly above the middle: the eye lands there first and the title bar
  // stays clear of the cramped bottom edge.
  const y = finiteOr(overrides?.y, Math.max(VIEWPORT_MARGIN, (size.h - h) / 3));
  return { x, y, w, h };
}

function finiteNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** Read the saved rectangle. Anything unexpected — no storage, disabled
 * storage, truncated or hand-edited JSON, a null, a string, a zero width —
 * degrades to "nothing saved", never to a crash or a broken layout. */
function readStoredState(storageKey: string): StoredState | null {
  try {
    const raw = window.localStorage.getItem(storageKey);
    if (!raw) return null;
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return null;
    const data = parsed as Record<string, unknown>;
    const x = finiteNumber(data.x);
    const y = finiteNumber(data.y);
    const w = finiteNumber(data.w);
    const h = finiteNumber(data.h);
    if (x === null || y === null || w === null || h === null) return null;
    if (w <= 0 || h <= 0) return null;
    return { rect: { x, y, w, h }, collapsed: data.collapsed === true };
  } catch {
    return null;
  }
}

/** Geometry is a convenience: a quota error or private mode must never keep
 * the panel from opening. */
function writeStoredState(storageKey: string, state: StoredState): void {
  try {
    window.localStorage.setItem(storageKey, JSON.stringify(state));
  } catch {
    /* ignored on purpose */
  }
}

/** A draggable, resizable, collapsible panel that floats above the page.
 *
 * Built for content that is unreadable in its in-page card — a long AI
 * analysis is the first customer: the window scrolls, grows and can be moved
 * aside instead of being cut off.
 *
 * Geometry is persisted per `storageKey`. Every restore is clamped into the
 * viewport, so a rectangle saved on a wide monitor does not open off screen on
 * a laptop, and a browser-window resize pulls a half-visible window back.
 *
 * Rendering goes through a portal to `document.body`: the card that opens the
 * window may have `overflow: hidden`, and a fixed-position panel nested in it
 * would be at the mercy of that ancestor's painting rules.
 *
 * Usage:
 *   <FloatingWindow
 *     open={open}
 *     onClose={() => setOpen(false)}
 *     title="AI 分析"
 *     storageKey="btrader.float.ai-analysis.v1"
 *     defaultRect={{ w: 560, h: 520 }}
 *     footer={<button className="btn-sm" onClick={...}>重新分析</button>}
 *   >
 *     <div className="ai-text">{analysis.text}</div>
 *   </FloatingWindow>
 */
export default function FloatingWindow({
  title,
  open,
  onClose,
  storageKey,
  defaultRect,
  minWidth = DEFAULT_MIN_WIDTH,
  minHeight = DEFAULT_MIN_HEIGHT,
  children,
  footer,
}: FloatingWindowProps) {
  // The stored value is read once per mount; like every other window setting it
  // is not expected to change while the component is alive.
  const [stored] = useState<StoredState | null>(() => readStoredState(storageKey));
  const [rect, setRect] = useState<FloatingWindowRect>(() => {
    const size = viewportSize();
    const base = stored?.rect ?? fallbackRect(defaultRect, size);
    return fitToViewport(base, size, minWidth, minHeight);
  });
  const [collapsed, setCollapsed] = useState<boolean>(() => stored?.collapsed ?? false);
  /** Drag in flight: cursor, pointer capture and the starting geometry. */
  const [dragMode, setDragMode] = useState<DragMode | null>(null);

  const dragRef = useRef<DragState | null>(null);
  /** Mirrors `rect` for event handlers and effects that must not wait for a
   * render (pointer moves, resize listeners, the storage debounce). */
  const rectRef = useRef<FloatingWindowRect>(rect);

  const applyRect = useCallback((next: FloatingWindowRect) => {
    rectRef.current = next;
    setRect(next);
  }, []);

  // Persist after the geometry settles. Dragging writes on pointer-up as well,
  // so closing the window mid-drag still keeps the last position.
  useEffect(() => {
    if (!open) return;
    const timer = window.setTimeout(() => {
      writeStoredState(storageKey, { rect: rectRef.current, collapsed });
    }, 250);
    return () => window.clearTimeout(timer);
  }, [rect, collapsed, open, storageKey]);

  // A browser-window resize can leave the panel partly outside: pull it back in.
  useEffect(() => {
    const handleResize = () => {
      applyRect(fitToViewport(rectRef.current, viewportSize(), minWidth, minHeight));
    };
    window.addEventListener("resize", handleResize);
    return () => window.removeEventListener("resize", handleResize);
  }, [applyRect, minWidth, minHeight]);

  // Expanding from the title bar must not restore a rectangle that no longer
  // fits the current viewport.
  useEffect(() => {
    if (collapsed) return;
    applyRect(fitToViewport(rectRef.current, viewportSize(), minWidth, minHeight));
  }, [collapsed, applyRect, minWidth, minHeight]);

  const beginDrag = useCallback(
    (mode: DragMode, event: ReactPointerEvent<HTMLElement>) => {
      if (event.button !== 0 || !event.isPrimary) return;
      const element = event.target instanceof Element ? event.target : null;
      if (element?.closest(NO_DRAG_SELECTOR)) return;
      // Cancelling the pointer-down is what keeps a drag from selecting the
      // title text or dragging the SVG icons around.
      event.preventDefault();
      const currentTarget = event.currentTarget;
      try {
        currentTarget.setPointerCapture(event.pointerId);
      } catch {
        /* capture is an optimisation; the handlers still fire without it */
      }
      dragRef.current = {
        mode,
        pointerId: event.pointerId,
        startX: event.clientX,
        startY: event.clientY,
        rect: rectRef.current,
      };
      setDragMode(mode);
    },
    [],
  );

  const handleDragMove = useCallback(
    (event: ReactPointerEvent<HTMLElement>) => {
      const drag = dragRef.current;
      if (!drag || drag.pointerId !== event.pointerId) return;
      event.preventDefault();
      const dx = event.clientX - drag.startX;
      const dy = event.clientY - drag.startY;
      const wanted: FloatingWindowRect =
        drag.mode === "move"
          ? { ...drag.rect, x: drag.rect.x + dx, y: drag.rect.y + dy }
          : { ...drag.rect, w: drag.rect.w + dx, h: drag.rect.h + dy };
      // Fitting on every move keeps the whole window inside the viewport while
      // the pointer is being dragged, not just when the drag ends.
      applyRect(fitToViewport(wanted, viewportSize(), minWidth, minHeight));
    },
    [applyRect, minWidth, minHeight],
  );

  const finishDrag = useCallback(
    (event: ReactPointerEvent<HTMLElement>) => {
      const drag = dragRef.current;
      if (!drag || drag.pointerId !== event.pointerId) return;
      dragRef.current = null;
      setDragMode(null);
      try {
        if (event.currentTarget.hasPointerCapture(event.pointerId)) {
          event.currentTarget.releasePointerCapture(event.pointerId);
        }
      } catch {
        /* the browser already released it */
      }
      writeStoredState(storageKey, { rect: rectRef.current, collapsed });
    },
    [collapsed, storageKey],
  );

  const handleLostCapture = useCallback(() => {
    // A capture can also disappear without a pointer-up (context menu, window
    // blur). Dropping the state stops a later pointermove from teleporting the
    // window.
    if (!dragRef.current) return;
    dragRef.current = null;
    setDragMode(null);
  }, []);

  const toggleCollapsed = useCallback(() => {
    const next = !collapsed;
    setCollapsed(next);
    writeStoredState(storageKey, { rect: rectRef.current, collapsed: next });
  }, [collapsed, storageKey]);

  if (!open || typeof document === "undefined") return null;

  const style: CSSProperties = {
    left: rect.x,
    top: rect.y,
    width: rect.w,
    height: collapsed ? "auto" : rect.h,
  };

  const windowClass = [
    "floating-window",
    collapsed ? "collapsed" : "",
    dragMode ? "dragging" : "",
  ]
    .filter(Boolean)
    .join(" ");

  return createPortal(
    <div className={windowClass} style={style} role="dialog" aria-label={title}>
      <div
        className="fw-head"
        onPointerDown={(event) => beginDrag("move", event)}
        onPointerMove={handleDragMove}
        onPointerUp={finishDrag}
        onPointerCancel={finishDrag}
        onLostPointerCapture={handleLostCapture}
      >
        <span className="fw-title" title={title}>
          {title}
        </span>
        <span className="fw-spacer" />
        <button
          type="button"
          className="fw-btn"
          title={collapsed ? "展开" : "最小化"}
          aria-label={collapsed ? "展开窗口" : "最小化窗口"}
          onClick={toggleCollapsed}
        >
          <svg viewBox="0 0 16 16" width="15" height="15" fill="none" aria-hidden="true">
            {collapsed ? (
              <path
                d="m4.5 6.5 3.5 3.5 3.5-3.5"
                stroke="currentColor"
                strokeWidth="1.4"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            ) : (
              <path
                d="m4.5 9.5 3.5-3.5 3.5 3.5"
                stroke="currentColor"
                strokeWidth="1.4"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            )}
          </svg>
        </button>
        <button
          type="button"
          className="fw-btn"
          title="关闭"
          aria-label="关闭窗口"
          onClick={onClose}
        >
          <svg viewBox="0 0 16 16" width="15" height="15" fill="none" aria-hidden="true">
            <path
              d="m4 4 8 8M12 4l-8 8"
              stroke="currentColor"
              strokeWidth="1.4"
              strokeLinecap="round"
            />
          </svg>
        </button>
      </div>

      {!collapsed && (
        <>
          {/* The body owns the scrollbar: model replies can be long, and
              cutting them off was the bug this window exists to fix. */}
          <div className="fw-body">{children}</div>
          {footer != null && <div className="fw-foot">{footer}</div>}
          <div
            className="fw-resize"
            aria-hidden="true"
            onPointerDown={(event) => beginDrag("resize", event)}
            onPointerMove={handleDragMove}
            onPointerUp={finishDrag}
            onPointerCancel={finishDrag}
            onLostPointerCapture={handleLostCapture}
          />
        </>
      )}
    </div>,
    document.body,
  );
}
