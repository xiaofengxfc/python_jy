import { useEffect, useMemo, useRef, useState } from "react";
import { api, query } from "../api";
import { TIMEFRAMES, fmt, fmtPrice } from "../format";
import type {
  BacktestJob,
  BacktestResult,
  BacktestRun,
  Strategy,
  SymbolInfo,
} from "../types";
import { ParamFields } from "../components/ParamFields";
import LineChart, { Legend, type Series } from "../components/LineChart";
import Table from "../components/Table";

type Tab = "single" | "optimize" | "history";

const TABS: [Tab, string][] = [
  ["single", "单次回测"],
  ["optimize", "参数优化"],
  ["history", "历史记录"],
];

const METRIC_LABELS: [string, string, "pct" | "num"][] = [
  ["total_return", "总收益", "pct"],
  ["annual_return", "年化收益", "pct"],
  ["max_drawdown", "最大回撤", "pct"],
  ["sharpe", "夏普", "num"],
  ["win_rate", "胜率", "pct"],
  ["profit_factor", "盈亏比", "num"],
  ["trades", "成交笔数", "num"],
  ["fee_paid", "手续费", "num"],
];

const SERIES_COLORS = ["#2563eb", "#0f766e", "#b45309", "#7c3aed"];

interface Config {
  market: string;
  symbol: string;
  timeframe: string;
  strategy: string;
  params: Record<string, unknown>;
  bars: number;
  initial_capital: number;
  fee_rate: number;
  slippage_bps: number;
  position_pct: number;
  leverage: number;
  allow_short: boolean;
  stop_loss_pct: number;
  take_profit_pct: number;
}

interface Compared {
  label: string;
  result: BacktestResult;
}

export default function BacktestPage() {
  const [tab, setTab] = useState<Tab>("single");
  const [strategies, setStrategies] = useState<Strategy[]>([]);
  const [symbols, setSymbols] = useState<SymbolInfo[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState(0);

  const [config, setConfig] = useState<Config>({
    market: "spot",
    symbol: "BTC/USDT",
    timeframe: "1h",
    strategy: "",
    params: {},
    bars: 800,
    initial_capital: 10000,
    fee_rate: 0.0004,
    slippage_bps: 1,
    position_pct: 1,
    leverage: 1,
    allow_short: true,
    stop_loss_pct: 0,
    take_profit_pct: 0,
  });

  const [result, setResult] = useState<BacktestResult | null>(null);
  const [resultLabel, setResultLabel] = useState("");
  const [compared, setCompared] = useState<Compared[]>([]);

  const [gridText, setGridText] = useState<Record<string, string>>({});
  const [optimizeRows, setOptimizeRows] = useState<Record<string, unknown>[]>([]);

  const [runs, setRuns] = useState<BacktestRun[]>([]);

  const strategy = useMemo(
    () => strategies.find((item) => item.key === config.strategy) ?? null,
    [strategies, config.strategy],
  );

  const pollRef = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearInterval(pollRef.current), []);

  useEffect(() => {
    api<{ items: Strategy[] }>("/api/strategies")
      .then((data) => {
        setStrategies(data.items);
        const first = data.items[0];
        if (first) {
          setConfig((current) => ({
            ...current,
            strategy: current.strategy || first.key,
            params: current.strategy ? current.params : { ...first.defaults },
          }));
        }
      })
      .catch((caught) =>
        setError(caught instanceof Error ? caught.message : String(caught)),
      );

    api<{ items: BacktestRun[] }>("/api/backtest/runs?limit=50")
      .then((data) => setRuns(data.items))
      .catch(() => setRuns([]));
  }, []);

  useEffect(() => {
    api<{ items: SymbolInfo[] }>(`/api/symbols?${query({ market: config.market })}`)
      .then((data) => {
        setSymbols(data.items);
        setConfig((current) => ({
          ...current,
          symbol: data.items.some((item) => item.symbol === current.symbol)
            ? current.symbol
            : (data.items[0]?.symbol ?? current.symbol),
        }));
      })
      .catch(() => setSymbols([]));
  }, [config.market]);

  const patch = (values: Partial<Config>) => setConfig((current) => ({ ...current, ...values }));

  const poll = (jobId: string, label: string, kind: "backtest" | "optimize") => {
    window.clearInterval(pollRef.current);
    pollRef.current = window.setInterval(async () => {
      try {
        const job = await api<BacktestJob>(`/api/backtest/jobs/${jobId}`);
        setProgress(job.progress ?? 0);
        if (job.status === "running") return;
        window.clearInterval(pollRef.current);
        setBusy(false);
        if (job.status === "error") {
          setError(job.error ?? "任务失败");
          return;
        }
        if (kind === "backtest" && job.result && "equity" in job.result) {
          setResult(job.result as BacktestResult);
          setResultLabel(label);
        } else if (kind === "optimize" && job.result && "rows" in job.result) {
          setOptimizeRows(job.result.rows);
        }
        api<{ items: BacktestRun[] }>("/api/backtest/runs?limit=50")
          .then((data) => setRuns(data.items))
          .catch(() => undefined);
      } catch (caught) {
        window.clearInterval(pollRef.current);
        setBusy(false);
        setError(caught instanceof Error ? caught.message : String(caught));
      }
    }, 700);
  };

  const body = (extra: Record<string, unknown> = {}) => ({
    market: config.market,
    symbol: config.symbol,
    timeframe: config.timeframe,
    strategy: config.strategy,
    params: config.params,
    bars: config.bars,
    initial_capital: config.initial_capital,
    fee_rate: config.fee_rate,
    slippage_bps: config.slippage_bps,
    position_pct: config.position_pct,
    leverage: config.market === "spot" ? 1 : config.leverage,
    allow_short: config.allow_short,
    stop_loss_pct: config.stop_loss_pct,
    take_profit_pct: config.take_profit_pct,
    save: true,
    ...extra,
  });

  const runBacktest = async () => {
    setBusy(true);
    setError("");
    setProgress(0.05);
    try {
      const started = await api<{ job_id: string }>("/api/backtest/run", {
        method: "POST",
        body: JSON.stringify(body()),
      });
      poll(
        started.job_id,
        `${strategy?.name ?? config.strategy} · ${config.symbol} · ${config.timeframe}`,
        "backtest",
      );
    } catch (caught) {
      setBusy(false);
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  };

  const runOptimize = async () => {
    const grid: Record<string, number[]> = {};
    for (const spec of strategy?.params ?? []) {
      const text = (gridText[spec.name] ?? "").trim();
      if (!text) continue;
      const values = text
        .split(/[,，\s]+/)
        .map((piece) => Number(piece))
        .filter((value) => Number.isFinite(value));
      if (values.length) grid[spec.name] = values;
    }
    if (!Object.keys(grid).length) {
      setError("请至少为一个参数填写候选值，例如 5,9,12");
      return;
    }
    setBusy(true);
    setError("");
    setProgress(0.05);
    try {
      const started = await api<{ job_id: string }>("/api/backtest/optimize", {
        method: "POST",
        body: JSON.stringify(body({ grid, metric: "sharpe", top_n: 30 })),
      });
      poll(started.job_id, "", "optimize");
    } catch (caught) {
      setBusy(false);
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  };

  const compareSeries = useMemo<Series[]>(() => {
    const entries = result
      ? [...compared.filter((item) => item.label !== resultLabel), { label: resultLabel, result }]
      : compared;
    return entries.slice(-4).map((entry, index) => ({
      name: entry.label,
      color: SERIES_COLORS[index % SERIES_COLORS.length]!,
      points: entry.result.equity.map((point) => ({ x: point.t, y: point.equity })),
    }));
  }, [compared, result, resultLabel]);

  const addToCompare = () => {
    if (!result) return;
    setCompared((current) => {
      const rest = current.filter((item) => item.label !== resultLabel);
      return [...rest, { label: resultLabel, result }].slice(-4);
    });
  };

  return (
    <>
      <div className="page-head">
        <h1 className="page-title">回测</h1>
        <div className="segmented">
          {TABS.map(([key, label]) => (
            <button key={key} type="button" aria-pressed={tab === key} onClick={() => setTab(key)}>
              {label}
            </button>
          ))}
        </div>
        <span className="spacer" />
        {busy && <span className="faint" style={{ fontSize: 11 }}>{(progress * 100).toFixed(0)}%</span>}
        <button
          type="button"
          className="btn-sm primary"
          disabled={busy || !config.strategy}
          onClick={tab === "optimize" ? runOptimize : runBacktest}
        >
          {busy ? "运行中…" : tab === "optimize" ? "开始优化" : "运行回测"}
        </button>
      </div>

      {error && <div className="banner error">{error}</div>}
      {busy && (
        <div className="progress" style={{ marginBottom: 14 }}>
          <div style={{ width: `${Math.max(5, progress * 100)}%` }} />
        </div>
      )}

      <div className="stack">
        <div className="stack">
          <div className="card">
            <div className="card-head">
              <span className="card-title">回测配置</span>
            </div>
            <div className="card-body stack">
              <div className="form-grid">
                <div className="field">
                  <label className="field-label" htmlFor="bt-market">
                    <span>市场</span>
                  </label>
                  <select
                    id="bt-market"
                    className="select"
                    value={config.market}
                    onChange={(event) => patch({ market: event.target.value })}
                  >
                    <option value="spot">现货</option>
                    <option value="usdm">U 本位合约</option>
                    <option value="coinm">币本位合约</option>
                  </select>
                </div>
                <div className="field">
                  <label className="field-label" htmlFor="bt-symbol">
                    <span>交易对</span>
                  </label>
                  <select
                    id="bt-symbol"
                    className="select"
                    value={config.symbol}
                    onChange={(event) => patch({ symbol: event.target.value })}
                  >
                    {symbols.map((item) => (
                      <option key={item.symbol} value={item.symbol}>
                        {item.display}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="field">
                  <label className="field-label" htmlFor="bt-timeframe">
                    <span>周期</span>
                  </label>
                  <select
                    id="bt-timeframe"
                    className="select"
                    value={config.timeframe}
                    onChange={(event) => patch({ timeframe: event.target.value })}
                  >
                    {TIMEFRAMES.map(([value, label]) => (
                      <option key={value} value={value}>
                        {label}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="field">
                  <label className="field-label" htmlFor="bt-bars">
                    <span>K 线根数</span>
                  </label>
                  <input
                    id="bt-bars"
                    className="input"
                    type="number"
                    min={60}
                    max={5000}
                    step={100}
                    value={config.bars}
                    onChange={(event) => patch({ bars: Number(event.target.value) })}
                  />
                </div>
              </div>

              <div className="field">
                <label className="field-label" htmlFor="bt-strategy">
                  <span>策略</span>
                  <em>{strategy?.allow_short ? "支持做空" : "仅做多"}</em>
                </label>
                <select
                  id="bt-strategy"
                  className="select"
                  value={config.strategy}
                  onChange={(event) => {
                    const found = strategies.find((item) => item.key === event.target.value);
                    patch({
                      strategy: event.target.value,
                      params: found ? { ...found.defaults } : {},
                    });
                    setGridText({});
                  }}
                >
                  {strategies.map((item) => (
                    <option key={item.key} value={item.key}>
                      {item.name}
                    </option>
                  ))}
                </select>
              </div>

              {tab === "optimize" ? (
                <div className="stack">
                  <div className="section-title">参数候选值</div>
                  <div className="hint">
                    每个参数填逗号分隔的候选值，系统会穷举所有组合。
                  </div>
                  {(strategy?.params ?? []).map((spec) => (
                    <div key={spec.name} className="field">
                      <label className="field-label" htmlFor={`grid-${spec.name}`}>
                        <span>{spec.label}</span>
                        <em>
                          默认 {String(spec.default)}
                          {spec.minimum !== null && spec.maximum !== null
                            ? ` · ${spec.minimum}–${spec.maximum}`
                            : ""}
                        </em>
                      </label>
                      <input
                        id={`grid-${spec.name}`}
                        className="input"
                        placeholder={`例如 ${String(spec.default)},${String(
                          Number(spec.default) + 5,
                        )}`}
                        value={gridText[spec.name] ?? ""}
                        onChange={(event) =>
                          setGridText((current) => ({
                            ...current,
                            [spec.name]: event.target.value,
                          }))
                        }
                      />
                    </div>
                  ))}
                </div>
              ) : (
                <div>
                  <div className="section-title">策略参数</div>
                  <ParamFields
                    specs={strategy?.params ?? []}
                    values={config.params}
                    onChange={(name, value) =>
                      patch({ params: { ...config.params, [name]: value } })
                    }
                  />
                </div>
              )}

              <div className="form-grid">
                <div className="field">
                  <label className="field-label" htmlFor="bt-capital">
                    <span>初始资金</span>
                  </label>
                  <input
                    id="bt-capital"
                    className="input"
                    type="number"
                    min={100}
                    value={config.initial_capital}
                    onChange={(event) => patch({ initial_capital: Number(event.target.value) })}
                  />
                </div>
                <div className="field">
                  <label className="field-label" htmlFor="bt-fee">
                    <span>手续费率</span>
                    <em>0.0004 = 4bp</em>
                  </label>
                  <input
                    id="bt-fee"
                    className="input"
                    type="number"
                    step={0.0001}
                    min={0}
                    value={config.fee_rate}
                    onChange={(event) => patch({ fee_rate: Number(event.target.value) })}
                  />
                </div>
                <div className="field">
                  <label className="field-label" htmlFor="bt-slip">
                    <span>滑点</span>
                    <em>bp</em>
                  </label>
                  <input
                    id="bt-slip"
                    className="input"
                    type="number"
                    step={0.5}
                    min={0}
                    value={config.slippage_bps}
                    onChange={(event) => patch({ slippage_bps: Number(event.target.value) })}
                  />
                </div>
                <div className="field">
                  <label className="field-label" htmlFor="bt-pct">
                    <span>仓位比例</span>
                    <em>{(config.position_pct * 100).toFixed(0)}%</em>
                  </label>
                  <input
                    id="bt-pct"
                    className="input"
                    type="number"
                    min={0.05}
                    max={1}
                    step={0.05}
                    value={config.position_pct}
                    onChange={(event) => patch({ position_pct: Number(event.target.value) })}
                  />
                </div>
                {config.market !== "spot" && (
                  <div className="field">
                    <label className="field-label" htmlFor="bt-lev">
                      <span>杠杆</span>
                      <em>不模拟强平</em>
                    </label>
                    <input
                      id="bt-lev"
                      className="input"
                      type="number"
                      min={1}
                      max={125}
                      value={config.leverage}
                      onChange={(event) => patch({ leverage: Number(event.target.value) })}
                    />
                    <div className="hint">
                      回测按「仓位比例 × 杠杆」放大下单量，不会因保证金不足被强平，
                      也没有资金费与维持保证金。高杠杆的收益与最大回撤都会偏乐观。
                    </div>
                  </div>
                )}
                <div className="field">
                  <label className="field-label" htmlFor="bt-sl">
                    <span>止损</span>
                    <em>0 = 关闭</em>
                  </label>
                  <input
                    id="bt-sl"
                    className="input"
                    type="number"
                    min={0}
                    max={1}
                    step={0.01}
                    value={config.stop_loss_pct}
                    onChange={(event) => patch({ stop_loss_pct: Number(event.target.value) })}
                  />
                </div>
                <div className="field">
                  <label className="field-label" htmlFor="bt-tp">
                    <span>止盈</span>
                    <em>0 = 关闭</em>
                  </label>
                  <input
                    id="bt-tp"
                    className="input"
                    type="number"
                    min={0}
                    max={1}
                    step={0.01}
                    value={config.take_profit_pct}
                    onChange={(event) => patch({ take_profit_pct: Number(event.target.value) })}
                  />
                </div>
              </div>

              <label className="check">
                <input
                  type="checkbox"
                  checked={config.allow_short}
                  onChange={(event) => patch({ allow_short: event.target.checked })}
                />
                允许做空（仅合约市场生效）
              </label>
            </div>
          </div>

          <div className="stack">
            {tab === "history" ? (
              <div className="card">
                <div className="card-head">
                  <span className="card-title">历史回测</span>
                  <span className="spacer" />
                  <span className="faint" style={{ fontSize: 11 }}>{runs.length} 条</span>
                </div>
                <Table
                  headers={["时间", "交易对", "周期", "策略", "收益", "夏普", "回撤", ""]}
                  rightFrom={4}
                  rows={runs.map((run) => [
                    new Date(Number(run.created_at)).toLocaleString("zh-CN", { hour12: false }),
                    run.symbol,
                    run.timeframe,
                    run.strategy,
                    <span className={(run.metrics.total_return ?? 0) >= 0 ? "up" : "down"}>
                      {fmt((run.metrics.total_return ?? 0) * 100, 2)}%
                    </span>,
                    fmt(run.metrics.sharpe ?? 0, 2),
                    <span className="down">{fmt((run.metrics.max_drawdown ?? 0) * 100, 2)}%</span>,
                    <button
                      type="button"
                      className="btn-sm"
                      onClick={() => {
                        setConfig((current) => ({
                          ...current,
                          market: run.market_type,
                          symbol: run.symbol,
                          timeframe: run.timeframe,
                          strategy: run.strategy,
                          params: run.params,
                        }));
                        setTab("single");
                        setResult(null);
                        setError("");
                      }}
                    >
                      载入参数
                    </button>,
                  ])}
                  empty="还没有保存的回测记录"
                />
              </div>
            ) : tab === "optimize" ? (
              <div className="card">
                <div className="card-head">
                  <span className="card-title">优化结果</span>
                  <span className="spacer" />
                  <span className="faint" style={{ fontSize: 11 }}>
                    按夏普排序 · {optimizeRows.length} 组
                  </span>
                </div>
                <OptimizeTable rows={optimizeRows} onPick={(row) => {
                  const next: Record<string, unknown> = {};
                  for (const [key, value] of Object.entries(row)) {
                    if (!key.startsWith("m_")) next[key] = value;
                  }
                  patch({ params: next });
                  setTab("single");
                }} />
              </div>
            ) : result ? (
              <>
                <div className="metrics">
                  {METRIC_LABELS.map(([key, label, kind]) => {
                    const value = result.metrics[key] ?? 0;
                    const shown =
                      kind === "pct" ? `${fmt(value * 100, 2)}%` : fmt(value, 2);
                    const tone =
                      key === "max_drawdown" || key === "fee_paid"
                        ? "down"
                        : key === "total_return" || key === "annual_return"
                          ? value >= 0
                            ? "up"
                            : "down"
                          : "";
                    return (
                      <div key={key} className="metric">
                        <span className="metric-label">{label}</span>
                        <span className={`metric-value num ${tone}`}>{shown}</span>
                      </div>
                    );
                  })}
                </div>

                <div className="card">
                  <div className="card-head">
                    <span className="card-title">权益曲线</span>
                    <span className="spacer" />
                    <button type="button" className="btn-sm" onClick={addToCompare}>
                      加入对比
                    </button>
                  </div>
                  <div className="card-body">
                    <LineChart
                      series={
                        compareSeries.length
                          ? compareSeries
                          : [
                              {
                                name: resultLabel,
                                color: SERIES_COLORS[0]!,
                                points: result.equity.map((point) => ({
                                  x: point.t,
                                  y: point.equity,
                                })),
                              },
                            ]
                      }
                      height={250}
                      baseline={config.initial_capital}
                      valueFormat={(value) => fmt(value, 0)}
                      empty="没有权益数据"
                    />
                    <div style={{ marginTop: 10 }}>
                      <Legend series={compareSeries.length ? compareSeries : []} />
                    </div>
                    {compared.length > 0 && (
                      <div className="hint" style={{ marginTop: 8 }}>
                        对比基于本次会话跑出的结果；最多叠加 4 条曲线。
                        <button
                          type="button"
                          className="btn-sm"
                          style={{ marginLeft: 8 }}
                          onClick={() => setCompared([])}
                        >
                          清空对比
                        </button>
                      </div>
                    )}
                  </div>
                </div>

                <div className="card">
                  <div className="card-head">
                    <span className="card-title">成交明细</span>
                    <span className="spacer" />
                    <span className="faint" style={{ fontSize: 11 }}>
                      {result.trades.length} 笔
                    </span>
                  </div>
                  <Table
                    headers={["时间", "方向", "价格", "数量", "盈亏"]}
                    rightFrom={2}
                    rows={result.trades.slice(-60).reverse().map((trade) => {
                      const pnl = Number(trade.pnl ?? 0);
                      return [
                        trade.timestamp
                          ? new Date(Number(trade.timestamp)).toLocaleString("zh-CN", {
                              hour12: false,
                            })
                          : "—",
                        <span className={String(trade.side) === "buy" ? "up" : "down"}>
                          {String(trade.side) === "buy" ? "买入" : "卖出"}
                        </span>,
                        fmtPrice(Number(trade.price ?? 0)),
                        fmt(Number(trade.amount ?? 0), 6),
                        <span className={pnl >= 0 ? "up" : "down"}>{fmt(pnl, 2)}</span>,
                      ];
                    })}
                    empty="策略在这段时间内没有成交"
                  />
                </div>
              </>
            ) : (
              <div className="placeholder">
                <div>
                  <strong>还没有回测结果</strong>
                  左侧选择策略与参数，点击右上角「运行回测」。
                </div>
              </div>
            )}
          </div>
        </div>
      </div>
    </>
  );
}

function OptimizeTable({
  rows,
  onPick,
}: {
  rows: Record<string, unknown>[];
  onPick: (row: Record<string, unknown>) => void;
}) {
  if (!rows.length) return <div className="empty">运行参数优化后在这里查看组合结果</div>;
  const paramKeys = Object.keys(rows[0] ?? {}).filter((key) => !key.startsWith("m_"));
  return (
    <Table
      headers={[
        ...paramKeys,
        "收益",
        "夏普",
        "回撤",
        "笔数",
        "",
      ]}
      rightFrom={paramKeys.length}
      rows={rows.map((row) => [
        ...paramKeys.map((key) => String(row[key] ?? "—")),
        <span className={(Number(row.m_total_return) || 0) >= 0 ? "up" : "down"}>
          {fmt((Number(row.m_total_return) || 0) * 100, 2)}%
        </span>,
        fmt(Number(row.m_sharpe) || 0, 2),
        <span className="down">{fmt((Number(row.m_max_drawdown) || 0) * 100, 2)}%</span>,
        fmt(Number(row.m_trades) || 0, 0),
        <button type="button" className="btn-sm" onClick={() => onPick(row)}>
          使用
        </button>,
      ])}
      empty="没有找到有效组合"
    />
  );
}
