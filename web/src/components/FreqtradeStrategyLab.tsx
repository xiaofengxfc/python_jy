import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { fmt } from "../format";
import type { BacktestResult, Strategy } from "../types";
import FreqtradeStrategyEditor from "./FreqtradeStrategyEditor";
import FreqtradeLocalPicker from "./FreqtradeLocalPicker";

interface Props {
  market: string;
  symbol: string;
  timeframe: string;
}

interface FtStatus {
  installed: boolean;
  version?: string;
  exchange?: string;
}

interface StrategyFile {
  name: string;
  file: string;
  classes: string[];
  generated?: boolean;
}

type Source = "ft" | "local";

const RESULT_METRICS: [string, string][] = [
  ["total_return", "总收益"],
  ["max_drawdown", "最大回撤"],
  ["sharpe", "夏普"],
  ["win_rate", "胜率"],
  ["trades", "成交"],
];

/** Phase-1 Freqtrade compatibility in the strategy lab.
 *
 * Freqtrade strategies are files under user_data/strategies, not btrader
 * Strategy subclasses. They can be listed, edited and backtested side by side
 * with the AI candidates; parameter optimisation remains in Freqtrade's own
 * backtest/hyperopt workflow so the two engines never pretend one parameter
 * schema fits the other.
 */
export default function FreqtradeStrategyLab({ market, symbol, timeframe }: Props) {
  const [status, setStatus] = useState<FtStatus | null>(null);
  const [strategies, setStrategies] = useState<StrategyFile[]>([]);
  const [name, setName] = useState("");
  const [result, setResult] = useState<BacktestResult | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [editorOpen, setEditorOpen] = useState(false);
  const [source, setSource] = useState<Source>("local");
  const [localStrategies, setLocalStrategies] = useState<Strategy[]>([]);
  const [localKey, setLocalKey] = useState("");
  const [localParams, setLocalParams] = useState<Record<string, unknown>>({});

  const load = useCallback(async () => {
    try {
      const [next, files, local] = await Promise.all([
        api<FtStatus>("/api/ft/status"),
        api<{ items: StrategyFile[] }>("/api/ft/strategies"),
        api<{ items: Strategy[] }>("/api/strategies"),
      ]);
      setStatus(next);
      setStrategies(files.items ?? []);
      setLocalStrategies(local.items ?? []);
      setName((current) => current || files.items?.[0]?.name || "");
      setLocalKey((current) => current || local.items?.[0]?.key || "");
      setLocalParams((current) =>
        Object.keys(current).length ? current : { ...(local.items?.[0]?.defaults ?? {}) },
      );
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const run = async () => {
    if (source === "ft" && !name) {
      setError("请先选择一个 Freqtrade 策略");
      return;
    }
    if (source === "local" && !localKey) {
      setError("请先选择一个本地策略");
      return;
    }
    setBusy("同步行情…");
    setError("");
    setNotice("");
    setResult(null);
    try {
      const data = await api<{ rows: number }>("/api/ft/data", {
        method: "POST",
        body: JSON.stringify({ pair: symbol, timeframe, market, limit: 1000 }),
      });
      setBusy(`已同步 ${data.rows} 根 K 线，回测中…`);
      const next = await api<BacktestResult>("/api/ft/backtest", {
        method: "POST",
        body: JSON.stringify(
          source === "local"
            ? { local_key: localKey, params: localParams, market, timeframe, pairs: [symbol] }
            : { strategy: name, market, timeframe, pairs: [symbol] },
        ),
      });
      setResult(next);
      setNotice(
        `回测完成：${String(next.metrics.trades ?? 0)} 笔成交` +
          (source === "local" ? `（适配器 ${String(next.params.strategy ?? "")}）` : ""),
      );
      if (source === "local") await load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy("");
    }
  };

  return (
    <div className="card ft-strategy-lab">
      <div className="card-head">
        <span className="card-title">Freqtrade 策略兼容</span>
        <span className="faint" style={{ fontSize: 11 }}>
          {status?.installed
            ? `${status.version || "已安装"} · ${status.exchange || "交易所未设置"}`
            : "Freqtrade 未安装"}
        </span>
        <span className="spacer" />
        <button type="button" className="btn-sm" onClick={() => void load()}>
          刷新
        </button>
      </div>
      <div className="card-body stack">
        {error && <div className="banner error">{error}</div>}
        {notice && <div className="banner ok">{notice}</div>}
        <div className="form-grid">
          <div className="field">
            <label className="field-label" htmlFor="ft-lab-source">
              <span>策略来源</span>
              <em>本地策略自动转 IStrategy</em>
            </label>
            <select
              id="ft-lab-source"
              className="select"
              value={source}
              disabled={Boolean(busy)}
              onChange={(event) => setSource(event.target.value as Source)}
            >
              <option value="local">本地 biTrader 策略</option>
              <option value="ft">Freqtrade 策略（IStrategy）</option>
            </select>
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
            <label className="field-label" htmlFor="ft-lab-strategy">
              <span>策略文件</span>
              <em>来自 Freqtrade user_data/strategies</em>
            </label>
            <select
              id="ft-lab-strategy"
              className="select"
              value={name}
              disabled={Boolean(busy) || strategies.length === 0}
              onChange={(event) => setName(event.target.value)}
            >
              {strategies.length === 0 && <option value="">（还没有策略文件）</option>}
              {strategies.map((item) => (
                <option key={item.name} value={item.name}>
                  {item.name}
                </option>
              ))}
            </select>
          </div>
          )}
          <div className="field">
            <label className="field-label">
              <span>当前回测上下文</span>
              <em>同步后使用现有 Freqtrade 数据目录</em>
            </label>
            <div className="ft-context num">
              {symbol} · {timeframe} · {market}
            </div>
          </div>
        </div>
        <div className="lab-opt-actions">
          <button
            type="button"
            className="btn-sm primary"
            disabled={Boolean(busy) || (source === "local" ? !localKey : !name)}
            onClick={() => void run()}
          >
            {busy || "同步 K 线并回测"}
          </button>
          <button
            type="button"
            className="btn-sm"
            disabled={source === "local" || !name}
            onClick={() => setEditorOpen(true)}
          >
            编辑源码
          </button>
          <span className="faint" style={{ fontSize: 11 }}>
            本地策略会自动生成 IStrategy 适配器；回测按现货模式运行，参数优化请用 Freqtrade Hyperopt。
          </span>
        </div>
        {result && (result as { warnings?: string[] }).warnings?.length ? (
          <div className="banner warn">
            {(result as { warnings?: string[] }).warnings?.join("；")}
          </div>
        ) : null}
        {result && (
          <div className="lab-best-metrics">
            {RESULT_METRICS.map(([key, label]) => (
              <span className="lab-metric" key={key}>
                <span className="faint">{label}</span>
                <strong className="num">{fmt(Number(result.metrics[key] ?? 0), 4)}</strong>
              </span>
            ))}
          </div>
        )}
      </div>
      {editorOpen && source === "ft" && name && (
        <FreqtradeStrategyEditor
          name={name}
          onClose={() => setEditorOpen(false)}
          onSaved={() => setNotice("策略已保存；重启 Freqtrade 引擎后生效。")}
        />
      )}
    </div>
  );
}
