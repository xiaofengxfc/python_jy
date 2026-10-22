/** 一排指标卡。数字大小一致，涨跌用颜色区分，不给每张卡单独排版。 */

export interface MetricItem {
  label: string;
  value: string;
  tone?: "up" | "down" | "muted";
}

interface Props {
  items: MetricItem[];
}

export default function MetricGrid({ items }: Props) {
  return (
    <div className="metric-grid">
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
