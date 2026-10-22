import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import MarkdownText from "./MarkdownText";
import type { AIAnalysis, AILevel, AIStatus } from "../types";

interface Props {
  market: string;
  symbol: string;
  timeframe: string;
  /** The analysis' price levels are lifted to the chart, which draws them over
   * the candles. Kept in the parent so the chart and this list never disagree. */
  onLevels: (levels: AILevel[], symbol: string) => void;
  /** Show or hide the lines without losing them. */
  showLevels: boolean;
  onShowLevels: (value: boolean) => void;
}

const KIND_LABEL: Record<AILevel["kind"], string> = {
  support: "支撑",
  resistance: "阻力",
  stop: "止损",
};

function pct(value: number): string {
  const sign = value >= 0 ? "+" : "";
  return `${sign}${(value * 100).toFixed(2)}%`;
}

/** The assistant's read on the pair currently on the chart.
 *
 * Everything here is a manual click: an analysis costs money and a trader who
 * is watching the chart does not want the model re-reading it on a timer. The
 * result is deliberately split — the prose goes in this panel, the numbers go
 * onto the chart — because that is what makes the two comparable at a glance.
 */
export default function AIPanel({
  market,
  symbol,
  timeframe,
  onLevels,
  showLevels,
  onShowLevels,
}: Props) {
  const [status, setStatus] = useState<AIStatus | null>(null);
  const [analysis, setAnalysis] = useState<AIAnalysis | null>(null);
  const [busy, setBusy] = useState<"analyze" | "news" | "review" | "">("");
  const [error, setError] = useState("");
  /** Second opinion on the headlines, in the same card as the chart read. */
  const [news, setNews] = useState<{ text: string; overall: number } | null>(null);
  const [review, setReview] = useState("");

  useEffect(() => {
    api<AIStatus>("/api/ai/status")
      .then(setStatus)
      .catch(() => setStatus(null));
  }, []);

  // A new pair means the old lines belong to a different instrument: keeping
  // them would draw BTC's support across an ETH chart.
  useEffect(() => {
    setAnalysis(null);
    setNews(null);
    setReview("");
    setError("");
    onLevels([], "");
    // `onLevels` is stable in the parent; re-running on it would clear on every
    // render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [market, symbol]);

  const analyze = useCallback(async () => {
    if (!symbol) return;
    setBusy("analyze");
    setError("");
    try {
      const result = await api<AIAnalysis>("/api/ai/analyze", {
        method: "POST",
        body: JSON.stringify({
          market,
          symbol,
          timeframes: ["15m", timeframe, "4h"].filter(
            (item, index, all) => all.indexOf(item) === index,
          ),
          bars: 200,
        }),
      });
      setAnalysis(result);
      onLevels(result.levels ?? [], result.symbol);
      if (!result.levels?.length) {
        setError("模型没有给出可用的价位，图上不会画线。");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy("");
    }
  }, [market, symbol, timeframe, onLevels]);

  const analyzeNews = useCallback(async () => {
    setBusy("news");
    setError("");
    try {
      const result = await api<{ text: string; overall: number }>("/api/ai/news", {
        method: "POST",
        body: JSON.stringify({ symbol, limit: 15 }),
      });
      setNews({ text: result.text, overall: result.overall ?? 0 });
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy("");
    }
  }, [symbol]);

  const reviewPosition = useCallback(async () => {
    setBusy("review");
    setError("");
    try {
      const result = await api<{ text: string }>("/api/ai/review", {
        method: "POST",
        body: JSON.stringify({ market, symbol, timeframe }),
      });
      setReview(result.text);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy("");
    }
  }, [market, symbol, timeframe]);

  const levels = analysis?.levels ?? [];

  if (!status?.enabled) {
    return (
      <div className="card">
        <div className="card-head">
          <span className="card-title">AI 分析</span>
        </div>
        <div className="faint" style={{ fontSize: "var(--fs-13)", lineHeight: 1.6 }}>
          未启用。到「设置 → AI 助手」选供应商，填入 API Key。
          之后可以在图上标支撑、阻力、止损，也能让模型读多周期 K 线。
        </div>
      </div>
    );
  }

  return (
    <div className="card">
      <div className="card-head">
        <span className="card-title">AI 分析</span>
        <span className="spacer" />
        <span className="faint" style={{ fontSize: "var(--fs-12)"}}>
          {status.model}
          {status.calls_today > 0 ? ` · 今日 ${status.calls_today} 次` : ""}
        </span>
      </div>

      {!status.configured && (
        <div className="banner warn" role="status">
          还没有可用的 API Key，请到设置里填写。
        </div>
      )}

      <div className="ai-actions">
        <button
          type="button"
          className="btn primary"
          disabled={!status.configured || busy !== "" || !symbol}
          onClick={() => void analyze()}
        >
          {busy === "analyze" ? "分析中…" : "分析当前交易对"}
        </button>
        <button
          type="button"
          className="btn"
          disabled={!status.configured || busy !== ""}
          onClick={() => void analyzeNews()}
        >
          {busy === "news" ? "读新闻…" : "新闻情绪"}
        </button>
        {status.share_position && (
          <button
            type="button"
            className="btn"
            disabled={!status.configured || busy !== ""}
            onClick={() => void reviewPosition()}
          >
            {busy === "review" ? "复核中…" : "持仓复核"}
          </button>
        )}
      </div>

      {error && <div className="banner error">{error}</div>}

      {analysis && (
        <>
          <div className="ai-meta faint">
            {analysis.timeframes.join(" · ")} · 现价 {analysis.price.toFixed(2)}
            {analysis.cached ? " · 缓存结果" : ""}
            {analysis.tokens ? ` · ${analysis.tokens} tokens` : ""}
          </div>
          <MarkdownText text={analysis.text} />
          {levels.length > 0 && (
            <div className="ai-levels">
              <div className="ai-levels-head">
                <span className="faint" style={{ fontSize: "var(--fs-12)"}}>
                  图上已画 {levels.length} 条线
                </span>
                <span className="spacer" />
                <button type="button" className="link-btn" onClick={() => onShowLevels(!showLevels)}>
                  {showLevels ? "隐藏" : "显示"}
                </button>
              </div>
              <ul className="ai-level-list">
                {levels.map((level) => (
                  <li key={`${level.kind}-${level.price}`} className={`ai-level ${level.kind}`}>
                    <span className="ai-level-kind">{KIND_LABEL[level.kind]}</span>
                    <span className="num">{level.price.toFixed(2)}</span>
                    <span className="faint">{pct(level.distance)}</span>
                    {level.label && <span className="faint ai-level-note">{level.label}</span>}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </>
      )}

      {news && (
        <div className="ai-block">
          <div className="ai-block-head">
            新闻情绪
            <span className={`ai-score ${news.overall >= 0 ? "up" : "down"}`}>
              {news.overall >= 0 ? "+" : ""}
              {news.overall.toFixed(2)}
            </span>
          </div>
          <MarkdownText text={news.text} />
        </div>
      )}

      {review && (
        <div className="ai-block">
          <div className="ai-block-head">持仓复核</div>
          <MarkdownText text={review} />
        </div>
      )}
    </div>
  );
}
