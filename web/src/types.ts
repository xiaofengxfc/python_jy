export type Market = "spot" | "usdm" | "coinm";
export type Side = "buy" | "sell";
export type OrderType = "limit" | "market";
export type SizeMode = "amount" | "quote";

export interface SymbolInfo {
  symbol: string;
  display: string;
  base: string;
  quote: string;
  price_precision: number;
  amount_precision: number;
  min_amount: number;
  min_cost: number;
  is_inverse: boolean;
}

export interface Candle {
  t: number;
  o: number;
  h: number;
  l: number;
  c: number;
  v: number;
}

export interface Ticker {
  symbol: string;
  last: number;
  bid: number;
  ask: number;
  high: number;
  low: number;
  base_volume: number;
  quote_volume: number;
  change_pct: number;
  timestamp: number;
}

export interface Book {
  bids: [number, number][];
  asks: [number, number][];
}

export interface PaperPosition {
  symbol: string;
  contracts: number;
  side: "long" | "short" | "flat";
  entry_price: number;
  realized_pnl: number;
  fees?: number;
}

export interface PaperOrder {
  id: string;
  symbol: string;
  market?: string;
  side: Side;
  type: OrderType;
  amount: number;
  price?: number | null;
  filled?: number;
  remaining?: number;
  average?: number;
  status: string;
  timestamp: number;
  reduceOnly?: boolean;
}

export interface PaperTrade {
  id: string;
  symbol: string;
  side: Side;
  price: number;
  amount: number;
  fee: number;
  timestamp: number;
}

/** A stop-loss / take-profit guard currently armed for one symbol. */
export interface PaperProtection {
  symbol: string;
  stopLoss: number;
  takeProfit: number;
  createdAt?: number;
  order?: string;
}

export interface PaperConditional {
  id: string;
  symbol: string;
  market: string;
  side: Side;
  type: OrderType;
  amount: number;
  price: number;
  trigger: number;
  direction: "below" | "above";
  status: string;
  createdAt: number;
}

export interface Account {
  quote_asset: string;
  cash: number;
  starting_balance: number;
  realized_pnl: number;
  positions: PaperPosition[];
  orders: PaperOrder[];
  trades: PaperTrade[];
  protections?: PaperProtection[];
  conditional?: PaperConditional[];
  /** True when the snapshot came from the exchange, not the paper account. */
  live?: boolean;
}

export interface OrderPreview {
  allowed: boolean;
  reason: string;
  symbol: string;
  price: number;
  amount_base: number;
  exchange_amount: number;
  quote_notional: number;
  fee_estimate: number;
  fee_rate: number;
  slippage_bps: number;
}

export interface Health {
  ok: boolean;
  market: Market;
  network: string;
  has_credentials: boolean;
  markets: { value: Market; label: string }[];
}

export interface ParamSpec {
  name: string;
  label: string;
  default: number | boolean | string;
  minimum: number | null;
  maximum: number | null;
  step: number | null;
  choices: (number | string)[];
  help: string;
}

export interface Strategy {
  key: string;
  name: string;
  description: string;
  allow_short: boolean;
  params: ParamSpec[];
  defaults: Record<string, unknown>;
}

export interface Portfolio {
  market: string;
  quote_asset: string;
  cash: number;
  starting_balance: number;
  equity: number;
  realized_pnl: number;
  positions: (PaperPosition & { last: number; unrealized: number; quote_value: number; fees: number })[];
  orders: (PaperOrder & { filled?: number; average?: number; cost?: number })[];
  trades: PaperTrade[];
  equity_history: { t: number; equity: number }[];
}

export interface AppSettings {
  network: string;
  market_type: string;
  last_symbol: string;
  timeframe: string;
  default_order_quote: number;
  kline_limit: number;
  risk: {
    max_order_quote: number;
    max_order_pct: number;
    max_position_quote: number;
    max_leverage: number;
    max_daily_loss: number;
    max_daily_trades: number;
    max_consecutive_losses: number;
    require_confirmation: boolean;
    kill_switch: boolean;
  };
  notification_sound: boolean;
  theme: string;
}

export interface CredentialItem {
  market: string;
  label: string;
  configured: boolean;
  masked: string;
  current: boolean;
}

export interface NewsItem {
  id: string;
  title: string;
  url: string;
  source: string;
  published_at: string;
  currencies: string[];
  kind: string;
  positive: number;
  negative: number;
  important: number;
}

/** CryptoPanic feed. `configured` is false until a token is saved; the panel
 * shows that as a setup hint rather than an empty list. */
export interface NewsFeed {
  configured: boolean;
  items: NewsItem[];
  cached: boolean;
  error: string;
  source: string;
}

/** Live breaker state. `halted` covers the kill switch and every cap, so the
 * panel can explain a refusal without guessing from the order flow. */
export interface RiskState {
  day: string;
  realized_pnl: number;
  day_pnl: number;
  trades: number;
  day_trades: number;
  consecutive_losses: number;
  halted: boolean;
  halted_reason: string;
  kill_switch: boolean;
  limits: {
    max_daily_loss: number;
    max_daily_trades: number;
    max_consecutive_losses: number;
    max_order_quote: number;
    max_order_pct: number;
    max_position_quote: number;
  };
}

export interface AutoStatus {
  running: boolean;
  config: {
    symbol: string;
    market: string;
    timeframe: string;
    strategy: string;
    params: Record<string, unknown>;
    mode: string;
    position_pct: number;
    leverage: number;
  } | null;
  last_signal: { target: number; reason: string; when: string } | null;
  seq: number;
  events: { seq: number; kind: string; level: string; message: string; when: string }[];
}

export interface EquityPoint {
  t: number;
  equity: number;
  drawdown: number;
  position: number;
  target: number;
  close: number;
}

export interface BacktestResult {
  metrics: Record<string, number>;
  params: Record<string, unknown>;
  equity: EquityPoint[];
  trades: Record<string, unknown>[];
}

export interface BacktestJob {
  id: string;
  kind: "backtest" | "optimize";
  status: "running" | "done" | "error";
  progress: number;
  started: number;
  error: string | null;
  result: BacktestResult | { rows: Record<string, unknown>[] } | null;
}

export interface BacktestRun {
  id: string;
  created_at: string;
  symbol: string;
  market_type: string;
  timeframe: string;
  strategy: string;
  params: Record<string, unknown>;
  metrics: Record<string, number>;
  start: string;
  end: string;
}

export interface StreamFrame {
  type: "ticker" | "book" | "subscribed" | "pong" | "error";
  symbol?: string;
  last?: number;
  bid?: number;
  ask?: number;
  high?: number;
  low?: number;
  base_volume?: number;
  quote_volume?: number;
  change_pct?: number;
  timestamp?: number;
  bids?: [number, number][];
  asks?: [number, number][];
  message?: string;
}
