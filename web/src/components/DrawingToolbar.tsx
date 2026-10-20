import type { Drawing, DrawingTool } from "./drawings";

interface Props {
  tool: DrawingTool;
  onChange: (tool: DrawingTool) => void;
  /** The drawing the pointer selected, if any: enables the delete action. */
  selected: Drawing | null;
  count: number;
  onDelete: () => void;
  onClear: () => void;
  /** Optional show/hide switch for everything on the chart. */
  visible?: boolean;
  onToggleVisible?: () => void;
}

const TOOLS: { value: DrawingTool; label: string; title: string }[] = [
  { value: "select", label: "选择", title: "选择并拖动已有画线；拖空白处平移图表" },
  { value: "hline", label: "水平线", title: "点击图表放置一条水平线" },
  { value: "trend", label: "趋势线", title: "按下并拖动，画一条两点趋势线" },
];

/** Drawing-board toolbar that sits above the chart.
 *
 * Stateless on purpose: the parent owns the tool, the selection and the list,
 * so a chart redraw (zoom, tick, resize) never resets what the user is doing.
 */
export default function DrawingToolbar({
  tool,
  onChange,
  selected,
  count,
  onDelete,
  onClear,
  visible,
  onToggleVisible,
}: Props) {
  return (
    <div className="card-head indicator-bar" role="toolbar" aria-label="图表画线">
      <span className="faint" style={{ fontSize: 11 }}>
        画线
      </span>
      <div className="segmented">
        {TOOLS.map((item) => (
          <button
            key={item.value}
            type="button"
            aria-pressed={tool === item.value}
            title={item.title}
            onClick={() => onChange(item.value)}
          >
            {item.label}
          </button>
        ))}
      </div>
      <span className="faint" style={{ fontSize: 11 }}>
        {count > 0 ? `${count} 条` : "无"}
      </span>
      <span className="spacer" />
      {onToggleVisible && (
        <button type="button" className="link-btn" onClick={onToggleVisible}>
          {visible === false ? "显示画线" : "隐藏画线"}
        </button>
      )}
      {selected && (
        <button type="button" className="link-btn" onClick={onDelete}>
          删除选中
        </button>
      )}
      {count > 0 && (
        <button type="button" className="link-btn" onClick={onClear}>
          清空
        </button>
      )}
    </div>
  );
}
