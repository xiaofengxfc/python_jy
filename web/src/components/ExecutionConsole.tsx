import { useCallback, useEffect, useRef, useState } from "react";
import { api, query } from "../api";
import { TIMEFRAMES, fmt } from "../format";
import type { AutoStatus, Strategy } from "../types";

/** 执行台：把一个已批准的策略挂上去，按自己的节奏把仓位拉到目标值。
 *
 * 这里只管「跑」——配置、启停、信号、日志。策略从哪来、怎么批的，
 * 是 AI 量化页和实验室的事。
 */

interface Props {
  strategies: Strategy[];
  market: string;
  symbol: string;
  timeframe: string;
}

const STATE_LABEL: Record<string, string> = {
  signal: "信号",
  order: "下单",
  state: "状态",
  error: "错误",
  warn: "提醒",
};

export default function ExecutionConsole({ strategies, market, symbol, timeframe }: Props) {
  const [status, setStatus] = useState<AutoStatus | null>(null);
  const [strategyKey, setStrategyKey] = useState("");
  const [positionPct, setPositionPct] = useState(1);
  const [leverage, setLeverage] = useState(1);
  const [pollSeconds, setPollSeconds] = useState(10);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const seq = useRef(0);

  useEffect(() => {
    if (!strategies.length) return;
    setStrategyKey((current) => current || strategies[0]!.key);
  }, [strategies]);

  const refresh = useCallback(async (incremental: boolean) => {
    try {
      const next = await api<AutoStatus>(
        `/api/auto/status?${query({ since: incremental ? seq.current : 0 })}`,
      );
      setStatus((current) => {
        if (!incremental || !current) return next;
        return { ...next, events: [...current.events, ...next.events].slice(-200) };
      });
      seq.current = next.seq ?? 0;
    } catch {
      // 轮询失败不该打断界面。
    }
  }, []);

  useEffect(() => {
    void refresh(false);
    const timer = window.setInterval(() => void refresh(true), 3000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const running = Boolean(status?.running);

  const start = async () => {
    if (busy) return;
    if (!strategyKey) {
      setError("先选一个策略。");
      return;
    }
    setBusy("start");
    setError("");
    setNotice("");
    try {
      await api("/api/auto/start", {
        method: "POST",
        body: JSON.stringify({
          symbol,
          market,
          timeframe,
          strategy: strategyKey,
          params: strategies.find((item) => item.key === strategyKey)?.defaults ?? {},
          position_pct: positionPct,
          leverage,
          poll_seconds: pollSeconds,
        }),
      });
      setNotice("已启动。到下面看信号和日志。");
      await refresh(false);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy("");
    }
  };

  const stop = async () => {
    if (busy) return;
    setBusy("stop");
    setError("");
    try {
      await api("/api/auto/stop", { method: "POST" });
      setNotice("已停止。");
      await refresh(false);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy("");
    }
  };

  const lastSignal = status?.last_signal ?? null;
  const data = status?.data ?? null;
  const events = [...(status?.events ?? [])].reverse().slice(0, 60);

  return (
    <section className="card">
      <div className="card-head">
        <span className="card-title">执行台</span>
        <span className="spacer" />
        <span className={`state-chip ${running ? "running" : "stopped"}`}>
          {running ? "运行中" : "已停止"}
        </span>
      </div>
      <div className="card-body">
        {error && <div className="banner error">{error}</div>}
        {notice && <div className="banner ok">{notice}</div>}

        <div className="form-grid">
          <div className="field">
            <label className="field-label" htmlFor="exec-strategy">
              <span>策略</span>
            </label>
            <select
              id="exec-strategy"
              className="select"
              value={strategyKey}
              disabled={running}
              onChange={(event) => setStrategyKey(event.target.value)}
            >
              {strategies.map((item) => (
                <option key={item.key} value={item.key}>
                  {item.name}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label className="field-label" htmlFor="exec-pct">
              <span>仓位比例</span>
              <em>占权益</em>
            </label>
            <input
              id="exec-pct"
              className="input"
              type="number"
              min={1}
              max={100}
              step={5}
              disabled={running}
              value={Math.round(positionPct * 100)}
              onChange={(event) => setPositionPct(Number(event.target.value) / 100)}
            />
          </div>
          <div className="field">
            <label className="field-label" htmlFor="exec-lev">
              <span>杠杆</span>
              <em>合约才用</em>
            </label>
            <input
              id="exec-lev"
              className="input"
              type="number"
              min={1}
              max={125}
              step={1}
              disabled={running}
              value={leverage}
              onChange={(event) => setLeverage(Number(event.target.value))}
            />
          </div>
          <div className="field">
            <label className="field-label" htmlFor="exec-poll">
              <span>轮询间隔</span>
              <em>秒</em>
            </label>
            <input
              id="exec-poll"
              className="input"
              type="number"
              min={1}
              step={1}
              disabled={running}
              value={pollSeconds}
              onChange={(event) => setPollSeconds(Number(event.target.value))}
            />
          </div>
        </div>

        <div className="exec-actions">
          <span className="faint" style={{ fontSize: "var(--fs-12-5)"}}>
            {symbol} · {TIMEFRAMES.find(([value]) => value === timeframe)?.[1] ?? timeframe}
          </span>
          <span className="spacer" />
          {running ? (
            <button type="button" className="btn-sm" disabled={busy !== ""} onClick={stop}>
              停止
            </button>
          ) : (
            <button type="button" className="btn-sm primary" disabled={busy !== ""} onClick={start}>
              启动执行
            </button>
          )}
        </div>

        {/* data 是这一轮的 K 线供给：根数不够时引擎在跑却什么都不做，
            只有后端那句话能说清原因。够用时就整块不出现。 */}
        {data?.short && (
          <div className="banner warn" role="status">
            {data.message || "K 线不够，这一轮不会出信号。"}
          </div>
        )}

        <div className="gate-list">
          <div className="gate-row">
            <span>当前信号</span>
            <b>
              {lastSignal
                ? `${lastSignal.reason} · 目标 ${fmt(lastSignal.target * 100, 0)}%`
                : "还没有信号"}
            </b>
          </div>
          {data?.short && (
            <div className="gate-row">
              <span>K 线</span>
              <b>
                {data.fetched_bars} / {data.required_bars} 根 · 单次上限 {data.max_bars}
              </b>
            </div>
          )}
        </div>

        <div className="exec-log">
          {!events.length && <div className="empty-note">没有日志。</div>}
          {events.map((item) => (
            <div key={item.seq} className={`exec-log-row ${item.level}`}>
              <span className="exec-log-kind">{STATE_LABEL[item.kind] ?? item.kind}</span>
              <span className="exec-log-text">{item.message}</span>
              <span className="exec-log-time faint">{item.when}</span>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}
