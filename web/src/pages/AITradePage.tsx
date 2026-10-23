import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, query } from "../api";
import { MARKET_LABELS, TIMEFRAMES, fmt, fmtPrice } from "../format";
import type {
  Account,
  AIAutoSettings,
  AITraderStatus,
  AppSettings,
  Health,
  PipelineStage,
  RiskState,
  Strategy,
  SymbolInfo,
} from "../types";
import MetricGrid from "../components/MetricGrid";

/** 全自动 AI 交易：一次决策走完整条流水线，人只负责开关和上限。 */

interface Props {
  settings: AppSettings | null;
  health: Health | null;
}

const STATE_LABEL: Record<string, string> = {
  stopped: "已停止",
  running: "运行中",
  paused: "已暂停",
  halted: "熔断",
};

const OUTCOME_LABEL: Record<string, string> = {
  order: "下单",
  reject: "拒绝",
  skip: "跳过",
  error: "下单失败",
};

const ACTION_LABEL: Record<string, string> = {
  open_long: "开多",
  open_short: "开空",
  close_long: "平多",
  close_short: "平空",
  hold: "观望",
};

const DEPTHS: [string, string, string][] = [
  ["quick", "快速", "只跑裁决，一次调用"],
  ["standard", "标准", "技术面 → 辩论 → 裁决"],
  ["deep", "深度", "再加新闻面与风控复核"],
];

const FALLBACK_AUTO: AIAutoSettings = {
  enabled: false,
  symbol: "",
  timeframe: "",
  leverage: 1,
  max_order_quote: 50,
  max_position_quote: 200,
  max_decisions_per_day: 96,
  max_orders_per_hour: 12,
  min_confidence: 0.6,
  decision_ttl_sec: 120,
  max_price_drift_pct: 0.005,
  cooldown_sec: 30,
  poll_sec: 60,
  require_stop: true,
  depth: "standard",
};

function clockText(value: number | undefined): string {
  if (!value) return "—";
  const date = new Date(value * 1000);
  return date.toLocaleTimeString("zh-CN", { hour12: false });
}

export default function AITradePage({ settings, health }: Props) {
  const [status, setStatus] = useState<AITraderStatus | null>(null);
  const [account, setAccount] = useState<Account | null>(null);
  const [risk, setRisk] = useState<RiskState | null>(null);
  const [strategies, setStrategies] = useState<Strategy[]>([]);
  const [symbols, setSymbols] = useState<SymbolInfo[]>([]);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState("");
  const [openSeq, setOpenSeq] = useState<number | null>(null);

  const [market, setMarket] = useState(settings?.market_type ?? "spot");
  const [symbol, setSymbol] = useState(settings?.last_symbol ?? "");
  const [timeframe, setTimeframe] = useState(settings?.timeframe ?? "15m");
  const [strategyKey, setStrategyKey] = useState("");
  const [auto, setAuto] = useState<AIAutoSettings>(FALLBACK_AUTO);
  const [depth, setDepth] = useState("standard");

  /* ------------------------------------------------------------ settings */
  useEffect(() => {
    if (!settings) return;
    const next = { ...FALLBACK_AUTO, ...(settings.ai_auto ?? {}) };
    setAuto(next);
    setDepth(next.depth || "standard");
    if (next.symbol) setSymbol((current) => current || next.symbol);
    if (next.timeframe) setTimeframe((current) => current || next.timeframe);
    setMarket((current) => current || settings.market_type);
  }, [settings]);

  useEffect(() => {
    api<{ items: Strategy[] }>("/api/strategies")
      .then((data) => {
        setStrategies(data.items ?? []);
        setStrategyKey((current) => current || data.items?.[0]?.key || "");
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

  /* -------------------------------------------------------------- polling */
  /** 轮询会重叠：后端慢的时候上一条还没回来，下一条已经发出去了。
   *  不给回执对号的话，先发后到的旧状态会把新状态盖回去——「已停止」闪成「运行中」，
   *  自动交易明明停着，界面说在跑。 */
  const statusSeq = useRef(0);

  const refresh = useCallback(async () => {
    const id = ++statusSeq.current;
    try {
      const next = await api<AITraderStatus>("/api/ai/trader/status?since=0");
      if (id !== statusSeq.current) return;
      setStatus(next);
    } catch (caught) {
      if (id !== statusSeq.current) return;
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 4000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const refreshAccount = useCallback(async () => {
    try {
      setAccount(await api<Account>(`/api/portfolio?${query({ market })}`));
    } catch {
      // 账户只是参考，读不到不影响启停。
    }
    try {
      setRisk(await api<RiskState>("/api/risk"));
    } catch {
      // 同上。
    }
  }, [market]);

  useEffect(() => {
    void refreshAccount();
    const timer = window.setInterval(() => void refreshAccount(), 8000);
    return () => window.clearInterval(timer);
  }, [refreshAccount]);

  /* -------------------------------------------------------------- actions */
  const patch = (values: Partial<AIAutoSettings>) =>
    setAuto((current) => ({ ...current, ...values }));

  const saveDepth = async (value: string) => {
    setDepth(value);
    try {
      await api("/api/settings", {
        method: "PUT",
        body: JSON.stringify({ ai_auto: { depth: value } }),
      });
    } catch {
      // 存不上就只在这次启动里生效，不挡流程。
    }
  };

  const start = async () => {
    if (busy) return;
    if (!symbol) {
      setError("先选一个交易对。");
      return;
    }
    if (!strategyKey) {
      setError("先选一个产生信号的策略。");
      return;
    }
    const mainnet = (health?.network ?? "testnet") === "mainnet";
    if (mainnet) {
      const ok = window.confirm(
        `即将在主网启动 AI 交易\n\n${symbol} · ${timeframe} · 深度 ${depth}\n\n` +
          "模型会真实下单，盈亏由交易所结算。确认继续？",
      );
      if (!ok) return;
    }
    setBusy("start");
    setError("");
    setNotice("");
    try {
      const selected = strategies.find((item) => item.key === strategyKey);
      const result = await api<{ ok: boolean; status: AITraderStatus }>("/api/ai/trader/start", {
        method: "POST",
        body: JSON.stringify({
          confirm_live: mainnet,
          market,
          symbol,
          timeframe,
          strategy: strategyKey,
          params: auto.strategy_params?.[strategyKey] ?? { ...(selected?.defaults ?? {}) },
          max_order_quote: auto.max_order_quote,
          max_position_quote: auto.max_position_quote,
          depth,
        }),
      });
      setStatus(result.status);
      setNotice(`已启动：${symbol} · ${timeframe} · ${depth}`);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy("");
    }
  };

  const control = async (path: string, body?: Record<string, unknown>, note = "") => {
    if (busy) return;
    setBusy(path);
    setError("");
    setNotice("");
    try {
      const result = await api<{ ok: boolean; status?: AITraderStatus }>(path, {
        method: "POST",
        body: body ? JSON.stringify(body) : undefined,
      });
      if (result.status) setStatus(result.status);
      await refresh();
      if (note) setNotice(note);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy("");
    }
  };

  /* ---------------------------------------------------------------- render */
  const state = status?.state ?? "stopped";
  const running = state === "running" || state === "paused";
  const paused = Boolean(status?.paused) || state === "paused";
  const counters = status?.counters ?? {};
  const events = useMemo(
    () => [...(status?.events ?? [])].reverse(),
    [status?.events],
  );
  const blocked = events.filter((item) => item.outcome === "reject").length;

  const equity = Number(account?.equity ?? 0);
  const dayPnl = Number(risk?.day_pnl ?? 0);

  return (
    <>
      <div className="page-head">
        <h1 className="page-title">AI 交易</h1>
        <span className="page-sub">全自动：一根 K 线一次决策，人只管开关和上限</span>
        <span className="spacer" />
        <span className={`state-chip ${state}`}>{STATE_LABEL[state] ?? state}</span>
        {running ? (
          <>
            <button
              type="button"
              className="btn-sm"
              disabled={busy !== ""}
              onClick={() =>
                void control("/api/ai/trader/pause", { paused: !paused }, paused ? "已继续。" : "已暂停。")
              }
            >
              {paused ? "继续" : "暂停"}
            </button>
            <button
              type="button"
              className="btn-sm"
              disabled={busy !== ""}
              onClick={() => void control("/api/ai/trader/stop", undefined, "已停止。")}
            >
              停止
            </button>
          </>
        ) : (
          <button type="button" className="btn-sm primary" disabled={busy !== ""} onClick={start}>
            启动
          </button>
        )}
        <button
          type="button"
          className="btn-sm ai-danger"
          disabled={busy !== ""}
          onClick={() => {
            if (window.confirm("急停会立刻停掉 AI 交易并平掉它挂着的保护单。确认？")) {
              void control("/api/ai/trader/kill", { reason: "人工急停" }, "已急停。");
            }
          }}
        >
          急停
        </button>
      </div>

      {error && <div className="banner error">{error}</div>}
      {notice && <div className="banner ok">{notice}</div>}
      {state === "halted" && (
        <div className="banner warn">
          熔断中：{status?.halted_reason || "原因未记录"}。处理完再启动。
        </div>
      )}

      <div className="ai-grid">
        <section className="card">
          <div className="card-head">
            <span className="card-title">运行配置</span>
            <span className="spacer" />
            <span className="faint" style={{ fontSize: "var(--fs-12)"}}>
              {MARKET_LABELS[market] ?? market}
            </span>
          </div>
          <div className="card-body form-grid">
            <div className="field">
              <label className="field-label" htmlFor="ai-market">
                <span>市场</span>
              </label>
              <select
                id="ai-market"
                className="select"
                value={market}
                disabled={running}
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
              <label className="field-label" htmlFor="ai-symbol">
                <span>交易对</span>
              </label>
              <select
                id="ai-symbol"
                className="select"
                value={symbol}
                disabled={running}
                onChange={(event) => setSymbol(event.target.value)}
              >
                {symbols.map((item) => (
                  <option key={item.symbol} value={item.symbol}>
                    {item.display || item.symbol}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label className="field-label" htmlFor="ai-timeframe">
                <span>K 线周期</span>
              </label>
              <select
                id="ai-timeframe"
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
            <div className="field">
              <label className="field-label" htmlFor="ai-strategy">
                <span>信号策略</span>
              </label>
              <select
                id="ai-strategy"
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

            <div className="field" style={{ gridColumn: "1 / -1" }}>
              <span className="field-label">
                <span>决策深度</span>
                <em>越深越慢，也越贵</em>
              </span>
              <div className="depth-pick">
                {DEPTHS.map(([value, label, hint]) => (
                  <button
                    key={value}
                    type="button"
                    aria-pressed={depth === value}
                    disabled={running}
                    onClick={() => void saveDepth(value)}
                  >
                    <b>{label}</b>
                    <span>{hint}</span>
                  </button>
                ))}
              </div>
            </div>

            <div className="field">
              <label className="field-label" htmlFor="ai-order-quote">
                <span>单笔上限</span>
                <em>USDT</em>
              </label>
              <input
                id="ai-order-quote"
                className="input"
                type="number"
                min={0}
                step={10}
                value={auto.max_order_quote}
                onChange={(event) => {
                  // 清空输入框时 value 是空串，Number("") 是 0，而后端的
                  // 0 等于「不限制」——一清空就把单笔上限关了。半截数字是 NaN，
                  // JSON 会变成 null。两种都不该写进配置。
                  const raw = event.target.value.trim();
                  if (raw === "") return;
                  const next = Number(raw);
                  if (!Number.isFinite(next) || next < 0) return;
                  patch({ max_order_quote: next });
                }}
              />
            </div>
            <div className="field">
              <label className="field-label" htmlFor="ai-position-quote">
                <span>持仓上限</span>
                <em>USDT</em>
              </label>
              <input
                id="ai-position-quote"
                className="input"
                type="number"
                min={0}
                step={10}
                value={auto.max_position_quote}
                onChange={(event) => {
                  const raw = event.target.value.trim();
                  if (raw === "") return;
                  const next = Number(raw);
                  if (!Number.isFinite(next) || next < 0) return;
                  patch({ max_position_quote: next });
                }}
              />
            </div>
            <div className="hint" style={{ gridColumn: "1 / -1" }}>
              上限在启动时生效。改完要重新启动一次。
            </div>
          </div>
        </section>

        <section className="card">
          <div className="card-head">
            <span className="card-title">决策时间线</span>
            <span className="spacer" />
            <span className="faint" style={{ fontSize: "var(--fs-12)"}}>
              今日 {counters.decisions_today ?? 0}/{counters.max_decisions_per_day ?? 0} 次决策 ·
              拒绝 {blocked} 次
            </span>
          </div>
          <div className="card-body">
            {!events.length && (
              <div className="empty-note">还没有决策。启动后每根 K 线记一条。</div>
            )}
            {!!events.length && (
            <ul className="timeline">
              {events.map((record) => {
                const key = record.seq ?? 0;
                const open = openSeq === key;
                const trace = record.trace ?? [];
                return (
                  <li key={key} className={`timeline-item ${record.outcome ?? ""}`}>
                    <button
                      type="button"
                      className="timeline-head"
                      aria-expanded={open}
                      onClick={() => setOpenSeq(open ? null : key)}
                    >
                      <span className="timeline-time">{clockText(record.at)}</span>
                      <span className={`ai-tag ${record.outcome ?? "info"}`}>
                        {OUTCOME_LABEL[record.outcome ?? ""] ?? record.outcome ?? "记录"}
                      </span>
                      <span className="timeline-action">
                        {ACTION_LABEL[record.action ?? ""] ?? record.action ?? "—"}
                        {record.quote ? ` · ${fmt(record.quote, 2)}` : ""}
                      </span>
                      <span className="timeline-reason faint">
                        {record.reason || record.rationale || ""}
                      </span>
                      <span className="spacer" />
                      <span className="faint">
                        {open ? "收起" : trace.length ? `${trace.length} 段` : ""}
                      </span>
                    </button>
                    {open && (
                      <div className="stage-list">
                        {!trace.length && <div className="faint">这条记录没有分段输出。</div>}
                        {trace.map((stage: PipelineStage) => (
                          <div
                            key={stage.stage}
                            className={`stage-card${stage.ok ? "" : " bad"}${
                              stage.skipped ? " skipped" : ""
                            }`}
                          >
                            <div className="stage-card-head">
                              <b>{stage.title}</b>
                              <span className="faint">
                                {stage.skipped
                                  ? "跳过"
                                  : stage.ok
                                    ? `${stage.tokens ?? 0} tokens · ${stage.elapsed ?? 0}s`
                                    : "没有结果"}
                              </span>
                            </div>
                            <div className="stage-text">{stage.text}</div>
                          </div>
                        ))}
                        {record.decision_id && (
                          <div className="faint" style={{ fontSize: "var(--fs-12)"}}>
                            决策 {record.decision_id}
                            {record.order_id ? ` · 订单 ${record.order_id}` : ""}
                          </div>
                        )}
                      </div>
                    )}
                  </li>
                );
              })}
            </ul>
            )}
          </div>
        </section>

        <section className="card">
          <div className="card-head">
            <span className="card-title">账户与风控</span>
            <span className="spacer" />
            <span className="faint" style={{ fontSize: "var(--fs-12)"}}>
              {health?.network === "mainnet" ? "主网" : "测试网"}
            </span>
          </div>
          <div className="card-body">
            <MetricGrid
              items={[
                { label: "权益", value: fmt(equity, 2) },
                {
                  label: "当日盈亏",
                  value: fmt(dayPnl, 2),
                  tone: dayPnl > 0 ? "up" : dayPnl < 0 ? "down" : "muted",
                },
                {
                  label: "今日成交",
                  value: `${risk?.day_trades ?? 0}/${risk?.limits.max_daily_trades ?? 0}`,
                },
              ]}
            />
            <div className="gate-list">
              <div className="gate-row">
                <span>熔断</span>
                <b className={risk?.halted ? "down" : "up"}>
                  {risk?.halted ? risk?.halted_reason || "已触发" : "正常"}
                </b>
              </div>
              <div className="gate-row">
                <span>急停开关</span>
                <b className={risk?.kill_switch ? "down" : "up"}>
                  {risk?.kill_switch ? "已按下" : "未按下"}
                </b>
              </div>
              <div className="gate-row">
                <span>连续亏损</span>
                <b>{risk?.consecutive_losses ?? 0} 次</b>
              </div>
              <div className="gate-row">
                <span>运行中的规则</span>
                <b>
                  {running
                    ? `${status?.config?.symbol ?? ""} · ${status?.config?.timeframe ?? ""}`
                    : "未启动"}
                </b>
              </div>
            </div>
            <div className="mini-table">
              <div className="mini-head">
                <span>最近成交</span>
              </div>
              {(account?.trades ?? []).slice(0, 6).map((trade) => (
                <div key={String(trade.id)} className="mini-row">
                  <span>{trade.symbol}</span>
                  <span className={trade.side === "buy" ? "up" : "down"}>
                    {trade.side === "buy" ? "买入" : "卖出"}
                  </span>
                  <span className="num">{fmtPrice(trade.price)}</span>
                  <span className="num faint">{fmt(trade.amount, 4)}</span>
                </div>
              ))}
              {!(account?.trades ?? []).length && <div className="faint">暂无成交。</div>}
            </div>
          </div>
        </section>
      </div>
    </>
  );
}
