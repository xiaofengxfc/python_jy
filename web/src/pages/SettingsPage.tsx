import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, query } from "../api";
import { TIMEFRAMES } from "../format";
import SymbolFallbackNote from "../components/SymbolFallbackNote";
import type {
  AISettings,
  AIStatus,
  AppSettings,
  CredentialItem,
  CredentialStorage,
  DeployCheck,
  DeployInfo,
  DeployResult,
  NotifyStatus,
  PlatformOption,
  RiskState,
  SymbolInfo,
  UpdateInfo,
  UpdateStatus,
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

/** 更新任务的终态：这几个一出现就不会再变，轮询到此为止。 */
const UPDATE_FINAL_STATES = ["done", "ok", "success", "failed", "error", "cancelled", "stopped"];

export default function SettingsPage({ settings, settingsError = "", onSaved }: Props) {
  const [draft, setDraft] = useState<AppSettings | null>(settings);
  /** 拉列表用：effect 里要读「用户原来选的那一对」，放进依赖会让它反复触发。 */
  const draftRef = useRef<AppSettings | null>(draft);
  draftRef.current = draft;
  /** 原来选的那一对不在当前市场列表里、被换成别的时要说明白。 */
  const [symbolFallback, setSymbolFallback] = useState<{ from: string; to: string } | null>(null);
  // 保存时只提交改过的字段：整份回传会把别处刚改的值盖回旧值。
  const baseline = useRef<AppSettings | null>(settings);
  const [platforms, setPlatforms] = useState<PlatformOption[]>([]);
  const [platform, setPlatform] = useState(settings?.platform ?? "binance");
  const [platformBusy, setPlatformBusy] = useState(false);
  const [credentials, setCredentials] = useState<CredentialItem[]>([]);
  /** 当前用的是系统密钥环还是本机加密文件：服务器上要让人一眼看出这事。 */
  const [credStorage, setCredStorage] = useState<CredentialStorage | null>(null);
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

  /** 域名与 HTTPS：只在 Linux 服务器上有（Caddy + systemd + ACME）。非 Linux 后端一律 501，
   *  那句话本身就是完整说明，原样显示；这张卡跟着置灰，别画一个点了才报错的按钮。 */
  const [deploy, setDeploy] = useState<DeployInfo | null>(null);
  const [deployUnavailable, setDeployUnavailable] = useState(false);
  const [deployError, setDeployError] = useState("");
  const [deployDomain, setDeployDomain] = useState("");
  const [deployCheck, setDeployCheck] = useState<DeployCheck | null>(null);
  const [deployTls, setDeployTls] = useState<DeployResult | null>(null);
  const [deployBusy, setDeployBusy] = useState<"load" | "check" | "tls" | "">("");

  const loadDeploy = useCallback(async () => {
    setDeployBusy("load");
    try {
      const info = await api<DeployInfo>("/api/deploy/info");
      setDeploy(info);
      setDeployUnavailable(false);
      setDeployError("");
      setDeployDomain((current) => current || info.domain || "");
    } catch (caught) {
      setDeploy(null);
      setDeployUnavailable(true);
      setDeployError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setDeployBusy("");
    }
  }, []);

  useEffect(() => {
    void loadDeploy();
  }, [loadDeploy]);

  const checkDeployDomain = async () => {
    const domain = deployDomain.trim();
    if (!domain) {
      setDeployError("先填一个域名，比如 trade.example.com。");
      return;
    }
    setDeployBusy("check");
    setDeployTls(null);
    try {
      setDeployCheck(
        await api<DeployCheck>("/api/deploy/check", {
          method: "POST",
          body: JSON.stringify({ domain }),
        }),
      );
      setDeployError("");
    } catch (caught) {
      setDeployCheck(null);
      setDeployError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setDeployBusy("");
    }
  };

  const issueCertificate = async () => {
    const domain = deployDomain.trim();
    if (!domain) {
      setDeployError("先填一个域名，比如 trade.example.com。");
      return;
    }
    const ok = window.confirm(
      `即将把 Caddyfile 的站点改成 ${domain}，然后 reload caddy。\n\n` +
        "reload 期间会有短暂中断：正在访问的页面会掉一下，几秒后自己好。证书由 Caddy 自己走 ACME 申请并自动续期。确认继续？",
    );
    if (!ok) return;
    setDeployBusy("tls");
    setDeployTls(null);
    // 上一次「检查」的结论和这次签发并排显示会自相矛盾（解析不一致 vs 签成功），先清掉。
    setDeployCheck(null);
    setDeployError("");
    try {
      const result = await api<DeployResult>("/api/deploy/tls", {
        method: "POST",
        body: JSON.stringify({ domain }),
      });
      setDeployTls(result);
      // 成功后当前域名、证书到期时间都变了，重读一遍状态。
      if (result.ok) await loadDeploy();
    } catch (caught) {
      setDeployError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setDeployBusy("");
    }
  };

  /** 软件更新：读本地与 GitHub 的差距、一键更新、逐步进度、回滚。
   *  更新最后一步是重启服务，重启那几十秒里轮询会断，所以读不到状态时先去等 /api/health。 */
  const [update, setUpdate] = useState<UpdateInfo | null>(null);
  const [updateError, setUpdateError] = useState("");
  const [updateBusy, setUpdateBusy] = useState<"" | "info" | "apply" | "rollback">("");
  const [updateJob, setUpdateJob] = useState<UpdateStatus | null>(null);
  const [updateNote, setUpdateNote] = useState("");
  /** 60 秒没等到服务回来：把手动命令显示出来。 */
  const [updateManual, setUpdateManual] = useState(false);
  /** 轮询的代次：换一次任务或重新进页面就作废上一轮的循环。 */
  const updateWatch = useRef(0);

  const loadUpdate = useCallback(async () => {
    setUpdateBusy("info");
    try {
      const info = await api<UpdateInfo>("/api/update/info");
      setUpdate(info);
      setUpdateError("");
    } catch (caught) {
      setUpdate(null);
      setUpdateError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setUpdateBusy("");
    }
  }, []);

  useEffect(() => {
    void loadUpdate();
  }, [loadUpdate]);

  /** 服务重启期间 /api/tickers 那类请求都会断，只问 /api/health：200 就说明回来了。
   *  一分钟还没回来就停在这里，把手动命令交给用户，不自己编原因。 */
  const waitForService = async (token: number): Promise<boolean> => {
    for (let tick = 0; tick < 60; tick += 1) {
      await new Promise((resolve) => window.setTimeout(resolve, 1000));
      if (updateWatch.current !== token) return false;
      try {
        const response = await fetch("/api/health");
        if (response.ok) return true;
      } catch {
        // 还没起来，接着等。
      }
    }
    setUpdateManual(true);
    setUpdateNote("60 秒没等到服务回来，用下面的命令手动重启。");
    setUpdateBusy("");
    return false;
  };

  /** 一秒一步地读进度，直到任务落地；中途读不到就去等 /api/health。 */
  const watchUpdate = useCallback(
    async (label: string, jobId: string) => {
      const token = ++updateWatch.current;
      for (let tick = 0; tick < 900; tick += 1) {
        await new Promise((resolve) => window.setTimeout(resolve, 1000));
        if (updateWatch.current !== token) return;
        let status: UpdateStatus | null = null;
        try {
          status = await api<UpdateStatus>(`/api/update/status?${query({ job_id: jobId })}`);
        } catch {
          // 读不到就是服务正在重启：等它回来再接着读同一个任务。
          setUpdateNote("服务正在重启，等 /api/health 回来…");
          const back = await waitForService(token);
          if (!back) return;
          continue;
        }
        if (status && Array.isArray(status.steps)) setUpdateJob(status);
        const state = String(status?.state ?? "");
        if (!UPDATE_FINAL_STATES.includes(state)) continue;
        if (state === "failed" || state === "error" || state === "cancelled" || state === "stopped") {
          setUpdateNote(`${label}停在失败的那一步，原文贴在下面。`);
        } else {
          const to = status?.to_sha ? String(status.to_sha).slice(0, 7) : "";
          setUpdateNote(to ? `已更新完成，当前提交 ${to}` : "已更新完成。");
        }
        setUpdateBusy("");
        await loadUpdate();
        return;
      }
      setUpdateNote("等了十几分钟还没结束，刷新页面看结果。");
      setUpdateBusy("");
    },
    [loadUpdate],
  );

  /** 立即更新：落后才可点，点了先问一次。 */
  const startUpdate = async () => {
    if (!update) return;
    const behind = update.behind_by ?? 0;
    const ok = window.confirm(
      `即将把本机更新到远端 ${update.remote_short ?? "最新提交"}（落后 ${behind} 个提交）。\n\n` +
        "第一步会检查有没有自动交易 / AI 量化 / 执行台在跑：在跑就直接拒绝，不替你停。" +
        "然后连远端、快进合并、装依赖、重启服务。重启期间页面会断几秒到几十秒；" +
        "任何一步失败就停在那一步，原样输出贴在卡里。确认继续？",
    );
    if (!ok) return;
    setUpdateBusy("apply");
    setUpdateNote("正在更新…");
    setUpdateJob(null);
    setUpdateManual(false);
    try {
      const result = await api<{ job_id: string }>("/api/update/apply", {
        method: "POST",
        body: JSON.stringify({}),
      });
      await watchUpdate("更新", String(result?.job_id ?? ""));
    } catch (caught) {
      setUpdateBusy("");
      setUpdateError(caught instanceof Error ? caught.message : String(caught));
    }
  };

  /** 回滚：回到这次更新之前的那个提交。 */
  const rollbackUpdate = async () => {
    const from = updateJob?.from_sha
      ? String(updateJob.from_sha).slice(0, 7)
      : update?.current_short ?? "更新前的那一个";
    const ok = window.confirm(
      `回滚就是回到更新前的提交 ${from}：更新带上来的那些提交在这个目录里就没有了（本地未提交的改动不动，因为更新要求工作树本来就干净）。\n\n回滚同样会重启服务几秒到几十秒，失败会停在那一步并把原样输出贴出来。确认回滚？`,
    );
    if (!ok) return;
    setUpdateBusy("rollback");
    setUpdateNote("正在回滚…");
    setUpdateJob(null);
    setUpdateManual(false);
    try {
      const result = await api<{ job_id: string }>("/api/update/rollback", {
        method: "POST",
        body: JSON.stringify({ confirm: true }),
      });
      await watchUpdate("回滚", String(result?.job_id ?? ""));
    } catch (caught) {
      setUpdateBusy("");
      setUpdateError(caught instanceof Error ? caught.message : String(caught));
    }
  };
  /** 落后才能更新：领先（本地有没推的提交）和分叉都不许点。 */
  const canUpdate = Boolean(
    update && (update.behind_by ?? 0) > 0 && (update.ahead_by ?? 0) === 0 && !update.error,
  );

  /** 本地新 / 已分叉各自说清（读不到远端时由上面那条 error banner 负责贴原话）。 */
  const updateBlock = useMemo(() => {
    if (!update || update.error) return "";
    const ahead = update.ahead_by ?? 0;
    const behind = update.behind_by ?? 0;
    if (ahead > 0 && behind > 0) {
      return `已分叉，需要手动处理：本地领先 ${ahead} 个、落后 ${behind} 个提交（先把本地提交 push 或 rebase 掉，再回来更新）。`;
    }
    if (ahead > 0 && behind === 0) {
      return `本地比远端新，先 push（领先 ${ahead} 个提交）：这些提交还没上 GitHub，更新没有可拉的东西。`;
    }
    return "";
  }, [update]);
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
      const data = await api<{
        network: string;
        items: CredentialItem[];
        storage: CredentialStorage;
      }>(`/api/credentials?network=${encodeURIComponent(network)}`);
      setCredentials(data.items);
      setCredStorage(data.storage ?? null);
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
        // 落到列表里，否则下拉会显示空白。换掉了要说一句，不然用户不知道自己的选择被改了。
        const current = draftRef.current;
        if (!current) return;
        const from = current.last_symbol;
        if (!items.length || items.some((item) => item.symbol === from)) {
          setSymbolFallback(null);
          return;
        }
        const fallback = items.find((item) => item.symbol === "BTC/USDT") ?? items[0];
        if (!fallback) return;
        setDraft((draftNow) => (draftNow ? { ...draftNow, last_symbol: fallback.symbol } : draftNow));
        setSymbolFallback({ from, to: fallback.symbol });
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
            {credStorage ? (
              <div className={`storage-note${credStorage.kind === "keyring" ? "" : " warn"}`}>
                <span className="storage-label">存放位置：{credStorage.label}</span>
                <span>{credStorage.detail}</span>
              </div>
            ) : null}
            密钥不会写入配置文件。建议在币安只勾选「读取」和「交易」权限，关闭「提现」，并绑定 IP 白名单。
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
              {aiStatus?.storage ? (
                <div
                  className={`storage-note${aiStatus.storage.kind === "keyring" ? "" : " warn"}`}
                  style={{ marginTop: 8 }}
                >
                  <span className="storage-label">存放位置：{aiStatus.storage.label}</span>
                  <span>{aiStatus.storage.detail}</span>
                </div>
              ) : null}
              <span className="field-hint">
                不写进配置文件。
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
                    onChange={(event) => {
                      patch({ last_symbol: event.target.value });
                      setSymbolFallback(null);
                    }}
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
                  <SymbolFallbackNote
                    from={symbolFallback?.from ?? ""}
                    to={symbolFallback?.to ?? ""}
                    market={symbolMarket}
                    layout="form"
                  />
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

        <div className="card">
          <div className="card-head">
            <span className="card-title">域名与 HTTPS</span>
            <span className="spacer" />
            <span className="faint" style={{ fontSize: "var(--fs-12)" }}>
              {deployUnavailable
                ? "只在 Linux 服务器上可用"
                : deploy?.https_enabled && deploy.domain
                  ? `https://${deploy.domain}`
                  : "还没配置"}
            </span>
          </div>
          <div className="card-body stack">
            {/* 501 的那句话是后端给的完整说明，原样显示，前端不翻译也不解释。 */}
            {deployError && <div className="banner warn">{deployError}</div>}

            {deploy && (
              <div className="deploy-rows">
                <div className="deploy-row">
                  <span>系统</span>
                  <b>{deploy.platform}</b>
                </div>
                <div className="deploy-row">
                  <span>Caddy</span>
                  <b>
                    {deploy.caddy_installed ? "已安装" : "未安装"}
                    {deploy.caddy_installed ? (deploy.caddy_active ? " · 运行中" : " · 未运行") : ""}
                  </b>
                </div>
                <div className="deploy-row">
                  <span>当前域名</span>
                  <b>{deploy.domain ?? "还没配置"}</b>
                </div>
                <div className="deploy-row">
                  <span>证书到期</span>
                  <b>{deploy.cert_expires ?? "—"}</b>
                </div>
                <div className="deploy-row">
                  <span>公网 IP</span>
                  <b>
                    {deploy.public_ip ?? "查不到"}
                    {deploy.public_ip_source ? `（来源：${deploy.public_ip_source}）` : ""}
                  </b>
                </div>
              </div>
            )}

            {deploy && !deploy.public_ip && deploy.public_ip_error && (
              <div className="hint">公网 IP 读不到：{deploy.public_ip_error}</div>
            )}
            {deploy && deploy.public_ip && (
              <div className="hint">
                域名 A 记录要指到这个地址（{deploy.public_ip}），ACME 才能从 80 端口验到这台机器。
              </div>
            )}

            <div className="field">
              <label className="field-label" htmlFor="deploy-domain">
                <span>域名</span>
                <em>换域名用同一个按钮</em>
              </label>
              <input
                id="deploy-domain"
                className="input"
                autoComplete="off"
                spellCheck={false}
                placeholder={deploy?.domain ?? "trade.example.com"}
                value={deployDomain}
                disabled={deployUnavailable || deployBusy !== ""}
                onChange={(event) => setDeployDomain(event.target.value)}
              />
            </div>

            <div className="row" style={{ gap: 8 }}>
              <button
                type="button"
                className="btn-sm"
                disabled={deployUnavailable || deployBusy !== ""}
                onClick={() => void checkDeployDomain()}
              >
                {deployBusy === "check" ? "检查中…" : "检查解析"}
              </button>
              <button
                type="button"
                className="btn-sm primary"
                disabled={deployUnavailable || deployBusy !== ""}
                onClick={() => void issueCertificate()}
              >
                {deployBusy === "tls" ? "正在签发…" : "签发证书"}
              </button>
              {deployBusy === "tls" && (
                <span className="faint" style={{ fontSize: "var(--fs-12)" }}>
                  写 Caddyfile → reload caddy → 轮询 HTTPS，最多约 40 秒
                </span>
              )}
            </div>

            {/* 检查结果：message 是后端给的原文，一致就绿、不一致就红。 */}
            {deployCheck && (
              <div className={`banner ${deployCheck.match ? "ok" : "error"}`}>
                {deployCheck.message}
              </div>
            )}

            {deployTls && (
              <div className={`banner ${deployTls.ok ? "ok" : "error"}`}>{deployTls.message}</div>
            )}
            {deployTls?.ok && (
              <div className="deploy-rows">
                <div className="deploy-row">
                  <span>访问地址</span>
                  <b>{deployTls.url}</b>
                </div>
                <div className="deploy-row">
                  <span>证书到期</span>
                  <b>{deployTls.cert_expires ?? "还没读到"}</b>
                </div>
              </div>
            )}

            {/* 失败时把原文贴出来：原因以 Caddy 自己的日志为准，不转述、不截断。 */}
            {deployTls?.log_tail && (
              <>
                <div className="hint">Caddy 日志原文（journalctl -u caddy -n 20）</div>
                <pre className="deploy-log">{deployTls.log_tail}</pre>
              </>
            )}
            {deployTls?.script_output && (
              <>
                <div className="hint">set-domain.sh 原样输出</div>
                <pre className="deploy-log">{deployTls.script_output}</pre>
              </>
            )}

            {!deployUnavailable && (
              <div className="hint">
                证书由 Caddy 自己走 ACME（HTTP-01）申请并自动续期。域名要先解析到这台机器的公网 IP，
                80 端口对外可达；IP 不能直接签证书。
              </div>
            )}
          </div>
        </div>

        {/* 软件更新：读 GitHub 上的最新提交，落后才可点更新。更新会重启服务，
            轮询断了就等 /api/health 回来；重启那一步是唯一会把服务弄挂的地方，
            所以 60 秒没回来就停下把手动命令交出去。 */}
        <div className="card">
          <div className="card-head">
            <span className="card-title">软件更新</span>
            <span className="spacer" />
            <span className="faint" style={{ fontSize: "var(--fs-12)" }}>
              {update?.current_short ? `当前 ${update.current_short}` : "还没读到"}
            </span>
          </div>
          <div className="card-body stack">
            {updateError && <div className="banner warn">{updateError}</div>}

            {update && (
              <div className="deploy-rows">
                <div className="deploy-row">
                  <span>当前提交</span>
                  <b className="num">{update.current_short ?? "—"}</b>
                </div>
                <div className="deploy-row">
                  <span>分支</span>
                  <b>{update.branch ?? "—"}</b>
                </div>
                <div className="deploy-row">
                  <span>远端最新</span>
                  <b className="num">{update.remote_short ?? "读不到"}</b>
                </div>
                <div className="deploy-row">
                  <span>差距</span>
                  <b>
                    落后 {update.behind_by ?? 0} 个 / 领先 {update.ahead_by ?? 0} 个
                  </b>
                </div>
                <div className="deploy-row">
                  <span>上次检查</span>
                  <b>
                    {update.checked_at ?? "—"}
                    {update.source ? `（来源：${update.source}）` : ""}
                  </b>
                </div>
              </div>
            )}

            {/* error 是后端原话：两条路都读不到时它非空，这时候一句「已是最新」都不能说。 */}
            {update?.error && <div className="banner warn">{update.error}</div>}
            {updateBlock && <div className="banner warn">{updateBlock}</div>}

            {update?.commits && update.commits.length > 0 && (
              <>
                <div className="hint">远端最新的提交（最多 10 条）</div>
                <pre className="deploy-log">
                  {update.commits
                    .slice(0, 10)
                    .map((item) => `${item.short}  ${item.title}`)
                    .join("\n")}
                </pre>
              </>
            )}

            <div className="row" style={{ gap: 8 }}>
              <button
                type="button"
                className="btn-sm"
                disabled={updateBusy !== ""}
                onClick={() => void loadUpdate()}
              >
                {updateBusy === "info" ? "检查中…" : "检查更新"}
              </button>
              <button
                type="button"
                className="btn-sm primary"
                disabled={!canUpdate || updateBusy !== ""}
                onClick={() => void startUpdate()}
              >
                {updateBusy === "apply" ? "正在更新…" : "立即更新"}
              </button>
              <button
                type="button"
                className="btn-sm"
                disabled={updateBusy !== ""}
                onClick={() => void rollbackUpdate()}
              >
                {updateBusy === "rollback" ? "正在回滚…" : "回滚"}
              </button>
            </div>

            {!update && !updateError && (
              <div className="hint">正在读本地与 GitHub 的提交…</div>
            )}
            {update && !update.error && !updateBlock && (update.behind_by ?? 0) === 0 && (
              <div className="hint">本地和远端是同一个提交。</div>
            )}

            {updateNote && <div className="banner ok">{updateNote}</div>}

            {/* 每步跑完才写一条；失败的原文原样贴出来，不截断也不转述。 */}
            {updateJob?.steps && updateJob.steps.length > 0 && (
              <>
                <div className="hint">进度（失败的那一步原文贴在下面）</div>
                <div className="update-steps">
                  {updateJob.steps.map((step) => (
                    <div key={step.name} className="update-step">
                      <span className={step.ok ? "num up" : "num down"}>
                        {step.ok ? "✓" : "✗"}
                      </span>
                      <b>{step.name}</b>
                      <span className="faint" style={{ fontSize: "var(--fs-12)" }}>
                        {step.seconds != null ? `${Number(step.seconds).toFixed(1)} 秒` : ""}
                      </span>
                    </div>
                  ))}
                </div>
              </>
            )}
            {updateJob?.steps
              ?.filter((step) => !step.ok)
              .map((step) => (
                <div key={`${step.name}-output`}>
                  <div className="hint">{step.name} 的原始输出</div>
                  <pre className="deploy-log">{step.output}</pre>
                </div>
              ))}
            {updateJob?.error && (
              <>
                <div className="hint">任务失败的原因（后端原话）</div>
                <pre className="deploy-log">{updateJob.error}</pre>
              </>
            )}

            {updateManual && (
              <>
                <div className="hint">手动重启命令（在仓库根目录执行）</div>
                <pre className="deploy-log">.venv\Scripts\python.exe run_web.py --host 127.0.0.1 --port 8765</pre>
              </>
            )}

            <div className="hint">
              更新只用快进合并，不会 --force、也不会 reset --hard；工作树不干净或自动交易在跑时后端直接拒绝。
            </div>
          </div>
        </div>
      </div>
    </>
  );
}
