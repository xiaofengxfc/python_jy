import { useCallback, useEffect, useRef, useState } from "react";
import { api, query } from "../api";
import { fmt, fmtPrice } from "../format";
import type { Strategy } from "../types";

interface Props {
  market: string;
  symbol: string;
  timeframe: string;
  onNavigate?: (page: "lab" | "backtest") => void;
}

/** One record written by `engine/ai_trader.py::_record`. The audit file also
 * holds `lifecycle` rows (start/stop/pause), so every field is optional. */
interface AIDecisionRecord {
  at?: number;
  seq?: number;
  kind?: string;
  event?: string;
  outcome?: string;
  reason?: string;
  decision_id?: string;
  action?: string;
  symbol?: string;
  quote?: number;
  price?: number | null;
  confidence?: number;
  signal_target?: number | null;
  stop_loss?: number | null;
  rationale?: string;
  order_id?: string | null;
  network?: string;
  state?: string;
}

interface AITraderConfig {
  symbol?: string;
  market_type?: string;
  timeframe?: string;
  live_enabled?: boolean;
  max_order_quote?: number;
  max_position_quote?: number;
}

interface AITraderCounters {
  decisions_today?: number;
  orders_last_hour?: number;
  max_decisions_per_day?: number;
  max_orders_per_hour?: number;
}

interface AITraderStatus {
  state?: string;
  paused?: boolean;
  halted_reason?: string;
  config?: AITraderConfig;
  counters?: AITraderCounters;
  last_decision?: AIDecisionRecord | null;
  events?: AIDecisionRecord[];
  seq?: number;
}

/** The subset of `AppSettings.ai_auto` this panel reads and writes. */
interface AIAutoSettings {
  enabled: boolean;
  live_enabled: boolean;
  symbol: string;
  timeframe: string;
  leverage: number;
  max_order_quote: number;
  max_position_quote: number;
  max_decisions_per_day: number;
  max_orders_per_hour: number;
  min_confidence: number;
  decision_ttl_sec: number;
  max_price_drift_pct: number;
  cooldown_sec: number;
  poll_sec: number;
  require_stop: boolean;
  /** Best params explicitly applied from the strategy lab, by strategy key. */
  strategy_params?: Record<string, Record<string, unknown>>;
}

const DEFAULT_AUTO: AIAutoSettings = {
  enabled: false,
  live_enabled: false,
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
  strategy_params: {},
};

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

const EVENT_LABEL: Record<string, string> = {
  start: "启动",
  stop: "停止",
  pause: "暂停",
  resume: "继续",
  halt: "熔断",
};

function pad(value: number): string {
  return String(value).padStart(2, "0");
}

/** `at` is Unix seconds (float), not milliseconds. */
function clockOf(seconds: number | undefined): string {
  if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds <= 0) return "—";
  const date = new Date(seconds * 1000);
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

function stampOf(seconds: number | undefined): string {
  if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds <= 0) return "—";
  const date = new Date(seconds * 1000);
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
  );
}

function outcomeLabel(item: AIDecisionRecord): string {
  if (item.kind === "lifecycle") return "状态";
  const key = item.outcome ?? "";
  return OUTCOME_LABEL[key] ?? item.outcome ?? "记录";
}

function outcomeClass(item: AIDecisionRecord): string {
  if (item.kind === "lifecycle") return "info";
  const key = item.outcome ?? "";
  if (key === "order" || key === "reject" || key === "skip") return key;
  return "info";
}

function actionLabel(action: string | undefined): string {
  if (!action) return "";
  return ACTION_LABEL[action] ?? action;
}

/** One line describing a decision without repeating the reason (the row shows
 * it separately in a muted colour). */
function decisionLine(item: AIDecisionRecord): string {
  if (item.kind === "lifecycle") {
    const name = item.event ?? "";
    return `生命周期：${EVENT_LABEL[name] ?? name ?? "变更"}`;
  }
  const parts: string[] = [];
  const action = actionLabel(item.action);
  if (action) parts.push(action);
  if (typeof item.quote === "number" && item.quote > 0) {
    parts.push(`${fmt(item.quote, 2)} USDT`);
  }
  if (typeof item.price === "number" && item.price > 0) {
    parts.push(`@ ${fmtPrice(item.price)}`);
  }
  if (typeof item.confidence === "number" && item.confidence > 0) {
    parts.push(`置信 ${(item.confidence * 100).toFixed(0)}%`);
  }
  return parts.join(" · ") || "决策";
}

function NumberField(props: {
  id: string;
  label: string;
  hint?: string;
  value: number;
  min?: number;
  max?: number;
  step?: number;
  disabled?: boolean;
  onChange: (value: number) => void;
}) {
  const { id, label, hint, value, min, max, step, disabled, onChange } = props;
  return (
    <div className="field">
      <label className="field-label" htmlFor={id}>
        <span>{label}</span>
        {hint ? <em>{hint}</em> : null}
      </label>
      <input
        id={id}
        className="input"
        type="number"
        min={min}
        max={max}
        step={step}
        disabled={disabled}
        value={value}
        onChange={(event) => {
          const next = Number(event.target.value);
          if (Number.isFinite(next)) onChange(next);
        }}
      />
    </div>
  );
}

/** AI 交易面板：模型看盘、复核并决定是否下单/仓位。
 * 「AI 量化」（生成/调参策略 → 回测 → 自动执行）是独立流程，走策略实验室
 * 与回测管线，不复用本面板的逐笔决策。
 *
 * The backend owns the run: this panel only starts, stops and reports. Status
 * is polled every 4 seconds with `since=seq`, so the event feed is appended
 * incrementally instead of re-downloading the whole ring buffer each tick.
 */
export default function AITradePanel({ market, symbol, timeframe, onNavigate }: Props) {
  const [status, setStatus] = useState<AITraderStatus | null>(null);
  const [events, setEvents] = useState<AIDecisionRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [pollError, setPollError] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState("");
  /** Which network the server trades on. Only the mainnet needs a by-hand
   * confirmation before each live run; the testnet is a rehearsal. */
  const [network, setNetwork] = useState("mainnet");
  const [strategies, setStrategies] = useState<Strategy[]>([]);
  const [strategyKey, setStrategyKey] = useState("");
  /** null while unknown; false is worth a banner because start would 400. */
  const [aiEnabled, setAiEnabled] = useState<boolean | null>(null);

  const [auto, setAuto] = useState<AIAutoSettings>(DEFAULT_AUTO);
  const [draft, setDraft] = useState<AIAutoSettings>(DEFAULT_AUTO);
  const [settingsLoaded, setSettingsLoaded] = useState(false);
  const [configOpen, setConfigOpen] = useState(false);

  const [auditOpen, setAuditOpen] = useState(false);
  const [audit, setAudit] = useState<AIDecisionRecord[]>([]);
  const [auditError, setAuditError] = useState("");
  const [auditBusy, setAuditBusy] = useState(false);

  const seq = useRef(0);

  /** Replace or append the feed. `replace` is used on the first load, after a
   * lifecycle action and whenever the backend's seq went backwards (a new
   * trader instance starts again at 1, so `since=old` would skip its events). */
  const applyStatus = useCallback((next: AITraderStatus, replace: boolean) => {
    seq.current = Number(next.seq ?? 0);
    setStatus(next);
    const incoming = next.events ?? [];
    setEvents((previous) => {
      const merged = replace ? incoming : [...previous, ...incoming];
      const seen = new Set<number>();
      const unique = merged.filter((item) => {
        if (typeof item.seq !== "number") return true;
        if (seen.has(item.seq)) return false;
        seen.add(item.seq);
        return true;
      });
      return unique.slice(-200);
    });
  }, []);

  const refresh = useCallback(
    async (incremental: boolean) => {
      try {
        const previous = seq.current;
        const next = await api<AITraderStatus>(
          `/api/ai/trader/status?${query({ since: incremental ? previous : 0 })}`,
        );
        const reset = Number(next.seq ?? 0) < previous;
        applyStatus(next, !incremental || reset);
        setPollError("");
      } catch (caught) {
        setPollError(caught instanceof Error ? caught.message : String(caught));
      } finally {
        setLoading(false);
      }
    },
    [applyStatus],
  );

  // One interval for the lifetime of the panel: `refresh` is stable, so a
  // render (tick, keystroke, poll result) never rebuilds the timer.
  useEffect(() => {
    void refresh(false);
    const timer = window.setInterval(() => void refresh(true), 4000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const loadSettings = useCallback(async () => {
    try {
      const data = await api<{ settings: { ai_auto?: Partial<AIAutoSettings> } }>("/api/settings");
      const merged: AIAutoSettings = { ...DEFAULT_AUTO, ...(data.settings.ai_auto ?? {}) };
      setAuto(merged);
      setDraft(merged);
      setSettingsLoaded(true);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
      setSettingsLoaded(true);
    }
  }, []);

  useEffect(() => {
    void loadSettings();
  }, [loadSettings]);

  useEffect(() => {
    api<{ network: string }>("/api/health")
      .then((data) => setNetwork(data.network))
      .catch(() => undefined);
  }, []);

  // A disabled assistant is the most common reason start returns 400; say it
  // up front instead of only after the click.
  useEffect(() => {
    api<{ enabled: boolean }>("/api/ai/status")
      .then((data) => setAiEnabled(Boolean(data.enabled)))
      .catch(() => setAiEnabled(null));
  }, []);

  // Strategy first: the AI is only allowed to review the direction emitted by
  // this strategy, so the panel must know which one is selected before start.
  useEffect(() => {
    api<{ items: Strategy[] }>("/api/strategies")
      .then((data) => {
        setStrategies(data.items);
        setStrategyKey((current) => current || data.items[0]?.key || "");
      })
      .catch((caught) =>
        setError(caught instanceof Error ? caught.message : String(caught)),
      );
  }, []);

  useEffect(() => {
    if (!auditOpen) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setAuditOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [auditOpen]);

  const openAudit = useCallback(async () => {
    setAuditOpen(true);
    setAuditBusy(true);
    setAuditError("");
    try {
      const data = await api<{ items: AIDecisionRecord[] }>("/api/ai/trader/audit?limit=100");
      setAudit(data.items ?? []);
    } catch (caught) {
      setAuditError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setAuditBusy(false);
    }
  }, []);

  const patch = useCallback((values: Partial<AIAutoSettings>) => {
    setDraft((current) => ({ ...current, ...values }));
  }, []);

  const state = status?.state ?? "stopped";
  const running = state === "running" || state === "paused";
  const halted = state === "halted";
  const paused = Boolean(status?.paused) || state === "paused";
  const counters = status?.counters ?? {};
  const last = status?.last_decision ?? null;

  const venue = network === "mainnet" ? "主网" : "测试网";
  const shownSymbol = (running ? status?.config?.symbol : "") || symbol || "—";
  const shownTimeframe = (running ? status?.config?.timeframe : "") || timeframe || "—";

  const stateLabel = STATE_LABEL[state] ?? state;

  const start = async () => {
    if (busy) return;
    if (!symbol) {
      setError("请先在图表上选择交易对。");
      return;
    }
    const selected = strategies.find((item) => item.key === strategyKey);
    if (!selected) {
      setError("请先选择产生信号的策略。");
      return;
    }
    if (!auto.live_enabled) {
      setError(
        "真下单未在设置中显式开启（ai_auto.live_enabled），启动被拒绝。请先在配置里打开「允许实盘」并保存。",
      );
      return;
    }
    if (network === "mainnet") {
      const ok = window.confirm(
        `即将在【主网】启动 AI 交易\n\n${symbol} · ${timeframe}\n\n` +
          "模型会真实下单，成交使用真实资金，盈亏由交易所结算。确认继续？",
      );
      if (!ok) return;
    }
    setBusy("start");
    setError("");
    setNotice("");
    try {
      const result = await api<{ ok: boolean; status: AITraderStatus }>("/api/ai/trader/start", {
        method: "POST",
        body: JSON.stringify({
          confirm_live: network === "mainnet",
          market,
          symbol,
          timeframe,
          strategy: strategyKey,
          params:
            auto.strategy_params?.[strategyKey] ?? { ...selected.defaults },
          max_order_quote: auto.max_order_quote,
          max_position_quote: auto.max_position_quote,
        }),
      });
      applyStatus(result.status, true);
      setNotice(`已启动：${venue} · ${symbol} · ${timeframe} · 策略 ${selected.name}`);
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
    setNotice("");
    try {
      const result = await api<{ ok: boolean; status: AITraderStatus }>("/api/ai/trader/stop", {
        method: "POST",
      });
      applyStatus(result.status, true);
      setNotice("已停止 AI 交易。");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy("");
    }
  };

  /** One decision on demand: same guards, same account, no poller.
   *
   * Useful to check that model + signal + risk actually line up before leaving
   * a loop running (and to nudge the model once on a quiet bar). */
  const decideOnce = async () => {
    if (busy) return;
    const picked = strategies.find((item) => item.key === strategyKey);
    if (!picked) {
      setError("请先选择产生信号的策略。");
      return;
    }
    if (network === "mainnet") {
      const ok = window.confirm(
        `即将在【主网】让模型决策一次\n\n${symbol} · ${timeframe}\n\n` +
          "如果模型给出开仓且通过闸门，会真的下单。确认继续？",
      );
      if (!ok) return;
    }
    setBusy("decide");
    setError("");
    setNotice("");
    try {
      const result = await api<{ ok: boolean; decision: AIDecisionRecord | null }>(
        "/api/ai/trader/decide",
        {
          method: "POST",
          body: JSON.stringify({
            confirm_live: network === "mainnet",
            market,
            symbol,
            timeframe,
            strategy: strategyKey,
            params: auto.strategy_params?.[strategyKey] ?? { ...picked.defaults },
            max_order_quote: auto.max_order_quote,
            max_position_quote: auto.max_position_quote,
          }),
        },
      );
      const record = result.decision;
      if (!record) {
        setNotice("这次没有产生决策记录（行情或策略信号不可用）。");
      } else {
        const label = OUTCOME_LABEL[record.outcome ?? ""] ?? record.outcome ?? "";
        const action = record.action
          ? ` · ${ACTION_LABEL[record.action] ?? record.action}`
          : "";
        const reason = record.reason ? ` · ${record.reason}` : "";
        setNotice(`本次决策：${label}${action}${reason}`);
      }
      await refresh(false);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy("");
    }
  };

  const togglePause = async () => {
    if (busy) return;
    const next = !paused;
    setBusy("pause");
    setError("");
    setNotice("");
    try {
      const result = await api<{ ok: boolean; status: AITraderStatus }>("/api/ai/trader/pause", {
        method: "POST",
        body: JSON.stringify({ paused: next }),
      });
      applyStatus(result.status, true);
      setNotice(next ? "已暂停：模型不再产生新决策。" : "已继续运行。");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy("");
    }
  };

  const halt = async () => {
    if (busy) return;
    setBusy("halt");
    setError("");
    setNotice("");
    try {
      const result = await api<{ ok: boolean; status: AITraderStatus }>("/api/ai/trader/halt", {
        method: "POST",
        body: JSON.stringify({ reason: "面板手动熔断" }),
      });
      applyStatus(result.status, true);
      setNotice("已熔断：AI 不再产生决策，需重新启动才能恢复。");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy("");
    }
  };

  /** 急停 uses the global endpoint: it also opens the risk kill switch and
   * stops the strategy auto-trader, not just this AI run. */
  const kill = async () => {
    if (busy) return;
    const ok = window.confirm(
      "急停会立即执行：\n\n" +
        "· 打开全局风控急停（之后所有下单都会被拒绝）\n" +
        "· 停止 AI 交易\n" +
        "· 停止策略自动交易\n\n" +
        "确认急停？",
    );
    if (!ok) return;
    setBusy("kill");
    setError("");
    setNotice("");
    try {
      const result = await api<{
        ok: boolean;
        ai_trader?: AITraderStatus;
        risk?: { halted?: boolean; halted_reason?: string };
      }>("/api/risk/kill", {
        method: "POST",
        body: JSON.stringify({ reason: "面板急停" }),
      });
      if (result.ai_trader) {
        applyStatus(result.ai_trader, true);
      } else {
        await refresh(false);
      }
      const reason = result.risk?.halted_reason ? `（${result.risk.halted_reason}）` : "";
      setNotice(`已急停${reason}。全局急停已开启，需到设置里关闭后才能恢复交易。`);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy("");
    }
  };

  const saveConfig = async () => {
    if (busy) return;
    setBusy("save");
    setError("");
    setNotice("");
    try {
      const result = await api<{ settings: { ai_auto?: Partial<AIAutoSettings> } }>("/api/settings", {
        method: "PUT",
        body: JSON.stringify({ ai_auto: draft }),
      });
      const merged: AIAutoSettings = { ...DEFAULT_AUTO, ...(result.settings.ai_auto ?? {}) };
      setAuto(merged);
      setDraft(merged);
      setNotice("AI 交易配置已保存（下次启动生效）。");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy("");
    }
  };

  const visible = events.slice(-20).reverse();
  const auditRows = [...audit].reverse();
  const selected = strategies.find((item) => item.key === strategyKey);

  /** Carry the currently selected signal strategy and chart context into the
   * lab/backtest page. Both pages read these one-shot handoffs on mount, so a
   * normal navigation from the sidebar stays untouched. */
  const handoff = (page: "lab" | "backtest") => {
    if (!selected) return;
    const payload = {
      strategy: selected.key,
      symbol,
      timeframe,
      market,
      at: Date.now(),
    };
    window.sessionStorage.setItem(`bitrader.${page}.handoff`, JSON.stringify(payload));
    onNavigate?.(page);
  };

  return (
    <>
      <div className="ai-head">
        <span className="card-title">AI 交易</span>
        <span className={`live-badge ${state === "stopped" ? "idle" : ""} ${halted ? "ai-badge-halt" : ""}`}>
          <span
            className={`dot ${state === "running" ? "live" : paused ? "off" : halted ? "ai-dot-halt" : ""}`}
          />
          {stateLabel}
        </span>
        <span className="faint" style={{ fontSize: 11 }}>
          {venue} · {shownSymbol} · {shownTimeframe}
        </span>
        <select
          className="select ai-strategy-select"
          aria-label="信号策略"
          value={strategyKey}
          disabled={running || busy !== ""}
          onChange={(event) => setStrategyKey(event.target.value)}
        >
          {strategies.length === 0 && <option value="">（无策略）</option>}
          {strategies.map((item) => (
            <option key={item.key} value={item.key}>
              {item.name}
            </option>
          ))}
        </select>
        <span className="spacer" />
        {strategyKey && auto.strategy_params?.[strategyKey] && (
          <span className="lab-chip approved" title="来自策略实验室，人工确认后保存">
            已应用优化参数
          </span>
        )}
        <div className="ai-head-actions">
          <button
            type="button"
            className="btn-sm"
            disabled={!strategyKey || busy !== ""}
            title="带着当前策略和图表参数打开策略实验室"
            onClick={() => handoff("lab")}
          >
            优化策略
          </button>
          <button
            type="button"
            className="btn-sm"
            disabled={!strategyKey || busy !== ""}
            title="带着当前策略和图表参数打开回测"
            onClick={() => handoff("backtest")}
          >
            回测
          </button>
          <button
            type="button"
            className="btn-sm"
            disabled={busy !== "" || running || !symbol || aiEnabled === false}
            title="按当前策略信号与配置，让模型立即决策一次（不启动轮询）"
            onClick={() => void decideOnce()}
          >
            {busy === "decide" ? "决策中…" : "决策一次"}
          </button>
          <button
            type="button"
            className="btn-sm primary"
            disabled={busy !== "" || running || !symbol}
            onClick={() => void start()}
          >
            {busy === "start" ? "启动中…" : "启动"}
          </button>
          <button
            type="button"
            className="btn-sm"
            disabled={busy !== "" || state === "stopped"}
            onClick={() => void stop()}
          >
            {busy === "stop" ? "停止中…" : "停止"}
          </button>
          <button
            type="button"
            className="btn-sm"
            disabled={busy !== "" || !running}
            onClick={() => void togglePause()}
          >
            {busy === "pause" ? "处理中…" : paused ? "继续" : "暂停"}
          </button>
          <button
            type="button"
            className="btn-sm"
            disabled={busy !== "" || state === "stopped" || halted}
            onClick={() => void halt()}
          >
            {busy === "halt" ? "熔断中…" : "熔断"}
          </button>
          <button
            type="button"
            className="btn-sm ai-danger"
            disabled={busy !== ""}
            onClick={() => void kill()}
          >
            {busy === "kill" ? "急停中…" : "急停"}
          </button>
        </div>
      </div>

      {halted && (
        <div className="banner error">
          已熔断{status?.halted_reason ? `：${status.halted_reason}` : ""}。重新启动前请确认原因已排除。
        </div>
      )}
      {aiEnabled === false && (
        <div className="banner warn">
          AI 助手未启用：启动会被后端拒绝（400）。请先到「设置 → AI 助手」选择供应商、填入 API Key 并启用。
        </div>
      )}
      {error && <div className="banner error">{error}</div>}
      {pollError && <div className="banner error">状态读取失败：{pollError}</div>}
      {notice && <div className="banner">{notice}</div>}

      <div className="ai-col">
        <div className="card">
          <div className="card-head">
            <span className="card-title">交易配置</span>
            <span className="spacer" />
            <span className="faint" style={{ fontSize: 11 }}>
              {auto.enabled ? "已启用" : "未启用"}
            </span>
            <button type="button" className="link-btn" onClick={() => setConfigOpen((value) => !value)}>
              {configOpen ? "收起" : "展开"}
            </button>
          </div>
          {configOpen ? (
            <div className="card-body">
              {!settingsLoaded ? (
                <div className="empty">读取配置…</div>
              ) : (
                <>
                  <div className="form-grid">
                    <div className="field">
                      <span className="field-label">
                        <span>启用</span>
                      </span>
                      <label className="check" htmlFor="ai-auto-enabled">
                        <input
                          id="ai-auto-enabled"
                          type="checkbox"
                          checked={draft.enabled}
                          onChange={(event) => patch({ enabled: event.target.checked })}
                        />
                        <span>启用 AI 交易</span>
                      </label>
                    </div>
                    <div className="field">
                      <span className="field-label">
                        <span>实盘开关</span>
                        <em>live_enabled</em>
                      </span>
                      <label className="check" htmlFor="ai-auto-live">
                        <input
                          id="ai-auto-live"
                          type="checkbox"
                          checked={draft.live_enabled}
                          onChange={(event) => patch({ live_enabled: event.target.checked })}
                        />
                        <span>允许实盘启动</span>
                      </label>
                    </div>
                    <NumberField
                      id="ai-auto-order"
                      label="单笔上限"
                      hint="USDT"
                      value={draft.max_order_quote}
                      min={0}
                      step={10}
                      onChange={(value) => patch({ max_order_quote: value })}
                    />
                    <NumberField
                      id="ai-auto-position"
                      label="最大持仓"
                      hint="USDT"
                      value={draft.max_position_quote}
                      min={0}
                      step={10}
                      onChange={(value) => patch({ max_position_quote: value })}
                    />
                    <NumberField
                      id="ai-auto-decisions"
                      label="日决策数"
                      hint="次/天"
                      value={draft.max_decisions_per_day}
                      min={0}
                      step={1}
                      onChange={(value) => patch({ max_decisions_per_day: value })}
                    />
                    <NumberField
                      id="ai-auto-orders"
                      label="小时订单数"
                      hint="单/小时"
                      value={draft.max_orders_per_hour}
                      min={0}
                      step={1}
                      onChange={(value) => patch({ max_orders_per_hour: value })}
                    />
                    <NumberField
                      id="ai-auto-confidence"
                      label="最小置信度"
                      hint={`${(draft.min_confidence * 100).toFixed(0)}%`}
                      value={draft.min_confidence}
                      min={0}
                      max={1}
                      step={0.05}
                      onChange={(value) => patch({ min_confidence: value })}
                    />
                    <NumberField
                      id="ai-auto-cooldown"
                      label="冷却"
                      hint="秒"
                      value={draft.cooldown_sec}
                      min={0}
                      step={5}
                      onChange={(value) => patch({ cooldown_sec: value })}
                    />
                  </div>
                  <div className="ai-config-actions">
                    <button
                      type="button"
                      className="btn-sm primary"
                      disabled={busy !== ""}
                      onClick={() => void saveConfig()}
                    >
                      {busy === "save" ? "保存中…" : "保存配置"}
                    </button>
                    <button
                      type="button"
                      className="btn-sm"
                      disabled={busy !== ""}
                      onClick={() => setDraft(auto)}
                    >
                      还原
                    </button>
                    <span className="spacer" />
                    <span className="faint" style={{ fontSize: 11 }}>
                      保存到 /api/settings · ai_auto
                    </span>
                  </div>
                </>
              )}
            </div>
          ) : (
            <div className="card-body">
              <div className="faint" style={{ fontSize: 11, lineHeight: 1.7 }}>
                单笔 {fmt(auto.max_order_quote, 0)} USDT · 最大持仓 {fmt(auto.max_position_quote, 0)}{" "}
                USDT · 日 {auto.max_decisions_per_day} 次 · 时 {auto.max_orders_per_hour} 单 · 最小置信度{" "}
                {(auto.min_confidence * 100).toFixed(0)}% · 冷却 {fmt(auto.cooldown_sec, 0)} 秒
              </div>
            </div>
          )}
        </div>

        <div className="card">
          <div className="card-head">
            <span className="card-title">运行概览</span>
            <span className="spacer" />
            <span className="faint" style={{ fontSize: 11 }}>
              {venue} · {shownSymbol}
            </span>
          </div>
          <div className="card-body">
            <div className="ai-stats">
              <div className="ai-stat">
                <span className="ai-stat-label">今日决策</span>
                <span className="ai-stat-value">
                  {counters.decisions_today ?? 0}
                  <em> / {counters.max_decisions_per_day ?? auto.max_decisions_per_day}</em>
                </span>
              </div>
              <div className="ai-stat">
                <span className="ai-stat-label">本小时订单</span>
                <span className="ai-stat-value">
                  {counters.orders_last_hour ?? 0}
                  <em> / {counters.max_orders_per_hour ?? auto.max_orders_per_hour}</em>
                </span>
              </div>
            </div>

            <div className="section-title">最近决策</div>
            {last ? (
              <div className="preview">
                <div className="preview-row">
                  <span>动作</span>
                  <strong>{actionLabel(last.action) || "—"}</strong>
                </div>
                <div className="preview-row">
                  <span>置信度</span>
                  <strong>
                    {typeof last.confidence === "number"
                      ? `${(last.confidence * 100).toFixed(0)}%`
                      : "—"}
                  </strong>
                </div>
                <div className="preview-row">
                  <span>金额 / 价格</span>
                  <strong>
                    {fmt(last.quote ?? 0, 2)} USDT · {fmtPrice(last.price)}
                  </strong>
                </div>
                <div className="preview-row">
                  <span>结果</span>
                  <strong className={outcomeClass(last) === "order" ? "up" : outcomeClass(last) === "reject" ? "down" : ""}>
                    {outcomeLabel(last)}
                  </strong>
                </div>
                <div className="preview-row">
                  <span>原因</span>
                  <strong style={{ textAlign: "right" }}>
                    {last.reason || last.rationale || "—"}
                  </strong>
                </div>
                <div className="preview-row">
                  <span>时间</span>
                  <strong className="num">{stampOf(last.at)}</strong>
                </div>
              </div>
            ) : (
              <div className="hint">
                {loading ? "读取状态…" : "还没有决策。启动后模型会按周期判断，观望也会记录一条。"}
              </div>
            )}
          </div>
        </div>
      </div>

      <div className="card ai-events">
        <div className="card-head">
          <span className="card-title">决策事件</span>
          <span className="spacer" />
          <span className="faint" style={{ fontSize: 11 }}>
            {events.length} 条 · 每 4 秒刷新
          </span>
          <button type="button" className="link-btn" onClick={() => void openAudit()}>
            审计
          </button>
        </div>
        <div className="feed ai-feed">
          {loading ? (
            <div className="empty">读取状态…</div>
          ) : visible.length === 0 ? (
            <div className="empty">暂无事件</div>
          ) : (
            visible.map((event, index) => (
              <div className="feed-row" key={`${event.seq ?? "x"}-${event.at ?? index}`}>
                <span className="feed-when num">{clockOf(event.at)}</span>
                <span className={`feed-kind ai-tag ${outcomeClass(event)}`}>{outcomeLabel(event)}</span>
                <span>
                  {decisionLine(event)}
                  {event.reason ? <span className="ai-reason"> · {event.reason}</span> : null}
                </span>
              </div>
            ))
          )}
        </div>
      </div>

      {auditOpen && (
        <div className="ai-drawer-mask" role="presentation" onClick={() => setAuditOpen(false)}>
          <aside
            className="ai-drawer"
            role="dialog"
            aria-modal="true"
            aria-label="AI 交易审计"
            onClick={(event) => event.stopPropagation()}
          >
            <div className="ai-drawer-head">
              <span className="card-title">交易审计</span>
              <span className="faint" style={{ fontSize: 11 }}>
                最近 {audit.length} 条
              </span>
              <span className="spacer" />
              <button type="button" className="link-btn" onClick={() => void openAudit()}>
                {auditBusy ? "刷新中…" : "刷新"}
              </button>
              <button type="button" className="link-btn" onClick={() => setAuditOpen(false)}>
                关闭
              </button>
            </div>
            <div className="ai-drawer-body">
              {auditError && <div className="banner error ai-drawer-error">{auditError}</div>}
              {auditBusy && audit.length === 0 ? (
                <div className="empty">读取审计…</div>
              ) : auditRows.length === 0 ? (
                <div className="empty">暂无审计记录</div>
              ) : (
                auditRows.map((item, index) => (
                  <div className="ai-audit-row" key={`${item.seq ?? "x"}-${item.at ?? index}`}>
                    <div className="ai-audit-top">
                      <span className="num ai-audit-time">{stampOf(item.at)}</span>
                      <span className={`ai-tag ${outcomeClass(item)}`}>{outcomeLabel(item)}</span>
                      <strong>{item.kind === "lifecycle" ? "生命周期" : actionLabel(item.action) || "—"}</strong>
                      {item.symbol && <span className="ai-audit-meta">{item.symbol}</span>}
                      {item.network && (
                        <span className="ai-audit-meta">
                          {item.network === "mainnet" ? "主网" : "测试网"}
                        </span>
                      )}
                    </div>
                    {item.reason && <div className="ai-audit-text">原因：{item.reason}</div>}
                    {item.rationale && <div className="ai-audit-text">{item.rationale}</div>}
                    <div className="ai-audit-meta">
                      金额 {fmt(item.quote ?? 0, 2)} USDT · 价格 {fmtPrice(item.price)} · 置信度{" "}
                      {typeof item.confidence === "number" ? `${(item.confidence * 100).toFixed(0)}%` : "—"}{" "}
                      · 订单 {item.order_id || "—"}
                    </div>
                  </div>
                ))
              )}
            </div>
          </aside>
        </div>
      )}
    </>
  );
}
