import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { fmt } from "../format";

interface Candidate {
  id: string;
  strategy_key: string;
  name: string;
  status: string;
  checks?: { ok?: boolean; errors?: string[] };
  backtest?: Record<string, number>;
  created_at?: number;
  note?: string;
  registered_key?: string;
  strategy_path?: string;
}

interface AutoStatus {
  running: boolean;
  config?: { symbol?: string; timeframe?: string; strategy?: string; mode?: string };
}

interface BacktestJob {
  status: string;
  error?: string;
  result?: { metrics?: Record<string, number> };
}

interface Props {
  market: string;
  symbol: string;
  timeframe: string;
  onNavigate?: (page: "lab" | "backtest") => void;
}

/** Candidate states come from the store in English; the list is Chinese. */
const STATUS_LABEL: Record<string, string> = {
  pending: "待批准",
  approved: "已批准",
  rejected: "已拒绝",
};

const statusLabel = (status: string) => STATUS_LABEL[status] ?? status;

/** AI 量化：候选策略 → 回测/优化 → 人工批准 → 部署并自动执行。
 * This is deliberately separate from AI 交易: that panel reviews a
 * strategy signal per candle, while this one owns the strategy lifecycle. */
export default function AIQuantPanel({ market, symbol, timeframe, onNavigate }: Props) {
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [code, setCode] = useState("");
  const [auto, setAuto] = useState<AutoStatus | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const load = async () => {
    try {
      const [list, status] = await Promise.all([
        api<{ items: Candidate[] }>("/api/ai/strategies/candidates"),
        api<AutoStatus>("/api/auto/status"),
      ]);
      setCandidates(list.items);
      setAuto(status);
      setSelectedId((current) => current || list.items[0]?.id || "");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  };

  useEffect(() => {
    void load();
  }, []);

  const selected = useMemo(
    () => candidates.find((item) => item.id === selectedId) ?? null,
    [candidates, selectedId],
  );

  useEffect(() => {
    if (!selected) {
      setCode("");
      return;
    }
    api<{ code: string }>(
      `/api/ai/strategies/candidates/${encodeURIComponent(selected.id)}`,
    )
      .then((data) => setCode(data.code ?? ""))
      .catch((caught) =>
        setError(caught instanceof Error ? caught.message : String(caught)),
      );
  }, [selected]);

  const deploy = async () => {
    if (!selected) return;
    setBusy("deploy");
    setError("");
    setNotice("");
    try {
      const key =
        selected.registered_key ||
        `ai_${selected.strategy_key}_${selected.id.slice(-6)}`
          .toLowerCase()
          .replace(/[^a-z0-9_]/g, "_")
          .slice(0, 64);
      if (!selected.registered_key) {
        // Legacy candidates were approved before the lab wrote local strategy
        // files. Register them once, using the same key transformation.
        const patched = code.replace(
          /key\s*=\s*["'][^"']*["']/,
          `key = "${key}"`,
        );
        if (patched === code) {
          throw new Error("候选源码缺少 key 属性，无法部署");
        }
        await api("/api/strategies", {
          method: "POST",
          body: JSON.stringify({ key, code: patched, create: true }),
        });
      }

      setNotice(
        selected.registered_key
          ? "使用批准时生成的本地策略，正在回测…"
          : "已注册策略，正在回测…",
      );
      const job = await api<{ job_id: string }>("/api/backtest/run", {
        method: "POST",
        body: JSON.stringify({
          market,
          symbol,
          timeframe,
          strategy: key,
          params: {},
          bars: 800,
          allow_short: market !== "spot",
          save: false,
        }),
      });
      let outcome: BacktestJob | null = null;
      for (let attempt = 0; attempt < 120; attempt += 1) {
        await new Promise((resolve) => window.setTimeout(resolve, 1000));
        const current = await api<BacktestJob>(
          `/api/backtest/jobs/${encodeURIComponent(job.job_id)}`,
        );
        if (current.status === "done") {
          outcome = current;
          break;
        }
        if (current.status === "error" || current.status === "failed") {
          throw new Error(current.error || "回测失败");
        }
      }
      if (!outcome) throw new Error("回测超时，已阻止部署");
      const metrics = outcome.result?.metrics ?? {};
      const tradeCount = Number(metrics.trades ?? 0);
      const totalReturn = Number(metrics.total_return ?? 0);
      await api(
        `/api/ai/strategies/candidates/${encodeURIComponent(selected.id)}/backtest`,
        {
          method: "POST",
          body: JSON.stringify({
            metrics,
            symbol,
            timeframe,
            source: "btrader",
          }),
        },
      );
      if (tradeCount <= 0) {
        throw new Error("回测没有成交，已阻止部署。请调整策略后重试");
      }
      if (totalReturn <= 0) {
        throw new Error(
          `回测收益 ${(totalReturn * 100).toFixed(2)}%，未通过正收益门槛，已阻止部署`,
        );
      }

      await api("/api/auto/start", {
        method: "POST",
        body: JSON.stringify({
          symbol,
          market,
          timeframe,
          strategy: key,
          params: {},
          position_pct: 1,
          leverage: 1,
          poll_seconds: 10,
          confirm_live: false,
        }),
      });
      setNotice(`已部署 ${selected.name}（${key}）并开始自动执行，下一根 K 线才会下单。`);
      await load();
    } catch (caught) {
      setNotice("");
      await load();
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy("");
    }
  };

  const stopAuto = async () => {
    setBusy("stop");
    setError("");
    setNotice("");
    try {
      await api("/api/auto/stop", { method: "POST" });
      setNotice("已停止量化自动执行。");
      await load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy("");
    }
  };

  const approved = candidates.filter((item) => item.status === "approved");
  const backtested = selected?.backtest && Object.keys(selected.backtest).length > 0;
  const canDeploy = Boolean(
    selected &&
      selected.status === "approved" &&
      selected.checks?.ok &&
      code &&
      !auto?.running,
  );

  return (
    <div className="stack ai-quant">
      <div className="ai-head">
        <span className="card-title">AI 量化</span>
        <span className="faint" style={{ fontSize: 11 }}>
          生成策略 → 回测/优化 → 人工批准 → 自动执行
        </span>
        <span className="spacer" />
        <button type="button" className="btn-sm" onClick={() => onNavigate?.("lab")}>
          打开策略实验室
        </button>
        <button type="button" className="btn-sm" onClick={() => onNavigate?.("backtest")}>
          去回测
        </button>
        <button type="button" className="btn-sm" onClick={() => void load()}>
          刷新
        </button>
      </div>

      {error && <div className="banner error">{error}</div>}
      {notice && <div className="banner ok">{notice}</div>}

      <div className="ai-section">
        <div className="stack">
          <div className="card">
            <div className="card-head">
              <span className="card-title">候选策略</span>
              <span className="spacer" />
              <span className="faint" style={{ fontSize: 11 }}>
                {approved.length} 个已批准 / {candidates.length} 个候选
              </span>
            </div>
            <div className="card-body stack">
              {candidates.length === 0 ? (
                <div className="empty">
                  还没有候选。先去策略实验室让模型生成一个，再回测并批准。
                </div>
              ) : (
                <div className="ai-quant-list">
                  {candidates.map((item) => (
                    <button
                      key={item.id}
                      type="button"
                      className={`ai-quant-item ${item.id === selectedId ? "active" : ""}`}
                      onClick={() => setSelectedId(item.id)}
                    >
                      <span className="ai-quant-name">{item.name || item.strategy_key}</span>
                      <span className={`ai-quant-state ${item.status}`}>
                        {statusLabel(item.status)}
                      </span>
                      <span className="faint">
                        {item.backtest && Object.keys(item.backtest).length
                          ? `回测收益 ${fmt(Number(item.backtest.total_return ?? 0) * 100, 2)}%`
                          : "未回测"}
                      </span>
                    </button>
                  ))}
                </div>
              )}
            </div>
          </div>

          <div className="card">
            <div className="card-head">
              <span className="card-title">部署</span>
              <span className="spacer" />
              <span className="faint" style={{ fontSize: 11 }}>
                {selected ? selected.id : "未选择"}
              </span>
            </div>
            <div className="card-body stack">
              <div className="ai-quant-checks">
                <span className={selected?.checks?.ok ? "up" : "down"}>
                  代码检查 {selected?.checks?.ok ? "通过" : "未通过"}
                </span>
                <span className={backtested ? "up" : "down"}>
                  回测 {backtested ? "已完成" : "未完成（建议先回测）"}
                </span>
                <span className={selected?.status === "approved" ? "up" : "down"}>
                  审核 {selected?.status === "approved" ? "已批准" : "未批准"}
                </span>
              </div>
              <pre className="ai-quant-code">{code ? code.slice(0, 1200) : "选择候选后查看源码…"}</pre>
              <div className="ai-quant-actions">
                <button
                  type="button"
                  className="btn-sm primary"
                  disabled={busy !== "" || !canDeploy}
                  onClick={() => void deploy()}
                >
                  {busy === "deploy" ? "部署中…" : "部署并自动执行"}
                </button>
                <button
                  type="button"
                  className="btn-sm"
                  disabled={busy !== "" || !auto?.running}
                  onClick={() => void stopAuto()}
                >
                  {busy === "stop" ? "停止中…" : "停止自动执行"}
                </button>
              </div>
              <div className="faint" style={{ fontSize: 11 }}>
                {auto?.running
                  ? `正在执行：${auto.config?.strategy ?? "—"} · ${auto.config?.symbol ?? ""} · ${auto.config?.timeframe ?? ""}`
                  : "未运行。部署前必须完成回测并由人工在实验室批准。"}
              </div>
            </div>
          </div>
        </div>

        <div className="stack">
          <div className="card">
            <div className="card-head">
              <span className="card-title">流程说明</span>
            </div>
            <div className="card-body ai-quant-steps">
              <div><b>1. 生成</b><span>模型读取策略模板和行情特征，写出候选源码。</span></div>
              <div><b>2. 回测</b><span>候选只在沙箱检查，回测/优化通过后才可批准。</span></div>
              <div><b>3. 批准</b><span>人工在策略实验室复核源码和指标，批准不是自动注册。</span></div>
              <div><b>4. 执行</b><span>本页把批准候选注册为正式策略，并启动自动交易（真实下单）。</span></div>
            </div>
          </div>
          <div className="card">
            <div className="card-head">
              <span className="card-title">当前选择</span>
            </div>
            <div className="card-body stack">
              <div className="preview-row">
                <span className="faint">策略</span>
                <b>{selected?.name || "—"}</b>
              </div>
              <div className="preview-row">
                <span className="faint">市场</span>
                <b>{market} · {symbol} · {timeframe}</b>
              </div>
              <div className="preview-row">
                <span className="faint">回测收益</span>
                <b className={Number(selected?.backtest?.total_return ?? 0) >= 0 ? "up" : "down"}>
                  {backtested
                    ? `${fmt(Number(selected?.backtest?.total_return ?? 0) * 100, 2)}%`
                    : "未回测"}
                </b>
              </div>
              <div className="preview-row">
                <span className="faint">最大回撤</span>
                <b>
                  {backtested
                    ? `${fmt(Number(selected?.backtest?.max_drawdown ?? 0) * 100, 2)}%`
                    : "—"}
                </b>
              </div>
              <div className="preview-row">
                <span className="faint">夏普 / 成交</span>
                <b>
                  {backtested
                    ? `${fmt(Number(selected?.backtest?.sharpe ?? 0), 2)} / ${fmt(
                        Number(selected?.backtest?.trades ?? 0),
                        0,
                      )}`
                    : "—"}
                </b>
              </div>
              <div className="preview-row">
                <span className="faint">审核备注</span>
                <b>{selected?.note || "—"}</b>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
