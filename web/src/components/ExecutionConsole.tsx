import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, query } from "../api";
import { TIMEFRAMES, fmt } from "../format";
import { createSectionSaver, resetSection } from "../settingsAutoSave";
import type { AppSettings, AutoStatus, ExecSettings, Strategy } from "../types";

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
  /** 设置的 exec 段：仓位比例 / 杠杆 / 轮询。默认 0.2 / 1 / 10：
 *  名义额 = 权益 × 仓位比例 × 杠杆，而上限是「500 USDT 与权益 25% 取大的」，
 *  仓位比例填 100% 时名义额是上限的 4 倍，引擎每一轮都会被风控拒。 */
  settings?: AppSettings | null;
  onSettingsChange?: (patch: Partial<AppSettings>) => void;
}

/** 后端 ExecSettings 的默认值（`core/config.py` 的 1 / 1 / 10），恢复默认时兜底用。 */
const DEFAULT_EXEC: ExecSettings = { position_pct: 0.2, leverage: 1, poll_seconds: 10 };

function readExec(settings: AppSettings | null | undefined): ExecSettings {
  const exec = settings?.exec;
  if (!exec) return { ...DEFAULT_EXEC };
  return {
    position_pct: Number.isFinite(exec.position_pct) ? exec.position_pct : DEFAULT_EXEC.position_pct,
    leverage: Number.isFinite(exec.leverage) ? exec.leverage : DEFAULT_EXEC.leverage,
    poll_seconds: Number.isFinite(exec.poll_seconds) ? exec.poll_seconds : DEFAULT_EXEC.poll_seconds,
  };
}

/** epoch 秒 → 本机时间。下一根 K 线收盘的时刻就用它显示。 */
function clockOf(epoch: number | null | undefined): string {
  if (!epoch) return "";
  return new Date(epoch * 1000).toLocaleTimeString("zh-CN", { hour12: false });
}

const STATE_LABEL: Record<string, string> = {
  signal: "信号",
  order: "下单",
  state: "状态",
  error: "错误",
  warn: "提醒",
};

export default function ExecutionConsole({
  strategies,
  market,
  symbol,
  timeframe,
  settings,
  onSettingsChange,
}: Props) {
  const [status, setStatus] = useState<AutoStatus | null>(null);
  const [strategyKey, setStrategyKey] = useState("");
  /** 三项都跟设置走，改一下 500ms 后落盘——刷新之后还是上次的值。 */
  const [exec, setExec] = useState<ExecSettings>(() => readExec(settings));
  const loadedExec = useRef(false);
  const saver = useMemo(
    () =>
      createSectionSaver("exec", {
        // 写盘失败要说一声：以前静默吞掉，用户只看到「改了又回去」。
        onError: (error) =>
          setError("设置没存上：" + (error instanceof Error ? error.message : String(error))),
        // 存成功后回填给 App：AI 量化的部署请求读的就是 settings.exec.leverage，
        // 不回填的话「在执行台改了杠杆再部署」用的还是旧值。
        onSaved: (saved) => {
          setError((current) => (current.startsWith("设置没存上：") ? "" : current));
          if (saved?.exec) onSettingsChange?.({ exec: saved.exec });
        },
      }),
    [],
  );
  const { position_pct: positionPct, leverage, poll_seconds: pollSeconds } = exec;

  /** 风控上限：真正的收口在 risk.max_leverage。输入框放到 125，也只按上限下单。 */
  const leverageCap = (() => {
    const cap = Number(settings?.risk?.max_leverage ?? 0);
    return Number.isFinite(cap) && cap >= 1 ? Math.min(cap, 125) : 125;
  })();

  useEffect(() => {
    // 只认第一份到手的设置：之后的更新是「自己刚存的」，拿它回填会把正在敲的
    // 数字顶掉（输入框被 500ms 前的值覆盖）。
    if (loadedExec.current) return;
    if (!settings?.exec) return;
    loadedExec.current = true;
    setExec(readExec(settings));
  }, [settings]);

  useEffect(() => () => saver.flush(), [saver]);

  const patchExec = (values: Partial<ExecSettings>) => {
    setExec((current) => ({ ...current, ...values }));
    saver.queue(values as Record<string, unknown>);
  };

  /** 恢复默认：只重置 exec 这一段，市场、交易对、主题都不动。 */
  const resetExec = async () => {
    const ok = window.confirm(
      "恢复执行台的默认值？（仓位比例 20%、杠杆 1、检查间隔 10 秒）\n\n" +
        "只重置这三项，市场、交易对、主题和策略都不动。",
    );
    if (!ok) return;
    saver.cancel();
    setError("");
    setNotice("");
    try {
      const next = await resetSection("exec");
      setExec(readExec(next));
      loadedExec.current = true;
      onSettingsChange?.({ exec: next.exec });
      setNotice("执行台默认值已恢复。");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  };
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
    // 主网启动是真钱下单，后端要求 confirm_live=true（不带就 400「主网自动
    // 交易需要显式确认」，以前这里从来没带过，主网执行台根本启动不了）。
    const live = settings?.network === "mainnet";
    if (
      live &&
      !window.confirm(
        `即将在【主网】用 ${symbol} 跑 ${strategyKey}：${positionPct * 100}% 仓位、${leverage}x、` +
          `按 ${timeframe} K 线调仓（每根收盘后一次）。\n\n` +
          "引擎会按信号自动下单，用的是真钱。确认启动？",
      )
    ) {
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
          confirm_live: live,
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
  /** 下一根 K 线收盘的时刻：节奏跟着周期走，界面要说清「不是每 10 秒下一次单」。 */
  const nextClose = running ? (status?.next_run_at ?? null) : null;
  const events = [...(status?.events ?? [])].reverse().slice(0, 60);

  return (
    <section className="card">
      <div className="card-head">
        <span className="card-title">执行台</span>
        <span className="spacer" />
        <button
          type="button"
          className="btn-sm"
          disabled={running || busy !== ""}
          title="只重置这三项，不动市场与主题"
          onClick={() => void resetExec()}
        >
          恢复默认
        </button>
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
              onChange={(event) => {
                const next = Number(event.target.value);
                if (!Number.isFinite(next) || next < 1) return;
                patchExec({ position_pct: next / 100 });
              }}
            />
          </div>
          <div className="field">
            <label className="field-label" htmlFor="exec-lev">
              <span>杠杆</span>
              <em>{"合约才用，上限 " + leverageCap + "x"}</em>
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
              onChange={(event) => {
                const next = Number(event.target.value);
                // 交易所只认 1–125 的整数（1.5 会被币安回 -1102）。
                if (!Number.isFinite(next) || next < 1 || !Number.isInteger(next)) return;
                patchExec({ leverage: Math.min(next, 125) });
              }}
            />
          </div>
          <div className="field">
            <label className="field-label" htmlFor="exec-poll">
              <span>检查间隔</span>
              <em>秒 · 只决定多快发现新 K 线</em>
            </label>
            <input
              id="exec-poll"
              className="input"
              type="number"
              min={1}
              step={1}
              disabled={running}
              value={pollSeconds}
              onChange={(event) => {
                const next = Number(event.target.value);
                if (!Number.isFinite(next) || next < 1) return;
                patchExec({ poll_seconds: next });
              }}
            />
          </div>
        </div>

        {/* 杠杆在这里等于放大名义仓位：仓位比例 × 杠杆 = 占权益多少，
            超过单笔上限（固定额与权益比例取大的那个）这一轮就不下单。
            不写这句，把杠杆调到 10 倍后引擎每一轮都被风控拒，看着像杠杆没生效。 */}
        <p className="faint">
          {"仓位比例 × 杠杆 = 名义仓位：现在是 " +
            Math.round(exec.position_pct * 100) +
            "% × " +
            exec.leverage +
            "x，占权益 " +
            Math.round(exec.position_pct * exec.leverage * 100) +
            "%；超过单笔上限（" +
            (settings?.risk?.max_order_quote ?? 0) +
            " USDT 与权益 " +
            Math.round(Number(settings?.risk?.max_order_pct ?? 0) * 100) +
            "% 取大的那个）这一轮不会下单。"}
        </p>

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
            <span>调仓节奏</span>
            <b>
              {`每根 ${TIMEFRAMES.find(([value]) => value === timeframe)?.[1] ?? timeframe} K 线收盘后调仓` +
                (nextClose ? ` · 下次 ${clockOf(nextClose)}` : "")}
            </b>
          </div>
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
