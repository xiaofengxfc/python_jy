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
  /** 交易所口径的数量：合约是张、现货是币。只用于排查与换算，界面上显示的数量一律读
   *  size_base。 */
  contracts: number;
  /** 基础币数量（符号与 contracts 一致）。币本位一张是 100 USD 而不是一个币，
   *  contracts 直接当币用会差 contractSize 倍（BTCUSD 差 500 倍）；这个字段由后端
   *  按合约规格折好。折不出来（缺价格、规格读不到）是 null —— 不填 0，0 会被读成
   *  「空仓」。现货行两个字段同值。 */
  size_base?: number | null;
  side: "long" | "short" | "flat";
  entry_price: number;
  /** Result of the pair so far: net of every fee paid, closed rounds included. */
  realized_pnl: number;
  fees?: number;
  /** Marked by the same server call that fills the 资产 page, so both pages
   * show one number for one position. */
  last: number;
  unrealized: number;
  /** 这个浮动盈亏是按什么数量算的。现货行数量取钱包余额、盈亏取账本数量，
   *  两个数不一样时界面必须说清楚，否则 0.02 挂在 1.0041 上看着像算错了。
   *  合约、钱包里白送的行是空串。 */
  unrealized_basis?: "" | "ledger";
  /** 账本里这一对的数量（已封顶于钱包余额）。origin=ledger 时才有意义。 */
  ledger_size?: number;
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
  /** 交易所的最小下单量（基础币）与最小名义额（计价币），取自 limits.amount.min /
   *  limits.cost.min。拿不到就是 null——不拿「最小数量 × 现价」凑一个数。 */
  min_amount?: number | null;
  min_notional?: number | null;
  /** 杠杆被 risk.max_leverage 收口时后端给的说明（「不许静默改数」），原样显示。 */
  leverage_note?: string | null;
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
  /** Key 存在哪儿：系统密钥环 / 本机文件存储（服务器上没有 SecretService 时）。 */
  storage: CredentialStorage;
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

/** 一次调用实际花掉的 token（供应商回报的用量）。合计就是 tokens。 */
export interface AIUsage {
  input: number;
  output: number;
  total: number;
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
  /** 输入/输出分开的用量。老后端没有这个字段，界面要能不看它。 */
  usage?: AIUsage;
  elapsed: number;
  model: string;
}

/* ------------------------------------------------------- AI 交易与候选策略 */
/* 三个页面（AI 交易、AI 量化、策略实验室）共用这几份形状。字段一律可选：
   后端升级前后互不匹配时，界面要能降级显示，而不是白屏。 */

/** 域名与 HTTPS（Linux 服务器才有）：GET /api/deploy/info 的回执。 */
export interface DeployInfo {
  platform: string;
  caddy_installed: boolean;
  caddy_active: boolean;
  domain: string | null;
  https_enabled: boolean;
  public_ip: string | null;
  public_ip_source: string;
  public_ip_error: string | null;
  cert_expires: string | null;
  data_dir: string;
}

/** POST /api/deploy/check：域名现在解析到哪、和本机公网 IP 一不一致。
 *
 *  proxied 为真表示解析全部落在 Cloudflare 边缘（橙云）：源站 IP 对不上是正常状态，
 *  这时 ok 已经是「可以签」。match 只说明源站 IP 一致不一致，别再拿它当成功判据。 */
export interface DeployCheck {
  ok: boolean;
  domain: string;
  resolves_to: string[];
  public_ip: string | null;
  public_ip_source: string;
  match: boolean;
  origin_match?: boolean;
  proxied?: boolean;
  proxy?: string | null;
  message: string;
}

/** POST /api/deploy/tls：签证书的结果。失败时 log_tail / script_output 是原文。 */
export interface DeployResult {
  ok: boolean;
  domain: string;
  url: string;
  cert_expires: string | null;
  log_tail: string;
  script_output: string;
  message: string;
}

/** 下单框按市场记住的杠杆，存在设置的 trade 里：{"usdm": 10, "coinm": 5}。
 *  只是「上次用过的倍数」，真上限仍是 risk.max_leverage，接口按上限收口。 */
export interface TradeSettings {
  leverage?: Record<string, number>;
}

/** GET /api/update/info：本地与 GitHub 的差距。读不到就是 error 非空、remote_sha 为 null，
 *  这时界面上一个字都不许说「已是最新」。 */
export interface UpdateCommit {
  sha: string;
  short: string;
  title: string;
  date: string;
}

/** GitHub Releases 那一套（桌面安装版没有 git 仓库，只能按 tag 比版本号）。 */
export interface UpdateDesktop {
  /** 本机版本（btrader.__version__）。 */
  current?: string | null;
  /** 远端版本，不带 v。 */
  latest?: string | null;
  /** 远端 tag，带 v（v0.6.2）。 */
  tag?: string | null;
  /** true 有新版 / false 已最新 / null 比不出来（当「查不到」处理，别当「已最新」）。 */
  newer?: boolean | null;
  download_url?: string | null;
  /** 没挂安装包（或链接给不出来）时后端写的那句话，原样显示。 */
  download_note?: string | null;
  /** 真的去问 GitHub 的时刻（缓存命中时是更早那次抓取的时间）。 */
  fetched_at?: string | null;
  /** true = 这份结果来自 300 秒缓存：界面上不写「查于」，写「缓存于」。 */
  cached?: boolean | null;
  published_at?: string | null;
  notes?: string | null;
  /** 查不到那一档的后端原话。 */
  error?: string | null;
}

export interface UpdateInfo {
  current_sha?: string | null;
  current_short?: string | null;
  branch?: string | null;
  remote_url?: string | null;
  remote_sha?: string | null;
  remote_short?: string | null;
  ahead_by?: number | null;
  behind_by?: number | null;
  commits?: UpdateCommit[];
  checked_at?: string | null;
  /** github（API 读到的提交列表）或 ls-remote（只问到 SHA，没有标题）。 */
  source?: string | null;
  /** 后端跑在哪个平台（Linux / Windows）：手动命令按它给。 */
  platform?: string | null;
  error?: string | null;
  /** false = 桌面安装版（没有 git 仓库、没有 pip/npm）：整张卡换成 Releases 的比对结果。 */
  supported?: boolean;
  /** server / desktop。 */
  mode?: string | null;
  /** 本机版本号（桌面态用它比）。 */
  version?: string | null;
  desktop?: UpdateDesktop | null;
}

/** 一键更新的一步：跑完才写。output 是命令的原文，失败时原样贴出来。 */
export interface UpdateStep {
  name: string;
  ok: boolean;
  output: string;
  seconds?: number | null;
}

/** GET /api/update/status：当前（或最后一次）更新任务的进度。 */
export interface UpdateStatus {
  state: string;
  steps?: UpdateStep[];
  from_sha?: string | null;
  to_sha?: string | null;
  error?: string | null;
  job_id?: string | null;
  /** 重启前的「等新进程接手」标记：还没收掉就说明在等，不当成普通的 running。 */
  restart_pending?: boolean | null;
  /** 后端跑在哪个平台（Linux / Windows）：手动命令按它给，别在 Linux 上教人跑 run_web.py。 */
  platform?: string | null;
  /** 这次任务真跑的端口与地址（重启前落盘）：手动命令按它拼，别写死 8765。 */
  port?: number | null;
  host?: string | null;
  /** 任务种类：apply / rollback。 */
  kind?: string | null;
}

/** 执行台最后用过的那三项，存在设置的 exec 里。不是风控：真上限在 risk 与
 *  AITrader 的闸里，改这三项动不了任何一条闸。 */
export interface ExecSettings {
  /** 0 到 1 的比例，1 = 全部可用余额（不是百分数）。 */
  position_pct: number;
  leverage: number;
  poll_seconds: number;
}

/** AI 交易自己的运行配置，存在设置的 ai_auto 里。 */
export interface AIAutoSettings {
  enabled: boolean;
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
  /** 决策节奏：bar 跟主周期，其余是固定节拍（5m / 15m / 30m / 1h / 4h）。 */
  cadence: string;
  /** 参考周期：除主周期之外还要看的结构周期，最多两个。 */
  timeframes: string[];
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
  /** 这一条决策按什么节奏跑出来的（bar / 15m / …）。 */
  cadence?: string;
  /** 固定节拍下的时间片编号；bar 模式恒 0。 */
  slot?: number;
  /** 这一轮看过的参考周期。 */
  timeframes?: string[];
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
    depth?: string;
    max_order_quote?: number;
    max_position_quote?: number;
    /** 这一轮真的按几倍在跑（已按 risk.max_leverage 收口）。 */
    leverage?: number;
    /** 被收口时的那句话，原样显示。 */
    leverage_note?: string;
    /** 这一轮按什么节奏在跑：bar / 15m / …。 */
    cadence?: string;
    /** 节拍秒数（0 = 周期不认识）。 */
    cadence_seconds?: number;
    /** 参考周期。 */
    timeframes?: string[];
  };
  counters?: {
    decisions_today?: number;
    orders_last_hour?: number;
    max_decisions_per_day?: number;
    max_orders_per_hour?: number;
  };
  /** 决策节奏：跟着 K 线周期走（1h → 每根 1h K 线收盘后一次）。 */
  cadence?: {
    /** bar = 跟主周期；其余是固定节拍（15m / 1h / …）。 */
    mode?: string;
    /** 一步节拍多少秒。0 = 算不出来，界面退回本页选的周期。 */
    seconds?: number;
    /** 主周期这根 K 线多长（秒）。 */
    bar_seconds?: number;
    /** 下一次决策的预计时刻（epoch 秒）。没在跑就是 null。 */
    next_run_at?: number | null;
    /** 上一次真正决策的那根 K 线的开放时间（毫秒）。 */
    last_bar_ts?: number;
    /** 固定节拍下已处理的时间片编号。 */
    last_slot?: number;
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
  /** AI 交易的运行配置。默认关闭；没有单独的实盘总闸，主网启动时再确认一次。 */
  ai_auto?: AIAutoSettings;
  /** 执行台最后用过的那三项：仓位比例 / 杠杆 / 轮询。 */
  exec?: ExecSettings;
  /** 下单框按市场记住的杠杆。只存合约市场，现货恒 1、不入库。 */
  trade?: TradeSettings;
  /** 收藏的交易对。跟账户走（存后端），不写 localStorage。 */
  favorites?: string[];
  /** 最近用过的交易对，后端记，界面上最多显示 8 条。 */
  recent_symbols?: string[];
}

/** GET /api/tickers 的一项：24 小时行情。change_pct 是百分数（+1.23 表示涨 1.23%），
 *  可能为 null（交易所没给或读不到），界面上留空，不拿 0 冒充。 */
export interface TickerQuote {
  symbol: string;
  last: number | null;
  change_pct: number | null;
  quote_volume: number | null;
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

/**
 * 密钥存在哪儿。桌面是系统密钥环（Windows 凭据管理器 / macOS 钥匙串），
 * Debian/Ubuntu 服务器上没有 SecretService/DBus，退化成数据目录里的加密文件。
 * kind: "keyring" | "file" | "none"
 */
export interface CredentialStorage {
  kind: string;
  label: string;
  detail: string;
  path: string;
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

/** 自动交易这一轮的 K 线供给情况，来自 GET /api/auto/status 的 data 字段。
 *
 * 引擎在跑不等于在决策：预热根数不够时它每轮直接 return，界面上这一行
 * 是唯一能解释「为什么没有信号」的地方。 */
export interface AutoDataStatus {
  /** 这一轮策略要多少根收盘 K 线（min_bars + 预热）。 */
  required_bars: number;
  /** 这一轮实际拿到多少根。 */
  fetched_bars: number;
  /** 交易所单次请求上限，现在是 1000。 */
  max_bars: number;
  /** true 表示根数不够，这一轮不会出信号。 */
  short: boolean;
  /** 不够时的完整说明，直接拿去显示。 */
  message: string;
}

export interface AutoStatus {
  running: boolean;
  config: {
    symbol: string;
    market: string;
    timeframe: string;
    strategy: string;
    params: Record<string, unknown>;
    network: string;
    position_pct: number;
    leverage: number;
    /** 被风控/交易所上限收口时后端给的那句话，有就显示。 */
    leverage_note?: string;
  } | null;
  last_signal: { target: number; reason: string; when: string } | null;
  seq: number;
  events: { seq: number; kind: string; level: string; message: string; when: string }[];
  /** 没在跑的时候是 null。 */
  data: AutoDataStatus | null;
  /** 下一根 K 线收盘（下一次可能调仓）的预计时刻，epoch 秒。引擎没报就是 null。 */
  next_run_at?: number | null;
}

export interface EquityPoint {
  t: number;
  equity: number;
  drawdown: number;
  position: number;
  target: number;
  close: number;
}

/** 一笔回测成交：一次完整的开平仓往返，字段与后端 TradeRecord.as_dict() 一一对应。 */
export interface BacktestTrade {
  /** 开仓那根 K 线的收盘时间，毫秒。 */
  entry_time: number;
  /** 平仓那根 K 线的收盘时间，毫秒。 */
  exit_time: number;
  side: "long" | "short";
  entry_price: number;
  exit_price: number;
  /** 带符号的数量：做空是负数，显示时取绝对值。 */
  qty: number;
  /** 已扣手续费的盈亏，计价货币。 */
  pnl: number;
  /** 相对开仓名义金额的收益率。 */
  pnl_pct: number;
  /** 持仓根数。 */
  bars: number;
  /** 平仓原因：signal / stop_loss / take_profit / end。 */
  exit_reason: string;
  fee: number;
}

export interface BacktestResult {
  metrics: Record<string, number>;
  params: Record<string, unknown>;
  equity: EquityPoint[];
  trades: BacktestTrade[];
  /** 这次回测算不进去的成本与极端情况（拿不到资金费历史、币本位最小量按张…）。
   *  后端每一条都要显示出来，别让用户以为全算过了。 */
  warnings?: string[];
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
