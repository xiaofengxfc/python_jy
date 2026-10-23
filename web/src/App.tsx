import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import Sidebar from "./components/Sidebar";
import TitleBar from "./components/TitleBar";
import LogPanel from "./components/LogPanel";
import TradingPage from "./pages/TradingPage";
import PortfolioPage from "./pages/PortfolioPage";
import SettingsPage from "./pages/SettingsPage";
import BacktestPage from "./pages/BacktestPage";
import StrategiesPage from "./pages/StrategiesPage";
import StrategyLabPage from "./pages/StrategyLabPage";
import AITradePage from "./pages/AITradePage";
import AIQuantPage from "./pages/AIQuantPage";
import type { AppSettings, Health } from "./types";

/** 每个页面各自独立：AI 交易和 AI 量化都是一整页，不再挤在交易页底下。 */
export type PageKey =
  | "trading"
  | "ai-trade"
  | "ai-quant"
  | "portfolio"
  | "backtest"
  | "strategy"
  | "lab"
  | "settings";

const PAGE_KEYS: PageKey[] = [
  "trading",
  "ai-trade",
  "ai-quant",
  "portfolio",
  "backtest",
  "strategy",
  "lab",
  "settings",
];

/** 当前视图存在 URL 的 hash 里（#/strategy、#/settings…）。
 *  以前只有内存里的 state，按一下 F5 就弹回交易页，后退也回不去。 */
function pageFromHash(): PageKey {
  if (typeof window === "undefined") return "trading";
  const raw = (window.location.hash.replace(/^#\/?/, "").split(/[?#/]/)[0] ?? "").trim();
  return PAGE_KEYS.find((key) => key === raw) ?? "trading";
}

export default function App() {
  const [page, setPage] = useState<PageKey>(pageFromHash);
  const [health, setHealth] = useState<Health | null>(null);
  const [healthError, setHealthError] = useState("");
  const [market, setMarket] = useState("spot");
  const [equity, setEquity] = useState<number | null>(null);
  const [settings, setSettings] = useState<AppSettings | null>(null);
  /** 读设置失败的原因。后端正在重启时读不到，界面不该一直停在「正在读取设置…」。 */
  const [settingsError, setSettingsError] = useState("");
  const [logOpen, setLogOpen] = useState(false);
  /** 跳到策略页时要预选的 key（回测页「编辑源码」、实验室「在策略页编辑」）。 */
  const [strategyFocus, setStrategyFocus] = useState<string | null>(null);
  const [logErrors, setLogErrors] = useState(0);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const data = await api<Health>("/api/health");
        if (cancelled) return;
        setHealth(data);
        setHealthError("");
        setMarket(data.market);
      } catch (error) {
        if (cancelled) return;
        setHealthError(
          `无法连接本地后端：${error instanceof Error ? error.message : String(error)}`,
        );
      }
    };
    void load();
    const timer = window.setInterval(load, 10000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, []);

  useEffect(() => {
    const theme = settings?.theme === "dark" ? "dark" : "light";
    document.documentElement.dataset.theme = theme;
  }, [settings?.theme]);

  useEffect(() => {
    let cancelled = false;
    let timer = 0;
    // 后端还没起来、或正在重启时读不到设置：隔 5 秒自己再试一次，
    // 用户不用刷新页面（设置页那张「正在读取设置…」就是卡在这里）。
    const load = async () => {
      try {
        const data = await api<{ settings: AppSettings }>("/api/settings");
        if (cancelled) return;
        setSettings(data.settings);
        setSettingsError("");
      } catch (error) {
        if (cancelled) return;
        setSettingsError(error instanceof Error ? error.message : String(error));
        timer = window.setTimeout(() => void load(), 5000);
      }
    };
    void load();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, []);

  /** 换页：state 和 hash 一起改。改 hash 会进浏览历史，后退/前进才跟得上。 */
  const go = useCallback((next: PageKey) => {
    setPage(next);
    const want = `#/${next}`;
    if (window.location.hash !== want) window.location.hash = want;
  }, []);

  // 刷新、后退、前进都走 hash：hash 变了就跟着换页；认不出来的 hash 回交易页。
  useEffect(() => {
    // 首屏把 hash 归一化（用 replace，别在历史里多塞一条）。
    const clean = pageFromHash();
    if (clean !== page || !window.location.hash) {
      window.history.replaceState(null, "", `#/${clean}`);
    }
    const sync = () => setPage(pageFromHash());
    window.addEventListener("hashchange", sync);
    window.addEventListener("popstate", sync);
    return () => {
      window.removeEventListener("hashchange", sync);
      window.removeEventListener("popstate", sync);
    };
    // 只在挂载时注册一次；page 只用来判断首屏要不要归一化。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleAccountChange = useCallback((value: number) => setEquity(value), []);
  const handleSettingsChange = useCallback(
    (patch: Partial<AppSettings>) => setSettings((current) => (current ? { ...current, ...patch } : current)),
    [],
  );
  const handleLogCounts = useCallback((errors: number) => setLogErrors(errors), []);

  // Inside the Electron shell the OS frame is gone, so the app draws its own
  // title bar and keeps room for it at the top of the grid.
  const desktop = typeof window !== "undefined" && Boolean(window.bitrader);

  return (
    <div className={desktop ? "app desktop" : "app"}>
      <TitleBar />
      <Sidebar
        page={page}
        onPick={go}
        onOpenLog={() => setLogOpen(true)}
        connected={health !== null && healthError === ""}
        network={health?.network ?? "testnet"}
        market={market}
        equity={equity}
        errorCount={logErrors}
      />

      <main className="main">
        {healthError && page !== "trading" && <div className="banner error">{healthError}</div>}

        {page === "trading" && (
          <TradingPage
            health={health}
            healthError={healthError}
            settings={settings}
            onMarketChange={setMarket}
            onAccountChange={handleAccountChange}
            onSettingsChange={handleSettingsChange}
          />
        )}
        {page === "ai-trade" && <AITradePage settings={settings} health={health} />}
        {page === "ai-quant" && (
          <AIQuantPage
            settings={settings}
            health={health}
            onNavigate={(next) => go(next)}
          />
        )}
        {page === "portfolio" && <PortfolioPage
            onEquity={handleAccountChange}
            defaultMarket={settings?.market_type ?? ""}
          />}
        {page === "backtest" && (
          <BacktestPage
            onEditStrategy={(key) => {
              setStrategyFocus(key);
              go("strategy");
            }}
          />
        )}
        {page === "strategy" && <StrategiesPage focus={strategyFocus} />}
        {page === "lab" && (
          <StrategyLabPage
            settings={settings}
            health={health}
            onNavigate={(next, key) => {
              if (key) setStrategyFocus(key);
              go(next);
            }}
          />
        )}
        {page === "settings" && (
          <SettingsPage
            settings={settings}
            settingsError={settingsError}
            onSaved={setSettings}
          />
        )}
      </main>

      <LogPanel
        open={logOpen}
        onClose={() => setLogOpen(false)}
        onCounts={handleLogCounts}
      />
    </div>
  );
}
