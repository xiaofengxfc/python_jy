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

/** A position as the exchange reports it, marked in quote currency. */
export interface Position {
  symbol: string;
  contracts: number;
  side: "long" | "short" | "flat";
  entry_price: number;
  /** Result of the pair so far: net of every fee paid, closed rounds included. */
  realized_pnl: number;
  fees?: number;
  /** Marked by the same server call that fills the 资产 page, so both pages
   * show one number for one position. */
  last: number;
  unrealized: number;
  quote_value: number;
}

/** One order row: resting on the venue, or already filled. */
export interface OrderRow {
  id: string;
  symbol: string;
  market?: string;
  side: Side;
  type: OrderType;
  amount: number;
  price?: number | null;
  /** Stop price of a protective order (0 for a plain limit/market order). */
  trigger?: number;
  filled?: number;
  remaining?: number;
  average?: number;
  status: string;
  timestamp: number;
  reduce_only?: boolean;
  reduceOnly?: boolean;
}

/** One fill from the venue's trade history. */
export interface TradeRow {
  id: string;
  symbol: string;
  /** The order this fill came from, so the detail dialog can trace it. */
  order?: string;
  market?: string;
  type?: OrderType;
  side: Side;
  price: number;
  amount: number;
  /** Price × amount actually exchanged on this fill. */
  cost?: number;
  fee: number;
  /** PnL booked by this fill: the price move of the closed part, minus fee. */
  realized?: number;
  /** Base coins closed by this fill (0 for a pure entry). */
  closedQty?: number;
  timestamp: number;
}

/** The stop / take-profit orders the venue currently holds for one symbol. */
export interface Protection {
  symbol: string;
  stop_loss: number;
  take_profit: number;
  /** Exchange order ids, so a cancel can name them. */
  orders?: string[];
}

/** The venue account: balances, positions, orders and recent fills. */
export interface Account {
  quote_asset: string;
  cash: number;
  /** Equity when the account was first connected; the PnL reference point. */
  starting_balance: number;
  /** Wallet plus marked positions: the same figure the 资产 page shows. */
  equity: number;
  realized_pnl: number;
  /** Realized PnL for each pair, including pairs whose position is closed. */
  realized_pnl_by_symbol?: Record<string, number>;
  positions: Position[];
  orders: OrderRow[];
  trades: TradeRow[];
  protections?: Protection[];
  /** False when this market has no API key configured yet. */
  has_credentials?: boolean;
  /** Read failure, already worded for the user; empty when the read worked. */
  error?: string;
  updated_at?: number;
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
  /** User-plugin filename; empty for built-ins. */
  source_name?: string;
  /** True only for files in the per-user strategy directory. */
  editable?: boolean;
}

export interface Portfolio {
  market: string;
  quote_asset: string;
  cash: number;
  starting_balance: number;
  equity: number;
  realized_pnl: number;
  realized_pnl_by_symbol?: Record<string, number>;
  positions: Position[];
  orders: OrderRow[];
  trades: TradeRow[];
  equity_history: { t: number; equity: number }[];
  has_credentials?: boolean;
  error?: string;
}

export interface NotifySettings {
  enabled: boolean;
  /** 企业微信群机器人地址，或任何收 JSON 的自建地址。自带 key，等同密码。 */
  webhook_url: string;
  /** auto 按地址判断，qyapi_markdown / qyapi_text 走群机器人，custom 用模板。 */
  msg_format: "auto" | "qyapi_markdown" | "qyapi_text" | "custom" | string;
  /** custom 格式的 JSON 模板，用 {title} 和 {content} 占位。 */
  webhook_template: string;
  /** 自定义请求头，每行一个「名称: 值」。 */
  webhook_headers: string;
  on_fill: boolean;
  on_protection: boolean;
  on_risk_halt: boolean;
  on_live_warning: boolean;
  /** 自动交易启停、回测/优化完成或失败。 */
  on_task: boolean;
}

/** Optional LLM assistant. The API key is not here: it lives in the keyring. */
export interface AISettings {
  enabled: boolean;
  provider: string;
  model: string;
  base_url: string;
  temperature: number;
  max_tokens: number;
  timeout: number;
  /** 0 disables the per-day budget. */
  daily_limit: number;
  cache_seconds: number;
  /** Positions and P/L are private; sending them to a model is opt-in. */
  share_position: boolean;
  /** Write a briefing after every closed candle in the auto-trader. */
  auto_brief: boolean;
}

/** One price level the model named, already range-checked by the backend. */
export interface AILevel {
  price: number;
  kind: "support" | "resistance" | "stop";
  label: string;
  /** (price - last) / last, so the legend can say 上方 1.2%. */
  distance: number;
}

export interface AIProviderInfo {
  key: string;
  label: string;
  model: string;
  base_url: string;
  needs_key: boolean;
  help: string;
}

export interface AIStatus extends AISettings {
  provider_label: string;
  provider_help: string;
  needs_key: boolean;
  has_key: boolean;
  key_hint: string;
  configured: boolean;
  providers: AIProviderInfo[];
  calls_today: number;
  audit: AIAuditItem[];
}

export interface AIAuditItem {
  at: number;
  kind: string;
  provider: string;
  model: string;
  ok: boolean;
  tokens?: number;
  elapsed?: number;
  error?: string;
  text?: string;
}

/** Result of one multi-timeframe analysis. */
export interface AIAnalysis {
  ok: boolean;
  text: string;
  levels: AILevel[];
  timeframes: string[];
  symbol: string;
  market: string;
  price: number;
  cached: boolean;
  tokens: number;
  elapsed: number;
  model: string;
}

export interface AppSettings {
  /** 交易所。现在只有币安接了完整链路。 */
  platform: string;
  network: string;
  market_type: string;
  last_symbol: string;
  timeframe: string;
  default_order_quote: number;
  kline_limit: number;
  /** HTTP(S) proxy for exchange calls; empty means direct. */
  proxy: string;
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
  notify: NotifySettings;
  ai: AISettings;
}

/** 凭证区顶部的交易所下拉项。enabled=false 的只占位，选了也下不了单。 */
export interface PlatformOption {
  key: string;
  label: string;
  enabled: boolean;
}

/** One entry the model ruled on during an AI-filtered backtest. */
export interface AIFilterJudged {
  entry_time: number;
  exit_time?: number;
  side: string;
  entry_price?: number;
  exit_price?: number;
  pnl?: number;
  verdict: "yes" | "no" | "keep";
  reason: string;
}

/** Metrics of one side of an AI-filtered comparison. */
export interface AIFilterMetrics {
  trades: number;
  total_return: number;
  max_drawdown: number;
  win_rate: number;
  profit_factor: number;
  avg_trade: number;
  net_pnl: number;
}

export interface AIFilterResult {
  symbol: string;
  timeframe: string;
  strategy: string;
  params: Record<string, unknown>;
  original: AIFilterMetrics;
  filtered: AIFilterMetrics;
  comparison: {
    delta_return: number;
    delta_drawdown: number;
    dropped: number;
    verdict: string;
  };
  judged: AIFilterJudged[];
  total: number;
  kept: number;
  failures: number;
  truncated: boolean;
}

/** Reachability of the local OpenClaw gateway, as reported by the backend. */
export interface NotifyStatus {
  enabled: boolean;
  format: string;
  /** 打码后的地址，只显示 key 的后四位。 */
  url: string;
  /** 地址填了没有。没填就发不出去。 */
  available: boolean;
  last_error: string;
  /** Unix seconds of the last failure, 0 when there has not been one. */
  last_error_at: number;
  last_success_at: number;
  dropped: number;
  /** 队列里还没发出去的条数。 */
  pending: number;
  /** 失败后暂存、等通道恢复再补发的条数。 */
  spooled: number;
  /** 熔断剩余秒数，0 表示没熔断。 */
  breaker_seconds: number;
}


/** Whether the local gateway process is up; WeChat push needs it. */
export interface GatewayStatus {
  /** idle | starting | running | failed | external */
  state: string;
  /** The port actually accepts connections right now. */
  reachable: boolean;
  /** We started this process, so we will stop it on exit. */
  managed: boolean;
  host: string;
  port: number;
  command: string[];
  error: string;
  started_at: number;
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
  /** Binance catalog label ("上币公告") or an RSS category. */
  kind: string;
}

/** Keyless public feed. `error` carries per-source failures without hiding
 * the headlines that did load. */
export interface NewsFeed {
  items: NewsItem[];
  cached: boolean;
  error: string;
  source: string;
  /** Pair the feed was filtered for; guards against showing stale rows. */
  symbol?: string;
  /** True when the pair had no news and market-wide headlines stand in. */
  fallback?: boolean;
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
  /** Where the run came from: "btrader" for a normal run, "cand:<id>" for a candidate. */
  source?: string;
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
