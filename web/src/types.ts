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
  /** 现货才分得出来：ledger 是买来的（有成交记录和成本价），wallet 是钱包里
   * 本来就有的（充值、空投、测试网赠送），成本价未知。合约恒为 ledger。 */
  origin?: "ledger" | "wallet";
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
  /** 钱包里没买过的那部分市值合计，算在权益里但不算持仓。 */
  wallet_value?: number;
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
  /** User-plugin filename in the per-user directory; empty for a plain built-in. */
  source_name?: string;
  /** The source can be saved. True for built-ins too: saving writes an override file. */
  editable?: boolean;
  /** A package implementation of this key exists, overridden or not. */
  builtin?: boolean;
  /** The user file for this key wins over the package original. */
  overridden?: boolean;
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
  timeout: number;
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

/* ------------------------------------------------------- AI 交易与候选策略 */
/* 三个页面（AI 交易、AI 量化、策略实验室）共用这几份形状。字段一律可选：
   后端升级前后互不匹配时，界面要能降级显示，而不是白屏。 */

/** AI 交易自己的运行配置，存在设置的 ai_auto 里。 */
export interface AIAutoSettings {
  enabled: boolean;
  live_enabled: boolean;
  symbol: string;
  timeframe: string;
  leverage: number;
  max_order_quote: number;
  max_position_quote: number;
  max_decisions_per_day: number;
  max_orders_per_hour: number;
  min_confidence: number;
  decision_ttl_sec: number;
  max_price_drift_pct: number;
  cooldown_sec: number;
  poll_sec: number;
  require_stop: boolean;
  /** 决策深度：quick 一次调用，standard 四段，deep 六段。 */
  depth: string;
  strategy_params?: Record<string, Record<string, unknown>>;
}

/** 流水线里的一段。败了也留着，界面据此标灰。 */
export interface PipelineStage {
  stage: string;
  title: string;
  ok: boolean;
  skipped?: boolean;
  text: string;
  model?: string;
  tokens?: number;
  elapsed?: number;
}

/** 一条决策记录。trace 是整条流水线的输出。 */
export interface AIDecisionRecord {
  at?: number;
  seq?: number;
  kind?: string;
  event?: string;
  outcome?: string;
  reason?: string;
  decision_id?: string;
  action?: string;
  symbol?: string;
  quote?: number;
  price?: number | null;
  confidence?: number;
  signal_target?: number | null;
  stop_loss?: number | null;
  rationale?: string;
  order_id?: string | null;
  network?: string;
  state?: string;
  depth?: string;
  trace?: PipelineStage[];
}

export interface AITraderStatus {
  state?: string;
  paused?: boolean;
  halted_reason?: string;
  config?: {
    network?: string;
    symbol?: string;
    strategy?: string;
    market_type?: string;
    timeframe?: string;
    live_enabled?: boolean;
    depth?: string;
    max_order_quote?: number;
    max_position_quote?: number;
  };
  counters?: {
    decisions_today?: number;
    orders_last_hour?: number;
    max_decisions_per_day?: number;
    max_orders_per_hour?: number;
  };
  last_decision?: AIDecisionRecord | null;
  events?: AIDecisionRecord[];
  seq?: number;
}

/** 一条 AI 生成的候选策略。 */
export interface CandidateIssue {
  code?: string;
  message?: string;
  line?: number;
}

export interface CandidateChecks {
  ok?: boolean;
  issues?: CandidateIssue[];
  imports?: string[];
  classes?: string[];
}

export interface Candidate {
  id: string;
  created_at?: number;
  strategy_key?: string;
  name?: string;
  base_key?: string;
  model?: string;
  status?: string;
  checks?: CandidateChecks;
  backtest?: Record<string, unknown>;
  note?: string;
  reviewer?: string;
  registered_key?: string;
  strategy_path?: string;
}

export interface OptimizeRound {
  round?: number;
  params?: Record<string, unknown>;
  metrics?: Record<string, number>;
  score?: number;
  accepted?: boolean;
  reason?: string;
  reflection?: string;
  tokens?: number;
  elapsed?: number;
  error?: string;
}

export interface OptimizeResult {
  rounds?: OptimizeRound[];
  best?: Record<string, unknown>;
  best_metrics?: Record<string, number>;
  best_score?: number;
  calls?: number;
  stopped?: string;
}

export interface OptimizeJob {
  id?: string;
  status?: string;
  progress?: number;
  started?: number;
  result?: OptimizeResult | null;
  error?: string | null;
}

export interface AppSettings {
  /** 交易所。现在只有币安接了完整链路。 */
  platform: string;
  network: string;
  market_type: string;
  last_symbol: string;
  timeframe: string;  /** HTTP(S) proxy for exchange calls; empty means direct. */
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
  /** AI 交易的运行配置。默认关闭，实盘另有显式开关。 */
  ai_auto?: AIAutoSettings;
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

/** 通知通道的当前状态，来自 GET /api/notify。 */
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
