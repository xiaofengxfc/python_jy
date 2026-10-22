import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { TIMEFRAMES } from "../format";
import type {
  AISettings,
  AIStatus,
  AppSettings,
  ChannelStatus,
  CredentialItem,
  InstallStatus,
  NotifyStatus,
  RiskState,
} from "../types";

interface Props {
  settings: AppSettings | null;
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

export default function SettingsPage({ settings, onSaved }: Props) {
  const [draft, setDraft] = useState<AppSettings | null>(settings);
  const [credentials, setCredentials] = useState<CredentialItem[]>([]);
  const [credentialsError, setCredentialsError] = useState("");
  const [saving, setSaving] = useState(false);
  const [flash, setFlash] = useState<{ tone: "ok" | "err"; text: string } | null>(null);
  const [network, setNetwork] = useState(settings?.network ?? "testnet");
  const [credDrafts, setCredDrafts] = useState<Record<string, Draft>>({});
  const [credBusy, setCredBusy] = useState<string>("");
  const [risk, setRisk] = useState<RiskState | null>(null);
  const [riskBusy, setRiskBusy] = useState(false);
  const [notifyStatus, setNotifyStatus] = useState<NotifyStatus | null>(null);
  const [notifyBusy, setNotifyBusy] = useState(false);
  const [wecomApiSecret, setWecomApiSecret] = useState("");
  const [wecomSecretBusy, setWecomSecretBusy] = useState(false);
  const [install, setInstall] = useState<InstallStatus | null>(null);
  const [installBusy, setInstallBusy] = useState(false);
  const [installError, setInstallError] = useState("");
  const [installOpen, setInstallOpen] = useState(false);
  const [installRegistry, setInstallRegistry] = useState("");
  const [installNpmPath, setInstallNpmPath] = useState("");
  const [installPlan, setInstallPlan] = useState<string[]>([]);
  const [channel, setChannel] = useState<ChannelStatus | null>(null);
  const [channelBusy, setChannelBusy] = useState(false);
  const [channelNote, setChannelNote] = useState("");
  const [wecomBotId, setWecomBotId] = useState("");
  const [wecomSecret, setWecomSecret] = useState("");
  const [aiStatus, setAiStatus] = useState<AIStatus | null>(null);
  const [aiKey, setAiKey] = useState("");
  const [aiBusy, setAiBusy] = useState<"key" | "test" | "">("");
  const [aiNote, setAiNote] = useState<{ tone: "ok" | "err"; text: string } | null>(null);

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

  const loadChannel = useCallback(async () => {
    try {
      const next = await api<ChannelStatus>("/api/install/openclaw/channel");
      setChannel(next);
      if (next.bot_id) setWecomBotId((current) => current || next.bot_id || "");
      return next;
    } catch {
      return null;
    }
  }, []);

  useEffect(() => {
    void loadChannel();
  }, [loadChannel]);

  // Re-read the channel state whenever a step finishes: registering the plugin
  // and scanning the code both change what the page should offer next.
  useEffect(() => {
    if (install?.running) return;
    if (install?.state === "running") return;
    void loadChannel();
  }, [install?.state, install?.running, loadChannel]);

  const registerChannel = async () => {
    setChannelBusy(true);
    setChannelNote("");
    try {
      setInstall(await api<InstallStatus>("/api/install/openclaw/channel", { method: "POST" }));
    } catch (error) {
      setChannelNote(error instanceof Error ? error.message : String(error));
    } finally {
      setChannelBusy(false);
    }
  };

  const saveChannelCredentials = async (restartAfter = false) => {
    setChannelBusy(true);
    setChannelNote("");
    try {
      const result = await api<{ note?: string; bot_id?: string }>(
        "/api/install/openclaw/channel/config",
        {
          method: "PUT",
          body: JSON.stringify({ bot_id: wecomBotId, secret: wecomSecret }),
        },
      );
      setWecomSecret("");
      if (restartAfter) {
        const restarted = await api<{ state: string; error: string }>(
          "/api/install/openclaw/gateway/restart",
          { method: "POST" },
        );
        setChannelNote(
          restarted.error || "API 凭证已保存，网关已重启，长连接会使用新凭证。",
        );
      } else {
        setChannelNote(result.note || "API 凭证已保存。");
      }
      await loadChannel();
    } catch (error) {
      setChannelNote(error instanceof Error ? error.message : String(error));
    } finally {
      setChannelBusy(false);
    }
  };

  const startGateway = async () => {
    setChannelBusy(true);
    setChannelNote("");
    try {
      const result = await api<{ state: string; error: string }>(
        "/api/install/openclaw/gateway",
        { method: "POST" },
      );
      setChannelNote(result.error || "网关已启动。");
      await loadChannel();
    } catch (error) {
      setChannelNote(error instanceof Error ? error.message : String(error));
    } finally {
      setChannelBusy(false);
    }
  };

  const restartGateway = async () => {
    // The channel session lives inside the gateway process: after a fresh scan
    // or an unbind, only a restart makes that change take effect there.
    setChannelBusy(true);
    setChannelNote("");
    try {
      const result = await api<{ state: string; error: string }>(
        "/api/install/openclaw/gateway/restart",
        { method: "POST" },
      );
      setChannelNote(result.error || "网关已重启。");
      await loadChannel();
    } catch (error) {
      setChannelNote(error instanceof Error ? error.message : String(error));
    } finally {
      setChannelBusy(false);
    }
  };

  const loadInstall = useCallback(async () => {
    try {
      const next = await api<InstallStatus>("/api/install/openclaw");
      setInstall(next);
      // Poll faster while npm is running: the log is the only feedback the
      // user gets, and a 10 s gap looks like a hang.
      return next;
    } catch {
      return null;
    }
  }, []);

  useEffect(() => {
    void loadInstall();
  }, [loadInstall]);

  useEffect(() => {
    if (!install?.running) return;
    const timer = window.setInterval(() => void loadInstall(), 2000);
    return () => window.clearInterval(timer);
  }, [install?.running, loadInstall]);

  const previewInstall = async () => {
    setInstallError("");
    try {
      const result = await api<{ command: string[] }>("/api/install/openclaw/plan", {
        method: "POST",
        body: JSON.stringify({
          registry: installRegistry,
          npm_path: installNpmPath,
        }),
      });
      setInstallPlan(result.command);
      setInstallOpen(true);
    } catch (error) {
      setInstallPlan([]);
      setInstallOpen(true);
      setInstallError(error instanceof Error ? error.message : String(error));
    }
  };

  const startInstall = async () => {
    setInstallBusy(true);
    setInstallError("");
    try {
      const next = await api<InstallStatus>("/api/install/openclaw", {
        method: "POST",
        body: JSON.stringify({
          registry: installRegistry,
          npm_path: installNpmPath,
        }),
      });
      setInstall(next);
    } catch (error) {
      setInstallError(error instanceof Error ? error.message : String(error));
    } finally {
      setInstallBusy(false);
    }
  };

  const cancelInstall = async () => {
    setInstallError("");
    try {
      setInstall(await api<InstallStatus>("/api/install/openclaw/cancel", { method: "POST" }));
    } catch (error) {
      setInstallError(error instanceof Error ? error.message : String(error));
    }
  };

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
      // Send with the values currently in the form, not the saved ones: the
      // button's job is to verify a target or path before the user commits it.
      const result = await api<{ ok: boolean; warning?: string }>("/api/notify/test", {
        method: "POST",
        body: JSON.stringify({
          message: "",
          mode: draft.notify.mode,
          channel: draft.notify.channel,
          target: draft.notify.target,
          account: draft.notify.account,
          command: draft.notify.command,
          corp_id: draft.notify.corp_id,
          agent_id: draft.notify.agent_id,
          api_base: draft.notify.api_base,
        }),
      });
      // ``ok`` only means the gateway accepted the message. WeChat delivers
      // only into a conversation the user opened, so without a context token
      // the honest answer is "已提交，但手机可能收不到" plus the one action
      // that fixes it.
      setFlash(
        result.warning
          ? { tone: "err", text: result.warning }
          : { tone: "ok", text: "测试消息已发送，请查看企业微信。" },
      );
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

  const saveWecomSecret = async () => {
    if (!draft) return;
    if (!wecomApiSecret.trim()) {
      setFlash({ tone: "err", text: "请先填写企业微信应用 Secret。" });
      return;
    }
    setWecomSecretBusy(true);
    setFlash(null);
    try {
      await api("/api/notify/wecom/secret", {
        method: "PUT",
        body: JSON.stringify({ secret: wecomApiSecret.trim() }),
      });
      setWecomApiSecret("");
      setFlash({ tone: "ok", text: "企业微信应用 Secret 已存入系统密钥环。" });
      await loadNotify();
    } catch (error) {
      setFlash({ tone: "err", text: error instanceof Error ? error.message : String(error) });
    } finally {
      setWecomSecretBusy(false);
    }
  };

  const save = async () => {
    if (!draft) return;
    setSaving(true);
    setFlash(null);
    try {
      const result = await api<{ settings: AppSettings }>("/api/settings", {
        method: "PUT",
        body: JSON.stringify({
          network: draft.network,
          market_type: draft.market_type,
          last_symbol: draft.last_symbol,
          timeframe: draft.timeframe,
          default_order_quote: draft.default_order_quote,
          kline_limit: draft.kline_limit,
          proxy: draft.proxy,
          theme: draft.theme,
          notification_sound: draft.notification_sound,
          risk: draft.risk,
          notify: draft.notify,
          ai: draft.ai,
        }),
      });
      // The secret is deliberately not part of the settings JSON; when the
      // user pasted a new one, store it in the keyring in the same save click.
      if (draft.notify.mode === "wecom_api" && wecomApiSecret.trim()) {
        await api("/api/notify/wecom/secret", {
          method: "PUT",
          body: JSON.stringify({ secret: wecomApiSecret.trim() }),
        });
        setWecomApiSecret("");
      }
      onSaved(result.settings);
      setNetwork(result.settings.network);
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
          <span className="page-sub">正在读取设置…</span>
        </div>
        <div className="placeholder">正在读取设置…</div>
      </>
    );
  }

  // The one bound recipient, if there is exactly one. Narrowed here because
  // the array index is optional and the card below reads it several times.

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
          {credentials.map((item) => {
            const entry = credDrafts[item.market] ?? EMPTY_DRAFT;
            const busy = credBusy === item.market;
            return (
              <div key={item.market} className="cred-row">
                <div className="cred-name">
                  <span>{item.label}</span>
                  <span className="cred-state">
                    {item.configured ? `已配置 · ${item.masked}` : "未配置"}
                  </span>
                </div>
                <div className="cred-fields">
                  <input
                    className="input"
                    type="password"
                    autoComplete="off"
                    placeholder="API Key"
                    value={entry.apiKey}
                    onChange={(event) =>
                      setCredDrafts((current) => ({
                        ...current,
                        [item.market]: { ...entry, apiKey: event.target.value },
                      }))
                    }
                  />
                  <input
                    className="input"
                    type="password"
                    autoComplete="off"
                    placeholder="API Secret"
                    value={entry.apiSecret}
                    onChange={(event) =>
                      setCredDrafts((current) => ({
                        ...current,
                        [item.market]: { ...entry, apiSecret: event.target.value },
                      }))
                    }
                  />
                  <div className="cred-actions">
                    <label className="check">
                      <input
                        type="checkbox"
                        checked={entry.verify}
                        onChange={(event) =>
                          setCredDrafts((current) => ({
                            ...current,
                            [item.market]: { ...entry, verify: event.target.checked },
                          }))
                        }
                      />
                      保存前做连接测试
                    </label>
                    <span className="spacer" />
                    <button
                      type="button"
                      className="btn-sm primary"
                      disabled={busy || !entry.apiKey || !entry.apiSecret}
                      onClick={() => saveCredential(item.market)}
                    >
                      {busy ? "处理中…" : "保存"}
                    </button>
                    {item.configured && (
                      <button
                        type="button"
                        className="btn-sm"
                        disabled={busy}
                        onClick={() => deleteCredential(item.market)}
                      >
                        删除
                      </button>
                    )}
                  </div>
                </div>
              </div>
            );
          })}
          <div className="card-body hint">
            密钥保存在系统凭据库（Windows 凭据管理器 / macOS 钥匙串），不会写入配置文件。
            建议在币安只勾选「读取」和「交易」权限，关闭「提现」，并绑定 IP 白名单。
          </div>
        </div>

        <div className="card">
          <div className="card-head">
            <span className="card-title">新闻来源</span>
            <span className="spacer" />
            <span className="faint" style={{ fontSize: 11 }}>
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
            <span className="faint" style={{ fontSize: 11 }}>
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

              <div className="field">
                <label className="field-label" htmlFor="set-ai-limit">
                  <span>每日调用上限</span>
                </label>
                <input
                  id="set-ai-limit"
                  className="input"
                  type="number"
                  min={0}
                  step={10}
                  value={draft.ai.daily_limit}
                  onChange={(event) => patchAI({ daily_limit: Number(event.target.value) })}
                />
                <span className="field-hint">填 0 表示不限制。超额后调用直接失败，不会继续计费。</span>
              </div>

              <div className="field">
                <label className="field-label" htmlFor="set-ai-cache">
                  <span>结果缓存</span>
                </label>
                <input
                  id="set-ai-cache"
                  className="input"
                  type="number"
                  min={0}
                  step={60}
                  value={draft.ai.cache_seconds}
                  onChange={(event) => patchAI({ cache_seconds: Number(event.target.value) })}
                />
                <span className="field-hint">秒。同一根已收盘 K 线在缓存期内重复问，不再计费。</span>
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
                <div className="faint" style={{ fontSize: 11, marginBottom: 4 }}>
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
                  <input
                    id="set-symbol"
                    className="input"
                    value={draft.last_symbol}
                    onChange={(event) => patch({ last_symbol: event.target.value })}
                  />
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
                <div className="field">
                  <label className="field-label" htmlFor="set-quote">
                    <span>默认下单金额</span>
                    <em>USDT</em>
                  </label>
                  <input
                    id="set-quote"
                    className="input"
                    type="number"
                    min={1}
                    step={1}
                    value={draft.default_order_quote}
                    onChange={(event) =>
                      patch({ default_order_quote: Number(event.target.value) })
                    }
                  />
                </div>
                <div className="field">
                  <label className="field-label" htmlFor="set-limit">
                    <span>K 线加载条数</span>
                    <em>100–1000</em>
                  </label>
                  <input
                    id="set-limit"
                    className="input"
                    type="number"
                    min={100}
                    max={1000}
                    step={100}
                    value={draft.kline_limit}
                    onChange={(event) => patch({ kline_limit: Number(event.target.value) })}
                  />
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
                <div className="faint" style={{ fontSize: 12 }}>
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
              <span className="faint" style={{ fontSize: 11 }}>
                {notifyStatus
                  ? notifyStatus.mode === "wecom_api"
                    ? notifyStatus.available
                      ? "自建应用 API 已配置"
                      : "API 配置不完整"
                    : notifyStatus.available
                      ? "OpenClaw 已找到"
                      : "未找到 OpenClaw"
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
              <div className="field">
                <label className="field-label">
                  <span>推送通道</span>
                  <em>自建应用 API 不依赖 OpenClaw，适合企业微信管理员直接配置</em>
                </label>
                <div className="segmented" role="group" aria-label="企业微信通知模式">
                  <button
                    type="button"
                    aria-pressed={draft.notify.mode === "wecom_api"}
                    onClick={() => patchNotify({ mode: "wecom_api" })}
                  >
                    自建应用 API
                  </button>
                  <button
                    type="button"
                    aria-pressed={draft.notify.mode !== "wecom_api"}
                    onClick={() => patchNotify({ mode: "openclaw" })}
                  >
                    OpenClaw 智能机器人
                  </button>
                </div>
              </div>
              <label className="check">
                <input
                  type="checkbox"
                  checked={draft.notify.enabled}
                  onChange={(event) => patchNotify({ enabled: event.target.checked })}
                />
                启用企业微信推送
              </label>
              {/* A bound channel plus a closed master switch is the state that
                  looks like "已经登录但收不到通知"; say it instead of leaving
                  the user to discover a checkbox. */}
              {draft.notify.mode !== "wecom_api" && channel?.configured && !draft.notify.enabled && (
                <div className="hint" style={{ color: "var(--warn, #b45309)" }}>
                  渠道已配置，但推送总开关是关的。成交和风控通知不会发出。
                  勾选上面的「启用企业微信推送」并保存。
                </div>
              )}
              <div className="form-grid">
                <div className="field">
                  <label className="field-label" htmlFor="notify-target">
                    <span>接收人 userid</span>
                    <em>
                      {draft.notify.mode === "wecom_api"
                        ? "企业微信通讯录里的成员账号，多个用 | 分隔"
                        : "企业微信通讯录里的成员账号"}
                    </em>
                  </label>
                  <input
                    id="notify-target"
                    value={draft.notify.target}
                    onChange={(event) => patchNotify({ target: event.target.value })}
                    placeholder={
                      draft.notify.mode === "wecom_api"
                        ? "例：XiaoJiaJian"
                        : "例：XiaoJiaJian（先给机器人发过消息的那个账号）"
                    }
                  />
                </div>
              {draft.notify.mode === "wecom_api" ? (
                <div className="field">
                  <label className="field-label" htmlFor="notify-agent-id">
                    <span>AgentId</span>
                    <em>企业微信自建应用的 AgentId</em>
                  </label>
                  <input
                    id="notify-agent-id"
                    value={draft.notify.agent_id}
                    onChange={(event) => patchNotify({ agent_id: event.target.value })}
                    placeholder="例：1000002"
                    inputMode="numeric"
                  />
                </div>
              ) : (
                <div className="field">
                  <label className="field-label" htmlFor="notify-channel">
                    <span>通道名</span>
                    <em>默认 wecom，一般不用改</em>
                  </label>
                  <input
                    id="notify-channel"
                    value={draft.notify.channel}
                    onChange={(event) => patchNotify({ channel: event.target.value })}
                    placeholder="wecom"
                  />
                </div>
              )}
              </div>
              {notifyStatus?.target_warning && (
                <div className="hint" style={{ color: "var(--warn, #b45309)" }}>
                  {notifyStatus.target_warning}
                </div>
              )}
              {draft.notify.mode === "wecom_api" ? (
                <>
                  <div className="form-grid">
                    <div className="field">
                      <label className="field-label" htmlFor="notify-corp-id">
                        <span>CorpID</span>
                        <em>企业微信“我的企业”里的企业 ID</em>
                      </label>
                      <input
                        id="notify-corp-id"
                        value={draft.notify.corp_id}
                        onChange={(event) => patchNotify({ corp_id: event.target.value })}
                        placeholder="例：ww1234567890abcdef"
                      />
                    </div>
                    <div className="field">
                      <label className="field-label" htmlFor="notify-api-base">
                        <span>API 地址</span>
                        <em>一般不用改。代理环境可以填内网地址</em>
                      </label>
                      <input
                        id="notify-api-base"
                        value={draft.notify.api_base}
                        onChange={(event) => patchNotify({ api_base: event.target.value })}
                        placeholder="https://qyapi.weixin.qq.com"
                      />
                    </div>
                  </div>
                  <div className="form-grid">
                    <div className="field">
                      <label className="field-label" htmlFor="notify-wecom-secret">
                        <span>Secret</span>
                        <em>
                          {notifyStatus?.secret_saved
                            ? "已保存，留空保持不变"
                            : "只存入系统密钥环，不写入配置文件"}
                        </em>
                      </label>
                      <div className="ticket-row">
                        <input
                          id="notify-wecom-secret"
                          type="password"
                          value={wecomApiSecret}
                          onChange={(event) => setWecomApiSecret(event.target.value)}
                          placeholder={notifyStatus?.secret_saved ? "••••••••（留空不修改）" : "输入自建应用 Secret"}
                          autoComplete="new-password"
                        />
                        <button
                          type="button"
                          className="btn-sm"
                          disabled={wecomSecretBusy || !wecomApiSecret.trim()}
                          onClick={() => void saveWecomSecret()}
                        >
                          {wecomSecretBusy ? "保存中…" : "保存 Secret"}
                        </button>
                      </div>
                    </div>
                  </div>
                  <div className="hint">
                    到企业微信后台的「应用管理 → 自建应用」取 CorpID、AgentId 和 Secret。
                    “接收人”填应用可见范围内的成员 userid。Secret 只进系统密钥环。
                  </div>
                </>
              ) : (
                <>
                  <div className="field">
                    <label className="field-label" htmlFor="notify-account">
                      <span>账号</span>
                      <em>插件内的账号标识，留空用 default</em>
                    </label>
                    <input
                      id="notify-account"
                      value={draft.notify.account}
                      onChange={(event) => patchNotify({ account: event.target.value })}
                      placeholder="default"
                    />
                  </div>
                  <div className="field">
                    <label className="field-label" htmlFor="notify-command">
                      <span>OpenClaw 路径</span>
                      <em>可留空，默认从 PATH 查找</em>
                    </label>
                    <input
                      id="notify-command"
                      value={draft.notify.command}
                      onChange={(event) => patchNotify({ command: event.target.value })}
                      placeholder="openclaw"
                    />
                  </div>
                </>
              )}
              <div className="form-grid">
                <label className="check">
                  <input
                    type="checkbox"
                    checked={draft.notify.on_fill}
                    onChange={(event) => patchNotify({ on_fill: event.target.checked })}
                  />
                  成交回报
                </label>
                <label className="check">
                  <input
                    type="checkbox"
                    checked={draft.notify.on_protection}
                    onChange={(event) => patchNotify({ on_protection: event.target.checked })}
                  />
                  止损 / 止盈触发
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
                  实盘保护单告警
                </label>
                <label className="check">
                  <input
                    type="checkbox"
                    checked={draft.notify.on_task}
                    onChange={(event) => patchNotify({ on_task: event.target.checked })}
                  />
                  任务状态（自动交易启停 / 回测完成或失败）
                </label>
                {draft.notify.mode !== "wecom_api" && (
                  <label className="check">
                    <input
                      type="checkbox"
                      checked={draft.notify.preflight}
                      onChange={(event) => patchNotify({ preflight: event.target.checked })}
                    />
                    发送前检查网关（连不上就立即报错，不必等 30 秒）
                  </label>
                )}
              </div>
              {notifyStatus && !notifyStatus.available && (
                <div className="verdict bad">
                  {draft.notify.mode === "wecom_api"
                    ? "自建应用 API 配置不完整：请填写 CorpID、AgentId、Secret 和接收人。"
                    : "未找到 OpenClaw：可点下方的“自动安装 OpenClaw”，或在上方填写 openclaw 的完整路径。"}
                </div>
              )}
              <div
                className="install-box"
                style={{ display: draft.notify.mode === "wecom_api" ? "none" : undefined }}
              >
                <div className="install-head">
                  <span className="card-title" style={{ fontSize: 12 }}>
                    OpenClaw 环境
                  </span>
                  <span className="faint" style={{ fontSize: 11 }}>
                    {install
                      ? install.node
                        ? `Node ${install.node}${install.npm ? ` · npm ${install.npm}` : ""}`
                        : "未检测到 Node.js"
                      : "检测中…"}
                  </span>
                  <span className="spacer" />
                  {install?.running ? (
                    <button type="button" className="btn-sm" onClick={() => void cancelInstall()}>
                      取消安装
                    </button>
                  ) : (
                    <button
                      type="button"
                      className="btn-sm"
                      disabled={installBusy}
                      onClick={() => void previewInstall()}
                    >
                      自动安装 OpenClaw
                    </button>
                  )}
                </div>
                {installError && <div className="verdict bad">{installError}</div>}
                {installOpen && (
                  <>
                    <div className="form-grid">
                      <div className="field">
                        <label className="field-label" htmlFor="install-registry">
                          <span>npm 镜像源</span>
                          <em>装不下来时换一个</em>
                        </label>
                        <select
                          id="install-registry"
                          className="select"
                          value={installRegistry}
                          disabled={install?.running}
                          onChange={(event) => {
                            setInstallRegistry(event.target.value);
                            setInstallPlan([]);
                          }}
                        >
                          <option value="">官方源 registry.npmjs.org</option>
                          {(install?.registries ?? [])
                            .filter((item) => item !== "https://registry.npmjs.org")
                            .map((item) => (
                              <option key={item} value={item}>
                                国内镜像 {item.replace("https://", "")}
                              </option>
                            ))}
                        </select>
                      </div>
                      <div className="field">
                        <label className="field-label" htmlFor="install-npm">
                          <span>npm 路径</span>
                          <em>可留空，默认从 PATH 查找</em>
                        </label>
                        <input
                          id="install-npm"
                          value={installNpmPath}
                          disabled={install?.running}
                          onChange={(event) => {
                            setInstallNpmPath(event.target.value);
                            setInstallPlan([]);
                          }}
                          placeholder={install?.npm_path || "npm"}
                        />
                      </div>
                    </div>
                    {install && !install.ready && (
                      <div className="verdict bad">{install.reason}</div>
                    )}
                    {installPlan.length > 0 && (
                      <pre className="install-cmd">{installPlan.join(" ")}</pre>
                    )}
                    {!install?.running && (
                      <div className="install-head">
                        <button
                          type="button"
                          className="btn-sm primary"
                          disabled={installBusy || install?.ready === false}
                          onClick={() => void startInstall()}
                        >
                          {installBusy ? "启动中…" : "开始安装"}
                        </button>
                        <button
                          type="button"
                          className="btn-sm"
                          onClick={() => {
                            setInstallOpen(false);
                            setInstallPlan([]);
                            setInstallError("");
                          }}
                        >
                          收起
                        </button>
                        <span className="faint" style={{ fontSize: 11 }}>
                          安装会执行 npm 全局安装，可能需要几分钟。
                        </span>
                      </div>
                    )}
                  </>
                )}
                {install && install.state !== "idle" && (
                  <>
                    <div className="hint">
                      {install.state === "running"
                        ? "正在安装…"
                        : install.state === "done"
                          ? "安装完成：openclaw 已可用。"
                          : install.state === "canceled"
                            ? "安装已取消。"
                            : "安装失败。"}
                      {install.error && install.state !== "done" ? ` ${install.error}` : ""}
                    </div>
                    {install.log.length > 0 && (
                      <pre className="install-cmd">{install.log.join("\n")}</pre>
                    )}
                  </>
                )}
                {/* The plugin lives in the gateway's own ledger, not in npm's
                    prefix: registering it is what makes ``message send`` know
                    the channel. */}
                <div className="install-head" style={{ marginTop: 10 }}>
                  <span className="card-title" style={{ fontSize: 12 }}>
                    企业微信渠道
                  </span>
                  <span className="faint" style={{ fontSize: 11 }}>
                    {!channel
                      ? "检测中…"
                      : channel.state === "ready"
                        ? "长连接已就绪，可直接推送"
                        : channel.state === "installed"
                          ? "插件已装，尚未填写 Bot ID / Secret"
                          : channel.state === "missing"
                            ? channel.cli
                              ? "插件未注册到网关"
                              : "未检测到 openclaw"
                            : "状态未知"}
                  </span>
                  <span className="spacer" />
                  <button
                    type="button"
                    className="btn-sm primary"
                    disabled={channelBusy || install?.running || !install?.available}
                    onClick={() => void registerChannel()}
                  >
                    注册企业微信插件
                  </button>
                  <button
                    type="button"
                    className="btn-sm"
                    disabled={channelBusy}
                    onClick={() => void loadChannel()}
                  >
                    刷新状态
                  </button>
                </div>
                {channel?.state === "missing" && channel.cli && (
                  <div className="verdict bad">
                    网关还不认识企业微信渠道。请先点“注册企业微信插件”。
                  </div>
                )}
                {channel?.state === "installed" && (
                  <div className="verdict bad">
                    插件已装，但还没有 Bot ID / Secret。请在下方填写 API 模式凭证。
                  </div>
                )}
                <div className="install-head" style={{ marginTop: 8 }}>
                  <span className="card-title" style={{ fontSize: 12 }}>
                    API 模式凭证
                  </span>
                  <span className="faint" style={{ fontSize: 11 }}>
                    Bot ID + Secret · 长连接，无需回调地址
                  </span>
                </div>
                <div className="form-grid">
                  <div className="field">
                    <label className="field-label" htmlFor="wecom-bot-id">
                      <span>Bot ID</span>
                      <em>企业微信智能机器人后台的机器人 ID</em>
                    </label>
                    <input
                      id="wecom-bot-id"
                      value={wecomBotId}
                      disabled={channelBusy}
                      onChange={(event) => setWecomBotId(event.target.value)}
                      placeholder="aibp..."
                    />
                  </div>
                  <div className="field">
                    <label className="field-label" htmlFor="wecom-secret">
                      <span>Secret</span>
                      <em>{channel?.configured ? "已保存，留空保持不变" : "只保存在 OpenClaw 配置中"}</em>
                    </label>
                    <input
                      id="wecom-secret"
                      type="password"
                      value={wecomSecret}
                      disabled={channelBusy}
                      onChange={(event) => setWecomSecret(event.target.value)}
                      placeholder={channel?.configured ? "••••••••（留空不修改）" : "输入机器人 Secret"}
                      autoComplete="new-password"
                    />
                  </div>
                </div>
                <div className="install-head">
                  <button
                    type="button"
                    className="btn-sm primary"
                    disabled={channelBusy || !wecomBotId.trim()}
                    onClick={() => void saveChannelCredentials()}
                  >
                    {channelBusy ? "保存中…" : "保存 API 凭证"}
                  </button>
                  <button
                    type="button"
                    className="btn-sm"
                    disabled={channelBusy || !wecomBotId.trim()}
                    onClick={() => void saveChannelCredentials(true)}
                  >
                    保存并重启
                  </button>
                  <span className="faint" style={{ fontSize: 11 }}>
                    保存后需重启网关，长连接才会使用新凭证。
                  </span>
                </div>
                {channelNote && <div className="hint">{channelNote}</div>}
                {/* The gateway is its own process: the long connection lives
                    inside it, so a configured channel still delivers nothing
                    while it is down. */}
                <div className="install-head" style={{ marginTop: 8 }}>
                  <span className="faint" style={{ fontSize: 11 }}>
                    {!channel?.gateway
                      ? "网关状态检测中…"
                      : channel.gateway.reachable
                        ? `网关运行中（${channel.gateway.host}:${channel.gateway.port}${
                            channel.gateway.managed ? "，随本程序启动" : "，由外部启动"
                          }）`
                        : channel.gateway.state === "starting"
                          ? "网关正在启动…"
                          : "网关未运行，推送会失败"}
                  </span>
                  <span className="spacer" />
                  {channel?.gateway && !channel.gateway.reachable && (
                    <button
                      type="button"
                      className="btn-sm"
                      disabled={channelBusy}
                      onClick={() => void startGateway()}
                    >
                      启动网关
                    </button>
                  )}
                  {channel?.gateway?.reachable && (
                    <button
                      type="button"
                      className="btn-sm"
                      disabled={channelBusy}
                      onClick={() => void restartGateway()}
                    >
                      重启网关
                    </button>
                  )}
                </div>
                {channel?.gateway?.error && (
                  <div className="verdict bad">{channel.gateway.error}</div>
                )}
              </div>
              {notifyStatus?.last_error && (
                <div className="verdict bad">
                  {notifyStatus.last_error}
                  {notifyStatus.last_error_at > 0 && (
                    <span className="faint" style={{ marginLeft: 6 }}>
                      （{new Date(notifyStatus.last_error_at * 1000).toLocaleTimeString()}）
                    </span>
                  )}
                </div>
              )}
              {notifyStatus && notifyStatus.breaker_seconds > 0 && (
                <div className="verdict bad">
                  通道连续发送失败，已暂停 {notifyStatus.breaker_seconds} 秒。
                  先确认网关在运行。点「发送测试」可以手动探活并恢复。
                </div>
              )}
              {notifyStatus && notifyStatus.spooled > 0 && (
                <div className="verdict bad">
                  {notifyStatus.spooled} 条消息发送失败，暂存在本地。
                  通道恢复后自动补发，标题带【补发】。超过 6 小时的丢弃。
                </div>
              )}
              {notifyStatus && notifyStatus.last_success_at > 0 && (
                <div className="hint">
                  最近一次成功发送：
                  {new Date(notifyStatus.last_success_at * 1000).toLocaleString()}
                  {notifyStatus.pending > 0 ? `，队列中还有 ${notifyStatus.pending} 条` : ""}
                </div>
              )}
              <div className="hint">
                {draft.notify.mode === "wecom_api"
                  ? "自建应用 API 直接调用 qyapi.weixin.qq.com，不用 OpenClaw。接收人填应用可见范围内的成员 userid。Secret 存系统密钥环，保存设置时生效。"
                  : "推送走本机 OpenClaw 网关的长连接，直接送到企业微信智能机器人。不用扫码，登录态不会过期。「接收人」填成员 userid，对方要先给机器人发过消息，推送才送得进去。「发送测试」直接用上面填的内容，不用先保存。"}
              </div>
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
              <span className="faint" style={{ fontSize: 11 }}>正常</span>
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
            <span className="faint" style={{ fontSize: 11 }}>填 0 表示不限制</span>
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
