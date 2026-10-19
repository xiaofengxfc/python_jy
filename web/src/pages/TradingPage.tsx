import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, query } from "../api";
import { MARKET_LABELS, TIMEFRAMES, fmtCompact, fmtPrice } from "../format";
import { useMarketStream } from "../hooks/useMarketStream";
import type { Account, Candle, Health, SymbolInfo } from "../types";
import BottomTabs from "../components/BottomTabs";
import Chart, { type Indicator } from "../components/Chart";
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

const INDICATORS: [Indicator, string][] = [
  ["none", "无"],
  ["boll", "BOLL"],
  ["macd", "MACD"],
  ["rsi", "RSI"],
];

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
  const [indicator, setIndicator] = useState<Indicator>("none");
  const [candles, setCandles] = useState<Candle[]>([]);
  const [chartError, setChartError] = useState("");
  const [chartLoading, setChartLoading] = useState(true);
  const [symbolError, setSymbolError] = useState("");
  const [account, setAccount] = useState<Account | null>(null);
  const [pickedPrice, setPickedPrice] = useState<number | null>(null);
  const [restTicker, setRestTicker] = useState<{ high: number; low: number } | null>(null);
  const [actionError, setActionError] = useState("");

  const { ticker, book, connected } = useMarketStream(market, symbol);
  const requestId = useRef(0);

  const marketItems = health?.markets ?? [
    { value: "spot" as const, label: MARKET_LABELS.spot },
    { value: "usdm" as const, label: MARKET_LABELS.usdm },
    { value: "coinm" as const, label: MARKET_LABELS.coinm },
  ];

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
      const data = await api<{ candles: Candle[] }>(
        `/api/klines?${query({ market, symbol, timeframe, limit: 400 })}`,
      );
      if (id !== requestId.current) return;
      setCandles(data.candles ?? []);
      setChartError("");
    } catch (error) {
      if (id !== requestId.current) return;
      setCandles([]);
      setChartError(error instanceof Error ? error.message : String(error));
    } finally {
      if (id === requestId.current) setChartLoading(false);
    }
  }, [market, symbol, timeframe]);

  useEffect(() => {
    void loadKlines();
  }, [loadKlines]);

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
      const data = await api<Account>("/api/account");
      setAccount(data);
      const equity =
        data.cash +
        data.positions.reduce((sum, position) => {
          const held = data.trades.find((trade) => trade.symbol === position.symbol);
          return sum + Math.abs(position.contracts) * (held?.price ?? position.entry_price);
        }, 0);
      onAccountChange(equity);
    } catch {
      /* the sidebar keeps the last known value */
    }
  }, [onAccountChange]);

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

  const banner = actionError || healthError || symbolError;

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

      {banner && <div className="banner">{banner}</div>}

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
              <span className="faint" style={{ fontSize: 11 }}>
                指标
              </span>
              <div className="segmented">
                {INDICATORS.map(([value, label]) => (
                  <button
                    key={value}
                    type="button"
                    aria-pressed={indicator === value}
                    onClick={() => setIndicator(value)}
                  >
                    {label}
                  </button>
                ))}
              </div>
            </div>
            <Chart
              candles={candles}
              timeframe={timeframe}
              lastPrice={lastPrice}
              loading={chartLoading}
              error={chartError}
              indicator={indicator}
            />
          </div>

          <BottomTabs
            account={account}
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
