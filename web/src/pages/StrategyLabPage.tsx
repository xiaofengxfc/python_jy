import { useCallback, useEffect, useRef, useState } from "react";
import { api, query } from "../api";
import { TIMEFRAMES, fmt } from "../format";
import type {
  AppSettings,
  BacktestJob,
  Candidate,
  Health,
  OptimizeJob,
  Strategy,
  SymbolInfo,
} from "../types";
import CandidateList, { CANDIDATE_STATE } from "../components/CandidateList";
import MetricGrid from "../components/MetricGrid";
import StageBar from "../components/StageBar";

/** 策略实验室：让模型写候选，人检查、回测、批准，再拿参数去优化。
 *
 * 后端接口没变，只是把三件事摆成三列：左边挑候选，中间看候选，右边干活
 * （生成和调参）。批准之后候选才进策略库，AI 量化页才拿得到它。
 */

interface Props {
  settings: AppSettings | null;
  health: Health | null;
  /** 已注册的候选要能跳到策略页改源码，带上它注册后的 key。 */
  onNavigate?: (page: "strategy", focusKey?: string) => void;
}

const STAGES = [
  { key: "generate", label: "生成" },
  { key: "check", label: "检查" },
  { key: "backtest", label: "回测" },
  { key: "approve", label: "批准" },
];

const METRICS: [string, string][] = [
  ["sharpe", "夏普"],
  ["total_return", "总收益"],
  ["win_rate", "胜率"],
  ["profit_factor", "盈亏比"],
];

const STOPPED_LABEL: Record<string, string> = {
  rounds: "轮次用尽",
  budget: "调用预算用尽",
  timeout: "超时",
  cancel: "已取消",
  no_improvement: "连续未改善",
};

function stageOf(candidate: Candidate | null): string {
  if (!candidate) return "generate";
  if (candidate.status === "approved") return "approve";
  if (candidate.backtest && Object.keys(candidate.backtest).length) return "backtest";
  return candidate.checks ? "check" : "generate";
}

function paramsText(params: Record<string, unknown> | undefined): string {
  if (!params) return "";
  return Object.entries(params)
    .map(([key, value]) => `${key}=${String(value)}`)
    .join(" ");
}

export default function StrategyLabPage({ settings, health, onNavigate }: Props) {
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [selected, setSelected] = useState<Candidate | null>(null);
  const [code, setCode] = useState("");
  const [codeOpen, setCodeOpen] = useState(false);

  const [strategies, setStrategies] = useState<Strategy[]>([]);
  const [symbols, setSymbols] = useState<SymbolInfo[]>([]);
  const [market, setMarket] = useState(settings?.market_type ?? "spot");
  const [symbol, setSymbol] = useState(settings?.last_symbol ?? "BTC/USDT");
  const [timeframe, setTimeframe] = useState(settings?.timeframe ?? "1h");

  const [tab, setTab] = useState<"generate" | "optimize">("generate");
  const [instruction, setInstruction] = useState("");
  const [genBase, setGenBase] = useState("");
  const [reviewNote, setReviewNote] = useState("");

  const [baseKey, setBaseKey] = useState("");
  const [rounds, setRounds] = useState(3);
  const [metric, setMetric] = useState("sharpe");
  const [job, setJob] = useState<OptimizeJob | null>(null);
  const [jobId, setJobId] = useState("");

  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const poll = useRef<number | undefined>(undefined);
  const handoff = useRef(false);

  /* ------------------------------------------------------------- 进入带参 */
  useEffect(() => {
    if (handoff.current) return;
    handoff.current = true;
    const raw = window.sessionStorage.getItem("bitrader.lab.handoff");
    if (!raw) return;
    window.sessionStorage.removeItem("bitrader.lab.handoff");
    try {
      const data = JSON.parse(raw) as {
        strategy?: string;
        symbol?: string;
        timeframe?: string;
        market?: string;
      };
      if (data.strategy) setBaseKey(data.strategy);
      if (data.symbol) setSymbol(data.symbol);
      if (data.timeframe) setTimeframe(data.timeframe);
      if (data.market) setMarket(data.market);
      setTab("optimize");
    } catch {
      // 带参坏了就当没带。
    }
  }, []);

  /* ---------------------------------------------------------------- 数据 */
  const loadCandidates = useCallback(async () => {
    try {
      const data = await api<{ items: Candidate[] }>("/api/ai/strategies/candidates");
      const items = data.items ?? [];
      setCandidates(items);
      setSelected((current) => items.find((item) => item.id === current?.id) ?? items[0] ?? null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  }, []);

  useEffect(() => {
    void loadCandidates();
  }, [loadCandidates]);

  useEffect(() => {
    api<{ items: Strategy[] }>("/api/strategies")
      .then((data) => {
        const items = data.items ?? [];
        setStrategies(items);
        if (items[0]) {
          setGenBase((current) => current || items[0]!.key);
          setBaseKey((current) => current || items[0]!.key);
        }
      })
      .catch(() => setStrategies([]));
  }, []);

  useEffect(() => {
    if (!market) return;
    api<{ items: SymbolInfo[] }>(`/api/symbols?${query({ market })}`)
      .then((data) => {
        const items = data.items ?? [];
        setSymbols(items);
        setSymbol((current) =>
          items.some((item) => item.symbol === current)
            ? current
            : (items.find((item) => item.symbol === "BTC/USDT") ?? items[0])?.symbol ?? "",
        );
      })
      .catch(() => setSymbols([]));
  }, [market]);

  const pickCandidate = useCallback(async (candidate: Candidate) => {
    setSelected(candidate);
    setCode("");
    setCodeOpen(false);
    try {
      const data = await api<{ candidate: Candidate; code: string }>(
        `/api/ai/strategies/candidates/${encodeURIComponent(candidate.id)}`,
      );
      setSelected(data.candidate);
      setCode(data.code ?? "");
    } catch {
      // 源码读不到不影响后面的动作。
    }
  }, []);

  useEffect(() => {
    if (selected) void pickCandidate(selected);
    // 只在换候选时读源码。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected?.id]);

  /* ---------------------------------------------------------------- 动作 */
  const generate = async () => {
    if (busy || !genBase) return;
    setBusy("generate");
    setError("");
    setNotice("");
    try {
      const data = await api<{ ok: boolean; candidate: Candidate }>(
        "/api/ai/strategies/candidates/generate",
        {
          method: "POST",
          body: JSON.stringify({
            base_key: genBase,
            instruction: instruction.trim(),
            market,
            symbol,
            timeframe,
          }),
        },
      );
      setCandidates((current) => [
        data.candidate,
        ...current.filter((item) => item.id !== data.candidate.id),
      ]);
      setSelected(data.candidate);
      setNotice(`已生成候选 ${data.candidate.strategy_key ?? data.candidate.id}。`);
      setTab("generate");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy("");
    }
  };

  const review = async (approve: boolean) => {
    if (!selected || busy) return;
    setBusy("review");
    setError("");
    setNotice("");
    try {
      const data = await api<{ ok: boolean; candidate: Candidate }>(
        `/api/ai/strategies/candidates/${encodeURIComponent(selected.id)}/review`,
        { method: "POST", body: JSON.stringify({ approve, note: reviewNote.trim() }) },
      );
      setSelected(data.candidate);
      setCandidates((current) =>
        current.map((item) => (item.id === data.candidate.id ? data.candidate : item)),
      );
      setNotice(
        approve
          ? data.candidate.registered_key
            ? `已批准，策略库多了 ${data.candidate.registered_key}。`
            : "已批准该候选。"
          : "已拒绝该候选。",
      );
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy("");
    }
  };

  /** 批准之前也能回测：后端把它装进一次性的 cand:<id> 注册表。 */
  const backtestCandidate = async () => {
    if (!selected || busy) return;
    setBusy("backtest");
    setError("");
    setNotice("回测已启动，等一会儿。");
    try {
      const started = await api<{ job_id: string }>("/api/backtest/run", {
        method: "POST",
        body: JSON.stringify({
          market,
          symbol,
          timeframe,
          strategy: "cand:" + selected.id,
          params: {},
          bars: 800,
          allow_short: market !== "spot",
          save: false,
        }),
      });
      let outcome: BacktestJob | null = null;
      for (let attempt = 0; attempt < 180; attempt += 1) {
        await new Promise((resolve) => window.setTimeout(resolve, 1000));
        const current = await api<BacktestJob>(
          `/api/backtest/jobs/${encodeURIComponent(started.job_id)}`,
        );
        if (current.status === "running") continue;
        outcome = current;
        break;
      }
      if (!outcome) throw new Error("回测超时，稍后到回测页重试");
      if (outcome.status !== "done") throw new Error(outcome.error || "回测失败");
      const payload = outcome.result;
      const metrics =
        payload && "metrics" in payload ? (payload.metrics as Record<string, number>) : {};
      const refreshed = await api<{ candidate: Candidate; code: string }>(
        `/api/ai/strategies/candidates/${encodeURIComponent(selected.id)}`,
      );
      setSelected(refreshed.candidate);
      setCandidates((current) =>
        current.map((item) =>
          item.id === refreshed.candidate.id ? refreshed.candidate : item,
        ),
      );
      setNotice(
        `回测完成：${Number(metrics.trades ?? 0)} 笔成交，总收益 ` +
          `${fmt(Number(metrics.total_return ?? 0) * 100, 2)}%。`,
      );
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy("");
    }
  };

  const stopPolling = useCallback(() => {
    if (poll.current) {
      window.clearInterval(poll.current);
      poll.current = undefined;
    }
  }, []);

  useEffect(() => stopPolling, [stopPolling]);

  const pollJob = useCallback(async (id: string) => {
    try {
      const data = await api<OptimizeJob>(`/api/ai/lab/optimize/${encodeURIComponent(id)}`);
      setJob(data);
      if (data.status === "running" || data.status === "cancelling") return;
      stopPolling();
      setBusy("");
      if (data.status === "done") setNotice("优化完成，看下面的轮次表。");
      if (data.status === "failed") setError(data.error || "优化失败");
    } catch (caught) {
      stopPolling();
      setBusy("");
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  }, [stopPolling]);

  const startOptimize = async () => {
    if (busy || !baseKey) return;
    setBusy("optimize");
    setError("");
    setNotice("");
    stopPolling();
    try {
      const data = await api<{ ok: boolean; job_id: string }>("/api/ai/lab/optimize", {
        method: "POST",
        body: JSON.stringify({ market, symbol, timeframe, base_key: baseKey, rounds, metric }),
      });
      setJobId(data.job_id);
      setJob({ id: data.job_id, status: "running", progress: 0 });
      setNotice(`优化已启动：${baseKey} · ${symbol} · ${timeframe} · ${rounds} 轮。`);
      poll.current = window.setInterval(() => void pollJob(data.job_id), 2000);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
      setBusy("");
    }
  };

  const cancelOptimize = async () => {
    if (!jobId) return;
    try {
      await api(`/api/ai/lab/optimize/${encodeURIComponent(jobId)}/cancel`, { method: "POST" });
      setNotice("已请求取消，等当前这一轮跑完。");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  };

  const applyBest = async () => {
    const best = job?.result?.best;
    if (!baseKey || !best) return;
    const label = strategies.find((item) => item.key === baseKey)?.name ?? baseKey;
    if (
      !window.confirm(
        `把最优参数写进「${label}」？\n\n只写 AI 交易的参数配置，不会启动、也不会改正在跑的实例。`,
      )
    ) {
      return;
    }
    setBusy("apply");
    setError("");
    try {
      const current = await api<{ settings: AppSettings }>("/api/settings");
      const saved = current.settings.ai_auto?.strategy_params ?? {};
      await api("/api/settings", {
        method: "PUT",
        body: JSON.stringify({
          ai_auto: { strategy_params: { ...saved, [baseKey]: best } },
        }),
      });
      setNotice(`已写入 ${label} 的参数，下一次启动 AI 交易时生效。`);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy("");
    }
  };

  /* ---------------------------------------------------------------- 渲染 */
  const stage = stageOf(selected);
  const [statusLabel, statusTone] = CANDIDATE_STATE[selected?.status ?? "pending"] ?? [
    "待审",
    "pending",
  ];
  const result = job?.result ?? null;
  const running = job?.status === "running" || job?.status === "cancelling";
  const backtest = selected?.backtest ?? {};

  return (
    <>
      <div className="page-head">
        <h1 className="page-title">策略实验室</h1>
        <span className="page-sub">模型写候选，人检查、回测、批准，再调参</span>
        <span className="spacer" />
        <span className="faint" style={{ fontSize: "var(--fs-12)"}}>
          {health?.markets.find((item) => item.value === market)?.label ?? market} · {symbol} ·{" "}
          {timeframe}
        </span>
      </div>

      <div className="page-toolbar">
        <span className="page-toolbar-label">标的</span>
        <select
          className="select"
          aria-label="市场"
          value={market}
          onChange={(event) => setMarket(event.target.value)}
        >
          {(health?.markets ?? []).map((item) => (
            <option key={item.value} value={item.value}>
              {item.label}
            </option>
          ))}
        </select>
        <select
          className="select"
          aria-label="交易对"
          value={symbol}
          onChange={(event) => setSymbol(event.target.value)}
        >
          {symbols.map((item) => (
            <option key={item.symbol} value={item.symbol}>
              {item.display || item.symbol}
            </option>
          ))}
        </select>
        <select
          className="select"
          aria-label="周期"
          value={timeframe}
          onChange={(event) => setTimeframe(event.target.value)}
        >
          {TIMEFRAMES.map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
        <span className="spacer" />
        <span className="faint" style={{ fontSize: "var(--fs-12-5)"}}>
          回测和优化都用这一组标的
        </span>
      </div>

      <StageBar stages={STAGES} current={stage} />

      {error && <div className="banner error">{error}</div>}
      {notice && <div className="banner ok">{notice}</div>}

      <div className="lab-grid">
        <section className="card">
          <div className="card-head">
            <span className="card-title">候选策略</span>
            <span className="spacer" />
            <button type="button" className="link-btn" onClick={() => void loadCandidates()}>
              刷新
            </button>
          </div>
          <div className="card-body">
            <CandidateList
              items={candidates}
              selectedId={selected?.id ?? ""}
              onPick={(candidate) => void pickCandidate(candidate)}
              empty="还没有候选。右边「生成」写一条。"
            />
          </div>
        </section>

        <section className="card">
          <div className="card-head">
            <span className="card-title">
              {selected?.name || selected?.strategy_key || "选一条候选"}
            </span>
            <span className="spacer" />
            {selected && <span className={`cand-state ${statusTone}`}>{statusLabel}</span>}
          </div>
          <div className="card-body">
            {!selected && <div className="empty-note">左边选一条候选。</div>}
            {selected && (
              <>
                <div className="checks">
                  <div className={`lab-check ${selected.checks?.ok ? "ok" : "bad"}`}>
                    {selected.checks?.ok ? "静态检查通过" : "静态检查未通过"}
                  </div>
                  {(selected.checks?.issues ?? []).length === 0 && selected.checks?.ok && (
                    <div className="faint" style={{ fontSize: "var(--fs-12-5)"}}>
                      导入白名单内，没有危险调用。
                    </div>
                  )}
                  {(selected.checks?.issues ?? []).map((issue, index) => (
                    <div key={index} className="faint" style={{ fontSize: "var(--fs-12-5)"}}>
                      {issue.line ? `第 ${issue.line} 行：` : ""}
                      {issue.message || issue.code}
                    </div>
                  ))}
                </div>

                <MetricGrid
                  items={[
                    {
                      label: "总收益",
                      value:
                        backtest.total_return === undefined
                          ? "—"
                          : `${fmt(Number(backtest.total_return) * 100, 2)}%`,
                      tone: Number(backtest.total_return ?? 0) > 0 ? "up" : "down",
                    },
                    { label: "夏普", value: backtest.sharpe === undefined ? "—" : fmt(Number(backtest.sharpe), 2) },
                    { label: "成交", value: backtest.trades === undefined ? "—" : String(backtest.trades) },
                  ]}
                />

                <div className="field">
                  <label className="field-label" htmlFor="lab-note">
                    <span>审查意见</span>
                    <em>可选</em>
                  </label>
                  <input
                    id="lab-note"
                    className="input"
                    value={reviewNote}
                    placeholder="写一句为什么批或拒"
                    onChange={(event) => setReviewNote(event.target.value)}
                  />
                </div>

                <div className="exec-actions">
                  <button
                    type="button"
                    className="btn-sm"
                    disabled={busy !== "" || !selected.checks?.ok || selected.status === "approved"}
                    onClick={() => void review(true)}
                  >
                    批准
                  </button>
                  <button
                    type="button"
                    className="btn-sm"
                    disabled={busy !== "" || selected.status === "rejected"}
                    onClick={() => void review(false)}
                  >
                    拒绝
                  </button>
                  <span className="spacer" />
                  <button
                    type="button"
                    className="btn-sm"
                    disabled={busy !== ""}
                    onClick={() => void backtestCandidate()}
                  >
                    {busy === "backtest" ? "回测中…" : "跑回测"}
                  </button>
                </div>

                {/* .link-btn 自带 margin-left:auto，一行放两个会各自右顶、中间撑开一大段；
                    spacer 吃掉空余量，两个链接就挨着排在右边，和原来单个链接的位置一致。 */}
                <div className="row">
                  <span className="spacer" />
                  <button
                    type="button"
                    className="link-btn"
                    onClick={() => setCodeOpen((open) => !open)}
                  >
                    {codeOpen ? "收起源码" : "看源码"}
                  </button>
                  {selected.registered_key && onNavigate && (
                    <button
                      type="button"
                      className="link-btn"
                      onClick={() => onNavigate("strategy", selected.registered_key)}
                    >
                      在策略页编辑
                    </button>
                  )}
                </div>
                {codeOpen && <pre className="lab-code">{code || "没有读到源码。"}</pre>}
              </>
            )}
          </div>
        </section>

        <section className="card">
          <div className="card-head">
            <span className="card-title">工作区</span>
            <span className="spacer" />
            <div className="segmented" role="group" aria-label="工作区">
              <button
                type="button"
                aria-pressed={tab === "generate"}
                onClick={() => setTab("generate")}
              >
                生成
              </button>
              <button
                type="button"
                aria-pressed={tab === "optimize"}
                onClick={() => setTab("optimize")}
              >
                优化
              </button>
            </div>
          </div>
          <div className="card-body">
            {tab === "generate" && (
              <>
                <div className="field">
                  <label className="field-label" htmlFor="lab-base">
                    <span>以哪个策略为基础</span>
                  </label>
                  <select
                    id="lab-base"
                    className="select"
                    value={genBase}
                    onChange={(event) => setGenBase(event.target.value)}
                  >
                    {strategies.map((item) => (
                      <option key={item.key} value={item.key}>
                        {item.name}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="field">
                  <label className="field-label" htmlFor="lab-instruction">
                    <span>想要什么</span>
                    <em>留空就让模型自由发挥</em>
                  </label>
                  <textarea
                    id="lab-instruction"
                    className="input"
                    rows={4}
                    value={instruction}
                    placeholder="例如：只在放量突破时开仓，震荡时不开"
                    onChange={(event) => setInstruction(event.target.value)}
                  />
                </div>
                <div className="exec-actions">
                  <span className="faint" style={{ fontSize: "var(--fs-12-5)"}}>
                    {symbol} · {timeframe}
                  </span>
                  <span className="spacer" />
                  <button
                    type="button"
                    className="btn-sm primary"
                    disabled={busy !== "" || !genBase}
                    onClick={() => void generate()}
                  >
                    {busy === "generate" ? "生成中…" : "生成候选"}
                  </button>
                </div>
              </>
            )}

            {tab === "optimize" && (
              <>
                <div className="form-grid">
                  <div className="field">
                    <label className="field-label" htmlFor="lab-opt-key">
                      <span>策略</span>
                    </label>
                    <select
                      id="lab-opt-key"
                      className="select"
                      value={baseKey}
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
                    <label className="field-label" htmlFor="lab-opt-metric">
                      <span>目标指标</span>
                    </label>
                    <select
                      id="lab-opt-metric"
                      className="select"
                      value={metric}
                      onChange={(event) => setMetric(event.target.value)}
                    >
                      {METRICS.map(([value, label]) => (
                        <option key={value} value={value}>
                          {label}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div className="field">
                    <label className="field-label" htmlFor="lab-opt-rounds">
                      <span>轮次</span>
                      <em>一轮一次模型调用</em>
                    </label>
                    <input
                      id="lab-opt-rounds"
                      className="input"
                      type="number"
                      min={1}
                      max={10}
                      value={rounds}
                      onChange={(event) => setRounds(Number(event.target.value))}
                    />
                  </div>
                </div>

                <div className="exec-actions">
                  <span className="faint" style={{ fontSize: "var(--fs-12-5)"}}>
                    {symbol} · {timeframe}
                  </span>
                  <span className="spacer" />
                  {running ? (
                    <button type="button" className="btn-sm" onClick={() => void cancelOptimize()}>
                      取消
                    </button>
                  ) : (
                    <button
                      type="button"
                      className="btn-sm primary"
                      disabled={busy !== "" || !baseKey}
                      onClick={() => void startOptimize()}
                    >
                      {busy === "optimize" ? "启动中…" : "开始优化"}
                    </button>
                  )}
                </div>

                {job && (
                  <div className="optimize-progress">
                    <div className="gate-row">
                      <span>状态</span>
                      <b>
                        {job.status === "running"
                          ? `运行中 ${Math.round(Number(job.progress ?? 0) * 100)}%`
                          : job.status === "done"
                            ? "已完成"
                            : job.status === "failed"
                              ? job.error || "失败"
                              : job.status === "cancelled"
                                ? "已取消"
                                : job.status}
                      </b>
                    </div>
                    {result?.stopped && (
                      <div className="faint" style={{ fontSize: "var(--fs-12-5)"}}>
                        结束原因：{STOPPED_LABEL[result.stopped] ?? result.stopped}
                        {result.calls ? ` · 共 ${result.calls} 次调用` : ""}
                      </div>
                    )}
                  </div>
                )}

                {!!result?.rounds?.length && (
                  <div className="mini-table">
                    <div className="mini-head">
                      <span>轮次</span>
                      <span>得分</span>
                      <span>收益</span>
                      <span>参数</span>
                    </div>
                    {result.rounds.map((round) => (
                      <div key={round.round} className="mini-row">
                        <span>{round.round}</span>
                        <span className={round.accepted ? "up" : ""}>
                          {round.score === undefined ? "—" : fmt(round.score, 3)}
                        </span>
                        <span>
                          {round.metrics?.total_return === undefined
                            ? "—"
                            : `${fmt(Number(round.metrics.total_return) * 100, 2)}%`}
                        </span>
                        <span className="faint">{paramsText(round.params) || round.reason || "—"}</span>
                      </div>
                    ))}
                  </div>
                )}

                {result?.best && (
                  <div className="optimize-best">
                    <div className="gate-row">
                      <span>最优参数</span>
                      <b>{paramsText(result.best)}</b>
                    </div>
                    <div className="exec-actions">
                      <span className="faint" style={{ fontSize: "var(--fs-12-5)"}}>
                        {result.best_score === undefined
                          ? ""
                          : `得分 ${fmt(result.best_score, 3)}`}
                      </span>
                      <span className="spacer" />
                      <button
                        type="button"
                        className="btn-sm primary"
                        disabled={busy !== ""}
                        onClick={() => void applyBest()}
                      >
                        写进 AI 交易
                      </button>
                    </div>
                  </div>
                )}
              </>
            )}
          </div>
        </section>
      </div>
    </>
  );
}
