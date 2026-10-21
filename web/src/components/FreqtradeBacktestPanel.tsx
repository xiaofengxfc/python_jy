import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { TIMEFRAMES, fmt, fmtPrice } from "../format";
import type { BacktestResult, Strategy } from "../types";
import LineChart from "../components/LineChart";
import Table from "../components/Table";
import FreqtradeStrategyEditor from "./FreqtradeStrategyEditor";
import FreqtradeLocalPicker from "./FreqtradeLocalPicker";

interface FreqtradeStatus {
  installed: boolean;
  version: string;
  running: boolean;
  api_ok: boolean;
  exchange: string;
  dry_run: boolean;
  root: string;
}

interface StrategyFile {
  name: string;
  file: string;
  classes: string[];
  /** `Btrader_*` shims are generated from a local strategy, not hand-written. */
  generated?: boolean;
}

type Source = "ft" | "local";

const MARKETS: [string, string][] = [
  ["spot", "现货（只做多）"],
  ["usdm", "U 本位合约（可做空）"],
];

const METRICS: [string, string, "pct" | "num"][] = [
  ["total_return", "总收益", "pct"],
  ["annual_return", "年化收益", "pct"],
  ["max_drawdown", "最大回撤", "pct"],
  ["sharpe", "夏普", "num"],
  ["win_rate", "胜率", "pct"],
  ["profit_factor", "盈亏比", "num"],
  ["trades", "成交笔数", "num"],
  ["fee_paid", "手续费", "num"],
];

export default function FreqtradeBacktestPanel() {
  const [status, setStatus] = useState<FreqtradeStatus | null>(null);
  const [strategies, setStrategies] = useState<StrategyFile[]>([]);
  const [strategy, setStrategy] = useState("");
  const [pair, setPair] = useState("BTC/USDT");
  const [timeframe, setTimeframe] = useState("1h");
  const [bars, setBars] = useState(1000);
  const [timerange, setTimerange] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [result, setResult] = useState<BacktestResult | null>(null);
  const [editorOpen, setEditorOpen] = useState(false);
  const [source, setSource] = useState<Source>("local");
  const [localStrategies, setLocalStrategies] = useState<Strategy[]>([]);
  const [localKey, setLocalKey] = useState("");
  const [localParams, setLocalParams] = useState<Record<string, unknown>>({});
  const [market, setMarket] = useState("spot");

  const load = () => {
    Promise.all([
      api<FreqtradeStatus>("/api/ft/status"),
      api<{ items: StrategyFile[] }>("/api/ft/strategies"),
      api<{ items: Strategy[] }>("/api/strategies"),
    ])
      .then(([next, files, local]) => {
        setStatus(next);
        setStrategies(files.items);
        setLocalStrategies(local.items ?? []);
        setStrategy((current) => current || files.items[0]?.name || "");
        setLocalKey((current) => current || local.items?.[0]?.key || "");
        setLocalParams((current) =>
          Object.keys(current).length ? current : { ...(local.items?.[0]?.defaults ?? {}) },
        );
      })
      .catch((caught) =>
        setError(caught instanceof Error ? caught.message : String(caught)),
      );
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const selected = useMemo(
    () => strategies.find((item) => item.name === strategy) ?? null,
    [strategies, strategy],
  );

  const run = async () => {
    if (source === "ft" && !strategy) {
      setError("请先选择 Freqtrade 策略");
      return;
    }
    if (source === "local" && !localKey) {
      setError("请先选择本地策略");
      return;
    }
    setBusy("同步行情…");
    setError("");
    setNotice("");
    setResult(null);
    try {
      const data = await api<{ rows: number; path: string }>("/api/ft/data", {
        method: "POST",
        body: JSON.stringify({ pair, timeframe, market, limit: bars }),
      });
      setBusy(`已同步 ${data.rows} 根 K 线，回测中…`);
      const next = await api<BacktestResult>("/api/ft/backtest", {
        method: "POST",
        body: JSON.stringify(
          source === "local"
            ? { local_key: localKey, params: localParams, market, timerange, timeframe, pairs: [pair] }
            : { strategy, market, timerange, timeframe, pairs: [pair] },
        ),
      });
      setResult(next);
      setNotice(
        `回测完成：${String(next.metrics.trades ?? 0)} 笔成交` +
          (source === "local" ? `（适配器 ${String(next.params.strategy ?? "")}）` : ""),
      );
      if (source === "local") load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy("");
    }
  };

  return (
    <div className="stack ft-backtest">
      <div className="card">
        <div className="card-head">
          <span className="card-title">Freqtrade 回测</span>
          <span className="faint" style={{ fontSize: 11 }}>
            {status?.installed
              ? `${status.version || "已安装"} · 行情源 ${status.exchange}`
              : "Freqtrade 未安装"}
          </span>
          <span className="spacer" />
          <button
            type="button"
            className="btn-sm primary"
            disabled={
              Boolean(busy) || (source === "local" ? !localKey : !strategy)
            }
            onClick={() => void run()}
          >
            {busy || "同步 K 线并回测"}
          </button>
        </div>
        <div className="card-body stack">
          {error && <div className="banner error">{error}</div>}
          {notice && <div className="banner ok">{notice}</div>}
          {!status?.installed && (
            <div className="banner warn">
              Freqtrade 环境未就绪。先在设置中配置独立 venv 的 Python 路径，或把
              venv 放到 <code>{"%APPDATA%\\biTrader\\freqtrade\\venv"}</code>。
            </div>
          )}
          <div className="form-grid">
            <div className="field">
              <label>策略来源</label>
              <select
                className="select"
                value={source}
                onChange={(event) => setSource(event.target.value as Source)}
                disabled={Boolean(busy)}
              >
                <option value="local">本地 biTrader 策略</option>
                <option value="ft">Freqtrade 策略（IStrategy）</option>
              </select>
              <div className="hint">
                {source === "local"
                  ? "自动生成 IStrategy 适配器，只跑 target 信号，关掉 Freqtrade 自带的止盈止损"
                  : "user_data/strategies 里手写的策略文件"}
              </div>
            </div>
            {source === "local" ? (
              <FreqtradeLocalPicker
                strategies={localStrategies}
                value={localKey}
                params={localParams}
                disabled={Boolean(busy)}
                onChange={setLocalKey}
                onParams={setLocalParams}
              />
            ) : (
              <div className="field">
                <label>Freqtrade 策略</label>
                <select
                  className="select"
                  value={strategy}
                  onChange={(event) => setStrategy(event.target.value)}
                  disabled={Boolean(busy)}
                >
                  {strategies.length === 0 && <option value="">（策略目录为空）</option>}
                  {strategies.map((item) => (
                    <option key={item.name} value={item.name}>
                      {item.name}{item.generated ? "（自动生成）" : ""}
                    </option>
                  ))}
                </select>
                <div className="hint">
                  {selected
                    ? "user_data/strategies/" + selected.file
                    : "把 Freqtrade 策略放到 sidecar 的 user_data/strategies"}
                </div>
                <button
                  type="button"
                  className="link-btn"
                  disabled={!strategy || Boolean(busy) || Boolean(selected?.generated)}
                  onClick={() => setEditorOpen(true)}
                >
                  {selected?.generated ? "自动生成的适配器不可编辑" : "编辑源码"}
                </button>
              </div>
            )}
            <div className="field">
              <label>市场</label>
              <select
                className="select"
                value={market}
                onChange={(event) => setMarket(event.target.value)}
                disabled={Boolean(busy)}
              >
                {MARKETS.map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
              <div className="hint">
                决定 K 线来源。Freqtrade 回测按现货模式运行，本地策略的做空段会被忽略。
              </div>
            </div>
            <div className="field">
              <label>交易对</label>
              <input
                className="input"
                value={pair}
                onChange={(event) => setPair(event.target.value.trim().toUpperCase())}
                disabled={Boolean(busy)}
              />
            </div>
            <div className="field">
              <label>周期</label>
              <select
                className="select"
                value={timeframe}
                onChange={(event) => setTimeframe(event.target.value)}
                disabled={Boolean(busy)}
              >
                {TIMEFRAMES.map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label>同步 K 线数</label>
              <input
                className="input num"
                type="number"
                min={60}
                max={5000}
                value={bars}
                onChange={(event) => setBars(Number(event.target.value) || 1000)}
                disabled={Boolean(busy)}
              />
              <div className="hint">最多 5000 根；按当前行情源下载后写入 sidecar。</div>
            </div>
            <div className="field">
              <label>时间范围</label>
              <input
                className="input"
                placeholder="20240101-20240201（可空）"
                value={timerange}
                onChange={(event) => setTimerange(event.target.value.trim())}
                disabled={Boolean(busy)}
              />
            </div>
          </div>
        </div>
      </div>

      {result && (
        <>
          {(result as { warnings?: string[] }).warnings?.length ? (
            <div className="banner warn">
              {(result as { warnings?: string[] }).warnings?.join("；")}
            </div>
          ) : null}
          <div className="metrics">
            {METRICS.map(([key, label, kind]) => {
              const value = Number(result.metrics[key] ?? 0);
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
              <span className="faint" style={{ fontSize: 11 }}>
                Freqtrade · {result.params.strategy as string}
              </span>
            </div>
            <div className="card-body">
              <LineChart
                series={[
                  {
                    name: "Freqtrade",
                    color: "#2563eb",
                    points: result.equity.map((point) => ({
                      x: point.t,
                      y: point.equity,
                    })),
                  },
                ]}
                height={240}
                valueFormat={(value) => fmt(value, 2)}
              />
            </div>
          </div>

          <div className="card">
            <div className="card-head">
              <span className="card-title">成交明细</span>
              <span className="spacer" />
              <span className="faint" style={{ fontSize: 11 }}>
                {result.trades.length} 条
              </span>
            </div>
            <Table
              headers={["时间", "方向", "价格", "数量", "盈亏"]}
              rightFrom={2}
              rows={result.trades.slice(-100).reverse().map((trade) => {
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
      )}

      {editorOpen && strategy && (
        <FreqtradeStrategyEditor
          name={strategy}
          onClose={() => setEditorOpen(false)}
        />
      )}
    </div>
  );
}
