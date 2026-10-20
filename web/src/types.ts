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

export interface NotifySettings {
  enabled: boolean;
  /** OpenClaw channel id for the WeChat plugin. */
  channel: string;
  /** WeChat conversation the gateway should deliver to. */
  target: string;
  /** Account id; empty means the gateway's default (single-account setups). */
  account: string;
  /** OpenClaw executable; empty means "openclaw" from PATH. */
  command: string;
  on_fill: boolean;
  on_protection: boolean;
  on_risk_halt: boolean;
  on_live_warning: boolean;
  /** 自动交易启停、回测/优化完成或失败。 */
  on_task: boolean;
  /** Probe the gateway port before spawning the CLI. */
  preflight: boolean;
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
  notify: NotifySettings;
}

/** Reachability of the local OpenClaw gateway, as reported by the backend. */
export interface NotifyStatus {
  enabled: boolean;
  channel: string;
  target: string;
  /** Account id used for the send; empty means the gateway's default. */
  account: string;
  command: string;
  available: boolean;
  /** Non-empty when the target cannot match a WeChat conversation. */
  target_warning: string;
  last_error: string;
  /** Unix seconds of the last failure, 0 when there has not been one. */
  last_error_at: number;
  last_success_at: number;
  dropped: number;
  pending: number;
  /** Messages kept on disk after a failed send, waiting to be replayed. */
  spooled: number;
  /** Seconds the channel is paused for after repeated failures; 0 = not paused. */
  breaker_seconds: number;
}

/** State of the "install OpenClaw" helper. */
export interface InstallStatus {
  /** The `openclaw` CLI is on PATH. */
  available: boolean;
  /** `node --version` output, empty when Node.js is missing. */
  node: string;
  node_path: string;
  npm: string;
  npm_path: string;
  /** A usable npm was found, so an install can be started. */
  ready: boolean;
  /** Why it cannot start (missing Node.js, bad path), Chinese, may be empty. */
  reason: string;
  registries: string[];
  running: boolean;
  /** idle | running | done | failed | canceled */
  state: string;
  command: string[];
  started_at: number;
  finished_at: number;
  error: string;
  log: string[];
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

/** One WeChat account bound to the gateway. */
export interface WeChatAccount {
  account: string;
  /** The WeChat id that scanned the QR; also a valid push target. */
  user: string;
  base_url: string;
}

/** A recipient id an outbound message can be addressed to. */
export interface WeChatTarget {
  target: string;
  account: string;
  /** peer: has messaged the bot; account: the scanner's own id. */
  source: string;
}

/** What the gateway says about the WeChat channel itself. */
export interface ChannelStatus {
  channel: string;
  /** The `openclaw` CLI exists. */
  cli: boolean;
  /** The gateway has the plugin in its catalog (even if not configured). */
  plugin: boolean;
  /** An account is linked, i.e. the QR scan has been done. */
  configured: boolean;
  /** The channel's monitor is live in the gateway. */
  running: boolean;
  /** The plugin is enabled. */
  enabled: boolean;
  /** Gateway-reported lifecycle, e.g. "starting" or "running". */
  lifecycle: string;
  /** Account id currently bound (matches `--account`). */
  account: string;
  accounts: WeChatAccount[];
  targets: WeChatTarget[];
  /**
   * A conversation exists (the user has messaged the bot), so a push can
   * actually reach the phone. A bound account alone is not enough.
   */
  has_context: boolean;
  /** missing | installed | ready | unknown */
  state: string;
  /** One-line summary for the card, or the raw CLI excerpt as a fallback. */
  detail: string;
  gateway?: GatewayStatus;
}

/** The link the WeChat login QR encodes, as printed by the CLI. */
export interface LoginStatus {
  /** idle | starting | waiting | done | failed | canceled | expired */
  state: string;
  /** The encoded link. Rendered as an image; also offered as a fallback. */
  url: string;
  /** A code is available to display right now. */
  has_qr: boolean;
  /** Bumped whenever the wizard prints a refreshed code. */
  revision: number;
  error: string;
  command: string[];
  started_at: number;
  /** Tail of the CLI's output, for the failure case. */
  lines: string[];
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
