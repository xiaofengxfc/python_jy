import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, query } from "../api";
import { TIMEFRAMES, fmt, fmtPrice } from "../format";
import type { AutoStatus, Health, SymbolInfo, Strategy } from "../types";
import { ParamFields } from "../components/ParamFields";

interface Props {
  /** Rendered inside the trading page: no page head, and the market / symbol /
   * timeframe follow the chart above instead of being chosen twice. */
  embedded?: boolean;
  market?: string;
  symbol?: string;
  timeframe?: string;
}

export default function AutoPage({
  embedded = false,
  market: sharedMarket,
  symbol: sharedSymbol,
  timeframe: sharedTimeframe,
}: Props = {}) {
  const [strategies, setStrategies] = useState<Strategy[]>([]);
  const [symbols, setSymbols] = useState<SymbolInfo[]>([]);
  const [status, setStatus] = useState<AutoStatus | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const [market, setMarket] = useState("spot");
  const [symbol, setSymbol] = useState("BTC/USDT");
  const [timeframe, setTimeframe] = useState("15m");
  const [strategyKey, setStrategyKey] = useState("");
  const [params, setParams] = useState<Record<string, unknown>>({});
  const [positionPct, setPositionPct] = useState(1);
  const [leverage, setLeverage] = useState(1);
  const [pollSeconds, setPollSeconds] = useState(10);
  /** Which network the server is on: only the mainnet costs a confirmation. */
  const [network, setNetwork] = useState("mainnet");
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const seq = useRef(0);

  const strategy = useMemo(
    () => strategies.find((item) => item.key === strategyKey) ?? null,
    [strategies, strategyKey],
  );

  useEffect(() => {
    api<{ items: Strategy[] }>("/api/strategies")
      .then((data) => {
        setStrategies(data.items);
        const first = data.items[0];
        if (first) {
          setStrategyKey(first.key);
          setParams({ ...first.defaults });
        }
      })
      .catch((caught) =>
        setError(caught instanceof Error ? caught.message : String(caught)),
      );
  }, []);

  useEffect(() => {
    api<Health>("/api/health")
      .then((data) => setNetwork(data.network))
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    api<{ items: SymbolInfo[] }>(`/api/symbols?${query({ market })}`)
      .then((data) => {
        setSymbols(data.items);
        setSymbol((current) =>
          data.items.some((item) => item.symbol === current)
            ? current
            : (data.items[0]?.symbol ?? current),
        );
      })
      .catch(() => setSymbols([]));
  }, [market]);

  const refresh = useCallback(async (incremental: boolean) => {
    try {
      const next = await api<AutoStatus>(
        `/api/auto/status?${query({ since: incremental ? seq.current : 0 })}`,
      );
      seq.current = next.seq;
      setStatus((current) => {
        if (!incremental || !current) return next;
        return { ...next, events: [...current.events, ...next.events].slice(-400) };
      });
    } catch {
      /* keep the last snapshot */
    }
  }, []);

  useEffect(() => {
    void refresh(false);
    const timer = window.setInterval(() => refresh(true), 2000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const running = status?.running ?? false;

  // Embedded in the trading page: the pair and timeframe are the ones already
  // on the chart, so an auto run can never quietly trade a different symbol
  // than the one the user is looking at. Changes while running are ignored
  // (the backend owns the running config).
  useEffect(() => {
    if (!embedded || running || !sharedMarket) return;
    setMarket(sharedMarket);
  }, [embedded, running, sharedMarket]);
  useEffect(() => {
    if (!embedded || running || !sharedSymbol) return;
    setSymbol(sharedSymbol);
  }, [embedded, running, sharedSymbol]);
  useEffect(() => {
    if (!embedded || running || !sharedTimeframe) return;
    setTimeframe(sharedTimeframe);
  }, [embedded, running, sharedTimeframe]);

  const start = async () => {
    setBusy(true);
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
          params,
          position_pct: positionPct,
          leverage: market === "spot" ? 1 : leverage,
          poll_seconds: pollSeconds,
          // Testnet runs need no acknowledgement; mainnet does, and the
          // checkbox above is the acknowledgement.
          confirm_live: confirmed,
        }),
      });
      setNotice(`量化执行引擎已启动：${strategy?.name ?? strategyKey} · ${symbol}`);
      seq.current = 0;
      await refresh(false);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  };

  const stop = async () => {
    setBusy(true);
    try {
      await api("/api/auto/stop", { method: "POST" });
      setNotice("已停止量化执行引擎。");
      seq.current = 0;
      await refresh(false);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  };

  const pickStrategy = (value: string) => {
    setStrategyKey(value);
    const found = strategies.find((item) => item.key === value);
    setParams(found ? { ...found.defaults } : {});
  };

  return (
    <>
      {embedded ? (
        <div className="card-head auto-head">
          <span className="card-title">量化执行引擎</span>
          <span className={`live-badge ${running ? "" : "idle"}`}>
            <span className={`dot ${running ? "live" : ""}`} />
            {running ? "运行中" : "未运行"}
          </span>
          <span className="faint" style={{ fontSize: 11 }}>
            跟随图表：{market} · {symbol || "—"} · {timeframe}
          </span>
          <span className="spacer" />
          {running ? (
            <button type="button" className="btn-sm" disabled={busy} onClick={stop}>
              {busy ? "处理中…" : "停止"}
            </button>
          ) : (
            <button
              type="button"
              className="btn-sm primary"
              disabled={busy || !strategyKey || (network === "mainnet" && !confirmed)}
              onClick={start}
            >
              {busy ? "启动中…" : "启动"}
            </button>
          )}
        </div>
      ) : (
        <div className="page-head">
          <h1 className="page-title">量化执行引擎</h1>
          <span className={`live-badge ${running ? "" : "idle"}`}>
            <span className={`dot ${running ? "live" : ""}`} />
            {running ? "运行中" : "未运行"}
          </span>
          <span className="spacer" />
          {running ? (
            <button type="button" className="btn-sm" disabled={busy} onClick={stop}>
              {busy ? "处理中…" : "停止"}
            </button>
          ) : (
            <button
              type="button"
              className="btn-sm primary"
              disabled={busy || !strategyKey || (network === "mainnet" && !confirmed)}
              onClick={start}
            >
              {busy ? "启动中…" : "启动"}
            </button>
          )}
        </div>
      )}

      <>
          {error && <div className="banner error">{error}</div>}
          {notice && <div className="banner">{notice}</div>}

          <div className={embedded ? "auto-section" : "stack"}>
        <div className="stack">
          <div className="card">
            <div className="card-head">
              <span className="card-title">策略与市场</span>
            </div>
            <div className="card-body stack">
              <div className="form-grid">
                {!embedded && (
                  <>
                    <div className="field">
                      <label className="field-label" htmlFor="auto-market">
                        <span>市场</span>
                      </label>
                      <select
                        id="auto-market"
                        className="select"
                        value={market}
                        disabled={running}
                        onChange={(event) => setMarket(event.target.value)}
                      >
                        <option value="spot">现货</option>
                        <option value="usdm">U 本位合约</option>
                        <option value="coinm">币本位合约</option>
                      </select>
                    </div>
                    <div className="field">
                      <label className="field-label" htmlFor="auto-symbol">
                        <span>交易对</span>
                      </label>
                      <select
                        id="auto-symbol"
                        className="select"
                        value={symbol}
                        disabled={running}
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
                      <label className="field-label" htmlFor="auto-timeframe">
                        <span>K 线周期</span>
                      </label>
                      <select
                        id="auto-timeframe"
                        className="select"
                        value={timeframe}
                        disabled={running}
                        onChange={(event) => setTimeframe(event.target.value)}
                      >
                        {TIMEFRAMES.map(([value, label]) => (
                          <option key={value} value={value}>
                            {label}
                          </option>
                        ))}
                      </select>
                    </div>
                  </>
                )}
                <div className="field">
                  <label className="field-label" htmlFor="auto-confirmed">
                    <span>下单环境</span>
                    <em>{network === "mainnet" ? "主网 · 真实资金" : "测试网 · 模拟资金"}</em>
                  </label>
                  {network === "mainnet" ? (
                    <label className="check" htmlFor="auto-confirmed">
                      <input
                        id="auto-confirmed"
                        type="checkbox"
                        checked={confirmed}
                        disabled={running}
                        onChange={(event) => setConfirmed(event.target.checked)}
                      />
                      我确认引擎会在主网自动下单
                    </label>
                  ) : (
                    <span className="faint" style={{ fontSize: 12 }}>
                      测试网下单不涉及真实资金，无需确认。
                    </span>
                  )}
                </div>
              </div>

              <div className="field">
                <label className="field-label" htmlFor="auto-strategy">
                  <span>策略</span>
                  <em>{strategy?.allow_short ? "支持做空" : "仅做多"}</em>
                </label>
                <select
                  id="auto-strategy"
                  className="select"
                  value={strategyKey}
                  disabled={running}
                  onChange={(event) => pickStrategy(event.target.value)}
                >
                  {strategies.map((item) => (
                    <option key={item.key} value={item.key}>
                      {item.name}
                    </option>
                  ))}
                </select>
                {strategy?.description && (
                  <div className="hint">{strategy.description}</div>
                )}
              </div>

              <div>
                <div className="section-title">策略参数</div>
                <ParamFields
                  specs={strategy?.params ?? []}
                  values={params}
                  onChange={(name, value) =>
                    setParams((current) => ({ ...current, [name]: value }))
                  }
                />
              </div>

              <div className="form-grid">
                <div className="field">
                  <label className="field-label" htmlFor="auto-pct">
                    <span>每次投入比例</span>
                    <em>{(positionPct * 100).toFixed(0)}%</em>
                  </label>
                  <input
                    id="auto-pct"
                    className="input"
                    type="range"
                    min={5}
                    max={100}
                    step={5}
                    disabled={running}
                    value={positionPct * 100}
                    onChange={(event) => setPositionPct(Number(event.target.value) / 100)}
                  />
                </div>
                <div className="field">
                  <label className="field-label" htmlFor="auto-poll">
                    <span>轮询间隔</span>
                    <em>{pollSeconds} 秒</em>
                  </label>
                  <input
                    id="auto-poll"
                    className="input"
                    type="number"
                    min={1}
                    step={1}
                    disabled={running}
                    value={pollSeconds}
                    onChange={(event) => setPollSeconds(Number(event.target.value))}
                  />
                </div>
                {market !== "spot" && (
                  <div className="field">
                    <label className="field-label" htmlFor="auto-lev">
                      <span>杠杆</span>
                      <em>{leverage}×</em>
                    </label>
                    <input
                      id="auto-lev"
                      className="input"
                      type="number"
                      min={1}
                      max={125}
                      disabled={running}
                      value={leverage}
                      onChange={(event) => setLeverage(Number(event.target.value))}
                    />
                  </div>
                )}
              </div>

              {network === "mainnet" && (
                <div className="banner error">
                  自动交易会真实下单。请确认 API Key 已关闭提现权限，风控上限按资金规模设置。
                </div>
              )}

              {status?.config && running && (
                <div className="preview">
                  <div className="preview-row">
                    <span>当前运行</span>
                    <strong>
                      {status.config.symbol} · {status.config.timeframe}
                    </strong>
                  </div>
                  <div className="preview-row">
                    <span>策略</span>
                    <strong>
                      {strategies.find((item) => item.key === status.config?.strategy)?.name ??
                        status.config.strategy}
                    </strong>
                  </div>
                  <div className="preview-row">
                    <span>投入</span>
                    <strong>{(status.config.position_pct * 100).toFixed(0)}%</strong>
                  </div>
                </div>
              )}
            </div>
          </div>

          <div className="card">
            <div className="card-head">
              <span className="card-title">最近信号</span>
            </div>
            <div className="card-body">
              {status?.last_signal ? (
                <div className="preview">
                  <div className="preview-row">
                    <span>目标仓位</span>
                    <strong className={status.last_signal.target >= 0 ? "up" : "down"}>
                      {fmt(status.last_signal.target * 100, 0)}%
                    </strong>
                  </div>
                  <div className="preview-row">
                    <span>依据</span>
                    <strong style={{ textAlign: "right" }}>{status.last_signal.reason}</strong>
                  </div>
                  <div className="preview-row">
                    <span>时间</span>
                    <strong className="num">{status.last_signal.when}</strong>
                  </div>
                </div>
              ) : (
                <div className="hint">还没有产生信号。策略启动后会在每根 K 线收盘时判断。</div>
              )}
            </div>
          </div>
        </div>

        <div className="card">
          <div className="card-head">
            <span className="card-title">运行日志</span>
            <span className="spacer" />
            <span className="faint" style={{ fontSize: 11 }}>
              {status?.events.length ?? 0} 条 · 每 2 秒刷新
            </span>
          </div>
          <div className="feed">
            {(status?.events.length ?? 0) === 0 ? (
              <div className="empty">暂无日志</div>
            ) : (
              [...(status?.events ?? [])]
                .reverse()
                .map((event) => (
                  <div key={event.seq} className="feed-row">
                    <span className="feed-when num">{event.when}</span>
                    <span
                      className={`feed-kind ${
                        event.kind === "signal"
                          ? "signal"
                          : event.level === "ERROR"
                            ? "error"
                            : event.level === "WARNING"
                              ? "warn"
                              : event.kind === "state"
                                ? "state"
                                : ""
                      }`}
                    >
                      {event.kind === "signal" ? "信号" : event.kind === "state" ? "状态" : "日志"}
                    </span>
                    <span>{event.message}</span>
                  </div>
                ))
            )}
          </div>
        </div>
      </div>
        </>
    </>
  );
}

export { fmtPrice };
