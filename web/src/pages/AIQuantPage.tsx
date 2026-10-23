import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, query } from "../api";
import SymbolFallbackNote from "../components/SymbolFallbackNote";
import { TIMEFRAMES, fmt } from "../format";
import type {
  AppSettings,
  BacktestJob,
  Candidate,
  Health,
  Strategy,
  SymbolInfo,
} from "../types";
import CandidateList, { CANDIDATE_STATE } from "../components/CandidateList";
import ExecutionConsole from "../components/ExecutionConsole";
import MetricGrid from "../components/MetricGrid";
import StageBar from "../components/StageBar";

/** AI 量化：候选策略从生成到部署，一条线走完。
 *
 * 生成和参数调优在策略实验室（要人来点），这里是「批了之后怎么跑」：
 * 回测、批准、部署、执行。
 */

interface Props {
  settings: AppSettings | null;
  health: Health | null;
  /** 让侧栏看到执行台刚存的那三项。 */
  onSettingsChange?: (patch: Partial<AppSettings>) => void;
  onNavigate: (page: "lab") => void;
}

const STAGES = [
  { key: "generate", label: "生成" },
  { key: "backtest", label: "回测" },
  { key: "approve", label: "批准" },
  { key: "deploy", label: "部署" },
];

const FILTERS: [string, string][] = [
  ["all", "全部"],
  ["pending", "待审"],
  ["approved", "已批准"],
  ["rejected", "已拒绝"],
];

function stageOf(candidate: Candidate | null): string {
  if (!candidate) return "generate";
  if (candidate.registered_key) return "deploy";
  if (candidate.status === "approved") return "approve";
  if (candidate.backtest && Object.keys(candidate.backtest).length) return "backtest";
  return "generate";
}

export default function AIQuantPage({ settings, health, onSettingsChange, onNavigate }: Props) {
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [selected, setSelected] = useState<Candidate | null>(null);
  const [code, setCode] = useState("");
  const [codeOpen, setCodeOpen] = useState(false);
  const [strategies, setStrategies] = useState<Strategy[]>([]);
  const [symbols, setSymbols] = useState<SymbolInfo[]>([]);
  const [filter, setFilter] = useState("all");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState("");
  const [progress, setProgress] = useState("");

  const [market, setMarket] = useState(settings?.market_type ?? "spot");
  const [symbol, setSymbol] = useState(settings?.last_symbol ?? "BTC/USDT");
  /** 切市场后那一对不在列表里、只能换掉时说明一句；可用时保持 null。 */
  const [symbolFallback, setSymbolFallback] = useState<{ from: string; to: string } | null>(null);
  // 当前选中的那一对：effect 只在换市场时跑，靠 ref 读最新值，别把它写进依赖。
  const symbolRef = useRef(symbol);
  symbolRef.current = symbol;
  const [timeframe, setTimeframe] = useState(settings?.timeframe ?? "1h");

  const load = useCallback(async () => {
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
    void load();
  }, [load]);

  useEffect(() => {
    api<{ items: Strategy[] }>("/api/strategies")
      .then((data) => setStrategies(data.items ?? []))
      .catch(() => setStrategies([]));
  }, []);

  useEffect(() => {
    if (!market) return;
    api<{ items: SymbolInfo[] }>(`/api/symbols?${query({ market })}`)
      .then((data) => {
        const items = data.items ?? [];
        setSymbols(items);
        // 设置里那一对是另一个市场的写法时，这里只能退到列表里能用的那一项。
        // 退回要说一句（和交易页、回测页、设置页共用 SymbolFallbackNote），
        // 否则用户以为自己选的对被吞了。
        const from = symbolRef.current;
        const known = items.some((item) => item.symbol === from);
        const to = known
          ? from
          : (items.find((item) => item.symbol === "BTC/USDT") ?? items[0])?.symbol ?? "";
        setSymbol(to);
        setSymbolFallback(known || !to || to === from ? null : { from, to });
      })
      .catch(() => setSymbols([]));
  }, [market]);

  const pick = useCallback(async (candidate: Candidate) => {
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
      // 源码只是参考，读不到不影响批准。
    }
  }, []);

  useEffect(() => {
    if (selected && !code) void pick(selected);
    // 只在换成另一条候选时读源码。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected?.id]);

  const visible = useMemo(
    () => (filter === "all" ? candidates : candidates.filter((item) => item.status === filter)),
    [candidates, filter],
  );

  const review = async (approve: boolean) => {
    if (!selected || busy) return;
    setBusy("review");
    setError("");
    setNotice("");
    try {
      const data = await api<{ ok: boolean; candidate: Candidate }>(
        `/api/ai/strategies/candidates/${encodeURIComponent(selected.id)}/review`,
        { method: "POST", body: JSON.stringify({ approve, note: "" }) },
      );
      setSelected(data.candidate);
      await load();
      setNotice(
        approve
          ? `已批准。策略 ${data.candidate.registered_key || ""} 进了策略库，可以回测和部署。`
          : "已拒绝。",
      );
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy("");
    }
  };

  const runBacktest = useCallback(async () => {
    if (!selected || busy) return;
    const key = selected.registered_key || selected.strategy_key || "";
    if (!key) {
      setError("这条候选还没有注册成策略，先批准。");
      return;
    }
    setBusy("backtest");
    setError("");
    setNotice("");
    setProgress("正在跑回测…");
    try {
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
        if (current.status === "error") {
          throw new Error(current.error || "回测失败");
        }
      }
      if (!outcome) throw new Error("回测超时");
      const payload = outcome.result;
      const metrics =
        payload && "metrics" in payload ? (payload.metrics as Record<string, number>) : {};
      const data = await api<{ ok: boolean; candidate: Candidate }>(
        `/api/ai/strategies/candidates/${encodeURIComponent(selected.id)}/backtest`,
        {
          method: "POST",
          body: JSON.stringify({ metrics, symbol, timeframe, source: "btrader" }),
        },
      );
      setSelected(data.candidate);
      await load();
      setNotice(
        `回测完成：收益 ${fmt(Number(metrics.total_return ?? 0) * 100, 2)}%，` +
          `${Number(metrics.trades ?? 0)} 笔成交。`,
      );
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setProgress("");
      setBusy("");
    }
  }, [busy, load, market, selected, symbol, timeframe]);

  const deploy = async () => {
    if (!selected || busy) return;
    const key = selected.registered_key || selected.strategy_key || "";
    if (!key) {
      setError("先批准，再部署。");
      return;
    }
    const ok = window.confirm(
      `部署 ${selected.name ?? key} 到执行台\n\n${symbol} · ${timeframe} · 满仓\n\n` +
        "部署后会立刻开始按轮询间隔下单。确认？",
    );
    if (!ok) return;
    setBusy("deploy");
    setError("");
    setNotice("");
    try {
      const trades = Number(selected.backtest?.trades ?? 0);
      const totalReturn = Number(selected.backtest?.total_return ?? 0);
      if (!trades) {
        throw new Error("这条候选没有回测成交记录，先跑一次回测再部署。");
      }
      if (totalReturn <= 0) {
        throw new Error(`回测收益 ${fmt(totalReturn * 100, 2)}%，没到正收益门槛，不能部署。`);
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
          confirm_live: (health?.network ?? "testnet") === "mainnet",
        }),
      });
      setNotice("已部署到执行台，下一根 K 线才会下单。");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy("");
    }
  };

  const stage = stageOf(selected);
  const [statusLabel, statusTone] = CANDIDATE_STATE[selected?.status ?? "pending"] ?? [
    "待审",
    "pending",
  ];
  const metrics = selected?.backtest ?? {};

  return (
    <>
      <div className="page-head">
        <h1 className="page-title">AI 量化</h1>
        <span className="page-sub">候选策略回测、批准、部署到执行台</span>
        <span className="spacer" />
        <button type="button" className="btn-sm" onClick={() => onNavigate("lab")}>
          去实验室生成
        </button>
      </div>

      <StageBar stages={STAGES} current={stage} />

      {error && <div className="banner error">{error}</div>}
      {notice && <div className="banner ok">{notice}</div>}

      <div className="quant-grid">
        <section className="card">
          <div className="card-head">
            <span className="card-title">候选策略</span>
            <span className="spacer" />
            <span className="faint" style={{ fontSize: "var(--fs-12)"}}>
              {candidates.length} 条
            </span>
          </div>
          <div className="card-body">
            <div className="segmented" role="group" aria-label="状态筛选">
              {FILTERS.map(([value, label]) => (
                <button
                  key={value}
                  type="button"
                  aria-pressed={filter === value}
                  onClick={() => setFilter(value)}
                >
                  {label}
                </button>
              ))}
            </div>
            <CandidateList
              items={visible}
              selectedId={selected?.id ?? ""}
              onPick={(candidate) => void pick(candidate)}
              empty="还没有候选。去实验室生成一条。"
            />
          </div>
        </section>

        <section className="card">
          <div className="card-head">
            <span className="card-title">
              {selected?.name || selected?.strategy_key || "选一条候选"}
            </span>
            <span className="spacer" />
            <span className={`cand-state ${statusTone}`}>{statusLabel}</span>
          </div>
          <div className="card-body">
            {!selected && <div className="empty-note">左边选一条候选。</div>}
            {selected && (
              <>
                <div className="form-grid">
                  <div className="field">
                    <label className="field-label" htmlFor="quant-market">
                      <span>市场</span>
                    </label>
                    <select
                      id="quant-market"
                      className="select"
                      value={market}
                      onChange={(event) => setMarket(event.target.value)}
                    >
                      {(health?.markets ?? []).map((item) => (
                        <option key={item.value} value={item.value}>
                          {item.label}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div className="field">
                    <label className="field-label" htmlFor="quant-symbol">
                      <span>交易对</span>
                    </label>
                    <select
                      id="quant-symbol"
                      className="select"
                      value={symbol}
                      onChange={(event) => {
                        setSymbol(event.target.value);
                        setSymbolFallback(null);
                      }}
                    >
                      {symbols.map((item) => (
                        <option key={item.symbol} value={item.symbol}>
                          {item.display || item.symbol}
                        </option>
                      ))}
                    </select>
                  </div>
                  {/* 网格里单独占一行：挤在交易对那一格里会折成三行。 */}
                  <SymbolFallbackNote
                    from={symbolFallback?.from ?? ""}
                    to={symbolFallback?.to ?? ""}
                    market={market}
                    layout="form"
                  />
                  <div className="field">
                    <label className="field-label" htmlFor="quant-timeframe">
                      <span>周期</span>
                    </label>
                    <select
                      id="quant-timeframe"
                      className="select"
                      value={timeframe}
                      onChange={(event) => setTimeframe(event.target.value)}
                    >
                      {TIMEFRAMES.map(([value, label]) => (
                        <option key={value} value={value}>
                          {label}
                        </option>
                      ))}
                    </select>
                  </div>
                </div>

                <div className="checks">
                  <div className={`lab-check ${selected.checks?.ok ? "ok" : "bad"}`}>
                    {selected.checks?.ok ? "静态检查通过" : "静态检查未通过"}
                  </div>
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
                      value: metrics.total_return === undefined
                        ? "—"
                        : `${fmt(Number(metrics.total_return) * 100, 2)}%`,
                      tone: Number(metrics.total_return ?? 0) > 0 ? "up" : "down",
                    },
                    { label: "最大回撤", value: metrics.max_drawdown === undefined ? "—" : `${fmt(Number(metrics.max_drawdown) * 100, 2)}%` },
                    { label: "夏普", value: metrics.sharpe === undefined ? "—" : fmt(Number(metrics.sharpe), 2) },
                    { label: "成交笔数", value: metrics.trades === undefined ? "—" : String(metrics.trades) },
                  ]}
                />

                <div className="exec-actions">
                  <button
                    type="button"
                    className="btn-sm"
                    disabled={busy !== "" || !selected.checks?.ok}
                    onClick={() => void review(true)}
                  >
                    批准
                  </button>
                  <button
                    type="button"
                    className="btn-sm"
                    disabled={busy !== ""}
                    onClick={() => void review(false)}
                  >
                    拒绝
                  </button>
                  <span className="spacer" />
                  <button
                    type="button"
                    className="btn-sm"
                    disabled={busy !== "" || !selected.registered_key}
                    onClick={() => void runBacktest()}
                  >
                    {busy === "backtest" ? "回测中…" : "跑回测"}
                  </button>
                  <button
                    type="button"
                    className="btn-sm primary"
                    disabled={busy !== "" || !selected.registered_key}
                    onClick={() => void deploy()}
                  >
                    部署
                  </button>
                </div>
                {progress && <div className="faint">{progress}</div>}

                <button
                  type="button"
                  className="link-btn"
                  onClick={() => setCodeOpen((open) => !open)}
                >
                  {codeOpen ? "收起源码" : "看源码"}
                </button>
                {codeOpen && <pre className="lab-code">{code || "没有读到源码。"}</pre>}
              </>
            )}
          </div>
        </section>

        <ExecutionConsole
          strategies={strategies}
          market={market}
          symbol={symbol}
          timeframe={timeframe}
          settings={settings}
          onSettingsChange={onSettingsChange}
        />
      </div>
    </>
  );
}
