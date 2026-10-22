import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, query } from "../api";
import { MARKET_LABELS, TIMEFRAMES, TIMEFRAME_MS, fmtCompact, fmtPrice } from "../format";
import { foldTick } from "../liveCandle";
import { useMarketStream } from "../hooks/useMarketStream";
import type { Account, AILevel, AppSettings, Candle, Health, SymbolInfo } from "../types";
import AIPanel from "../components/AIPanel";
import AITradingPanel from "../components/AITradingPanel";
import BottomTabs, { type BottomTab } from "../components/BottomTabs";
import AutoPage from "./AutoPage";
import TradingViewChart from "../chart/TradingViewChart";
import {
  INDICATOR_SPECS,
  clampParam,
  defaultParams,
  type Indicator,
  type IndicatorConfig,
  type IndicatorParams,
} from "../chart/specs";
import DrawingToolbar from "../components/DrawingToolbar";
import type { Drawing, DrawingTool } from "../components/drawings";
import { levelsToDrawings, loadDrawings, saveDrawings } from "../components/drawingsStore";
import OrderBook from "../components/OrderBook";
import OrderTicket from "../components/OrderTicket";
import SymbolPicker from "../components/SymbolPicker";

interface Props {
  health: Health | null;
  healthError: string;
  settings: AppSettings | null;
  onMarketChange: (market: string) => void;
  onAccountChange: (equity: number) => void;
  onNavigate?: (page: "lab" | "backtest") => void;
}

const FEE_RATE = 0.0004;

const INDICATORS: { group: string; items: [Indicator, string][] }[] = [
  {
    group: "主图",
    items: [
      ["ma", "MA"],
      ["ema", "EMA"],
      ["boll", "BOLL"],
    ],
  },
  {
    group: "副图",
    items: [
      ["macd", "MACD"],
      ["rsi", "RSI"],
      ["kdj", "KDJ"],
      ["wr", "WR"],
      ["cci", "CCI"],
      ["obv", "OBV"],
      ["atr", "ATR"],
      ["dmi", "DMI"],
    ],
  },
];

/** Which indicators are switched on lives in the browser: it is a view
 * preference, not account state, and it must survive a reload without asking
 * the backend. */
const INDICATOR_STORAGE_KEY = "btrader.indicators.v1";

/** Clamp a stored parameter set into the spec, filling missing keys from the
 * defaults. A config written by an older build must not break the chart. */
function normaliseParams(name: Indicator, raw: unknown): IndicatorParams {
  const base = defaultParams(name);
  if (!raw || typeof raw !== "object") return base;
  const values = raw as Record<string, unknown>;
  INDICATOR_SPECS[name].params.forEach((param) => {
    const stored = values[param.key];
    if (typeof stored !== "number" || !Number.isFinite(stored)) return;
    base[param.key] = Math.min(param.max, Math.max(param.min, stored));
  });
  return base;
}

function loadIndicators(): IndicatorConfig[] {
  try {
    const raw = localStorage.getItem(INDICATOR_STORAGE_KEY);
    if (raw == null) return [{ name: "ma", params: defaultParams("ma") }];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    const seen = new Set<Indicator>();
    const out: IndicatorConfig[] = [];
    parsed.forEach((item) => {
      const name = (item as { name?: unknown } | null)?.name;
      if (typeof name !== "string" || !(name in INDICATOR_SPECS)) return;
      const indicator = name as Indicator;
      if (seen.has(indicator)) return;
      seen.add(indicator);
      out.push({
        name: indicator,
        params: normaliseParams(indicator, (item as { params?: unknown }).params),
      });
    });
    return out;
  } catch {
    return [{ name: "ma", params: defaultParams("ma") }];
  }
}

export default function TradingPage({
  health,
  healthError,
  settings,
  onMarketChange,
  onAccountChange,
  onNavigate,
}: Props) {
  const [market, setMarket] = useState("spot");
  const [symbol, setSymbol] = useState("");
  const [symbols, setSymbols] = useState<SymbolInfo[]>([]);
  const [timeframe, setTimeframe] = useState(settings?.timeframe || "15m");
  /** Every indicator switched on, with its parameters. Several may be stacked
   * at once; MA is the default because trend context is what most traders look
   * at first. Loaded from localStorage so a reload keeps the setup. */
  const [indicators, setIndicators] = useState<IndicatorConfig[]>(loadIndicators);
  /** Indicator whose parameter editor is open, if any. */
  const [editing, setEditing] = useState<Indicator | null>(null);
  const [candles, setCandles] = useState<Candle[]>([]);
  /** Timeframe the series in `candles` was loaded for. While a switch is in
   * flight the old series is still on screen; folding it with the new bar
   * duration would merge bars of different lengths into one. */
  const [candlesFor, setCandlesFor] = useState("");
  /** Non-empty when the candles came from the fallback feed, not Binance. */
  const [chartSource, setChartSource] = useState("");
  const [chartError, setChartError] = useState("");
  const [chartLoading, setChartLoading] = useState(true);
  const [symbolError, setSymbolError] = useState("");
  const [account, setAccount] = useState<Account | null>(null);
  const [liveError, setLiveError] = useState("");
  const [pickedPrice, setPickedPrice] = useState<number | null>(null);
  const [restTicker, setRestTicker] = useState<{ high: number; low: number } | null>(null);
  const [actionError, setActionError] = useState("");
  /** Price levels the AI named for the pair on screen, and whether the chart
   * should draw them. Hiding rather than clearing keeps a second click cheap:
   * the analysis is already paid for. */
  const [aiLevelsOn, setAiLevelsOn] = useState(true);
  /** Lines the trader drew by hand plus AI levels converted to editable lines. */
  const [drawings, setDrawings] = useState<Drawing[]>([]);
  const [drawingTool, setDrawingTool] = useState<DrawingTool>("select");
  const [selectedDrawing, setSelectedDrawing] = useState<string | null>(null);
  const [drawingsVisible, setDrawingsVisible] = useState(true);
  const [bottomTab, setBottomTab] = useState<BottomTab>("open");

  const { ticker, book, connected, tradeVolume, tradeWhen, tradePrice, tradeSeq } =
    useMarketStream(market, symbol);
  const requestId = useRef(0);

  /** The AI panel hands its levels up here; the chart reads them from state, so
   * a redraw (zoom, resize, live tick) never needs to re-ask the model. Levels
   * arrive as editable drawings so the trader can move a support line the model
   * placed too optimistically instead of retyping it. */
  const applyAiLevels = useCallback((levels: AILevel[], owner: string) => {
    setAiLevelsOn(true);
    setDrawings((current) => {
      const manual = current.filter((item) => item.source !== "ai");
      return owner ? [...manual, ...levelsToDrawings(levels)] : manual;
    });
  }, []);

  const marketItems = health?.markets ?? [
    { value: "spot" as const, label: MARKET_LABELS.spot },
    { value: "usdm" as const, label: MARKET_LABELS.usdm },
    { value: "coinm" as const, label: MARKET_LABELS.coinm },
  ];

  // Keep the indicator setup in the browser so a reload restores it. Writing
  // on every change is cheap here: a handful of numbers in one key.
  useEffect(() => {
    try {
      localStorage.setItem(INDICATOR_STORAGE_KEY, JSON.stringify(indicators));
    } catch {
      // A full or disabled storage must not break trading.
    }
  }, [indicators]);

  /* -------------------------------------------------------- drawings */
  // One key per market+symbol+timeframe: a support drawn on BTC 15m is not a
  // level on ETH 1h. The skip flag avoids writing the previous pair's lines
  // into the new pair's key before the load effect has run.
  const skipDrawingSave = useRef(true);
  useEffect(() => {
    skipDrawingSave.current = true;
    if (!symbol) {
      setDrawings([]);
      setSelectedDrawing(null);
      return;
    }
    setDrawings(loadDrawings(market, symbol, timeframe));
    setSelectedDrawing(null);
  }, [market, symbol, timeframe]);
  useEffect(() => {
    if (!symbol) return;
    if (skipDrawingSave.current) {
      skipDrawingSave.current = false;
      return;
    }
    // Dragging a line fires on every pointer move; writing localStorage that
    // often is wasted work (and the write is synchronous). A short debounce
    // still survives a reload right after the drag.
    const timer = window.setTimeout(() => {
      saveDrawings(market, symbol, timeframe, drawings);
    }, 250);
    return () => window.clearTimeout(timer);
  }, [drawings, market, symbol, timeframe]);

  /* ---------------------------------------------------------- symbols */
  const defaultsApplied = useRef(false);
  useEffect(() => {
    if (!settings || defaultsApplied.current) return;
    defaultsApplied.current = true;
    if (settings.timeframe) setTimeframe(settings.timeframe);
  }, [settings]);

  useEffect(() => {
    let cancelled = false;
    setSymbolError("");
    api<{ items: SymbolInfo[]; degraded: boolean; error: string | null }>(
      `/api/symbols?${query({ market })}`,
    )
      .then((data) => {
        if (cancelled) return;
        setSymbols(data.items);
        if (data.degraded) setSymbolError(data.error ?? "交易对列表来自离线备用数据");
        // Prefer the configured default when it is actually listed, then
        // fall back to USDT pairs (what most people start with).
        const preferred = settings?.last_symbol || symbol;
        const known = data.items.some((item) => item.symbol === preferred);
        if (!known) {
          const fallback =
            data.items.find((item) => item.symbol === "BTC/USDT") ??
            data.items.find((item) => item.quote === "USDT") ??
            data.items[0];
          setSymbol(fallback?.symbol ?? "");
        } else if (preferred !== symbol) {
          setSymbol(preferred);
        }
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        setSymbolError(error instanceof Error ? error.message : String(error));
      });
    return () => {
      cancelled = true;
    };
    // `symbol` decides only whether the current choice still exists. Settings
    // are read when they arrive so a saved default can win over the hardcoded
    // BTC fallback.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [market, settings?.last_symbol]);

  /* ------------------------------------------- 图上标注（成交/持仓） */
  /** 当前交易对的价格小数位：交易所给几位就显示几位，和下单页一致。 */
  const pricePrecision = useMemo(
    () => symbols.find((item) => item.symbol === symbol)?.price_precision ?? 2,
    [symbols, symbol],
  );

  /** 成交买卖点：账本里属于当前交易对的成交，标到对应的 K 线上。
   * 只取最近 60 笔，免得几百个箭头糊满整张图。 */
  const chartMarkers = useMemo(() => {
    const trades = account?.trades ?? [];
    return trades
      .filter((trade) => trade.symbol === symbol && trade.timestamp > 0)
      .slice(-60)
      .map((trade) => ({
        time: trade.timestamp,
        side: trade.side === "buy" ? ("buy" as const) : ("sell" as const),
        label: (trade.side === "buy" ? "买 " : "卖 ") + fmtCompact(trade.amount),
      }));
  }, [account?.trades, symbol]);

  /** 当前交易对的持仓均价，画一条虚线标在图上。 */
  const entryPrice = useMemo(() => {
    const held = (account?.positions ?? []).find(
      (item) => item.symbol === symbol && Math.abs(item.contracts) > 0,
    );
    return held?.entry_price ?? null;
  }, [account?.positions, symbol]);

  /* ------------------------------------------------------------ klines */
  const loadKlines = useCallback(async () => {
    if (!symbol) return;
    const id = ++requestId.current;
    setChartLoading(true);
    try {
      const data = await api<{ candles: Candle[]; source?: string; degraded?: boolean }>(
        `/api/klines?${query({ market, symbol, timeframe, limit: settings?.kline_limit ?? 400 })}`,
      );
      if (id !== requestId.current) return;
      setCandles(data.candles ?? []);
      setCandlesFor(timeframe);
      // Binance's futures domains are unreachable on some networks, in which
      // case the server serves Gate.io perpetual candles. The chart must say so:
      // these are not Binance's prices.
      setChartSource(data.degraded ? data.source || "备用行情源" : "");
      setChartError("");
    } catch (error) {
      if (id !== requestId.current) return;
      setCandles([]);
      setCandlesFor(timeframe);
      setChartError(error instanceof Error ? error.message : String(error));
    } finally {
      if (id === requestId.current) setChartLoading(false);
    }
  }, [market, symbol, timeframe, settings?.kline_limit]);

  useEffect(() => {
    void loadKlines();
  }, [loadKlines]);

  /* ----------------------------------------------------- older history */
  // Dragging past the oldest bar asks for the window before it. The chart
  // prepends the result and shifts its scroll offset by the number of bars
  // added, so the view stays anchored where the user left it.
  const [olderLoading, setOlderLoading] = useState(false);
  const loadOlder = useCallback(async () => {
    if (!symbol || olderLoading) return;
    const first = candles[0]?.t;
    if (!first) return;
    setOlderLoading(true);
    try {
      const data = await api<{ candles: Candle[] }>(
        `/api/klines?${query({ market, symbol, timeframe, limit: settings?.kline_limit ?? 400, before: first })}`,
      );
      const older = data.candles ?? [];
      if (!older.length) return;
      setCandles((previous) => {
        const head = previous[0]?.t ?? 0;
        const fresh = older.filter((bar) => bar.t < head);
        return fresh.length ? [...fresh, ...previous] : previous;
      });
    } catch {
      /* the chart simply keeps what it has */
    } finally {
      setOlderLoading(false);
    }
  }, [market, symbol, timeframe, candles, olderLoading, settings?.kline_limit]);

  /* ------------------------------------------------- live last candle */
  // The socket is the only thing that moves between REST snapshots, so fold
  // each quote into the tail bar. A skipped interval means the socket slept
  // through bars we never saw: re-fetch instead of leaving a hole.
  const liveReload = useRef(0);
  const barMs = TIMEFRAME_MS[timeframe] ?? 0;
  const tickPrice = ticker?.last ?? 0;
  const tickWhen = ticker?.timestamp || 0;
  useEffect(() => {
    if (!(tickPrice > 0) || !(barMs > 0)) return;
    // Ignore frames while a timeframe switch is still loading: the series on
    // screen still belongs to the old timeframe.
    if (candlesFor !== timeframe) return;
    let gap = false;
    setCandles((previous) => {
      if (!previous.length) return previous;
      const result = foldTick(previous, barMs, tickPrice, tickWhen || Date.now());
      gap = result.gap;
      return result.candles;
    });
    if (gap) {
      const now = Date.now();
      if (now - liveReload.current > 5000) {
        liveReload.current = now;
        void loadKlines();
      }
    }
  }, [tickPrice, tickWhen, barMs, loadKlines, candlesFor, timeframe]);

  // Public trades carry the traded size a ticker lacks, so they are the only
  // source for the tail bar's volume. The frame sequence guards against React
  // replaying an effect and adding the same batch twice.
  const tradeSeen = useRef(0);
  useEffect(() => {
    if (!tradeSeq || tradeSeq === tradeSeen.current) return;
    tradeSeen.current = tradeSeq;
    const price = tradePrice || tickPrice;
    if (!(price > 0) || !(barMs > 0) || !(tradeVolume > 0)) return;
    if (candlesFor !== timeframe) return;
    let gap = false;
    setCandles((previous) => {
      if (!previous.length) return previous;
      const result = foldTick(
        previous,
        barMs,
        price,
        tradeWhen || Date.now(),
        tradeVolume,
      );
      gap = result.gap;
      return result.candles;
    });
    if (gap) {
      const now = Date.now();
      if (now - liveReload.current > 5000) {
        liveReload.current = now;
        void loadKlines();
      }
    }
  }, [tradeSeq, tradeVolume, tradePrice, tradeWhen, tickPrice, barMs, loadKlines]);

  // REST ticker is the fallback when the socket is down; it also fills the
  // 24h high/low before the first WebSocket frame arrives.
  useEffect(() => {
    if (!symbol) return;
    let cancelled = false;
    api<{ high: number; low: number }>(`/api/ticker?${query({ market, symbol })}`)
      .then((data) => {
        if (!cancelled) setRestTicker(data);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [market, symbol]);

  /* ----------------------------------------------------------- account */
  const refreshAccount = useCallback(async () => {
    try {
      const data = await api<Account>(`/api/account?${query({ market, symbol })}`);
      setAccount(data);
      setLiveError("");
      // Server-side equity (cash + marked positions) so the sidebar quotes the
      // same number as the 资产 page. The local sum used to mark positions at
      // the last *trade* price, which drifted away from the portfolio total.
      const equity =
        data.equity ??
        data.cash +
          data.positions.reduce(
            (sum, position) =>
              sum + Math.abs(position.contracts) * (position.last || position.entry_price),
            0,
          );
      onAccountChange(equity);
    } catch (error) {
      // A read failure must be visible: an empty account would look like the
      // user owns nothing instead of "the key or the network failed".
      setLiveError(error instanceof Error ? error.message : String(error));
      /* the sidebar keeps the last known value */
    }
  }, [market, symbol, onAccountChange]);

  useEffect(() => {
    void refreshAccount();
    const timer = window.setInterval(refreshAccount, 4000);
    return () => window.clearInterval(timer);
  }, [refreshAccount]);

  const current = useMemo(
    () => symbols.find((item) => item.symbol === symbol),
    [symbols, symbol],
  );

  const base = current?.base ?? symbol.split("/")[0] ?? "";
  const quote = current?.quote ?? "USDT";
  const lastPrice = ticker?.last || candles[candles.length - 1]?.c || 0;
  const changePct = ticker?.change_pct ?? 0;
  const high = ticker?.high || restTicker?.high || 0;
  const low = ticker?.low || restTicker?.low || 0;
  const volume = ticker?.quote_volume ?? ticker?.base_volume ?? 0;

  const positionQty = useMemo(() => {
    const held = account?.positions.find((position) => position.symbol === symbol);
    return held?.contracts ?? 0;
  }, [account, symbol]);

  const cash = account?.cash ?? 0;

  const handleMarket = (value: string) => {
    setMarket(value);
    onMarketChange(value);
    setSymbols([]);
    setSymbol("");
    setCandles([]);
    setPickedPrice(null);
  };

  const banner = actionError || healthError || liveError || symbolError;

  return (
    <>
      <div className="page-head">
        <h1 className="page-title">交易</h1>
        <span className="page-sub">{MARKET_LABELS[market] ?? market}</span>
        <span className="spacer" />
        <span className={`live-badge ${connected ? "" : "idle"}`}>
          <span className={`dot ${connected ? "live" : ""}`} />
          {connected ? "实时" : "重连中"}
        </span>
        <span className={`env-badge ${health?.network === "mainnet" ? "live" : ""}`}>
          {health?.network === "mainnet" ? "主网 · 真实资金" : "测试网 · 模拟资金"}
        </span>
        <div className="segmented">
          {marketItems.map((item) => (
            <button
              key={item.value}
              type="button"
              aria-pressed={market === item.value}
              onClick={() => handleMarket(item.value)}
            >
              {item.label}
            </button>
          ))}
        </div>
      </div>

      {account && account.has_credentials === false && (
        <div className="mode-flag">
          该市场还没有可用的 API Key，只能看行情不能下单。到设置页填写后再回来。
        </div>
      )}

      {banner && <div className="banner error">{banner}</div>}

      <div className="trading">
        <div className="trading-main">
          <div className="symbol-strip">
            <SymbolPicker
              symbols={symbols}
              value={symbol}
              onChange={(value) => {
                setSymbol(value);
                setPickedPrice(null);
              }}
              loading={symbols.length === 0}
            />

            <div className="price-line">
              <span className="price num">{fmtPrice(lastPrice)}</span>
              <span className={`change num ${changePct >= 0 ? "up" : "down"}`}>
                {changePct >= 0 ? "+" : ""}
                {changePct.toFixed(2)}%
              </span>
            </div>

            <div className="stats">
              <div className="stat">
                <span className="stat-label">24h 高</span>
                <span className="stat-value num">{fmtPrice(high)}</span>
              </div>
              <div className="stat">
                <span className="stat-label">24h 低</span>
                <span className="stat-value num">{fmtPrice(low)}</span>
              </div>
              <div className="stat">
                <span className="stat-label">24h 量</span>
                <span className="stat-value num">
                  {fmtCompact(volume)} <span className="unit">{quote}</span>
                </span>
              </div>
            </div>
          </div>

          <div className="card">
            <div className="card-head">
              <div className="segmented">
                {TIMEFRAMES.map(([value, label]) => (
                  <button
                    key={value}
                    type="button"
                    aria-pressed={timeframe === value}
                    onClick={() => setTimeframe(value)}
                  >
                    {label}
                  </button>
                ))}
              </div>
              <span className="spacer" />
              <button
                type="button"
                className={`chart-ai-link${bottomTab === "ai" ? " active" : ""}`}
                aria-pressed={bottomTab === "ai"}
                onClick={() => setBottomTab("ai")}
                title="在下方打开 AI 分析（内嵌，不遮挡图表）"
              >
                AI 分析
              </button>
            </div>
            <DrawingToolbar
              tool={drawingTool}
              onChange={setDrawingTool}
              selected={drawings.find((item) => item.id === selectedDrawing) ?? null}
              count={drawings.length}
              onDelete={() => {
                if (!selectedDrawing) return;
                setDrawings((current) => current.filter((item) => item.id !== selectedDrawing));
                setSelectedDrawing(null);
              }}
              onClear={() => {
                setDrawings([]);
                setSelectedDrawing(null);
              }}
              visible={drawingsVisible}
              onToggleVisible={() => setDrawingsVisible((value) => !value)}
            />
            <div className="card-head indicator-bar">
              <span className="faint" style={{ fontSize: 11 }}>
                指标
              </span>
              {INDICATORS.map(({ group, items }) => (
                <div className="segmented" key={group} title={group}>
                  {items.map(([value, label]) => {
                    const on = indicators.some((item) => item.name === value);
                    return (
                      <button
                        key={value}
                        type="button"
                        aria-pressed={on}
                        onClick={() =>
                          setIndicators((current) =>
                            current.some((item) => item.name === value)
                              ? current.filter((item) => item.name !== value)
                              : [...current, { name: value, params: defaultParams(value) }],
                          )
                        }
                        onDoubleClick={() => {
                          // Double-click opens the parameters of an active
                          // indicator without adding a separate gear button
                          // for every one of the eleven types.
                          if (on) setEditing(value);
                        }}
                        title={on ? `${label} 参数（双击编辑）` : `添加 ${label}`}
                      >
                        {label}
                      </button>
                    );
                  })}
                </div>
              ))}
              {indicators.length > 0 && (
                <button
                  type="button"
                  className="link-btn"
                  onClick={() => {
                    setIndicators([]);
                    setEditing(null);
                  }}
                >
                  清空
                </button>
              )}
            </div>
            {editing && indicators.some((item) => item.name === editing) && (
              <div className="card-head indicator-params">
                <span className="faint" style={{ fontSize: 11 }}>
                  {INDICATOR_SPECS[editing].label} 参数
                </span>
                {INDICATOR_SPECS[editing].params.map((param) => {
                  const current =
                    indicators.find((item) => item.name === editing)?.params[param.key] ??
                    param.value;
                  return (
                    <label key={param.key} className="indicator-param">
                      <span>{param.label}</span>
                      <input
                        type="number"
                        min={param.min}
                        max={param.max}
                        step={param.step}
                        value={current}
                        onChange={(event) => {
                          const next = Number(event.target.value);
                          if (!Number.isFinite(next)) return;
                          setIndicators((items) =>
                            items.map((item) =>
                              item.name === editing
                                ? {
                                    ...item,
                                    params: {
                                      ...item.params,
                                      [param.key]: clampParam(editing, param.key, next),
                                    },
                                  }
                                : item,
                            ),
                          );
                        }}
                      />
                    </label>
                  );
                })}
                <span className="spacer" />
                <button
                  type="button"
                  className="link-btn"
                  onClick={() =>
                    setIndicators((items) =>
                      items.map((item) =>
                        item.name === editing ? { ...item, params: defaultParams(editing) } : item,
                      ),
                    )
                  }
                >
                  恢复默认
                </button>
                <button type="button" className="link-btn" onClick={() => setEditing(null)}>
                  收起
                </button>
              </div>
            )}
            {chartSource && (
              <div className="banner warn" role="status">
                行情来源：{chartSource}（币安合约接口在当前网络不可达，图表改用备用永续行情；
                价格与币安存在价差，下单仍走币安）
              </div>
            )}
            <TradingViewChart
              candles={candles}
              seriesKey={`${market}:${symbol}`}
              dataKey={`${market}:${symbol}:${candlesFor || timeframe}`}
              timeframe={timeframe}
              lastPrice={lastPrice}
              loading={chartLoading}
              error={chartError}
              indicators={indicators}
              onLoadOlder={() => void loadOlder()}
              olderLoading={olderLoading}
              drawings={drawings}
              onDrawingsChange={setDrawings}
              drawingTool={drawingTool}
              selectedId={selectedDrawing}
              onSelect={setSelectedDrawing}
              onToolDone={() => setDrawingTool("select")}
              drawingsVisible={drawingsVisible}
              markers={chartMarkers}
              entryPrice={entryPrice}
              pricePrecision={pricePrecision}
            />
          </div>

          <BottomTabs
            account={account}
            symbol={symbol}
            market={market}
            onChanged={() => {
              setActionError("");
              void refreshAccount();
            }}
            onError={(message) => setActionError(message)}
            activeTab={bottomTab}
            onTabChange={setBottomTab}
            aiPanel={
              <AIPanel
                market={market}
                symbol={symbol}
                timeframe={timeframe}
                onLevels={applyAiLevels}
                showLevels={aiLevelsOn}
                onShowLevels={setAiLevelsOn}
              />
            }
          />
        </div>

        <div className="trading-rail">
          <div className="card">
            <div className="card-head">
              <span className="card-title">盘口</span>
              <span className="spacer" />
              <span className="faint num" style={{ fontSize: 11 }}>
                {ticker?.bid && ticker?.ask
                  ? `${fmtPrice(ticker.bid)} / ${fmtPrice(ticker.ask)}`
                  : "—"}
              </span>
            </div>
            <OrderBook book={book} onPickPrice={setPickedPrice} />
          </div>

          <OrderTicket
            market={market}
            symbol={symbol}
            base={base}
            quote={quote}
            lastPrice={lastPrice}
            cash={cash}
            positionQty={positionQty}
            realizedPnl={
              account?.positions.find((position) => position.symbol === symbol)
                ?.realized_pnl ?? 0
            }
            defaultQuote={settings?.default_order_quote}
            feeRate={FEE_RATE}
            pickedPrice={pickedPrice}
            protection={account?.protections?.find((item) => item.symbol === symbol) ?? null}
            network={health?.network ?? "mainnet"}
            onPickedPriceConsumed={() => setPickedPrice(null)}
            onFilled={refreshAccount}
          />
        </div>
      </div>

      {/* AI 量化的执行引擎：策略信号 → 自动下单。跟随图表的交易对与周期，
          与下面的 AI 交易互斥运行（共用交易权租约）。 */}
      <AutoPage embedded market={market} symbol={symbol} timeframe={timeframe} />

      {/* AI 交易（模型逐根 K 线决策）/ AI 量化（策略生命周期），两个独立流程。 */}
      <AITradingPanel
        market={market}
        symbol={symbol}
        timeframe={timeframe}
        onNavigate={onNavigate}
      />
    </>
  );
}
