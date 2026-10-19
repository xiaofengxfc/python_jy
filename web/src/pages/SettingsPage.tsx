import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { TIMEFRAMES } from "../format";
import type { AppSettings, CredentialItem, RiskState } from "../types";

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

interface NewsTokenState {
  configured: boolean;
  masked: string;
  source: string;
}

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
  const [newsToken, setNewsToken] = useState<NewsTokenState | null>(null);
  const [newsDraft, setNewsDraft] = useState("");
  const [newsBusy, setNewsBusy] = useState(false);

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

  const loadNewsToken = useCallback(async () => {
    try {
      setNewsToken(await api<NewsTokenState>("/api/news/token"));
    } catch {
      // The card simply stays unknown; saving still works.
    }
  }, []);

  useEffect(() => {
    void loadNewsToken();
  }, [loadNewsToken]);

  const saveNewsToken = async () => {
    if (!newsDraft.trim()) return;
    setNewsBusy(true);
    setFlash(null);
    try {
      await api("/api/news/token", {
        method: "POST",
        body: JSON.stringify({ token: newsDraft.trim() }),
      });
      setNewsDraft("");
      setFlash({ tone: "ok", text: "CryptoPanic Token 已保存。" });
      await loadNewsToken();
    } catch (error) {
      setFlash({ tone: "err", text: error instanceof Error ? error.message : String(error) });
    } finally {
      setNewsBusy(false);
    }
  };

  const deleteNewsToken = async () => {
    setNewsBusy(true);
    setFlash(null);
    try {
      await api("/api/news/token", { method: "DELETE" });
      setFlash({ tone: "ok", text: "CryptoPanic Token 已删除。" });
      await loadNewsToken();
    } catch (error) {
      setFlash({ tone: "err", text: error instanceof Error ? error.message : String(error) });
    } finally {
      setNewsBusy(false);
    }
  };

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
              {newsToken
                ? newsToken.configured
                  ? `CryptoPanic 已配置 · ${newsToken.masked}`
                  : "CryptoPanic 未配置"
                : "读取中…"}
            </span>
          </div>
          <div className="card-body">
            <div className="news-token-row">
              <input
                className="input"
                type="password"
                autoComplete="off"
                placeholder="CryptoPanic API Token"
                value={newsDraft}
                onChange={(event) => setNewsDraft(event.target.value)}
              />
              <button
                type="button"
                className="btn-sm primary"
                disabled={newsBusy || !newsDraft.trim()}
                onClick={() => void saveNewsToken()}
              >
                {newsBusy ? "处理中…" : "保存"}
              </button>
              {newsToken?.configured && (
                <button
                  type="button"
                  className="btn-sm"
                  disabled={newsBusy}
                  onClick={() => void deleteNewsToken()}
                >
                  删除
                </button>
              )}
            </div>
            <div className="hint" style={{ marginTop: 8 }}>
              在 cryptopanic.com/developers/api/keys 免费申请 Token。Token 与交易所密钥一样
              保存在系统凭据库，不会写入配置文件；未配置时交易页的「新闻」标签会给出提示。
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
                    <option value="30m">30分</option>
                    <option value="1w">1周</option>
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
