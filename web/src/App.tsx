import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import Sidebar from "./components/Sidebar";
import TitleBar from "./components/TitleBar";
import LogPanel from "./components/LogPanel";
import TradingPage from "./pages/TradingPage";
import PortfolioPage from "./pages/PortfolioPage";
import SettingsPage from "./pages/SettingsPage";
import BacktestPage from "./pages/BacktestPage";
import StrategyLabPage from "./pages/StrategyLabPage";
import type { AppSettings, Health } from "./types";

export type PageKey = "trading" | "portfolio" | "auto" | "backtest" | "lab" | "settings";

export default function App() {
  const [page, setPage] = useState<PageKey>("trading");
  const [health, setHealth] = useState<Health | null>(null);
  const [healthError, setHealthError] = useState("");
  const [market, setMarket] = useState("spot");
  const [equity, setEquity] = useState<number | null>(null);
  const [settings, setSettings] = useState<AppSettings | null>(null);
  const [logOpen, setLogOpen] = useState(false);
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
    api<{ settings: AppSettings }>("/api/settings")
      .then((data) => setSettings(data.settings))
      .catch(() => undefined);
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

        {page === "trading" && (
          <TradingPage
            health={health}
            healthError={healthError}
            onMarketChange={setMarket}
            onAccountChange={handleAccountChange}
            onNavigate={setPage}
          />
        )}
        {page === "portfolio" && <PortfolioPage onEquity={handleAccountChange} />}
        {page === "backtest" && <BacktestPage />}
        {page === "lab" && <StrategyLabPage />}
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
