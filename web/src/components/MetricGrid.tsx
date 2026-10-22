/** 一排指标卡。数字大小一致，涨跌用颜色区分，不给每张卡单独排版。 */

export interface MetricItem {
  label: string;
  value: string;
  tone?: "up" | "down" | "muted";
}

interface Props {
  items: MetricItem[];
  /** 紧凑模式：候选列表旁边那一排用得上。 */
  dense?: boolean;
}

export default function MetricGrid({ items, dense = false }: Props) {
  return (
    <div className={`metric-grid${dense ? " dense" : ""}`}>
      {items.map((item) => (
        <div key={item.label} className="metric-cell">
          <span className="metric-cell-label">{item.label}</span>
          <span className={`metric-cell-value${item.tone ? " " + item.tone : ""}`}>
            {item.value}
          </span>
        </div>
      ))}
    </div>
  );
}
