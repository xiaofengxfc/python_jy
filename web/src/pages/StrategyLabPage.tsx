import { useCallback, useEffect, useRef, useState } from "react";
import { api, query } from "../api";
import { TIMEFRAMES, fmt } from "../format";
import type { Strategy, SymbolInfo } from "../types";

/* --------------------------------------------------------------- types */
/* The lab endpoints live outside `types.ts` because they are still new: only
   this page consumes them, and every field is optional so an older backend
   (or a partially written record) cannot break the screen. */

interface CandidateIssue {
  code?: string;
  message?: string;
  line?: number;
}

interface CandidateChecks {
  ok?: boolean;
  issues?: CandidateIssue[];
  imports?: string[];
  classes?: string[];
}

interface Candidate {
  id: string;
  created_at?: number;
  strategy_key?: string;
  name?: string;
  base_key?: string;
  model?: string;
  status?: string;
  checks?: CandidateChecks;
  backtest?: Record<string, unknown>;
  note?: string;
  reviewer?: string;
}

interface OptimizeRound {
  round?: number;
  params?: Record<string, unknown>;
  metrics?: Record<string, number>;
  score?: number;
  accepted?: boolean;
  reason?: string;
  reflection?: string;
  tokens?: number;
  elapsed?: number;
  error?: string;
}

interface OptimizeResult {
  rounds?: OptimizeRound[];
  best?: Record<string, unknown>;
  best_metrics?: Record<string, number>;
  best_score?: number;
  calls?: number;
  stopped?: string;
}

interface OptimizeJob {
  id?: string;
  status?: string;
  progress?: number;
  started?: number;
  result?: OptimizeResult | null;
  error?: string | null;
}

/* ------------------------------------------------------------- constants */
const CANDIDATE_STATE: Record<string, [string, string]> = {
  pending: ["待审", "pending"],
  approved: ["已批准", "approved"],
  rejected: ["已拒绝", "rejected"],
};

const JOB_STATE: Record<string, string> = {
  running: "运行中",
  cancelling: "取消中",
  done: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

const METRICS: [string, string][] = [
  ["sharpe", "夏普"],
  ["total_return", "总收益"],
  ["win_rate", "胜率"],
  ["profit_factor", "盈亏比"],
];

const MARKETS: [string, string][] = [
  ["spot", "现货"],
  ["usdm", "U 本位"],
  ["coinm", "币本位"],
];

const STOPPED_LABEL: Record<string, string> = {
  rounds: "轮次用尽",
  budget: "调用预算用尽",
  timeout: "超时",
  cancel: "已取消",
  no_improvement: "连续未改善",
};

function pad(value: number): string {
  return String(value).padStart(2, "0");
}

function stampOf(seconds: number | undefined): string {
  if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds <= 0) return "—";
  const date = new Date(seconds * 1000);
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}`
  );
}

function paramsText(params: Record<string, unknown> | undefined): string {
  if (!params) return "—";
  const entries = Object.entries(params);
  if (!entries.length) return "—";
  return entries
    .map(([key, value]) => `${key}=${typeof value === "number" ? fmt(value, 2) : String(value)}`)
    .join(" · ");
}

/** One candidate row. Clicking it loads the source below. */
function statusChip(candidate: Candidate): React.ReactNode {
  const [label, kind] = CANDIDATE_STATE[candidate.status ?? ""] ?? [candidate.status ?? "未知", ""];
  return <span className={`lab-chip ${kind}`}>{label}</span>;
}

/* ----------------------------------------------------------------- page */
export default function StrategyLabPage() {
  /* candidates */
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [candidateError, setCandidateError] = useState("");
  const [candidateLoading, setCandidateLoading] = useState(true);
  const [selected, setSelected] = useState<Candidate | null>(null);
  const [code, setCode] = useState("");
  const [codeError, setCodeError] = useState("");
  const [codeLoading, setCodeLoading] = useState(false);
  const [reviewNote, setReviewNote] = useState("");

  /* generation */
  const [instruction, setInstruction] = useState("");
  const [genBase, setGenBase] = useState("");
  const [generateBusy, setGenerateBusy] = useState(false);

  /* shared strategy / symbol catalogues */
  const [strategies, setStrategies] = useState<Strategy[]>([]);
  const [symbols, setSymbols] = useState<SymbolInfo[]>([]);

  /* optimisation */
  const [market, setMarket] = useState("spot");
  const [symbol, setSymbol] = useState("BTC/USDT");
  const [timeframe, setTimeframe] = useState("1h");
  const [baseKey, setBaseKey] = useState("");
  const [rounds, setRounds] = useState(3);
  const [metric, setMetric] = useState("sharpe");
  const [optimizeBusy, setOptimizeBusy] = useState(false);
  const [jobId, setJobId] = useState("");
  const [job, setJob] = useState<OptimizeJob | null>(null);

  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const pollRef = useRef<number | undefined>(undefined);

  useEffect(() => () => window.clearInterval(pollRef.current), []);

  /* ------------------------------------------------------------- loading */
  const loadCandidates = useCallback(async () => {
    setCandidateLoading(true);
    try {
      const data = await api<{ items: Candidate[] }>("/api/ai/strategies/candidates");
      setCandidates(data.items ?? []);
      setCandidateError("");
    } catch (caught) {
      setCandidateError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setCandidateLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadCandidates();
  }, [loadCandidates]);

  useEffect(() => {
    api<{ items: Strategy[] }>("/api/strategies")
      .then((data) => {
        setStrategies(data.items ?? []);
        const first = data.items?.[0];
        if (first) {
          setGenBase((current) => current || first.key);
          setBaseKey((current) => current || first.key);
        }
      })
      .catch(() => setStrategies([]));
  }, []);

  useEffect(() => {
    api<{ items: SymbolInfo[] }>(`/api/symbols?${query({ market })}`)
      .then((data) => {
        setSymbols(data.items ?? []);
        setSymbol((current) =>
          (data.items ?? []).some((item) => item.symbol === current)
            ? current
            : ((data.items ?? [])[0]?.symbol ?? current),
        );
      })
      .catch(() => setSymbols([]));
  }, [market]);

  /* ------------------------------------------------------------ actions */
  const pickCandidate = useCallback(async (candidate: Candidate) => {
    setSelected(candidate);
    setCode("");
    setCodeError("");
    setCodeLoading(true);
    setReviewNote("");
    try {
      const data = await api<{ candidate: Candidate; code: string }>(
        `/api/ai/strategies/candidates/${encodeURIComponent(candidate.id)}`,
      );
      setSelected(data.candidate ?? candidate);
      setCode(data.code ?? "");
    } catch (caught) {
      setCodeError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setCodeLoading(false);
    }
  }, []);

  const generate = useCallback(
    async (baseOverride?: string) => {
      if (generateBusy) return;
      setGenerateBusy(true);
      setError("");
      setNotice("");
      try {
        const data = await api<{ ok: boolean; candidate: Candidate }>(
          "/api/ai/strategies/candidates/generate",
          {
            method: "POST",
            body: JSON.stringify({
              base_key: baseOverride ?? genBase,
              instruction: instruction.trim(),
              market,
              symbol,
              timeframe,
            }),
          },
        );
        const created = data.candidate;
        setCandidates((current) => [created, ...current.filter((item) => item.id !== created.id)]);
        setNotice(`已生成候选 ${created.strategy_key ?? created.id}。`);
        await pickCandidate(created);
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : String(caught));
      } finally {
        setGenerateBusy(false);
      }
    },
    [generateBusy, genBase, instruction, market, symbol, timeframe, pickCandidate],
  );

  const review = useCallback(
    async (approve: boolean) => {
      if (!selected) return;
      setError("");
      setNotice("");
      try {
        const data = await api<{ ok: boolean; candidate: Candidate }>(
          `/api/ai/strategies/candidates/${encodeURIComponent(selected.id)}/review`,
          {
            method: "POST",
            body: JSON.stringify({ approve, note: reviewNote.trim() }),
          },
        );
        const updated = data.candidate;
        setSelected(updated);
        setCandidates((current) =>
          current.map((item) => (item.id === updated.id ? updated : item)),
        );
        setNotice(approve ? "已批准该候选。" : "已拒绝该候选。");
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : String(caught));
      }
    },
    [selected, reviewNote],
  );

  /* -------------------------------------------------------- optimisation */
  const stopPolling = useCallback(() => {
    window.clearInterval(pollRef.current);
    pollRef.current = undefined;
  }, []);

  const poll = useCallback(
    async (id: string) => {
      try {
        const data = await api<OptimizeJob>(`/api/ai/lab/optimize/${encodeURIComponent(id)}`);
        setJob(data);
        if (data.status === "done" || data.status === "failed" || data.status === "cancelled") {
          stopPolling();
          setOptimizeBusy(false);
        }
      } catch (caught) {
        stopPolling();
        setOptimizeBusy(false);
        setError(caught instanceof Error ? caught.message : String(caught));
      }
    },
    [stopPolling],
  );

  const startOptimize = useCallback(async () => {
    if (!baseKey) {
      setError("请先选择要优化的策略。");
      return;
    }
    setOptimizeBusy(true);
    setError("");
    setNotice("");
    stopPolling();
    try {
      const data = await api<{ ok: boolean; job_id: string }>("/api/ai/lab/optimize", {
        method: "POST",
        body: JSON.stringify({
          market,
          symbol,
          timeframe,
          base_key: baseKey,
          rounds,
          metric,
        }),
      });
      setJobId(data.job_id);
      setJob({ id: data.job_id, status: "running", progress: 0 });
      setNotice(`优化任务已启动：${baseKey} · ${symbol} · ${timeframe} · ${rounds} 轮。`);
      pollRef.current = window.setInterval(() => void poll(data.job_id), 2000);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
      setOptimizeBusy(false);
    }
  }, [baseKey, market, symbol, timeframe, rounds, metric, poll, stopPolling]);

  const cancelOptimize = useCallback(async () => {
    if (!jobId) return;
    setError("");
    try {
      await api(`/api/ai/lab/optimize/${encodeURIComponent(jobId)}/cancel`, { method: "POST" });
      setNotice("已请求取消，等待当前轮结束。");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  }, [jobId]);

  const result = job?.result ?? null;
  const bestJson = result?.best ? JSON.stringify(result.best, null, 2) : "";

  const copyBest = async () => {
    if (!bestJson) return;
    try {
      await navigator.clipboard.writeText(bestJson);
      setNotice("已复制 best 参数 JSON，可在「自动交易」的参数字段里粘贴。");
    } catch (caught) {
      setError(`复制失败：${caught instanceof Error ? caught.message : String(caught)}`);
    }
  };

  const jobRunning = job?.status === "running" || job?.status === "cancelling";

  return (
    <>
      <div className="page-head">
        <h1 className="page-title">策略实验室</h1>
        <span className="page-sub">AI 生成候选策略 · 多轮参数优化</span>
        <span className="spacer" />
        <span className="faint" style={{ fontSize: 11 }}>
          {candidates.length} 个候选 · 审核不会自动注册策略
        </span>
        <button type="button" className="btn-sm" disabled={candidateLoading} onClick={() => void loadCandidates()}>
          {candidateLoading ? "刷新中…" : "刷新候选"}
        </button>
      </div>

      <div className="lab-page">
        {error && <div className="banner error">{error}</div>}
        {notice && <div className="banner">{notice}</div>}
        {candidateError && <div className="banner error">候选列表读取失败：{candidateError}</div>}

        <div className="lab-grid">
          {/* ------------------------------------------------ left column */}
          <div className="stack">
            <div className="card">
              <div className="card-head">
                <span className="card-title">候选策略</span>
                <span className="spacer" />
                <span className="faint" style={{ fontSize: 11 }}>
                  生成一次约 5–20 秒
                </span>
              </div>
              <div className="card-body">
                <div className="lab-gen">
                  <div className="field">
                    <label className="field-label" htmlFor="lab-base">
                      <span>参考策略</span>
                      <em>可以留空</em>
                    </label>
                    <select
                      id="lab-base"
                      className="select"
                      value={genBase}
                      disabled={generateBusy}
                      onChange={(event) => setGenBase(event.target.value)}
                    >
                      <option value="">从零生成</option>
                      {strategies.map((item) => (
                        <option key={item.key} value={item.key}>
                          {item.name}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div className="field lab-instruction">
                    <label className="field-label" htmlFor="lab-instruction">
                      <span>改进要求</span>
                      <em>一句话，自然语言</em>
                    </label>
                    <input
                      id="lab-instruction"
                      className="input"
                      type="text"
                      placeholder="例：在布林带基础上加入成交量过滤，减少震荡假信号"
                      value={instruction}
                      disabled={generateBusy}
                      onChange={(event) => setInstruction(event.target.value)}
                    />
                  </div>
                  <button
                    type="button"
                    className="btn-sm primary lab-generate"
                    disabled={generateBusy}
                    onClick={() => void generate()}
                  >
                    {generateBusy ? "生成中…" : "AI 生成候选"}
                  </button>
                </div>

                <div className="lab-list">
                  {candidateLoading ? (
                    <div className="empty">读取候选…</div>
                  ) : candidates.length === 0 ? (
                    <div className="empty">还没有候选。写一句改进要求后点「AI 生成候选」。</div>
                  ) : (
                    candidates.map((item) => {
                      const issues = item.checks?.issues ?? [];
                      const ok = Boolean(item.checks?.ok);
                      return (
                        <button
                          key={item.id}
                          type="button"
                          className={`lab-candidate ${selected?.id === item.id ? "active" : ""}`}
                          onClick={() => void pickCandidate(item)}
                        >
                          <span className="lab-candidate-key num">{item.strategy_key ?? item.id}</span>
                          <span className="lab-candidate-name">{item.name ?? "—"}</span>
                          {statusChip(item)}
                          <span className={`lab-check ${ok ? "ok" : "bad"}`}>
                            {ok ? "检查通过" : `检查未通过 · ${issues.length} 个问题`}
                          </span>
                          <span className="lab-candidate-meta faint">
                            {stampOf(item.created_at)}
                            {item.base_key ? ` · 基于 ${item.base_key}` : ""}
                            {item.model ? ` · ${item.model}` : ""}
                          </span>
                        </button>
                      );
                    })
                  )}
                </div>
              </div>
            </div>

            <div className="card">
              <div className="card-head">
                <span className="card-title">代码预览</span>
                <span className="spacer" />
                {selected?.status === "pending" && (
                  <>
                    <input
                      className="input input-xs lab-note"
                      placeholder="审核备注（可选）"
                      value={reviewNote}
                      onChange={(event) => setReviewNote(event.target.value)}
                    />
                    <button type="button" className="btn-sm primary" onClick={() => void review(true)}>
                      批准
                    </button>
                    <button type="button" className="btn-sm" onClick={() => void review(false)}>
                      拒绝
                    </button>
                  </>
                )}
                <button
                  type="button"
                  className="btn-sm"
                  disabled={generateBusy}
                  onClick={() => void generate(selected?.base_key || genBase)}
                >
                  {generateBusy ? "生成中…" : "重新生成"}
                </button>
              </div>
              <div className="card-body">
                {!selected ? (
                  <div className="empty">在左侧选择一个候选，查看它生成的源码。</div>
                ) : codeLoading ? (
                  <div className="empty">读取源码…</div>
                ) : codeError ? (
                  <div className="banner error">{codeError}</div>
                ) : (
                  <>
                    <div className="lab-code-meta faint">
                      {selected.strategy_key} · {selected.id}
                      {selected.checks?.classes?.length
                        ? ` · 类 ${selected.checks.classes.join(", ")}`
                        : ""}
                    </div>
                    {selected.checks?.imports?.length ? (
                      <div className="lab-code-meta faint">
                        导入：{selected.checks.imports.join(", ")}
                      </div>
                    ) : null}
                    {(selected.checks?.issues ?? []).length > 0 && (
                      <div className="lab-issues">
                        {(selected.checks?.issues ?? []).map((issue, index) => (
                          <div className="lab-issue" key={`${issue.code ?? "issue"}-${index}`}>
                            <span className="lab-chip rejected">{issue.code ?? "问题"}</span>
                            <span>
                              {issue.message ?? ""}
                              {typeof issue.line === "number" ? `（第 ${issue.line} 行）` : ""}
                            </span>
                          </div>
                        ))}
                      </div>
                    )}
                    <pre className="lab-code">{code || "# 空文件"}</pre>
                  </>
                )}
              </div>
            </div>
          </div>

          {/* ----------------------------------------------- right column */}
          <div className="stack">
            <div className="card">
              <div className="card-head">
                <span className="card-title">多轮参数优化</span>
                <span className="spacer" />
                <span className={`lab-chip ${jobRunning ? "pending" : job?.status === "done" ? "approved" : ""}`}>
                  {JOB_STATE[job?.status ?? ""] ?? "未开始"}
                </span>
              </div>
              <div className="card-body">
                <div className="form-grid">
                  <div className="field">
                    <label className="field-label" htmlFor="lab-opt-strategy">
                      <span>策略</span>
                    </label>
                    <select
                      id="lab-opt-strategy"
                      className="select"
                      value={baseKey}
                      disabled={jobRunning}
                      onChange={(event) => setBaseKey(event.target.value)}
                    >
                      {strategies.map((item) => (
                        <option key={item.key} value={item.key}>
                          {item.name}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div className="field">
                    <label className="field-label" htmlFor="lab-opt-market">
                      <span>市场</span>
                    </label>
                    <select
                      id="lab-opt-market"
                      className="select"
                      value={market}
                      disabled={jobRunning}
                      onChange={(event) => setMarket(event.target.value)}
                    >
                      {MARKETS.map(([value, label]) => (
                        <option key={value} value={value}>
                          {label}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div className="field">
                    <label className="field-label" htmlFor="lab-opt-symbol">
                      <span>交易对</span>
                    </label>
                    <select
                      id="lab-opt-symbol"
                      className="select"
                      value={symbol}
                      disabled={jobRunning}
                      onChange={(event) => setSymbol(event.target.value)}
                    >
                      {symbols.map((item) => (
                        <option key={item.symbol} value={item.symbol}>
                          {item.display}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div className="field">
                    <label className="field-label" htmlFor="lab-opt-timeframe">
                      <span>周期</span>
                    </label>
                    <select
                      id="lab-opt-timeframe"
                      className="select"
                      value={timeframe}
                      disabled={jobRunning}
                      onChange={(event) => setTimeframe(event.target.value)}
                    >
                      {TIMEFRAMES.map(([value, label]) => (
                        <option key={value} value={value}>
                          {label}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div className="field">
                    <label className="field-label" htmlFor="lab-opt-rounds">
                      <span>轮次</span>
                      <em>1–5</em>
                    </label>
                    <input
                      id="lab-opt-rounds"
                      className="input"
                      type="number"
                      min={1}
                      max={5}
                      step={1}
                      disabled={jobRunning}
                      value={rounds}
                      onChange={(event) => {
                        const next = Number(event.target.value);
                        if (Number.isFinite(next)) setRounds(Math.max(1, Math.min(5, next)));
                      }}
                    />
                  </div>
                  <div className="field">
                    <label className="field-label" htmlFor="lab-opt-metric">
                      <span>优化指标</span>
                    </label>
                    <select
                      id="lab-opt-metric"
                      className="select"
                      value={metric}
                      disabled={jobRunning}
                      onChange={(event) => setMetric(event.target.value)}
                    >
                      {METRICS.map(([value, label]) => (
                        <option key={value} value={value}>
                          {label}
                        </option>
                      ))}
                    </select>
                  </div>
                </div>

                <div className="lab-opt-actions">
                  <button
                    type="button"
                    className="btn-sm primary"
                    disabled={optimizeBusy || !baseKey}
                    onClick={() => void startOptimize()}
                  >
                    {jobRunning ? "优化进行中…" : "启动优化"}
                  </button>
                  <button
                    type="button"
                    className="btn-sm"
                    disabled={!jobRunning}
                    onClick={() => void cancelOptimize()}
                  >
                    取消
                  </button>
                  <span className="spacer" />
                  <span className="faint" style={{ fontSize: 11 }}>
                    每 2 秒刷新 · 最多 10 次模型调用
                  </span>
                </div>

                {job && (
                  <div className="lab-progress">
                    <div className="progress">
                      <div style={{ width: `${Math.round((job.progress ?? 0) * 100)}%` }} />
                    </div>
                    <div className="lab-progress-meta faint">
                      {JOB_STATE[job.status ?? ""] ?? job.status ?? "—"} ·{" "}
                      {Math.round((job.progress ?? 0) * 100)}%
                      {job.result?.stopped
                        ? ` · ${STOPPED_LABEL[job.result.stopped] ?? job.result.stopped}`
                        : ""}
                      {typeof job.result?.calls === "number" ? ` · 调用 ${job.result.calls} 次` : ""}
                    </div>
                  </div>
                )}

                {job?.error && <div className="banner error">优化失败：{job.error}</div>}
                {!job && (
                  <div className="hint">
                    选一个已注册策略，模型会按轮次给出新参数并回测，只有指标改善且回撤不变差才接受。
                  </div>
                )}
              </div>
            </div>

            <div className="card">
              <div className="card-head">
                <span className="card-title">轮次结果</span>
                <span className="spacer" />
                <span className="faint" style={{ fontSize: 11 }}>
                  {result?.rounds?.length ?? 0} 轮
                </span>
              </div>
              <div className="card-body">
                {!result || (result.rounds ?? []).length === 0 ? (
                  <div className="empty">
                    {jobRunning ? "第一轮还在回测…" : "还没有结果。启动优化后这里会逐轮显示。"}
                  </div>
                ) : (
                  <div className="lab-table-wrap">
                    <table className="table">
                      <thead>
                        <tr>
                          <th>轮次</th>
                          <th>参数</th>
                          <th className="right">评分</th>
                          <th>结果</th>
                          <th>理由 / 反思</th>
                        </tr>
                      </thead>
                      <tbody>
                        {(result.rounds ?? []).map((row) => (
                          <tr key={row.round ?? JSON.stringify(row.params)}>
                            <td className="num">{row.round ?? "—"}</td>
                            <td className="num">{paramsText(row.params)}</td>
                            <td className="right num">{fmt(row.score, 4)}</td>
                            <td>
                              <span className={`lab-chip ${row.accepted ? "approved" : "rejected"}`}>
                                {row.accepted ? "接受" : "保留"}
                              </span>
                            </td>
                            <td className="lab-reason">
                              {row.reason || row.error || "—"}
                              {row.reflection ? ` · ${row.reflection}` : ""}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}

                {result && (
                  <div className="lab-best">
                    <div className="lab-best-head">
                      <span className="card-title">最优参数</span>
                      <span className="lab-chip approved">评分 {fmt(result.best_score, 4)}</span>
                      <span className="spacer" />
                      <button type="button" className="link-btn" disabled={!bestJson} onClick={() => void copyBest()}>
                        复制 JSON
                      </button>
                    </div>
                    <pre className="lab-best-json">{bestJson || "{}"}</pre>
                    <div className="lab-best-metrics">
                      {Object.entries(result.best_metrics ?? {}).map(([key, value]) => (
                        <span className="lab-metric" key={key}>
                          <span className="faint">{key}</span>
                          <strong className="num">{typeof value === "number" ? fmt(value, 4) : String(value)}</strong>
                        </span>
                      ))}
                    </div>
                    <div className="hint">
                      暂不自动写入自动交易：把这段 JSON 的字段名与「自动交易 → 策略参数」对应后手动填入。
                    </div>
                  </div>
                )}
              </div>
            </div>
          </div>
        </div>
      </div>
    </>
  );
}
