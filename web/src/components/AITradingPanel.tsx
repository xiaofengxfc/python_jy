import { useState } from "react";
import AITradePanel from "./AITradePanel";
import AIQuantPanel from "./AIQuantPanel";

interface Props {
  market: string;
  symbol: string;
  timeframe: string;
  onNavigate?: (page: "lab" | "backtest") => void;
}

/** One bottom tab, two independent products:
 * - AI 自主交易: strategy emits a signal, AI reviews/vetoes and sizes it.
 * - AI 自主量化: AI generates/tunes a strategy, backtest, approve, execute.
 */
export default function AITradingPanel({ market, symbol, timeframe, onNavigate }: Props) {
  const [tab, setTab] = useState<"trade" | "quant">("trade");

  return (
    <div className="ai-workspace">
      <div className="ai-mode-switch" role="tablist" aria-label="AI 交易模式">
        <button
          type="button"
          role="tab"
          aria-selected={tab === "trade"}
          className={tab === "trade" ? "active" : ""}
          onClick={() => setTab("trade")}
        >
          AI 自主交易
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === "quant"}
          className={tab === "quant" ? "active" : ""}
          onClick={() => setTab("quant")}
        >
          AI 自主量化
        </button>
      </div>
      {tab === "trade" ? (
        <AITradePanel market={market} symbol={symbol} timeframe={timeframe} />
      ) : (
        <AIQuantPanel
          market={market}
          symbol={symbol}
          timeframe={timeframe}
          onNavigate={onNavigate}
        />
      )}
    </div>
  );
}
