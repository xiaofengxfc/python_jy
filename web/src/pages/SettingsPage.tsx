import { useCallback, useEffect, useRef, useState } from "react";
import { api, query } from "../api";
import { TIMEFRAMES } from "../format";
import type {
  AISettings,
  AIStatus,
  AppSettings,
  CredentialItem,
  NotifyStatus,
  PlatformOption,
  RiskState,
  SymbolInfo,
} from "../types";

interface Props {
  settings: AppSettings | null;
  /** 读设置失败的原因（后端没起来、正在重启）。空串表示还在读或者读到了。 */
  settingsError?: string;
  onSaved: (settings: AppSettings) => void;
}

interface Draft {
  apiKey: string;
  apiSecret: string;
  verify: boolean;
}

const EMPTY_DRAFT: Draft = { apiKey: "", apiSecret: "", verify: true };

const NEWS_SOURCES = ["币安公告", "CoinTelegraph", "Decrypt"];

const NETWORKS: [string, string][] = [
  ["testnet", "测试网"],
  ["mainnet", "主网"],
];

const RISK_FIELDS: [keyof AppSettings["risk"], string, string][] = [
  ["max_order_quote", "单笔上限", "USDT"],
  ["max_position_quote", "持仓上限", "USDT"],
  ["max_leverage", "杠杆上限", "×"],
  ["max_daily_loss", "日亏损上限", "USDT"],
  ["max_daily_trades", "日成交笔数", "笔"],
  ["max_consecutive_losses", "连续亏损熔断", "次"],
];

const PCT_FIELDS: [keyof AppSettings["risk"], string, string][] = [
  ["max_order_pct", "单笔上限比例", "% 本金"],
];

export default function SettingsPage({ settings, settingsError = "", onSaved }: Props) {
  const [draft, setDraft] = useState<AppSettings | null>(settings);
  // 保存时只提交改过的字段：整份回传会把别处刚改的值盖回旧值。
  const baseline = useRef<AppSettings | null>(settings);
  const [platforms, setPlatforms] = useState<PlatformOption[]>([]);
  const [platform, setPlatform] = useState(settings?.platform ?? "binance");
  const [platformBusy, setPlatformBusy] = useState(false);
  const [credentials, setCredentials] = useState<CredentialItem[]>([]);
  const [credentialsError, setCredentialsError] = useState("");
  const [saving, setSaving] = useState(false);
  const [flash, setFlash] = useState<{ tone: "ok" | "err"; text: string } | null>(null);
  const [network, setNetwork] = useState(settings?.network ?? "testnet");
  const [credDrafts, setCredDrafts] = useState<Record<string, Draft>>({});
  /** 凭据卡片当前看哪个市场：一次只显示一个，三份表单挤在一起没法对齐。 */
  const [credMarket, setCredMarket] = useState("");
  const [credBusy, setCredBusy] = useState<string>("");
  const [risk, setRisk] = useState<RiskState | null>(null);
  const [riskBusy, setRiskBusy] = useState(false);
  const [notifyStatus, setNotifyStatus] = useState<NotifyStatus | null>(null);
  const [notifyBusy, setNotifyBusy] = useState(false);
  const [aiStatus, setAiStatus] = useState<AIStatus | null>(null);
  const [aiKey, setAiKey] = useState("");
  const [aiBusy, setAiBusy] = useState<"key" | "test" | "">("");
  const [aiNote, setAiNote] = useState<{ tone: "ok" | "err"; text: string } | null>(null);

  /** 重新读服务端设置。进页面就读一次：别处改过的值必须先看到，再允许保存。 */
  const loadSettings = useCallback(async () => {
    try {
      const data = await api<{
        settings: AppSettings;
        platform: string;
        platforms: PlatformOption[];
      }>("/api/settings");
      setPlatforms(data.platforms ?? []);
      setPlatform(data.platform ?? data.settings.platform ?? "binance");
      baseline.current = data.settings;
      setDraft(data.settings);
      setNetwork(data.settings.network);
      onSaved(data.settings);
      return data.settings;
    } catch (error) {
      setFlash({ tone: "err", text: error instanceof Error ? error.message : String(error) });
      return null;
    }
  }, [onSaved]);

  useEffect(() => {
    void loadSettings();
  }, [loadSettings]);

  const switchPlatform = async (next: string) => {
    if (next === platform || platformBusy) return;
    const target = platforms.find((item) => item.key === next);
    if (target && !target.enabled) {
      setFlash({ tone: "err", text: `${target.label} 还没接进来，现在只能选币安。` });
      return;
    }
    setPlatformBusy(true);
    setFlash(null);
    try {
      const result = await api<{ settings: AppSettings }>("/api/settings", {
        method: "PUT",
        body: JSON.stringify({ platform: next }),
      });
      baseline.current = result.settings;
      setDraft(result.settings);
      setPlatform(result.settings.platform);
      onSaved(result.settings);
      await loadCredentials();
      setFlash({ tone: "ok", text: `已切到${target?.label ?? next}，凭据也换成这个平台的。` });
    } catch (error) {
      setFlash({ tone: "err", text: error instanceof Error ? error.message : String(error) });
    } finally {
      setPlatformBusy(false);
    }
  };

  const loadAI = useCallback(async () => {
    try {
      const next = await api<AIStatus>("/api/ai/status");
      setAiStatus(next);
      return next;
    } catch {
      return null;
    }
  }, []);

  useEffect(() => {
    void loadAI();
  }, [loadAI]);

  const loadNotify = useCallback(async () => {
    try {
      setNotifyStatus(await api<NotifyStatus>("/api/notify"));
    } catch {
      // Reachability is informational; a failure must not block the form.
    }
  }, []);

  useEffect(() => {
    void loadNotify();
    const timer = window.setInterval(() => void loadNotify(), 10000);
    return () => window.clearInterval(timer);
  }, [loadNotify]);

  const patchNotify = (values: Partial<AppSettings["notify"]>) => {
    setDraft((current) =>
      current ? { ...current, notify: { ...current.notify, ...values } } : current,
    );
  };

  const testNotify = async () => {
    if (!draft) return;
    setNotifyBusy(true);
    setFlash(null);
    try {
      // 用表单里当前的值发，不是已保存的值：按钮的作用就是在保存之前先验一遍。
      const result = await api<{ sent: boolean; format: string; url: string }>("/api/notify/test", {
        method: "POST",
        body: JSON.stringify({
          message: "",
          webhook_url: draft.notify.webhook_url,
          msg_format: draft.notify.msg_format,
          webhook_headers: draft.notify.webhook_headers,
          webhook_template: draft.notify.webhook_template,
        }),
      });
      setFlash({
        tone: "ok",
        text: `已发出（${result.format}）。到企业微信群里看一眼，收到了就说明通道通了。`,
      });
      await loadNotify();
    } catch (error) {
      setFlash({ tone: "err", text: error instanceof Error ? error.message : String(error) });
    } finally {
      setNotifyBusy(false);
    }
  };

  const loadRisk = useCallback(async () => {
    try {
      setRisk(await api<RiskState>("/api/risk"));
    } catch {
      // The panel is informational; a failed poll must not disturb the form.
    }
  }, []);

  useEffect(() => {
    void loadRisk();
    const timer = window.setInterval(() => void loadRisk(), 5000);
    return () => window.clearInterval(timer);
  }, [loadRisk]);

  const resumeRisk = async () => {
    setRiskBusy(true);
    setFlash(null);
    try {
      setRisk(await api<RiskState>("/api/risk/resume", { method: "POST" }));
      setFlash({ tone: "ok", text: "已解除熔断，日内额度已重置。" });
    } catch (error) {
      setFlash({ tone: "err", text: error instanceof Error ? error.message : String(error) });
    } finally {
      setRiskBusy(false);
    }
  };

  useEffect(() => {
    if (settings) {
      setDraft(settings);
      setNetwork(settings.network);
      baseline.current = settings;
      setPlatform(settings.platform ?? "binance");
    }
  }, [settings]);

  const loadCredentials = useCallback(async () => {
    try {
      // Always name the network: the tab the user is looking at decides which
      // keys are shown, and 主网 must never display 测试网 的 key.
      const data = await api<{ network: string; items: CredentialItem[] }>(
        `/api/credentials?network=${encodeURIComponent(network)}`,
      );
      setCredentials(data.items);
      setCredentialsError("");
    } catch (error) {
      setCredentialsError(error instanceof Error ? error.message : String(error));
    }
  }, [network]);

  useEffect(() => {
    void loadCredentials();
  }, [loadCredentials, network]);

  /** 默认交易对的选项来自交易页同一个接口，两个页面永远同一份列表。 */
  const [symbols, setSymbols] = useState<SymbolInfo[]>([]);
  const symbolMarket = draft?.market_type ?? "";
  useEffect(() => {
    if (!symbolMarket) return;
    let cancelled = false;
    api<{ items: SymbolInfo[] }>(`/api/symbols?${query({ market: symbolMarket })}`)
      .then((data) => {
        if (cancelled) return;
        const items = data.items ?? [];
        setSymbols(items);
        // 老配置里可能存着这个市场没有的交易对（以前是自由输入框）。
        // 落到列表里，否则下拉会显示空白。
        setDraft((current) => {
          if (!current) return current;
          if (!items.length || items.some((item) => item.symbol === current.last_symbol)) {
            return current;
          }
          const fallback = items.find((item) => item.symbol === "BTC/USDT") ?? items[0];
          return fallback ? { ...current, last_symbol: fallback.symbol } : current;
        });
      })
      .catch(() => {
        if (!cancelled) setSymbols([]);
      });
    return () => {
      cancelled = true;
    };
  }, [symbolMarket]);

  const patch = (values: Partial<AppSettings>) => {
    setDraft((current) => (current ? { ...current, ...values } : current));
  };

  const patchRisk = (key: keyof AppSettings["risk"], value: number | boolean) => {
    setDraft((current) =>
      current ? { ...current, risk: { ...current.risk, [key]: value } } : current,
    );
  };

  const patchAI = (values: Partial<AISettings>) => {
    setDraft((current) => (current ? { ...current, ai: { ...current.ai, ...values } } : current));
  };

  /** Switching provider also moves the model and endpoint: keeping DeepSeek's
   * model name while switching to OpenAI would send an unknown model id. */
  const pickProvider = (key: string) => {
    const info = aiStatus?.providers.find((item) => item.key === key);
    patchAI({ provider: key, model: info?.model ?? "", base_url: info?.base_url ?? "" });
  };

  const saveAIKey = async () => {
    if (!aiKey.trim()) {
      setAiNote({ tone: "err", text: "请先填入 API Key。" });
      return;
    }
    setAiBusy("key");
    setAiNote(null);
    try {
      const next = await api<AIStatus>("/api/ai/key", {
        method: "POST",
        body: JSON.stringify({ key: aiKey.trim() }),
      });
      setAiStatus(next);
      setAiKey("");
      setAiNote({ tone: "ok", text: "API Key 已存入系统密钥环。" });
    } catch (error) {
      setAiNote({ tone: "err", text: error instanceof Error ? error.message : String(error) });
    } finally {
      setAiBusy("");
    }
  };

  const testAI = async () => {
    setAiBusy("test");
    setAiNote(null);
    try {
      const result = await api<{ text: string; model: string; elapsed: number }>(
        "/api/ai/test",
        { method: "POST" },
      );
      setAiNote({
        tone: "ok",
        text: `连通正常：${result.model} 用时 ${result.elapsed.toFixed(1)}s，返回「${result.text.trim()}」。`,
      });
      await loadAI();
    } catch (error) {
      setAiNote({ tone: "err", text: error instanceof Error ? error.message : String(error) });
    } finally {
      setAiBusy("");
    }
  };

  /** 只提交和最新值不一样的字段。整份回传会把别处刚改的值盖回旧值。 */
  const changedPayload = (next: AppSettings, base: AppSettings | null) => {
    const fields: Record<string, unknown> = {
      network: next.network,
      market_type: next.market_type,
      last_symbol: next.last_symbol,
      timeframe: next.timeframe,
      proxy: next.proxy,
      theme: next.theme,
      notification_sound: next.notification_sound,
      risk: next.risk,
      notify: next.notify,
      ai: next.ai,
    };
    if (!base) return fields;
    const payload: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(fields)) {
      const before = (base as unknown as Record<string, unknown>)[key];
      if (JSON.stringify(before) !== JSON.stringify(value)) payload[key] = value;
    }
    return payload;
  };

  const save = async () => {
    if (!draft) return;
    const payload = changedPayload(draft, baseline.current);
    if (Object.keys(payload).length === 0) {
      setFlash({ tone: "ok", text: "没有改动，不用保存。" });
      return;
    }
    setSaving(true);
    setFlash(null);
    try {
      const result = await api<{ settings: AppSettings }>("/api/settings", {
        method: "PUT",
        body: JSON.stringify(payload),
      });

      onSaved(result.settings);
      baseline.current = result.settings;
      setNetwork(result.settings.network);
      setPlatform(result.settings.platform ?? platform);
      setFlash({ tone: "ok", text: "设置已保存。" });
      await loadCredentials();
      await loadAI();
    } catch (error) {
      setFlash({ tone: "err", text: error instanceof Error ? error.message : String(error) });
    } finally {
      setSaving(false);
    }
  };

  const saveCredential = async (market: string) => {
    const entry = credDrafts[market] ?? EMPTY_DRAFT;
    setCredBusy(market);
    setFlash(null);
    try {
      await api("/api/credentials", {
        method: "POST",
        body: JSON.stringify({
          market,
          network,
          api_key: entry.apiKey,
          api_secret: entry.apiSecret,
          verify: entry.verify,
        }),
      });
      setCredDrafts((current) => ({ ...current, [market]: EMPTY_DRAFT }));
      setFlash({ tone: "ok", text: `${market.toUpperCase()} 凭据已保存并通过连接测试。` });
      await loadCredentials();
    } catch (error) {
      setFlash({ tone: "err", text: error instanceof Error ? error.message : String(error) });
    } finally {
      setCredBusy("");
    }
  };

  const deleteCredential = async (market: string) => {
    setCredBusy(market);
    setFlash(null);
    try {
      await api(`/api/credentials?market=${market}&network=${network}`, { method: "DELETE" });
      setFlash({ tone: "ok", text: "凭据已删除。" });
      await loadCredentials();
    } catch (error) {
      setFlash({ tone: "err", text: error instanceof Error ? error.message : String(error) });
    } finally {
      setCredBusy("");
    }
  };

  if (!draft) {
    return (
      <>
        <div className="page-head">
          <h1 className="page-title">设置</h1>
          <span className="page-sub">{settingsError ? "读不到设置" : "正在读取设置…"}</span>
        </div>
        <div className="placeholder">
          {settingsError
            ? `读取设置失败：${settingsError}。后端起来后会自动重试，也可以刷新页面。`
            : "正在读取设置…"}
        </div>
      </>
    );
  }

  // The one bound recipient, if there is exactly one. Narrowed here because
  // the array index is optional and the card below reads it several times.

  /* ---------------------------------------------------------- 凭据 */
  // 选中的市场不在列表里（第一次加载、或换了网络）就退到第一个。
  const credCurrent =
    credentials.find((item) => item.market === credMarket) ?? credentials[0] ?? null;
  const activeMarket = credCurrent?.market ?? "";
  const credEntry = credDrafts[activeMarket] ?? EMPTY_DRAFT;

  return (
    <>
      <div className="page-head">
        <h1 className="page-title">设置</h1>
        <span className="page-sub">修改后需要点击保存</span>
        <span className="spacer" />
        <button type="button" className="btn-sm primary" onClick={save} disabled={saving}>
          {saving ? "保存中…" : "保存设置"}
        </button>
      </div>

      {flash && <div className={`banner ${flash.tone === "err" ? "error" : ""}`}>{flash.text}</div>}

      <div className="stack">
        <div className="card">
          <div className="card-head">
            <span className="card-title">API 凭据</span>
            <span className="spacer" />
            <div className="segmented">
              {NETWORKS.map(([value, label]) => (
                <button
                  key={value}
                  type="button"
                  aria-pressed={network === value}
                  onClick={() => setNetwork(value)}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>
          {credentialsError && <div className="banner error">{credentialsError}</div>}
          <div className="cred-card">
            <div className="cred-top">
              <label className="cred-platform">
                <span>平台</span>
                <select
                  className="select"
                  value={platform}
                  disabled={platformBusy}
                  onChange={(event) => void switchPlatform(event.target.value)}
                >
                  {(platforms.length
                    ? platforms
                    : [{ key: "binance", label: "币安", enabled: true }]
                  ).map((item) => (
                    <option key={item.key} value={item.key} disabled={!item.enabled}>
                      {item.enabled ? item.label : `${item.label}（暂未开放）`}
                    </option>
                  ))}
                </select>
              </label>
              <div className="segmented" role="group" aria-label="市场">
                {credentials.map((item) => (
                  <button
                    key={item.market}
                    type="button"
                    aria-pressed={credCurrent?.market === item.market}
                    onClick={() => setCredMarket(item.market)}
                  >
                    {item.label}
                  </button>
                ))}
              </div>
              <span className={`cred-state${credCurrent?.configured ? " on" : ""}`}>
                {credCurrent
                  ? credCurrent.configured
                    ? `已配置 · ${credCurrent.masked}`
                    : "未配置"
                  : "读取中…"}
              </span>
            </div>

            <div className="cred-grid">
              <label className="cred-field">
                <span>API Key</span>
                <input
                  className="input"
                  type="password"
                  autoComplete="off"
                  placeholder={credCurrent?.configured ? "已保存，留空表示不改" : "粘贴 API Key"}
                  value={credEntry.apiKey}
                  onChange={(event) =>
                    setCredDrafts((current) => ({
                      ...current,
                      [activeMarket]: { ...credEntry, apiKey: event.target.value },
                    }))
                  }
                />
              </label>
              <label className="cred-field">
                <span>API Secret</span>
                <input
                  className="input"
                  type="password"
                  autoComplete="off"
                  placeholder={credCurrent?.configured ? "已保存，留空表示不改" : "粘贴 API Secret"}
                  value={credEntry.apiSecret}
                  onChange={(event) =>
                    setCredDrafts((current) => ({
                      ...current,
                      [activeMarket]: { ...credEntry, apiSecret: event.target.value },
                    }))
                  }
                />
              </label>
            </div>

            <div className="cred-actions">
              <label className="check">
                <input
                  type="checkbox"
                  checked={credEntry.verify}
                  onChange={(event) =>
                    setCredDrafts((current) => ({
                      ...current,
                      [activeMarket]: { ...credEntry, verify: event.target.checked },
                    }))
                  }
                />
                先做连接测试
              </label>
              <span className="spacer" />
              <button
                type="button"
                className="btn-sm primary"
                disabled={!activeMarket || credBusy === activeMarket || !credEntry.apiKey || !credEntry.apiSecret}
                onClick={() => saveCredential(activeMarket)}
              >
                {credBusy === activeMarket ? "处理中…" : "保存"}
              </button>
              {credCurrent?.configured && (
                <button
                  type="button"
                  className="btn-sm"
                  disabled={credBusy === activeMarket}
                  onClick={() => deleteCredential(activeMarket)}
                >
                  清除
                </button>
              )}
            </div>

            <div className="cred-others">
              <span>其它市场</span>
              {credentials
                .filter((item) => item.market !== activeMarket)
                .map((item) => (
                  <span key={item.market} className={item.configured ? "on" : ""}>
                    {item.label} {item.configured ? item.masked : "未配置"}
                  </span>
                ))}
            </div>
          </div>
          <div className="card-body hint">
            密钥保存在系统凭据库（Windows 凭据管理器 / macOS 钥匙串），不会写入配置文件。
            建议在币安只勾选「读取」和「交易」权限，关闭「提现」，并绑定 IP 白名单。
          </div>
        </div>

        <div className="card">
          <div className="card-head">
            <span className="card-title">新闻来源</span>
            <span className="spacer" />
            <span className="faint" style={{ fontSize: "var(--fs-12)"}}>
              免费公开源 · 无需配置
            </span>
          </div>
          <div className="card-body">
            <div className="hint">
              新闻面板聚合下面这些公开源，都不需要密钥：
              <ul style={{ margin: "8px 0 0 18px", padding: 0 }}>
                {NEWS_SOURCES.map((source) => (
                  <li key={source}>{source}</li>
                ))}
              </ul>
            </div>
            <div className="hint" style={{ marginTop: 8 }}>
              币安公告不按币种过滤，上币、下架、维护都会影响交易。
              CoinTelegraph 和 Decrypt 只保留标题提到当前币种的条目。
            </div>
          </div>
        </div>

        <div className="card">
          <div className="card-head">
            <span className="card-title">AI 助手</span>
            <span className="spacer" />
            <span className="faint" style={{ fontSize: "var(--fs-12)"}}>
              {aiStatus?.provider_label ?? "未配置"}
              {aiStatus?.calls_today ? ` · 今日 ${aiStatus.calls_today} 次` : ""}
            </span>
          </div>
          <div className="card-body">
            <div className="hint" style={{ marginBottom: 12 }}>
              模型读多周期 K 线，标关键价位，写收盘简报。它只给判断，不下单：
              订单仍然只由策略和风控产生，模型进不了策略循环。
              指标在本地算好，以文本发给模型，不传截图。
            </div>

            <div className="form-grid">
              <div className="field">
                <label className="field-label" htmlFor="set-ai-enabled">
                  <span>启用 AI 助手</span>
                </label>
                <label className="check">
                  <input
                    id="set-ai-enabled"
                    type="checkbox"
                    checked={draft.ai.enabled}
                    onChange={(event) => patchAI({ enabled: event.target.checked })}
                  />
                  <span>打开后交易页出现「AI 分析」面板</span>
                </label>
              </div>

              <div className="field">
                <label className="field-label" htmlFor="set-ai-provider">
                  <span>模型供应商</span>
                </label>
                <select
                  id="set-ai-provider"
                  className="select"
                  value={draft.ai.provider}
                  onChange={(event) => pickProvider(event.target.value)}
                >
                  {(aiStatus?.providers ?? []).map((item) => (
                    <option key={item.key} value={item.key}>
                      {item.label}
                    </option>
                  ))}
                </select>
              </div>

              <div className="field">
                <label className="field-label" htmlFor="set-ai-model">
                  <span>模型</span>
                </label>
                <input
                  id="set-ai-model"
                  className="input"
                  value={draft.ai.model}
                  placeholder="deepseek-chat"
                  onChange={(event) => patchAI({ model: event.target.value })}
                />
              </div>

              <div className="field">
                <label className="field-label" htmlFor="set-ai-base">
                  <span>接口地址</span>
                </label>
                <input
                  id="set-ai-base"
                  className="input"
                  value={draft.ai.base_url}
                  placeholder="https://api.deepseek.com"
                  onChange={(event) => patchAI({ base_url: event.target.value })}
                />
              </div>

            </div>

            <div className="field" style={{ marginTop: 12 }}>
              <label className="field-label" htmlFor="set-ai-key">
                <span>API Key</span>
              </label>
              <div className="row" style={{ gap: 8 }}>
                <input
                  id="set-ai-key"
                  className="input"
                  type="password"
                  value={aiKey}
                  placeholder={aiStatus?.has_key ? `已保存：${aiStatus.key_hint}` : "粘贴 API Key"}
                  onChange={(event) => setAiKey(event.target.value)}
                />
                <button
                  type="button"
                  className="btn-sm"
                  onClick={() => void saveAIKey()}
                  disabled={aiBusy !== ""}
                >
                  {aiBusy === "key" ? "保存中…" : "保存 Key"}
                </button>
                <button
                  type="button"
                  className="btn-sm"
                  onClick={() => void testAI()}
                  disabled={aiBusy !== "" || !aiStatus?.has_key}
                >
                  {aiBusy === "test" ? "测试中…" : "测试连通"}
                </button>
              </div>
              <span className="field-hint">
                存在系统凭据库（Windows 凭据管理器 / macOS 钥匙串），不写进配置文件。
                {aiStatus?.provider_help ? ` ${aiStatus.provider_help}` : ""}
                {aiStatus && aiStatus.needs_key && !aiStatus.has_key ? " 该供应商需要 Key。" : ""}
              </span>
            </div>

            {aiNote && <div className={`banner ${aiNote.tone === "ok" ? "" : "error"}`}>{aiNote.text}</div>}

            <div className="form-grid" style={{ marginTop: 12 }}>
              <div className="field">
                <label className="check">
                  <input
                    type="checkbox"
                    checked={draft.ai.share_position}
                    onChange={(event) => patchAI({ share_position: event.target.checked })}
                  />
                  <span>允许把持仓和盈亏发给模型（持仓复核需要）</span>
                </label>
                <span className="field-hint">
                  默认关闭。持仓数量、开仓价、未实现盈亏属于隐私数据，打开后才出现在请求里。
                </span>
              </div>

              <div className="field">
                <label className="check">
                  <input
                    type="checkbox"
                    checked={draft.ai.auto_brief}
                    onChange={(event) => patchAI({ auto_brief: event.target.checked })}
                  />
                  <span>自动交易每根收盘 K 线推一条 AI 简报</span>
                </label>
                <span className="field-hint">
                  简报在独立线程生成，不占下单线程。生成失败只记日志，不影响交易。
                </span>
              </div>
            </div>

            {aiStatus && aiStatus.audit.length > 0 && (
              <div style={{ marginTop: 12 }}>
                <div className="faint" style={{ fontSize: "var(--fs-12)", marginBottom: 4 }}>
                  最近调用
                </div>
                <ul className="ai-audit settings-ai-audit">
                  {aiStatus.audit.slice(0, 6).map((item, index) => (
                    <li key={`${item.at}-${index}`} className="ai-audit-row">
                      <span className={`ai-audit-dot ${item.ok ? "ok" : "err"}`} />
                      <span>{new Date(item.at * 1000).toLocaleTimeString()}</span>
                      <span className="faint">{item.kind}</span>
                      <span className="faint">{item.model}</span>
                      {typeof item.elapsed === "number" && (
                        <span className="faint num">{item.elapsed.toFixed(1)}s</span>
                      )}
                      {item.error && <span className="ai-audit-err">{item.error}</span>}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        </div>


        <div className="stack">
          <div className="card">
            <div className="card-head">
              <span className="card-title">交易默认值</span>
            </div>
            <div className="card-body">
              <div className="form-grid">
                <div className="field">
                  <label className="field-label" htmlFor="set-network">
                    <span>默认网络</span>
                  </label>
                  <select
                    id="set-network"
                    className="select"
                    value={draft.network}
                    onChange={(event) => patch({ network: event.target.value })}
                  >
                    {NETWORKS.map(([value, label]) => (
                      <option key={value} value={value}>
                        {label}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="field">
                  <label className="field-label" htmlFor="set-market">
                    <span>默认市场</span>
                  </label>
                  <select
                    id="set-market"
                    className="select"
                    value={draft.market_type}
                    onChange={(event) => patch({ market_type: event.target.value })}
                  >
                    <option value="spot">现货</option>
                    <option value="usdm">U 本位合约</option>
                    <option value="coinm">币本位合约</option>
                  </select>
                </div>
                <div className="field">
                  <label className="field-label" htmlFor="set-symbol">
                    <span>默认交易对</span>
                  </label>
                  <select
                    id="set-symbol"
                    className="select"
                    value={draft.last_symbol}
                    onChange={(event) => patch({ last_symbol: event.target.value })}
                  >
                    {!symbols.some((item) => item.symbol === draft.last_symbol) && (
                      <option value={draft.last_symbol}>{draft.last_symbol || "先选市场"}</option>
                    )}
                    {symbols.map((item) => (
                      <option key={item.symbol} value={item.symbol}>
                        {item.display || item.symbol}
                      </option>
                    ))}
                  </select>
                  <span className="field-hint">
                    选项跟着「默认市场」走，和交易页的下拉是同一份。
                  </span>
                </div>
                <div className="field">
                  <label className="field-label" htmlFor="set-timeframe">
                    <span>默认 K 线周期</span>
                  </label>
                  <select
                    id="set-timeframe"
                    className="select"
                    value={draft.timeframe}
                    onChange={(event) => patch({ timeframe: event.target.value })}
                  >
                    {TIMEFRAMES.map(([value, label]) => (
                      <option key={value} value={value}>
                        {label}
                      </option>
                    ))}
                  </select>
                </div>
              </div>
              <div className="field">
                <label className="field-label" htmlFor="set-proxy">
                  <span>网络代理</span>
                  <em>可选，例如 http://127.0.0.1:7890</em>
                </label>
                <input
                  id="set-proxy"
                  className="input"
                  type="text"
                  placeholder="留空表示直连"
                  value={draft.proxy}
                  onChange={(event) => patch({ proxy: event.target.value })}
                />
                <div className="faint" style={{ fontSize: "var(--fs-13)"}}>
                  部分网络会污染币安合约域名（fapi.binance.com / dapi.binance.com）的 DNS。
                  填了代理，合约 K 线和下单才能直连币安。留空则不走代理。
                </div>
              </div>
            </div>
          </div>

          <div className="card">
            <div className="card-head">
              <span className="card-title">外观与通知</span>
            </div>
            <div className="card-body stack">
              <div className="field">
                <span className="field-label">
                  <span>主题</span>
                </span>
                <div className="segmented" style={{ alignSelf: "flex-start" }}>
                  <button
                    type="button"
                    aria-pressed={draft.theme === "light"}
                    onClick={() => patch({ theme: "light" })}
                  >
                    浅色
                  </button>
                  <button
                    type="button"
                    aria-pressed={draft.theme === "dark"}
                    onClick={() => patch({ theme: "dark" })}
                  >
                    深色
                  </button>
                </div>
              </div>
              <label className="check">
                <input
                  type="checkbox"
                  checked={draft.notification_sound}
                  onChange={(event) => patch({ notification_sound: event.target.checked })}
                />
                成交与信号提示音
              </label>
              <div className="hint">
                提示只在应用内弹出，不会触发系统通知。主题切换在保存后立即生效。
              </div>
            </div>
          </div>

          <div className="card">
            <div className="card-head">
              <span className="card-title">企业微信通知</span>
              <span className="spacer" />
              <span className="faint" style={{ fontSize: "var(--fs-12)"}}>
                {notifyStatus
                  ? notifyStatus.available
                    ? `地址已填 · ${notifyStatus.format}`
                    : "还没填地址"
                  : "检测中…"}
              </span>
              <button
                type="button"
                className="btn-sm"
                disabled={notifyBusy}
                onClick={() => void testNotify()}
              >
                {notifyBusy ? "发送中…" : "发送测试"}
              </button>
            </div>
            <div className="card-body stack">
              <label className="check">
                <input
                  type="checkbox"
                  checked={draft.notify.enabled}
                  onChange={(event) => patchNotify({ enabled: event.target.checked })}
                />
                启用微信推送
              </label>
          
              <div className="field">
                <label className="field-label" htmlFor="set-notify-url">
                  <span>Webhook 地址</span>
                  <em>企业微信群里加一个机器人，把它的地址粘进来。地址自带 key，等同密码。</em>
                </label>
                <input
                  id="set-notify-url"
                  className="input"
                  value={draft.notify.webhook_url}
                  placeholder="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=…"
                  onChange={(event) => patchNotify({ webhook_url: event.target.value })}
                />
              </div>
          
              <div className="field">
                <label className="field-label" htmlFor="set-notify-format">
                  <span>消息格式</span>
                  <em>自动：企业微信群机器人用 markdown，其它地址发 title 加 content 的 JSON</em>
                </label>
                <select
                  id="set-notify-format"
                  className="input"
                  value={draft.notify.msg_format}
                  onChange={(event) => patchNotify({ msg_format: event.target.value })}
                >
                  <option value="auto">自动判断</option>
                  <option value="qyapi_markdown">企业微信群机器人（markdown）</option>
                  <option value="qyapi_text">企业微信群机器人（纯文本）</option>
                  <option value="custom">自定义模板</option>
                </select>
              </div>
          
              {draft.notify.msg_format === "custom" && (
                <div className="field">
                  <label className="field-label" htmlFor="set-notify-template">
                    <span>自定义模板</span>
                    <em>JSON，用 {"{title}"} 和 {"{content}"} 占位。留空就发默认的 title 加 content。</em>
                  </label>
                  <textarea
                    id="set-notify-template"
                    className="input"
                    rows={3}
                    value={draft.notify.webhook_template}
                    placeholder='{"msgtype": "text", "text": {"content": "{content}"}}'
                    onChange={(event) => patchNotify({ webhook_template: event.target.value })}
                  />
                </div>
              )}
          
              <div className="field">
                <label className="field-label" htmlFor="set-notify-headers">
                  <span>自定义请求头</span>
                  <em>每行一个「名称: 值」。地址里带鉴权参数时不用填。</em>
                </label>
                <textarea
                  id="set-notify-headers"
                  className="input"
                  rows={2}
                  value={draft.notify.webhook_headers}
                  placeholder="Authorization: Bearer …"
                  onChange={(event) => patchNotify({ webhook_headers: event.target.value })}
                />
              </div>
          
              <div className="field">
                <label className="field-label">
                  <span>发哪些消息</span>
                </label>
                <div className="risk-grid">
                  <label className="check">
                    <input
                      type="checkbox"
                      checked={draft.notify.on_fill}
                      onChange={(event) => patchNotify({ on_fill: event.target.checked })}
                    />
                    成交
                  </label>
                  <label className="check">
                    <input
                      type="checkbox"
                      checked={draft.notify.on_protection}
                      onChange={(event) => patchNotify({ on_protection: event.target.checked })}
                    />
                    止损止盈触发
                  </label>
                  <label className="check">
                    <input
                      type="checkbox"
                      checked={draft.notify.on_risk_halt}
                      onChange={(event) => patchNotify({ on_risk_halt: event.target.checked })}
                    />
                    风控熔断
                  </label>
                  <label className="check">
                    <input
                      type="checkbox"
                      checked={draft.notify.on_live_warning}
                      onChange={(event) => patchNotify({ on_live_warning: event.target.checked })}
                    />
                    风险提示
                  </label>
                  <label className="check">
                    <input
                      type="checkbox"
                      checked={draft.notify.on_task}
                      onChange={(event) => patchNotify({ on_task: event.target.checked })}
                    />
                    任务开始与结束
                  </label>
                </div>
              </div>
          
              {notifyStatus && !notifyStatus.available && draft.notify.enabled && (
                <div className="verdict bad">
                  开关打开但没填地址，消息发不出去。把企业微信群机器人的地址粘到上面。
                </div>
              )}
              {notifyStatus?.last_error && (
                <div className="verdict bad">上次失败：{notifyStatus.last_error}</div>
              )}
              {notifyStatus && (notifyStatus.spooled > 0 || notifyStatus.breaker_seconds > 0) && (
                <div className="hint">
                  {notifyStatus.spooled > 0 ? `已暂存 ${notifyStatus.spooled} 条，通道恢复后自动补发。` : ""}
                  {notifyStatus.breaker_seconds > 0
                    ? ` 连续失败，暂停发送 ${notifyStatus.breaker_seconds} 秒。`
                    : ""}
                </div>
              )}
            </div>
          </div>


        </div>

        <div className="card">
          <div className="card-head">
            <span className="card-title">风控状态</span>
            <span className="spacer" />
            {risk?.halted ? (
              <button
                type="button"
                className="btn-sm primary"
                disabled={riskBusy || risk.kill_switch}
                onClick={() => void resumeRisk()}
              >
                {riskBusy ? "处理中…" : "解除熔断"}
              </button>
            ) : (
              <span className="faint" style={{ fontSize: "var(--fs-12)"}}>正常</span>
            )}
          </div>
          <div className="card-body stack">
            {risk ? (
              <>
                <div className={`verdict ${risk.halted ? "bad" : "ok"}`}>
                  {risk.halted
                    ? `已熔断：${risk.halted_reason}`
                    : "风控正常，未触发任何熔断"}
                </div>
                <div className="form-grid">
                  <div className="field">
                    <span className="field-label">
                      <span>本轮盈亏</span>
                      <em>USDT</em>
                    </span>
                    <strong className={`num ${risk.day_pnl < 0 ? "down" : "up"}`}>
                      {risk.day_pnl.toFixed(2)}
                    </strong>
                  </div>
                  <div className="field">
                    <span className="field-label">
                      <span>本轮成交</span>
                      <em>笔</em>
                    </span>
                    <strong className="num">
                      {risk.day_trades} / {risk.limits.max_daily_trades || "∞"}
                    </strong>
                  </div>
                  <div className="field">
                    <span className="field-label">
                      <span>连续亏损</span>
                      <em>次</em>
                    </span>
                    <strong className="num">
                      {risk.consecutive_losses} /{" "}
                      {risk.limits.max_consecutive_losses || "∞"}
                    </strong>
                  </div>
                  <div className="field">
                    <span className="field-label">
                      <span>当日累计</span>
                      <em>USDT</em>
                    </span>
                    <strong className={`num ${risk.realized_pnl < 0 ? "down" : "up"}`}>
                      {risk.realized_pnl.toFixed(2)}
                    </strong>
                  </div>
                </div>
                <div className="hint">
                  解除熔断只恢复下单权限。当日已发生的亏损仍计入统计，额度从解除时刻重新起算。
                  全局急停要在上面的风控参数里关。
                </div>
              </>
            ) : (
              <div className="hint">正在读取风控状态…</div>
            )}
          </div>
        </div>

        <div className="card">
          <div className="card-head">
            <span className="card-title">风控参数</span>
            <span className="spacer" />
            <span className="faint" style={{ fontSize: "var(--fs-12)"}}>填 0 表示不限制</span>
          </div>
          <div className="card-body stack">
            <div className="form-grid">
              {PCT_FIELDS.map(([key, label, unit]) => (
                <div key={key} className="field">
                  <label className="field-label" htmlFor={`risk-${key}`}>
                    <span>{label}</span>
                    <em>{unit}</em>
                  </label>
                  <input
                    id={`risk-${key}`}
                    className="input"
                    type="number"
                    min={0}
                    max={100}
                    step={1}
                    value={Math.round(Number(draft.risk[key]) * 100)}
                    onChange={(event) =>
                      patchRisk(key, Number(event.target.value) / 100)
                    }
                  />
                </div>
              ))}
              {RISK_FIELDS.map(([key, label, unit]) => (
                <div key={key} className="field">
                  <label className="field-label" htmlFor={`risk-${key}`}>
                    <span>{label}</span>
                    <em>{unit}</em>
                  </label>
                  <input
                    id={`risk-${key}`}
                    className="input"
                    type="number"
                    min={0}
                    step={key === "max_leverage" ? 1 : 10}
                    value={Number(draft.risk[key])}
                    onChange={(event) => patchRisk(key, Number(event.target.value))}
                  />
                </div>
              ))}
            </div>
            <div className="row" style={{ gap: 20 }}>
              <label className="check">
                <input
                  type="checkbox"
                  checked={draft.risk.require_confirmation}
                  onChange={(event) =>
                    patchRisk("require_confirmation", event.target.checked)
                  }
                />
                下单前需要确认
              </label>
              <label className="check">
                <input
                  type="checkbox"
                  checked={draft.risk.kill_switch}
                  onChange={(event) => patchRisk("kill_switch", event.target.checked)}
                />
                全局熔断（禁止所有新订单）
              </label>
            </div>
          </div>
        </div>
      </div>
    </>
  );
}
