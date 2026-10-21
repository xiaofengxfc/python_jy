import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { fmt } from "../format";
import { useFreqtradeStream } from "../hooks/useFreqtradeStream";
import Table from "./Table";

interface FreqtradeStatus {
  enabled: boolean;
  installed: boolean;
  version: string;
  python: string;
  root: string;
  running: boolean;
  api_ok: boolean;
  trade_port: number;
  exchange: string;
  dry_run: boolean;
  strategy: string;
  timeframe: string;
  pairs: string[];
  log_tail: string;
}

interface StrategyFile {
  name: string;
  file: string;
  classes: string[];
}

interface FtTrade {
  trade_id: number;
  pair: string;
  is_short: boolean;
  amount: number;
  open_rate: number;
  current_rate?: number;
  profit_pct?: number;
  profit_abs?: number;
  open_date?: string;
  stake_amount?: number;
}

interface FtProfit {
  profit_all_coin?: number;
  profit_all_percent?: number;
  trade_count?: number;
  closed_trade_count?: number;
}

/** Status and lifecycle for the optional Freqtrade sidecar. Kept separate from
 * the legacy `/api/auto/*` panel so both engines can coexist during migration. */
export default function FreqtradeCard() {
  const [status, setStatus] = useState<FreqtradeStatus | null>(null);
  const [strategies, setStrategies] = useState<StrategyFile[]>([]);
  const [strategy, setStrategy] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [showLog, setShowLog] = useState(false);
  const [trades, setTrades] = useState<FtTrade[]>([]);
  const [profit, setProfit] = useState<FtProfit | null>(null);
  const [streamEvent, setStreamEvent] = useState("");

  const refresh = useCallback(async () => {
    try {
      const [next, files] = await Promise.all([
        api<FreqtradeStatus>("/api/ft/status"),
        api<{ items: StrategyFile[] }>("/api/ft/strategies"),
      ]);
      setStatus(next);
      setStrategies(files.items);
      setStrategy((current) => current || next.strategy || files.items[0]?.name || "");
      if (next.running && next.api_ok) {
        const [tradeData, profitData] = await Promise.all([
          api<{ items: FtTrade[] }>("/api/ft/trades"),
          api<FtProfit>("/api/ft/profit"),
        ]);
        setTrades(tradeData.items ?? []);
        setProfit(profitData);
      } else {
        setTrades([]);
        setProfit(null);
      }
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 5000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const start = async () => {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const [auto, ai] = await Promise.all([
        api<{ running?: boolean }>("/api/auto/status"),
        api<{ state?: string }>("/api/ai/trader/status?since=0"),
      ]);
      if (auto.running || ai.state === "running") {
        throw new Error("旧引擎或 AI 自主交易正在运行，请先停止再启动 Freqtrade");
      }
      const next = await api<FreqtradeStatus>("/api/ft/start", {
        method: "POST",
        body: JSON.stringify({ strategy, dry_run: true }),
      });
      setStatus(next);
      setNotice("Freqtrade 纸面引擎已启动。");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  };

  const stop = async () => {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const next = await api<FreqtradeStatus>("/api/ft/stop", { method: "POST" });
      setStatus(next);
      setNotice("Freqtrade 已停止。");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  };

  const closeTrade = async (tradeId: number | "all") => {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await api("/api/ft/forceexit", {
        method: "POST",
        body: JSON.stringify({ tradeid: tradeId === "all" ? "all" : String(tradeId) }),
      });
      setNotice(tradeId === "all" ? "已请求全部平仓。" : `已请求平仓 #${tradeId}。`);
      await refresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  };

  const streamConnected = useFreqtradeStream(
    Boolean(status?.running && status.api_ok),
    (message) => {
      const kind = String(message.type || "");
      if (kind === "entry" || kind === "exit" || kind === "protection") {
        setStreamEvent(
          kind === "entry" ? "收到开仓事件" : kind === "exit" ? "收到平仓事件" : "风控状态更新",
        );
        void refresh();
      }
    },
  );

  const state = !status
    ? "读取中"
    : !status.installed
      ? "未安装"
      : status.running && status.api_ok
        ? "运行中"
        : status.running
          ? "启动中"
          : "已停止";
  const tone = state === "运行中" ? "up" : state === "未安装" ? "down" : "";

  return (
    <div className="card ft-card">
      <div className="card-head">
        <span className="card-title">Freqtrade 引擎</span>
        <span className={`faint ${tone}`} style={{ fontSize: 11 }}>
          ● {state}
        </span>
        <span className="faint" style={{ fontSize: 11 }}>
          {streamConnected ? "WS 实时" : "REST"}
        </span>
        <span className="spacer" />
        {status?.version && <span className="faint" style={{ fontSize: 11 }}>{status.version}</span>}
        <button type="button" className="btn-sm" onClick={() => void refresh()}>
          刷新
        </button>
      </div>
      <div className="card-body ft-body">
        {error && <div className="banner error">{error}</div>}
        {notice && <div className="banner ok">{notice}</div>}
        {status && !status.installed && (
          <div className="banner warn">
            还没有独立的 Freqtrade 环境。请创建 venv 并安装：
            <code className="ft-path">{status.python}</code>
            <br />
            安装后在 <code>%APPDATA%\biTrader\config.json</code> 的
            <code>freqtrade.python</code> 指向它，或把 venv 放到该路径。
          </div>
        )}
        {status && (
          <>
            <div className="ft-grid">
              <div>
                <span className="faint">交易所</span>
                <b>{status.exchange}</b>
              </div>
              <div>
                <span className="faint">模式</span>
                <b>{status.dry_run ? "纸面" : "实盘"}</b>
              </div>
              <div>
                <span className="faint">API 端口</span>
                <b className="num">{status.trade_port}</b>
              </div>
              <div>
                <span className="faint">交易对</span>
                <b>{status.pairs.join(" · ") || "—"}</b>
              </div>
            </div>
            <div className="ft-row">
              <label className="field-label" htmlFor="ft-strategy">
                <span>Freqtrade 策略</span>
                <em>来自 user_data/strategies</em>
              </label>
              <select
                id="ft-strategy"
                className="select"
                value={strategy}
                onChange={(event) => setStrategy(event.target.value)}
                disabled={busy || status.running}
              >
                {strategies.length === 0 && <option value="">（还没有策略文件）</option>}
                {strategies.map((item) => (
                  <option key={item.name} value={item.name}>
                    {item.name}
                  </option>
                ))}
              </select>
              <button
                type="button"
                className="btn primary"
                disabled={busy || status.running || !status.installed || !strategy}
                onClick={() => void start()}
              >
                {busy ? "处理中…" : "启动纸面"}
              </button>
              <button
                type="button"
                className="btn"
                disabled={busy || !status.running}
                onClick={() => void stop()}
              >
                停止
              </button>
              <button
                type="button"
                className="link-btn"
                onClick={() => setShowLog((value) => !value)}
              >
                {showLog ? "隐藏日志" : "查看日志"}
              </button>
            </div>
            <div className="faint" style={{ fontSize: 11 }}>
              根目录：<code className="ft-path">{status.root}</code>
            </div>
            {showLog && (
              <pre className="ft-log">{status.log_tail || "（暂无日志）"}</pre>
            )}
            {status.running && status.api_ok && (
              <div className="ft-positions">
                <div className="ft-pos-head">
                  <span>持仓 {trades.length}</span>
                  <span className="faint">
                    累计盈亏 {fmt(Number(profit?.profit_all_coin ?? 0), 2)} USDT
                    （{fmt(Number(profit?.profit_all_percent ?? 0) * 100, 2)}%）
                  </span>
                  {streamEvent && (
                    <span className="faint" style={{ fontSize: 11 }}>
                      {streamEvent}
                    </span>
                  )}
                  <span className="spacer" />
                  <button
                    type="button"
                    className="btn-sm"
                    disabled={busy || trades.length === 0}
                    onClick={() => void closeTrade("all")}
                  >
                    全部平仓
                  </button>
                </div>
                <Table
                  headers={["交易对", "方向", "开仓价", "现价", "盈亏", ""]}
                  rightFrom={2}
                  rows={trades.map((trade) => {
                    const pnl = Number(trade.profit_pct ?? 0) * 100;
                    return [
                      trade.pair,
                      <span className={trade.is_short ? "down" : "up"}>
                        {trade.is_short ? "空" : "多"}
                      </span>,
                      fmt(Number(trade.open_rate ?? 0), 2),
                      fmt(Number(trade.current_rate ?? 0), 2),
                      <span className={pnl >= 0 ? "up" : "down"}>{fmt(pnl, 2)}%</span>,
                      <button
                        type="button"
                        className="btn-sm"
                        disabled={busy}
                        onClick={() => void closeTrade(trade.trade_id)}
                      >
                        平仓
                      </button>,
                    ];
                  })}
                  empty="当前没有持仓"
                />
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
