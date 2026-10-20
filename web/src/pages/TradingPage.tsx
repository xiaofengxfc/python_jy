import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, query } from "../api";
import { MARKET_LABELS, TIMEFRAMES, TIMEFRAME_MS, fmtCompact, fmtPrice } from "../format";
import { foldTick } from "../liveCandle";
import { useMarketStream } from "../hooks/useMarketStream";
import type { Account, Candle, Health, SymbolInfo } from "../types";
import BottomTabs from "../components/BottomTabs";
import Chart, {
  INDICATOR_SPECS,
  clampParam,
  defaultParams,
  type Indicator,
  type IndicatorConfig,
  type IndicatorParams,
} from "../components/Chart";
import OrderBook from "../components/OrderBook";
import OrderTicket from "../components/OrderTicket";
import SymbolPicker from "../components/SymbolPicker";

interface Props {
  health: Health | null;
  healthError: string;
  onMarketChange: (market: string) => void;
  onAccountChange: (equity: number) => void;
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
  onMarketChange,
  onAccountChange,
}: Props) {
  const [market, setMarket] = useState("spot");
  const [symbol, setSymbol] = useState("");
  const [symbols, setSymbols] = useState<SymbolInfo[]>([]);
  const [timeframe, setTimeframe] = useState("15m");
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
  /** Paper is the default; live reads balances and routes orders to the venue. */
  const [mode, setMode] = useState<"paper" | "live">("paper");
  const [liveConfirmOpen, setLiveConfirmOpen] = useState(false);
  const [liveError, setLiveError] = useState("");
  const [pickedPrice, setPickedPrice] = useState<number | null>(null);
  const [restTicker, setRestTicker] = useState<{ high: number; low: number } | null>(null);
  const [actionError, setActionError] = useState("");

  const { ticker, book, connected, tradeVolume, tradeWhen, tradePrice, tradeSeq } =
    useMarketStream(market, symbol);
  const requestId = useRef(0);

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

  /* ---------------------------------------------------------- symbols */
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
        // Prefer USDT pairs, which is what most people start with.
        const known = data.items.some((item) => item.symbol === symbol);
        if (!known) {
          const preferred =
            data.items.find((item) => item.symbol === "BTC/USDT") ??
            data.items.find((item) => item.quote === "USDT") ??
            data.items[0];
          setSymbol(preferred?.symbol ?? "");
        }
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        setSymbolError(error instanceof Error ? error.message : String(error));
      });
    return () => {
      cancelled = true;
    };
    // `symbol` decides only whether the current choice still exists.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [market]);

  /* ------------------------------------------------------------ klines */
  const loadKlines = useCallback(async () => {
    if (!symbol) return;
    const id = ++requestId.current;
    setChartLoading(true);
    try {
      const data = await api<{ candles: Candle[]; source?: string; degraded?: boolean }>(
        `/api/klines?${query({ market, symbol, timeframe, limit: 400 })}`,
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
        `/api/klines?${query({ market, symbol, timeframe, limit: 400, before: first })}`,
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
  const refreshAccount = useCallback(async () => {
    try {
      const path =
        mode === "live"
          ? `/api/account/live?${query({ market, symbol })}`
          : "/api/account";
      const data = await api<Account>(path);
      setAccount(data);
      setLiveError("");
      const equity =
        data.cash +
        data.positions.reduce((sum, position) => {
          const held = data.trades.find((trade) => trade.symbol === position.symbol);
          return sum + Math.abs(position.contracts) * (held?.price ?? position.entry_price);
        }, 0);
      onAccountChange(equity);
    } catch (error) {
      // A live read failure must be visible: an empty account would look like
      // the user owns nothing instead of "the key or the network failed".
      if (mode === "live") {
        setLiveError(error instanceof Error ? error.message : String(error));
      }
      /* the sidebar keeps the last known value */
    }
  }, [mode, market, symbol, onAccountChange]);

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

  /** Paper is the default; going live is an explicit, acknowledged switch. */
  const pickMode = (next: "paper" | "live") => {
    if (next === mode) return;
    if (next === "live") {
      setLiveConfirmOpen(true);
      return;
    }
    setMode("paper");
    setAccount(null);
    setLiveError("");
  };

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
        <div className="segmented" role="group" aria-label="交易模式">
          <button
            type="button"
            aria-pressed={mode === "paper"}
            onClick={() => pickMode("paper")}
          >
            纸面
          </button>
          <button
            type="button"
            aria-pressed={mode === "live"}
            onClick={() => pickMode("live")}
          >
            实盘
          </button>
        </div>
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

      {mode === "live" && (
        <div className="mode-flag">
          实盘模式：委托会真实发送到{health?.network === "mainnet" ? "主网" : "测试网"}
          （{market === "spot" ? "现货" : market === "usdm" ? "U 本位合约" : "币本位合约"}）。
        </div>
      )}

      {liveConfirmOpen && (
        <div className="modal-mask" role="presentation">
          <div className="modal" role="dialog" aria-modal="true" aria-label="切换到实盘">
            <div className="modal-title">切换到实盘交易？</div>
            <p className="modal-text">
              之后的委托会真实发送到
              <b>{health?.network === "mainnet" ? "币安主网" : "币安测试网"}</b>
              ，成交使用真实资金，盈亏由交易所结算。纸面持仓与实盘互不相通。
            </p>
            <p className="modal-text faint">
              请确认：API Key 只勾选「读取 + 交易」、未开启提现；每笔委托都会经过本地风控校验。
            </p>
            <div className="modal-actions">
              <button
                type="button"
                className="btn ghost"
                onClick={() => setLiveConfirmOpen(false)}
              >
                继续纸面
              </button>
              <button
                type="button"
                className="btn sell"
                onClick={() => {
                  setLiveConfirmOpen(false);
                  setMode("live");
                  setAccount(null);
                  setLiveError("");
                }}
              >
                我已确认，切换实盘
              </button>
            </div>
          </div>
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
            </div>
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
            <Chart
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
            />
          </div>

          <BottomTabs
            account={account}
            symbol={symbol}
            market={market}
            mode={mode}
            onChanged={() => {
              setActionError("");
              void refreshAccount();
            }}
            onError={(message) => setActionError(message)}
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
            realizedPnl={account?.realized_pnl ?? 0}
            feeRate={FEE_RATE}
            pickedPrice={pickedPrice}
            protection={account?.protections?.find((item) => item.symbol === symbol) ?? null}
            mode={mode}
            onPickedPriceConsumed={() => setPickedPrice(null)}
            onFilled={refreshAccount}
            onResetPaper={async () => {
              await api("/api/paper/reset", { method: "POST" });
              void refreshAccount();
            }}
          />
        </div>
      </div>
    </>
  );
}
