import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import Sidebar from "./components/Sidebar";
import TitleBar from "./components/TitleBar";
import LogPanel from "./components/LogPanel";
import TradingPage from "./pages/TradingPage";
import PortfolioPage from "./pages/PortfolioPage";
import SettingsPage from "./pages/SettingsPage";
import AutoPage from "./pages/AutoPage";
import BacktestPage from "./pages/BacktestPage";
import type { AppSettings, Health } from "./types";

export type PageKey = "trading" | "portfolio" | "auto" | "backtest" | "settings";

export default function App() {
  const [page, setPage] = useState<PageKey>("trading");
  const [health, setHealth] = useState<Health | null>(null);
  const [healthError, setHealthError] = useState("");
  const [market, setMarket] = useState("spot");
  const [equity, setEquity] = useState<number | null>(null);
  const [settings, setSettings] = useState<AppSettings | null>(null);
  const [logOpen, setLogOpen] = useState(false);
  const [logErrors, setLogErrors] = useState(0);
  /** WeChat login verdict; "expired" is worth interrupting the user for, since
   * every push is silently dropped until they scan again. */
  const [notifySession, setNotifySession] = useState<string>("");

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
    api<{ settings: AppSettings }>("/api/settings")
      .then((data) => setSettings(data.settings))
      .catch(() => undefined);
  }, []);

  // WeChat's bot token expires on its own and then every push is dropped. Poll
  // it slowly and put a banner up top, so the user finds out from the app
  // rather than from a task notification that never arrived.
  useEffect(() => {
    let cancelled = false;
    const check = async () => {
      try {
        const data = await api<{
          enabled?: boolean;
          session?: { state?: string; detail?: string };
        }>("/api/notify");
        if (cancelled) return;
        setNotifySession(data.enabled === false ? "" : data.session?.state || "");
      } catch {
        if (!cancelled) setNotifySession("");
      }
    };
    void check();
    const timer = window.setInterval(check, 120_000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, []);

  const handleAccountChange = useCallback((value: number) => setEquity(value), []);
  const handleLogCounts = useCallback((errors: number) => setLogErrors(errors), []);

  // Inside the Electron shell the OS frame is gone, so the app draws its own
  // title bar and keeps room for it at the top of the grid.
  const desktop = typeof window !== "undefined" && Boolean(window.bitrader);

  return (
    <div className={desktop ? "app desktop" : "app"}>
      <TitleBar />
      <Sidebar
        page={page}
        onPick={setPage}
        onOpenLog={() => setLogOpen(true)}
        connected={health !== null && healthError === ""}
        network={health?.network ?? "testnet"}
        market={market}
        equity={equity}
        errorCount={logErrors}
      />

      <main className="main">
        {healthError && page !== "trading" && <div className="banner error">{healthError}</div>}
        {notifySession === "expired" && (
          <div className="banner error">
            微信登录已过期（session timeout），任务通知现在发不出去。
            <button type="button" className="link-btn" onClick={() => setPage("settings")}>
              去重新扫码
            </button>
          </div>
        )}

        {page === "trading" && (
          <TradingPage
            health={health}
            healthError={healthError}
            onMarketChange={setMarket}
            onAccountChange={handleAccountChange}
          />
        )}
        {page === "portfolio" && <PortfolioPage onEquity={handleAccountChange} />}
        {page === "auto" && <AutoPage />}
        {page === "backtest" && <BacktestPage />}
        {page === "settings" && (
          <SettingsPage settings={settings} onSaved={setSettings} />
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
