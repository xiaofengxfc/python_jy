import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import { TIMEFRAMES } from "../format";
import type {
  AppSettings,
  ChannelStatus,
  CredentialItem,
  InstallStatus,
  LoginStatus,
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
  const [install, setInstall] = useState<InstallStatus | null>(null);
  const [installBusy, setInstallBusy] = useState(false);
  const [installError, setInstallError] = useState("");
  const [installOpen, setInstallOpen] = useState(false);
  const [installWithWechat, setInstallWithWechat] = useState(true);
  const [installRegistry, setInstallRegistry] = useState("");
  const [installNpmPath, setInstallNpmPath] = useState("");
  const [installPlan, setInstallPlan] = useState<string[]>([]);
  const [channel, setChannel] = useState<ChannelStatus | null>(null);
  const [channelBusy, setChannelBusy] = useState(false);
  const [channelNote, setChannelNote] = useState("");
  const [login, setLogin] = useState<LoginStatus | null>(null);
  // Bumped on every new code so the <img> reloads instead of showing the old SVG.
  const [qrNonce, setQrNonce] = useState(0);

  const loadChannel = useCallback(async () => {
    try {
      const next = await api<ChannelStatus>("/api/install/openclaw/channel");
      setChannel(next);
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

  // A binding knows its own recipient id, so a fresh install never has to be
  // told what to put in 接收目标 by hand. Fill it only while it is empty: an
  // explicit value the user typed always wins.
  useEffect(() => {
    if (!channel || channel.targets.length !== 1) return;
    const only = channel.targets[0];
    if (!only) return;
    setDraft((current) => {
      if (!current || current.notify.target.trim()) return current;
      return {
        ...current,
        notify: {
          ...current.notify,
          target: only.target,
          account: current.notify.account || only.account,
        },
      };
    });
  }, [channel]);

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

  const openLogin = async (force = false) => {
    setChannelBusy(true);
    setChannelNote("");
    try {
      // ``force`` cancels the pending wizard and prints a fresh code: the
      // button exists for the "过期/扫不了" case, and reusing the old session
      // would hand back the very code the user just rejected.
      const result = await api<LoginStatus>(
        `/api/install/openclaw/login${force ? "?force=true" : ""}`,
        { method: "POST" },
      );
      setLogin(result);
      setQrNonce((n) => n + 1);
      if (force) setChannelNote("已重新生成二维码，请重新扫描。");
    } catch (error) {
      setChannelNote(error instanceof Error ? error.message : String(error));
    } finally {
      setChannelBusy(false);
    }
  };

  const refreshLogin = async () => {
    try {
      setLogin(await api<LoginStatus>("/api/install/openclaw/login"));
    } catch {
      /* the poll is best effort; the panel keeps the last known state */
    }
  };

  const cancelLogin = async () => {
    setChannelBusy(true);
    try {
      setLogin(
        await api<LoginStatus>("/api/install/openclaw/login/cancel", { method: "POST" }),
      );
    } catch (error) {
      setChannelNote(error instanceof Error ? error.message : String(error));
    } finally {
      setChannelBusy(false);
    }
  };

  // Finishing the scan is one step, not three. The server already knows the
  // bound account's recipient id, so it persists it, turns the push switch on
  // and proves the channel with a test message.
  const completeBind = async () => {
    setChannelBusy(true);
    setChannelNote("");
    try {
      const result = await api<{
        target: string;
        account: string;
        settings: AppSettings;
        test: { ok: boolean; error: string } | null;
      }>("/api/install/openclaw/bind", { method: "POST" });
      onSaved(result.settings);
      const test = result.test;
      setChannelNote(
        `已绑定 ${result.account || "当前账号"}，接收目标 ${result.target}，推送已开启。` +
          (test
            ? test.ok
              ? "测试消息已发送，请查看微信。"
              : `测试消息发送失败：${test.error}`
            : ""),
      );
      await loadChannel();
      await loadNotify();
    } catch (error) {
      setChannelNote(error instanceof Error ? error.message : String(error));
    } finally {
      setChannelBusy(false);
    }
  };

  const logoutAccount = async () => {
    // Destructive and irreversible without a fresh scan, so it asks first.
    if (!window.confirm("解绑当前微信账号？解绑后需要重新扫码才能接收通知。")) return;
    setChannelBusy(true);
    setChannelNote("");
    try {
      const result = await api<{ account: string; removed: string[] }>(
        "/api/install/openclaw/logout",
        { method: "POST" },
      );
      setChannelNote(
        `已解绑 ${result.account || "当前账号"}` +
          `（清理 ${result.removed.length} 个文件）。需要时重新扫码即可。`,
      );
      setLogin(null);
      await loadChannel();
    } catch (error) {
      setChannelNote(error instanceof Error ? error.message : String(error));
    } finally {
      setChannelBusy(false);
    }
  };

  const openLoginTerminal = async () => {
    setChannelBusy(true);
    setChannelNote("");
    try {
      const result = await api<{ command: string[] }>(
        "/api/install/openclaw/terminal",
        { method: "POST" },
      );
      setChannelNote(
        "已打开终端窗口，二维码在窗口里；完成后回到本页点“刷新状态”。" +
          `若没有弹出窗口，请手动执行：${result.command.join(" ")}`,
      );
    } catch (error) {
      setChannelNote(error instanceof Error ? error.message : String(error));
    } finally {
      setChannelBusy(false);
    }
  };

  // Poll while a code is pending: it becomes "done" the moment WeChat reports
  // the scan, and the page must swap the QR for a success message on its own.
  useEffect(() => {
    const state = login?.state;
    if (state !== "starting" && state !== "waiting") return;
    const timer = window.setInterval(() => {
      void refreshLogin();
    }, 2000);
    return () => window.clearInterval(timer);
  }, [login?.state]);

  // A finished scan changes the channel state, so the panel must re-read it.
  useEffect(() => {
    if (login?.state === "done") void loadChannel();
  }, [login?.state, loadChannel]);

  // ...and it must also finish the job by itself: persist the recipient, turn
  // the push switch on and prove delivery. Running it once per scan is what
  // keeps a finished binding from turning into a "still nothing arrives" loop.
  const bindKey = useRef("");
  useEffect(() => {
    if (login?.state !== "done" || !channel?.configured) return;
    const key = `${channel.account}|${login.started_at}`;
    if (bindKey.current === key) return;
    bindKey.current = key;
    void completeBind();
  }, [login?.state, login?.started_at, channel?.account, channel?.configured]);

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
          with_wechat: installWithWechat,
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
          with_wechat: installWithWechat,
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
          channel: draft.notify.channel,
          target: draft.notify.target,
          account: draft.notify.account,
          command: draft.notify.command,
        }),
      });
      // ``ok`` only means the gateway accepted the message. WeChat delivers
      // only into a conversation the user opened, so without a context token
      // the honest answer is "已提交，但手机可能收不到" plus the one action
      // that fixes it.
      setFlash(
        result.warning
          ? { tone: "err", text: result.warning }
          : { tone: "ok", text: "测试消息已发送，请查看微信。" },
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
      const data = await api<{ items: CredentialItem[] }>("/api/credentials");
      setCredentials(data.items);
      setCredentialsError("");
    } catch (error) {
      setCredentialsError(error instanceof Error ? error.message : String(error));
    }
  }, []);

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
          theme: draft.theme,
          notification_sound: draft.notification_sound,
          risk: draft.risk,
          notify: draft.notify,
        }),
      });
      onSaved(result.settings);
      setNetwork(result.settings.network);
      setFlash({ tone: "ok", text: "设置已保存。" });
      await loadCredentials();
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
  const boundTarget = channel?.targets.length === 1 ? channel?.targets[0] : undefined;

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
              新闻面板聚合以下无需密钥的公开来源，交易对相关度按币种过滤：
              <ul style={{ margin: "8px 0 0 18px", padding: 0 }}>
                {NEWS_SOURCES.map((source) => (
                  <li key={source}>{source}</li>
                ))}
              </ul>
            </div>
            <div className="hint" style={{ marginTop: 8 }}>
              币安公告不会按币种过滤（上币、下架、维护都会影响交易），
              CoinTelegraph 与 Decrypt 只保留标题提及当前币种的内容。不再需要任何 Token。
            </div>
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
              <span className="card-title">微信通知（ClawBot）</span>
              <span className="spacer" />
              <span className="faint" style={{ fontSize: 11 }}>
                {notifyStatus
                  ? notifyStatus.available
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
              <label className="check">
                <input
                  type="checkbox"
                  checked={draft.notify.enabled}
                  onChange={(event) => patchNotify({ enabled: event.target.checked })}
                />
                启用微信推送
              </label>
              {/* A bound channel plus a closed master switch is the state that
                  looks like "已经登录但收不到通知"; say it instead of leaving
                  the user to discover a checkbox. */}
              {channel?.configured && !draft.notify.enabled && (
                <div className="hint" style={{ color: "var(--warn, #b45309)" }}>
                  微信通道已绑定，但推送总开关是关的：成交、风控通知都不会发出。
                  勾选上面的“启用微信推送”并保存后生效。
                </div>
              )}
              <div className="form-grid">
                <div className="field">
                  <label className="field-label" htmlFor="notify-target">
                    <span>接收目标</span>
                    <em>形如 xxx@im.wechat</em>
                  </label>
                  <input
                    id="notify-target"
                    value={draft.notify.target}
                    onChange={(event) => patchNotify({ target: event.target.value })}
                    placeholder="先给 ClawBot 发一条消息，或点下方“填入已绑定账号”"
                  />
                </div>
                <div className="field">
                  <label className="field-label" htmlFor="notify-channel">
                    <span>通道名</span>
                    <em>默认 openclaw-weixin</em>
                  </label>
                  <input
                    id="notify-channel"
                    value={draft.notify.channel}
                    onChange={(event) => patchNotify({ channel: event.target.value })}
                    placeholder="openclaw-weixin"
                  />
                </div>
              </div>
              {/* The bound account already knows its own WeChat id, so the user
                  should never have to copy it out of a log. One click fills it. */}
              {boundTarget && (
                <div className="install-head" style={{ marginTop: 2 }}>
                  <span className="faint" style={{ fontSize: 11 }}>
                    已绑定账号：{channel?.account || boundTarget.account}
                    {boundTarget.target !== draft.notify.target &&
                      `，可用接收目标 ${boundTarget.target}`}
                  </span>
                  <span className="spacer" />
                  <button
                    type="button"
                    className="btn-sm"
                    disabled={boundTarget.target === draft.notify.target}
                    onClick={() =>
                      patchNotify({
                        target: boundTarget.target,
                        account: boundTarget.account,
                      })
                    }
                  >
                    填入已绑定账号
                  </button>
                  <button
                    type="button"
                    className="btn-sm"
                    disabled={channelBusy}
                    onClick={() => void logoutAccount()}
                  >
                    解绑账号
                  </button>
                </div>
              )}
              {notifyStatus?.target_warning && (
                <div className="hint" style={{ color: "var(--warn, #b45309)" }}>
                  {notifyStatus.target_warning}
                </div>
              )}
              <div className="field">
                <label className="field-label" htmlFor="notify-account">
                  <span>账号</span>
                  <em>绑定多个微信账号时填写，留空用默认</em>
                </label>
                <input
                  id="notify-account"
                  value={draft.notify.account}
                  onChange={(event) => patchNotify({ account: event.target.value })}
                  placeholder="例：6b0e6d3bca3a-im-bot"
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
              </div>
              {notifyStatus && !notifyStatus.available && (
                <div className="verdict bad">
                  未找到 OpenClaw：可点下方的“自动安装 OpenClaw”，或在上方填写 openclaw 的完整路径。
                </div>
              )}
              <div className="install-box">
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
                    <label className="check">
                      <input
                        type="checkbox"
                        checked={installWithWechat}
                        disabled={install?.running}
                        onChange={(event) => {
                          setInstallWithWechat(event.target.checked);
                          setInstallPlan([]);
                        }}
                      />
                      同时安装微信 ClawBot 插件（缺少它无法推送到微信）
                    </label>
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
                {/* Registration and login are separate from installing: npm only
                    puts files on disk, the gateway keeps its own plugin ledger,
                    and the scan happens between the user and WeChat. */}
                <div className="install-head" style={{ marginTop: 10 }}>
                  <span className="card-title" style={{ fontSize: 12 }}>
                    微信通道
                  </span>
                  <span className="faint" style={{ fontSize: 11 }}>
                    {!channel
                      ? "检测中…"
                      : channel.state === "ready"
                        ? "已绑定，可直接推送"
                        : channel.state === "installed"
                          ? "插件已注册，尚未扫码绑定"
                          : channel.state === "missing"
                            ? channel.cli
                              ? "插件未注册到网关"
                              : "未检测到 openclaw"
                            : "状态未知"}
                  </span>
                  <span className="spacer" />
                  {channel?.configured && (
                    <>
                      <button
                        type="button"
                        className="btn-sm"
                        disabled={channelBusy}
                        onClick={() => void completeBind()}
                      >
                        完成绑定并开启通知
                      </button>
                      <button
                        type="button"
                        className="btn-sm"
                        disabled={channelBusy}
                        onClick={() => void logoutAccount()}
                      >
                        解绑账号
                      </button>
                    </>
                  )}
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
                    npm 装好了，但网关还不认识微信渠道。请先点“注册插件”。
                  </div>
                )}
                {/* The last step is on the phone, and nothing else in the app can
                    stand in for it: WeChat only allows a bot to reply inside a
                    conversation the user opened first. */}
                {channel?.configured && channel.has_context === false && (
                  <div className="verdict bad">
                    还差一步：请在手机微信里打开 ClawBot，随便发一条消息（例如 hi）。
                    微信只允许机器人回复你已开启的会话，会话建立前推送不会送达。
                    发完点“刷新状态”，这里会变成已就绪。
                  </div>
                )}
                <div className="install-head">
                  <button
                    type="button"
                    className="btn-sm"
                    disabled={channelBusy || install?.running || !install?.available}
                    onClick={() => void registerChannel()}
                  >
                    注册微信插件
                  </button>
                  <button
                    type="button"
                    className="btn-sm primary"
                    disabled={channelBusy || channel?.plugin === false}
                    onClick={() => void openLogin()}
                  >
                    扫码授权
                  </button>
                  <button
                    type="button"
                    className="btn-sm"
                    disabled={channelBusy || channel?.plugin === false}
                    onClick={() => void openLoginTerminal()}
                  >
                    在终端中打开
                  </button>
                  <span className="faint" style={{ fontSize: 11 }}>
                    二维码直接显示在本页，用手机微信扫描即可。
                  </span>
                </div>
                {/* The CLI draws its code with half-block characters, which only
                    scan when the terminal font renders them square. cmd.exe uses
                    a raster font, so the code came out distorted; the link it
                    prints is what this panel turns into a real image. */}
                {login && login.state !== "idle" && (
                  <div className="install-head" style={{ alignItems: "flex-start" }}>
                    <div>
                      {login.has_qr && (login.state === "waiting" || login.state === "starting") ? (
                        <>
                          <img
                            // Keyed on the server's revision: the wizard
                            // refreshes an expiring code on its own, and the
                            // page must show the new one instead of the dead
                            // SVG the browser already cached.
                            src={`/api/install/openclaw/login/qr.svg?n=${qrNonce}-${login.revision}`}
                            alt="微信扫码登录二维码"
                            width={220}
                            height={220}
                            style={{ background: "#fff", borderRadius: 8, padding: 8 }}
                          />
                          <div className="faint" style={{ fontSize: 11, marginTop: 6 }}>
                            打开微信「扫一扫」，扫描上方二维码完成绑定。
                          </div>
                          <div className="install-head" style={{ marginTop: 6 }}>
                            <button
                              type="button"
                              className="btn-sm"
                              disabled={channelBusy}
                              onClick={() => void openLogin(true)}
                            >
                              刷新二维码
                            </button>
                            <button
                              type="button"
                              className="btn-sm"
                              disabled={channelBusy}
                              onClick={() => void cancelLogin()}
                            >
                              取消
                            </button>
                          </div>
                          {login.url && (
                            <div className="faint" style={{ fontSize: 11, marginTop: 6 }}>
                              手机无法扫码时，可在微信中打开：
                              <a href={login.url} target="_blank" rel="noreferrer">
                                授权链接
                              </a>
                            </div>
                          )}
                        </>
                      ) : login.state === "done" ? (
                        <div className="verdict ok">扫码授权完成，微信通道已绑定。</div>
                      ) : login.state === "expired" ? (
                        <div className="verdict bad">
                          二维码已过期。请点“扫码授权”重新生成。
                        </div>
                      ) : login.state === "canceled" ? (
                        <div className="hint">已取消本次扫码授权。</div>
                      ) : (
                        <div className="verdict bad">{login.error || "扫码登录失败。"}</div>
                      )}
                      {login.state === "failed" && login.lines.length > 0 && (
                        <pre className="install-cmd">{login.lines.join("\n")}</pre>
                      )}
                    </div>
                  </div>
                )}
                {channelNote && <div className="hint">{channelNote}</div>}
                {/* The gateway is its own process: a channel can be perfectly
                    configured and still deliver nothing while it is down. */}
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
              {notifyStatus && notifyStatus.last_success_at > 0 && (
                <div className="hint">
                  最近一次成功发送：
                  {new Date(notifyStatus.last_success_at * 1000).toLocaleString()}
                  {notifyStatus.pending > 0 ? `，队列中还有 ${notifyStatus.pending} 条` : ""}
                </div>
              )}
              <div className="hint">
                推送经由本机 OpenClaw 网关转给微信。先在微信「设置 → 插件 → ClawBot」中完成扫码绑定，
                再填入通道返回的会话 ID；“发送测试”使用的就是上面填写的内容，无需先保存。
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
                  解除熔断只恢复下单权限，当日已发生的亏损仍会计入统计；
                  额度按解除时刻重新起算。全局急停需在上方风控参数中关闭。
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
