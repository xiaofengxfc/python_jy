import type { ReactNode } from "react";
import { MARKET_LABELS } from "../format";
import type { PageKey } from "../App";

interface Props {
  page: PageKey;
  onPick: (page: PageKey) => void;
  onOpenLog: () => void;
  connected: boolean;
  network: string;
  market: string;
  equity: number | null;
  errorCount: number;
}

const ICONS: Record<PageKey, ReactNode> = {
  trading: (
    <path
      d="M2 11.5 5.5 8l2.5 2.5L13.5 4.5M10 4.5h3.5V8"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinecap="round"
      strokeLinejoin="round"
    />
  ),
  portfolio: (
    <>
      <rect
        x="2.5"
        y="3.5"
        width="11"
        height="9"
        rx="1.5"
        stroke="currentColor"
        strokeWidth="1.4"
      />
      <path d="M2.5 6.5h11" stroke="currentColor" strokeWidth="1.4" />
    </>
  ),
  backtest: (
    <path
      d="M3 13V8M8 13V3M13 13V6"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinecap="round"
    />
  ),
  lab: (
    <>
      <path
        d="M6 2.5h4M6.5 2.5v3.1L3.9 11.1a1.4 1.4 0 0 0 1.2 2.2h5.8a1.4 1.4 0 0 0 1.2-2.2L9.5 5.6V2.5"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path d="M5.1 9.5h5.8" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
    </>
  ),
  settings: (
    <>
      <circle cx="8" cy="8" r="2.2" stroke="currentColor" strokeWidth="1.4" />
      <path
        d="M8 2v1.4M8 12.6V14M2 8h1.4M12.6 8H14M3.8 3.8l1 1M11.2 11.2l1 1M12.2 3.8l-1 1M4.8 11.2l-1 1"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
      />
    </>
  ),
};

const LABELS: [PageKey, string][] = [
  ["trading", "交易"],
  ["portfolio", "资产"],
  ["backtest", "回测"],
  ["lab", "实验室"],
  ["settings", "设置"],
];

export default function Sidebar({
  page,
  onPick,
  onOpenLog,
  connected,
  network,
  market,
  equity,
  errorCount,
}: Props) {
  const netLabel = network === "mainnet" ? "主网" : "测试网";
  const marketLabel = MARKET_LABELS[market] ?? market;
  const equityText = equity === null
    ? "—"
    : equity.toLocaleString("en-US", {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      });

  return (
    <aside className="sidebar">
      <div className="brand" title="biTrader · 币安交易工作台">
        <div className="brand-mark">bT</div>
      </div>

      <nav className="nav">
        {LABELS.map(([key, label]) => (
          <button
            key={key}
            type="button"
            className="nav-item tip"
            data-tip={label}
            aria-label={label}
            aria-current={page === key ? "page" : undefined}
            onClick={() => onPick(key)}
          >
            <svg className="nav-icon" viewBox="0 0 16 16" fill="none" aria-hidden="true">
              {ICONS[key]}
            </svg>
          </button>
        ))}
      </nav>

      <div className="sidebar-foot">
        <button
          type="button"
          className="icon-btn tip"
          data-tip={`日志${errorCount > 0 ? `\n${errorCount} 个错误` : ""}`}
          aria-label="报错日志"
          onClick={onOpenLog}
        >
          <svg viewBox="0 0 16 16" width="18" height="18" fill="none" aria-hidden="true">
            <path
              d="M3.5 2.5h9v11h-9z"
              stroke="currentColor"
              strokeWidth="1.4"
              strokeLinejoin="round"
            />
            <path
              d="M5.5 5.5h5M5.5 8h5M5.5 10.5h3"
              stroke="currentColor"
              strokeWidth="1.4"
              strokeLinecap="round"
            />
          </svg>
          {errorCount > 0 && <span className="badge">{errorCount > 99 ? "99+" : errorCount}</span>}
        </button>

        <span
          className={`tip ${connected ? "dot live" : "dot"}`}
          data-tip={`${connected ? "行情已连接" : "行情重连中"}\n${netLabel} · ${marketLabel}\n权益 ${equityText}`}
        />
      </div>
    </aside>
  );
}
