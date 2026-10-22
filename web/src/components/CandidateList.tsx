import type { Candidate } from "../types";
import { fmt } from "../format";

/** 候选策略列表。AI 量化页和策略实验室共用一份，状态徽标也在这里定。 */

export const CANDIDATE_STATE: Record<string, [string, string]> = {
  pending: ["待审", "pending"],
  approved: ["已批准", "approved"],
  rejected: ["已拒绝", "rejected"],
};

/** 三个指标，按候选记录里存过的回测结果取，没有就显示 —。 */
function metricOf(candidate: Candidate, key: string): string {
  const value = candidate.backtest?.[key];
  const number = Number(value);
  if (value === undefined || value === null || Number.isNaN(number)) return "—";
  return key === "total_return" ? `${fmt(number * 100, 2)}%` : fmt(number, 2);
}

export function candidateMetrics(candidate: Candidate): {
  label: string;
  value: string;
  tone?: "up" | "down";
}[] {
  const totalReturn = Number(candidate.backtest?.total_return ?? 0);
  return [
    {
      label: "收益",
      value: metricOf(candidate, "total_return"),
      tone: totalReturn > 0 ? "up" : totalReturn < 0 ? "down" : undefined,
    },
    { label: "回撤", value: metricOf(candidate, "max_drawdown") },
    { label: "夏普", value: metricOf(candidate, "sharpe") },
  ];
}

interface Props {
  items: Candidate[];
  selectedId: string;
  onPick: (candidate: Candidate) => void;
  empty: string;
}

export default function CandidateList({ items, selectedId, onPick, empty }: Props) {
  if (!items.length) {
    return <div className="cand-empty faint">{empty}</div>;
  }
  return (
    <ul className="cand-list">
      {items.map((candidate) => {
        const [label, tone] = CANDIDATE_STATE[candidate.status ?? "pending"] ?? [
          candidate.status ?? "待审",
          "pending",
        ];
        const metrics = candidateMetrics(candidate);
        return (
          <li key={candidate.id}>
            <button
              type="button"
              className={`cand-item${candidate.id === selectedId ? " active" : ""}`}
              onClick={() => onPick(candidate)}
            >
              <span className="cand-top">
                <b>{candidate.name || candidate.strategy_key || candidate.id}</b>
                <span className={`cand-state ${tone}`}>{label}</span>
              </span>
              <span className="cand-meta faint">
                {candidate.base_key || "—"}
                {candidate.model ? ` · ${candidate.model}` : ""}
              </span>
              <span className="cand-metrics">
                {metrics.map((item) => (
                  <span key={item.label} className={item.tone ?? ""}>
                    {item.label} {item.value}
                  </span>
                ))}
              </span>
            </button>
          </li>
        );
      })}
    </ul>
  );
}
