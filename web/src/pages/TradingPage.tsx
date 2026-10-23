import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, query } from "../api";
import { MARKET_LABELS, TIMEFRAMES, TIMEFRAME_MS, fmtCompact, fmtPrice } from "../format";
import { foldTick } from "../liveCandle";
import { useMarketStream } from "../hooks/useMarketStream";
import { changeTone, fmtChangePct, loadTickers } from "../tickers";
import type { Account, AILevel, AppSettings, Candle, Health, SymbolInfo, TickerQuote } from "../types";
import AIPanel from "../components/AIPanel";
import BottomTabs, { type BottomTab } from "../components/BottomTabs";
import SymbolFallbackNote from "../components/SymbolFallbackNote";
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

/** 一次拉多少根 K 线。交易所上限 1000，拉满省得用户再去配。 */
const KLINE_BARS = 1000;

interface Props {
  health: Health | null;
  healthError: string;
  settings: AppSettings | null;
  onMarketChange: (market: string) => void;
  onAccountChange: (equity: number) => void;
  onNavigate?: (page: "lab" | "backtest") => void;
  /** 收藏、最近用过存后端（settings.favorites / recent_symbols），状态由 App 拿着。 */
  onSettingsChange?: (patch: Partial<AppSettings>) => void;
}

/** 「最近用过」保留几条。 */
const RECENT_LIMIT = 8;

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
  onSettingsChange,
}: Props) {
  const [market, setMarket] = useState(settings?.market_type || "spot");
  const [symbol, setSymbol] = useState("");
  const [symbols, setSymbols] = useState<SymbolInfo[]>([]);
  /** 24 小时行情：只问看得见的那批交易对。接口没上时是空对象，涨跌幅整列不显示。 */
  const [quotes, setQuotes] = useState<Record<string, TickerQuote>>({});
  /** 选择器面板开着吗（它自己管开关，这里只用来决定要不要定时刷新涨幅）。 */
  const [pickerOpen, setPickerOpen] = useState(false);
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
  /** 设置里的默认交易对在这个市场没有（ETH/USDT 是现货写法，合约里叫 ETH/USDT:USDT）。
   *  退回会把用户自己的选择悄悄换掉，所以记下来在行情头下面说一句，可用时不出现。 */
  const [symbolFallback, setSymbolFallback] = useState<{ from: string; to: string } | null>(null);
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
  // 设置里改过的东西要落到本页，但不能把手上正在看的东西抢走：只有用户没在
  // 本页手动改过的那一项，才跟着设置走。
  const picked = useRef({ market: false, symbol: false, timeframe: false });
  useEffect(() => {
    if (!settings) return;
    if (!picked.current.market && settings.market_type) setMarket(settings.market_type);
    if (!picked.current.timeframe && settings.timeframe) setTimeframe(settings.timeframe);
  }, [settings?.market_type, settings?.timeframe]);
  useEffect(() => {
    if (!settings?.last_symbol || picked.current.symbol) return;
    setSymbol((current) => current || settings.last_symbol);
  }, [settings?.last_symbol]);

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
          const chosen = fallback?.symbol ?? "";
          setSymbol(chosen);
          // 设置里存的是另一个市场的写法时为真：这时候要告诉用户换成了哪个。
          setSymbolFallback(
            settings?.last_symbol && chosen && chosen !== settings.last_symbol
              ? { from: settings.last_symbol, to: chosen }
              : null,
          );
        } else {
          if (preferred !== symbol) setSymbol(preferred);
          setSymbolFallback(null);
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
    // 没有交易对（市场列表没读回来、或后端没起来）就别装作在加载：
    // 以前这里直接 return，chartLoading 停在 true，图表永远显示「加载中…」。
    if (!symbol) {
      setChartLoading(false);
      return;
    }
    const id = ++requestId.current;
    setChartLoading(true);
    try {
      const data = await api<{ candles: Candle[]; source?: string; degraded?: boolean }>(
        `/api/klines?${query({ market, symbol, timeframe, limit: KLINE_BARS })}`,
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
  }, [market, symbol, timeframe]);

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
        `/api/klines?${query({ market, symbol, timeframe, limit: KLINE_BARS, before: first })}`,
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
  }, [market, symbol, timeframe, candles, olderLoading]);

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
  /** `freshPair` 只在成交那一刻用：缓存里那一行还是「钱包余额」，面板会把刚买来的
   *  持仓标成「卖出」并预填整笔余额（t49 实测，最长约 10 秒）。后端只对单个交易对
   *  给现读，没有 symbol 时它会照旧给缓存（全钱包现读是 /api/portfolio 的活）。 */
  const refreshAccount = useCallback(async (freshPair = false) => {
    try {
      const data = await api<Account>(
        `/api/account?${query({ market, symbol, refresh: freshPair && symbol ? 1 : undefined })}`,
      );
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
      return data;
    } catch (error) {
      // A read failure must be visible: an empty account would look like the
      // user owns nothing instead of "the key or the network failed".
      setLiveError(error instanceof Error ? error.message : String(error));
      /* the sidebar keeps the last known value */
      return null;
    }
  }, [market, symbol, onAccountChange]);

  /** 成交之后把面板追到「账本口径」。刚成交这一对在本地成交账本里还没有记录，
   *  回填线程 5 秒一轮才登记，这之前现读返回的行仍是「钱包余额」；登记之后，按交易对
   *  缓存又不肯更新（10 秒）。所以成交后连读几次现读，看到这一对变成账本行就停，
   *  最多 6 次（约 12 秒）——一次成交最多几次签名读，不是每次轮询都读。 */
  const fillSeq = useRef(0);
  const refreshAfterFill = useCallback(async () => {
    const token = ++fillSeq.current;
    for (let attempt = 0; attempt < 6; attempt += 1) {
      const data = await refreshAccount(true);
      if (fillSeq.current !== token) return;
      const row = data?.positions.find((item) => item.symbol === symbol);
      // 合约的持仓天生就是账本口径，一次就够。
      if (!row || market !== "spot" || row.origin === "ledger") return;
      await new Promise((resolve) => window.setTimeout(resolve, 2400));
      if (fillSeq.current !== token) return;
    }
  }, [refreshAccount, symbol, market]);

  useEffect(() => {
    void refreshAccount();
    const timer = window.setInterval(refreshAccount, 4000);
    return () => window.clearInterval(timer);
  }, [refreshAccount]);

  const favorites = useMemo(() => settings?.favorites ?? [], [settings?.favorites]);
  const recent = useMemo(() => settings?.recent_symbols ?? [], [settings?.recent_symbols]);

  /** 收藏跟账户走：先改本地（界面立刻响应），再存后端。存不上不回滚——下一次读设置会纠正。
   *  连点两个星标时 setState 还没重渲染，读闭包里的 favorites 会把前一次覆盖掉（实测两只点
   *  只留下一只），所以「当前值」走 ref，点一下就立刻更新。 */
  const favoritesRef = useRef(favorites);
  favoritesRef.current = favorites;
  const recentRef = useRef(recent);
  recentRef.current = recent;

  const toggleFavorite = (pair: string) => {
    const current = favoritesRef.current;
    const next = current.includes(pair)
      ? current.filter((item) => item !== pair)
      : [...current, pair];
    favoritesRef.current = next;
    onSettingsChange?.({ favorites: next });
    void api("/api/settings", { method: "PUT", body: JSON.stringify({ favorites: next }) }).catch(
      () => {},
    );
  };

  /** 最近用过也跟账户走（settings.recent_symbols），刷新还在。 */
  const rememberRecent = (pair: string) => {
    const next = [pair, ...recentRef.current.filter((item) => item !== pair)].slice(0, RECENT_LIMIT);
    recentRef.current = next;
    onSettingsChange?.({ recent_symbols: next });
    void api("/api/settings", { method: "PUT", body: JSON.stringify({ recent_symbols: next }) }).catch(
      () => {},
    );
  };

  // 选择器窗口是 300 行：连同当前交易对、收藏、最近用过一起问，最多 6 批 × 50 个。
  // 顺序就是优先级——当前交易对、收藏、最近用过排在最前面，第一屏一定有数。
  const quotesWanted = useMemo(
    () => [symbol, ...favorites, ...recent, ...symbols.slice(0, 300).map((item) => item.symbol)],
    [symbol, favorites, recent, symbols],
  );
  /** 在跑的那一串批次请求：切页面、关面板时把它掐掉，别让剩下几批在后台继续跑。 */
  const quotesAbort = useRef<AbortController | null>(null);
  const refreshQuotes = useCallback(() => {
    quotesAbort.current?.abort();
    const controller = new AbortController();
    quotesAbort.current = controller;
    void loadTickers(market, quotesWanted, { signal: controller.signal }).then((map) => {
      if (!controller.signal.aborted) setQuotes(map);
    });
  }, [market, quotesWanted]);

  // 进页面先拉一次；页面切走（卸载）时中止还没发的批次。
  // 先等交易对列表到手：列表还空着时只凑得出当前交易对和几个收藏，
  // 那样等于白花一次请求，真正那 6 批随后还要重来一遍。
  useEffect(() => {
    if (!symbols.length) return;
    refreshQuotes();
    return () => quotesAbort.current?.abort();
  }, [refreshQuotes, symbols.length]);

  // 面板开着才定时刷新。关掉面板之后一条 /api/tickers 都不再发，在跑的批次当场中止。
  const pickerOpenRef = useRef(false);
  useEffect(() => {
    const was = pickerOpenRef.current;
    pickerOpenRef.current = pickerOpen;
    if (!pickerOpen) {
      if (was) quotesAbort.current?.abort();
      return;
    }
    const timer = window.setInterval(refreshQuotes, 20000);
    return () => window.clearInterval(timer);
  }, [pickerOpen, refreshQuotes]);

  const current = useMemo(
    () => symbols.find((item) => item.symbol === symbol),
    [symbols, symbol],
  );

  const base = current?.base ?? symbol.split("/")[0] ?? "";
  const quote = current?.quote ?? "USDT";
  const lastPrice = ticker?.last || candles[candles.length - 1]?.c || 0;
  /** 涨跌幅以 ticker 为准；读不到就是 null —— 界面上留空，不拿 0 冒充「平盘」。 */
  const changePct = ticker?.change_pct ?? null;
  const high = ticker?.high || restTicker?.high || 0;
  const low = ticker?.low || restTicker?.low || 0;
  const volume = ticker?.quote_volume ?? ticker?.base_volume ?? 0;
  /** 行情还没到（或后端读不到）时这些位留空显示「—」，不拿 0 冒充数据。 */
  const hasQuote = Boolean(ticker) || lastPrice > 0;

  const positionQty = useMemo(() => {
    const held = account?.positions.find((position) => position.symbol === symbol);
    return held?.contracts ?? 0;
  }, [account, symbol]);

  const cash = account?.cash ?? 0;

  const handleMarket = (value: string) => {
    picked.current.market = true;
    setMarket(value);
    onMarketChange(value);
    setSymbols([]);
    setSymbol("");
    setCandles([]);
    setPickedPrice(null);
    setSymbolFallback(null);
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
                picked.current.symbol = true;
                setSymbol(value);
                setPickedPrice(null);
                setSymbolFallback(null);
                rememberRecent(value);
              }}
              loading={symbols.length === 0}
              favorites={favorites}
              recent={recent}
              quotes={quotes}
              onToggleFavorite={toggleFavorite}
              onOpenChange={setPickerOpen}
            />

            <div className="price-line">
              <span className="price num">{hasQuote ? fmtPrice(lastPrice) : "—"}</span>
              {hasQuote ? (
                fmtChangePct(changePct) ? (
                  <span className={`change num ${changeTone(changePct)}`}>
                    {fmtChangePct(changePct)}
                  </span>
                ) : (
                  <span className="change num faint">—</span>
                )
              ) : (
                <span className="change num faint">—</span>
              )}
            </div>

            <div className="stats">
              <div className="stat">
                <span className="stat-label">24h 高</span>
                <span className="stat-value num">{high > 0 ? fmtPrice(high) : "—"}</span>
              </div>
              <div className="stat">
                <span className="stat-label">24h 低</span>
                <span className="stat-value num">{low > 0 ? fmtPrice(low) : "—"}</span>
              </div>
              <div className="stat">
                <span className="stat-label">24h 量</span>
                <span className="stat-value num">
                  {volume > 0 ? (
                    <>
                      {fmtCompact(volume)} <span className="unit">{quote}</span>
                    </>
                  ) : (
                    "—"
                  )}
                </span>
              </div>
            </div>
          </div>

          <SymbolFallbackNote
            from={symbolFallback?.from ?? ""}
            to={symbolFallback?.to ?? ""}
            market={market}
          />

          <div className="card">
            <div className="card-head">
              <div className="segmented">
                {TIMEFRAMES.map(([value, label]) => (
                  <button
                    key={value}
                    type="button"
                    aria-pressed={timeframe === value}
                    onClick={() => {
                  picked.current.timeframe = true;
                  setTimeframe(value);
                }}
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
              <span className="faint" style={{ fontSize: "var(--fs-12)"}}>
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
                <span className="faint" style={{ fontSize: "var(--fs-12)"}}>
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
                行情来源：{chartSource}。币安合约接口在当前网络不可达，图表改用备用永续行情。
                它和币安有价差，下单仍然走币安。
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
            network={health?.network ?? "mainnet"}
            onChanged={() => {
              setActionError("");
              // 刚成交/刚平完，把这一对追到账本口径（见 refreshAfterFill）。
              void refreshAfterFill();
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
              <span className="faint num" style={{ fontSize: "var(--fs-12)"}}>
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

            feeRate={FEE_RATE}
            pickedPrice={pickedPrice}
            protection={account?.protections?.find((item) => item.symbol === symbol) ?? null}
            network={health?.network ?? "mainnet"}
            onPickedPriceConsumed={() => setPickedPrice(null)}
            onFilled={() => void refreshAfterFill()}
          />
        </div>
      </div>


      {/* AI 交易和 AI 量化各自有整页，左侧栏进；交易页只管看图、下单、持仓。 */}
    </>
  );
}
