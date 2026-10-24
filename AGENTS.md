# 项目约定

biTrader：后端 Python + FastAPI，在 `src/btrader/`。前端 React + TS，在 `web/`。
新会话先读这份文件，再改代码。

## 1. 改完要让它生效

| 改了什么 | 要做什么 |
|---|---|
| `web/src/**`（TS、TSX、CSS） | 在 `web` 目录执行 `npm run build`。产物直接写进 `src/btrader/web/static` |
| `src/btrader/**`（Python） | 重启后端，没有热重载 |
| 两者都改 | 先 build，再重启 |
| 文档 | 不用重启 |

重启后端（PowerShell，在仓库根目录执行）：

```powershell
Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue |
  ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
Start-Process -FilePath ".venv\Scripts\pythonw.exe" `
  -ArgumentList "run_web.py","--host","127.0.0.1","--port","8765" `
  -WorkingDirectory (Get-Location) -WindowStyle Hidden
```

用 `pythonw.exe` 而不是 `python.exe`：pythonw 是 GUI 子系统的解释器，压根不申请控制台，屏幕上不会出现终端窗口。
`-WindowStyle Hidden` 只藏住 PowerShell 拉起的那个进程自己的窗口，管不到子进程的控制台——以前用 python.exe，启动时就会闪一个黑框。
代价是没有控制台就没有 stdio：`sys.stdout` / `sys.stderr` 都是 `None`，所以 `run_web.py` 自己调 `setup_logging` 并把 console 输出兜到 `<数据目录>/logs/console.log`，日志看 `<数据目录>/logs/bitrader.log`（不是终端）。

Node 不在 PATH 时，先把 Node 目录加进去。

数据目录按「跑的是什么」分两套，只由 `paths._base_dir()` 一处决定：

| 跑的是什么 | 数据目录 |
|---|---|
| 装好的桌面端（PyInstaller 冻结，`sys.frozen` 为真） | `%APPDATA%\biTrader` |
| 源码跑起来（`run_web.py`、`python -m btrader.web`、electron dev） | `%APPDATA%\biTrader-dev` |

`BITRADER_DATA_DIR` 压过两者（测试与临时验证用它，`tests/conftest.py` 的
`isolated_data_dir` 就是这条）。桌面壳只在 `app.isPackaged` 时把继承来的
`BITRADER_DATA_DIR` 删掉，目录名不写在壳里——再加第三条启动路径也照这个来。
`/api/health` 的 `build` 与 `data_dir` 两个字段直接说当前读的是哪一份，别再靠猜。
开发时造的配置与成交因此进不了桌面端的库，反过来也一样。

## 2. 测试

```powershell
.venv\Scripts\python.exe -m pytest -q
```

提交前必须全绿。测试不访问网络：`tests/conftest.py` 把 `notify._post_json` 换成
一个直接报错的替身，暂存文件也指向临时目录。要验证发送的测试自己替换它。
另有一条 `isolated_data_dir`：`BITRADER_DATA_DIR` 指向每个用例自己的临时目录。
没有它，凡是构造 `AppContext` 的用例都会打开 `%APPDATA%/biTrader/bitrader.db`
（跑一次测试就跑一次 schema 迁移）并往真实 `config.json` 里写东西。

## 3. 改代码的注意事项

- 图表用 TradingView lightweight-charts，代码在 `web/src/chart/`。
  指标算法在 `indicators.ts`，画线图元在 `primitives.ts`。
  指标算法是从手绘 canvas 版搬过来的，不要顺手优化。数值一变，图就和历史截图对不上。
- 含中文的源文件只用编辑器改。不要用 `Set-Content` 或 `Out-File`，
  这两个命令会按 GBK 写文件，中文变乱码，之后就是 SyntaxError。
- `cmd` 脚本要 CRLF 加纯 ASCII。否则 `rem` 行会被当成命令执行。
- 前端只读磁盘上的静态产物。`index.html` 带 `no-store`，改完刷新页面就行。
- 交易页的市场、交易对、周期以设置为初值：用户没在本页手动改过的那一项跟着设置走。
  判断依据是 `picked` 那三个标记，新增可配置项时照这个来，不要写死默认值。
- K 线一次拉 1000 根（交易所上限），不做成设置项。下单金额由下单框自己记，也不进设置。
  自动交易按实际参数的 `min_bars + 2` 决定这一轮拉多少根：`kline_limit`（默认 500）只是下限，
  上限仍是 1000。真不够（要的比 1000 多、或者交易所只有那么长历史）时不发请求，只报一次
  「K 线不足 N 根，请把周期改小」，状态在 `GET /api/auto/status` 的 `data` 字段里。
  以前固定拉 `kline_limit` 根，慢线填到 500 就每轮静默 return——界面显示在跑，实际什么都不做，
  日志一个字都没有。
- 界面字号只改 `web/src/styles.css` 里的 `--fs-*` 令牌，组件里不写字面量：
  CSS 写 `font-size: var(--fs-13)`，JSX 内联写 `style={{ fontSize: "var(--fs-13)" }}`。
  档位：页面标题 `.page-title` 用 `--fs-14`，正文走 `--font-size`（= `--fs-14`），
  卡片标题 `.card-title` 和表格 `td` 用 `--fs-13`，表头 `th` 用 `--fs-12`。
  令牌名就是像素值，整体再抬一档要连名字一起改，别只改数值。窄窗口靠收 `.table` 的内边距，
  不靠缩字号。图表刻度也跟着令牌走：`chart/theme.ts` 用 `cssPx("--fs-12", 12)` 把令牌换成
  lightweight-charts 要的数字，令牌改了图表跟着变。
- 内容列宽上限分两档：默认表单页 1080；1400 档是资产页 `.stack.portfolio`、回测页
  `.stack.backtest`、策略页 `.strategy-page`，加三个 AI 页 `.ai-grid` / `.quant-grid` / `.lab-grid`。
  列靠 `margin: auto` 居中。
  左右内边距不等：`.main` 左 22、右 12，右边 10px 由 `scrollbar-gutter: stable` 预留给滚动条，
  视觉上两边都是 22；别把右边改回 22，否则内容会横向跳。页面标题条用负 margin 反向出血铺满整行，
  `.page-head` 是 `margin: 0 -12px 0 -22px` 配 `padding: 0 12px 0 22px`；加新页照抄这套，别自己另写边距。

## 4. 账户只有一个来源

- 没有模拟账户。所有委托直接发到 `ctx.network` 指向的交易所，主网和测试网都是真下单。
  界面上没有「纸面/实盘」开关。
- 账户数字只来自 `ctx.account`，也就是 `engine/account.py` 的 `AccountService`。
  `/api/account?market&symbol` 和 `/api/portfolio` 读同一份快照。
  单交易对读会借用全钱包的权益，缓存 10 秒，所以侧栏和资产页永远是同一个数。
- 要「此刻」就带 `refresh=1`：`GET /api/account?market&symbol&refresh=1` 走 `live_snapshot` 现读，
  默认仍吃缓存。缓存是给每 2 秒轮询的面板用的；但刚成交完那 10 秒里缓存里那一行还是「钱包余额」，
  界面于是把程序刚买的仓位画成「卖出 / 预填整笔余额」。现读只按交易对读：不带 symbol 的整钱包
  现读是 `/api/portfolio` 的后台刷新在做的，面板按需触发一次代价太大，所以那种情况照旧给缓存。
  这条路不进 `account_lock`——读账户不该排在订单后面。
- 下单预览（`POST /api/orders/preview`）带 `min_amount`（最小数量，基础币）和 `min_notional`
  （最小名义额，计价币）两个字段，取自交易所的 `limits.amount.min` / `limits.cost.min`；
  拿不到就是 null，不拿「最小数量 × 现价」凑一个数。它只是**报数**，不是新闸门：太小的单仍然是
  交易所自己拒（实测 4.77 USDT 被 NOTIONAL 拒），界面据此提示。
- 权益基线存在 `account.baseline.v1:<network>:<market>`，只有全钱包读会写。
  单交易对读只继承。如果让单交易对读也写，第一次读的那一对会把偏小的金额
  冻成「起始资金」。
- 回测把四件真实成本算进去了，各有各的开关，**别在没解释的情况下动默认值**：
  强平（`liquidate`，只在合约且杠杆 > 1 时；多 `entry×(1-1/L+m)`、空 `entry×(1+1/L-m)`，
  打穿就按触发价成交、开盘已穿过就按开盘价，`cash` 亏光归零，`exit_reason="liquidation"`，
  强平那根不许再进场）；资金费（`funding` + `run_backtest(funding_rates=)`，结算落在
  `(上一根, 这一根]` 就按这根收盘价扣 `qty×close×rate`，空头 qty 为负自然收钱；拿不到历史
  就落一条 `warnings`，不假装算了）；跳空成交（止损止盈在开盘已跳过触发价时按开盘价成交，
  **没有开关，之前按理论价是错的**）；最小量与步长（`min_amount` / `amount_step`，单位是基础币，
  小于最小量跳过并记一条 warning）。指标多了 `funding_paid`、`liquidations`，结果里多了 `warnings`。
  最小量与步长默认从 `contract_spec` 取（`amount_step` 是 t57 读进来的绝对步长，别再自己读
  `precision.amount`）；币本位两样都是「张」，折成币要价格，于是两样都不折并落一条 `notes`/
  `warnings`，不许一边折一边不折。资金费率按 `since` 往后翻页（一页 1000 条 ≈333 天，上限 5 页），
  翻不满时 warning 说明「只覆盖到 X」——静默少掉后半段只会让曲线更乐观。三条路（单次回测、
  参数优化、AI 复核）共用 `routes._apply_funding`，别各写一份。
  护栏：`funding=False, liquidate=False` 且不给最小量时，数字必须和加这些成本之前**逐位相同**
  （`tests/test_backtest_model.py` 钉着改前引擎跑出来的数）。现货不扣资金费、不强平：
  `_validate_backtest` 按市场收死，和杠杆、做空同一条口径。
- 回测也按市场类型收死：现货不许借杠杆、不许做空（`leverage=1`、`allow_short=False`），
  合约照常。`_validate_backtest` 原样透传 `leverage` / `allow_short` 的时候，直调接口能给现货
  跑出「做空 10 倍杠杆」的收益曲线，那条曲线在真实市场里根本不存在。
  策略实验室的优化打分（`routes_lab.start_optimize`）同一口径：`allow_short=market.is_futures`、
  杠杆恒 1。以前它用 `BacktestConfig` 的默认值 `allow_short=True`，现货优化把「靠做空吃到的收益」
  当成绩顶上排名，和合约的打分逐位相同。
- 止损、止盈、条件单在交易所侧是**算法单**，跟普通挂单不是一套接口：`fetch_open_orders` 里一条
  都没有，要读 `fapiPrivateGetOpenAlgoOrders`、撤 `fapiPrivateDeleteAlgoOrder`（币本位把 fapi 换成
  dapi），返回的是 `algoId` + `algoType: CONDITIONAL` 而不是 `orderId`。以前三处都只读普通挂单，
  于是挂出去的止损在面板上看不见、重挂时 `cleared` 恒为 0（旧单没撤，连挂四次四张止损并存）、
  按 algoId 撤单报 -2011、平仓前 `cancel_symbol_orders` 也看不见它们——「平仓前撤掉 reduceOnly
  止损」这条保证曾经是假的。现在 `rest.fetch_algo_orders` / `cancel_algo_order` 单独处理，
  两个 `cancel_*` 合并两条链路，`cancel_order` 遇 -2011 回退算法单，`account._open_orders`
  把算法单映射成同样的 order 形状交给 `_protections`。加新接口时别忘了这一路。
- 现货没有 reduce-only 触发单。止损、止盈、条件单只在合约市场挂到交易所侧。
  现货请求会返回明确文案：`PUT /api/positions/protection` 和
  `POST /api/orders/conditional`。
- 现货平仓只平**本地成交账本**记录的数量，不平钱包。现货没有持仓对象，所谓持仓就是钱包里那个币的
  可用余额，里面混着用户自己充提的币：不带 size 的「全平」在现货上等于清空钱包，实测把用户自己的
  1 BTC 一次卖掉。现在 `POST /api/positions/close` 不带 size 时，合约照旧全平，现货只平
  `trading._live_ledger()["books"][symbol]["contracts"]` 并封顶于钱包余额；账本没有这一对就 400
  让调用方显式给 size。界面上的钱包余额那一行因此是「卖出」（预填该行余额），账本行才是「平仓」。
- 现货的「仓位」只算**本程序买的**：仓位上限（`max_position_quote`）与 AI 交易的同一个上限
  都按本地成交账本里这一对的数量算，并封顶于钱包余额（账本比钱包多说明币已被提走）。
  以前直接拿钱包余额当持仓：钱包里有用户自己的 1 BTC（约 8.1 万）、上限 5000，于是任何现货
  买单都 400「持仓金额将达 81225.76，超过上限 5000.00」，现货买不了任何东西。合约侧读的是
  交易所那份持仓，本来就对，不要动。
- 现货行的未实现盈亏只按**账本数量 × 账本均价**算，行上带 `unrealized_basis: "ledger"` 和
  `ledger_size` 标明是账本口径；`contracts` / `quote_value` 仍是钱包事实。两者混用过一次：
  `entry_price` 取账本、数量取钱包，钱包 1.00042 而账本 0.00042 时报出 +1000 USDT 的假浮盈。
  没有账本的行（充值、空投、测试网白送）origin 是 `wallet`，未实现盈亏恒 0。
- 保护单接口是重挂语义。先调 `cancel_protective_orders`，只撤该交易对的止损止盈，
  不会撤掉挂着的限价单。条件单的类型由触发价和现价的相对位置决定，不要自己猜。
- 符号有两种写法：现货是 `BTC/USDT`，合约带结算币 `BTC/USDT:USDT`，币本位 `BTC/USD:BTC`，
  交割还多一段 `-YYMMDD`。分两类入口：**下单与预览要求写法与实际市场一致**（`_check_symbol_shape`，
  合约必须带结算币、现货不许带，写错直接 400 并给出该写的名字）——因为写错不会报错，ccxt 会把裸名
  解析成同一个 swap 真把单挂出去，比报错难发现得多；撤单、平仓、保护单、条件单这些则两种写法都收，
  先过 `_venue_symbol` 补齐再往下走，别在各自的函数里各猜一次。反过来 `/api/symbols` 按市场类型过滤：
  ccxt 的 `load_markets()` 一次把现货与三种合约全取回来，只按报价过滤会让 600 条合约混进现货列表
  （1461 项里真正能交易的现货只有 861）。
  整串**最多一个冒号**：结算币段里再带冒号（`BTC/USDT:USDT:USDT`）判写法错并说清该写成什么，
  交割的 `-YYMMDD` 后缀照旧能过。这条判定只有 `core.types.symbol_shape_error` 一份，
  `_check_symbol_shape`（下单与预览、`/api/tickers`）和收藏/最近交易对都先过它，别各写一份。
- 合约的 24 小时 ticker 响应里没有 `bidPrice/askPrice`（只有现货有），ccxt 的 unified `bid/ask`
  因此是 None，别用 `float(raw.get("bid") or 0.0)` 把它压成 0——那样界面是「有最新价但没有买卖价」。
  取价顺序：unified → 原始 payload → 盘口顶档（`rest.ticker_bid_ask`），都没有才留 0，前端按 0 判空。
- 成交之后立刻把这一对记账：`/api/orders` 与 `/api/positions/close` 拿到 `filled > 0` 的回执就
  当场 `sync_symbol_fills`（`web/app.py::_book_the_fill`）。成交同步本身是 5 秒一轮的，不补这一次，
  刚买完那十几秒面板会把程序自己买的仓位画成「钱包余额 / 卖出（预填整笔余额）」，
  不带 size 的现货平仓还会以「账本里没有这一对」400；平完之后也会停在旧数量上。
  `refresh=1` 只解决快照缓存，账本是另一回事——两者都要。
- 本机时钟偏差也走这一层：币安对时间戳的容忍度很小，实测本机快 1.3 秒，所有签名请求
  都被 `-1021` 拒，界面上只有一句「无法读取当前持仓」，看不出是时钟。`rest.BinanceREST`
  在客户端 `fetch` 那一层装了守卫：只拦这一类错误，`load_time_difference()` 后重试一次。
  这个版本的 ccxt python 客户端没有 `adjustForTimeDifference`，别把守卫删掉。
- SQLite 里 `mode='paper'` 的旧成交还在库里，界面只显示 `live` 的成交。
- 存储的每一项都要带作用域，别让两个账户（或两个市场）共用一个 key：
  成交账本 `live.ledger.v1:<network>`（`trading.ledger_key`；`seen` 的元素是
  `<market>:<trade_id>`——现货与合约的成交编号各自从 1 数起）、当日风控
  `risk.state.v1:<network>`（`risk.risk_state_key`，切网络走 `set_scope`）、权益基线
  `account.baseline.v1:<network>:<market>`、K 线缓存
  `cache/klines/<平台>-<网络>/<市场>/<交易对>/<周期>.parquet`（`KlineStore.set_source`）。
  `orders` / `trades` 的主键是 `(account, market_type, id)`，`account` 只能由
  `core.credentials.account_id(network, platform)` 算：写入方是 `TradingService._account_id()`，
  读取方是 `AccountService._account_id()`，各算一份就等于成交历史永远读不出来，而且不报错。
  读历史一律带 `account` + `market_type`（`recent_trades` / `recent_orders`），
  不带就会把两个网络、两个市场的行混进同一个列表。
  库版本在 `PRAGMA user_version`（`storage.SCHEMA_VERSION`）：v1 重建了 `orders` / `trades`，
  旧表改名成 `orders_v0_backup` / `trades_v0_backup` 留档，不删——旧行没有 `market_type`，
  撞号的那一条早被覆盖，读不出来，但用户的记录不该被一次启动悄悄抹掉。

## 5. 已确认的限制

- 推送只有一条通道：用户自己填的 Webhook 地址。企业微信群机器人地址最省事，
  发 `{"msgtype": "markdown"}`；别的地址发 `{"title", "content"}` 或用自定义模板。
- 地址自带 key，等同密码。`config.json` 里存明文，界面只显示后四位，日志不打印。
- 一次发送是普通 HTTP POST，超时 10 秒，不经过任何本地网关或 Node 进程。
- 企业微信群机器人限流：每分钟 20 条，超了返回 `errcode 45009`。消息会暂存补发。
- 群机器人只能发到机器人所在的群，没有「发给某个人」这一说。要发给人，改用自建应用。
- 连续三次失败熔断五分钟；失败的消息留在 `notify-spool.jsonl`，六小时后过期。

## 6. AI 助手

改 `btrader/llm.py` 和 `btrader/ai.py` 之前，先读 README 的「AI 助手」一节。

- 密钥不进 `config.json`：走 `core/secret_store` 的同一个 store（和交易所 Key 一样），
  service 叫 `biTrader-ai`、user 叫 `api_key` —— 这两个名字别改，改了等于让老用户已存的 Key 作废。
  桌面有系统密钥环就用密钥环；Debian/Ubuntu 服务器上没有 SecretService/DBus 就落到数据目录的
  加密文件（AES-256-GCM，600/700），`GET /api/ai/status` 的 `storage` 字段告诉界面是哪种。
  `AISettings` 只存供应商、模型、接口地址、温度、开关。
- `llm.py` 只管收发：多供应商、缓存、审计、限额。
  `ai.py` 只管提示词和解析。两个模块都不碰交易。改提示词只动 `ai.py`。
- 以下五条不要破坏：
  - 模型不进 `strategy.generate()` 和 `run_backtest()` 的循环，也不下单。
  - 回测复核只给开仓前的 K 线，`ai.entry_window` 是唯一入口。
  - 模型给的价位要过 `ai.validate_levels` 才能画到图上。
  - 调用失败或回答读不懂时保留该笔交易。不能因为超时就让策略变好。
  - 自动交易简报走 `ai.BriefQueue`，独立线程，队列满就丢，不能阻塞 `_tick()`。
- 币安合约域名在部分网络不可达。AI 端点统一走 `routes._market_frame`，
  它和 `/api/klines` 一样会回落到 Gate 永续行情。
  不要在 AI 路由里直接调 `ctx.klines.fetch`，否则图表能用，分析接口报 500。
- AI 测试在 `tests/test_ai.py`，全部离线：`FakeLLM` 替身加 `monkeypatch` 密钥环。
- 输出上限不再是「我们替模型定一个额度」（t60 起改的）。OpenAI 兼容协议里 `max_tokens` 是可选的，
  调用方没传就**根本不发**这个字段，让模型自己决定什么时候停；ollama 的 `num_predict` 同理，
  只有显式传了 `max_tokens` 才写。只有「供应商必填」那一档才有兜底数字：Anthropic 的
  `max_tokens` 是必填字段，给 `FALLBACK_MAX_TOKENS`（32000），被供应商以「max_tokens 超过
  模型上限」拒掉时自动降到 `FALLBACK_MAX_TOKENS_RETRY`（8192）重试**一次**；第二次还被拒就
  报中文实话，别把英文原文甩给用户。这两个常量都在 `llm.py`，别在调用点写数字。
  以前固定 4000/8000：推理模型先把额度花在思考上，分析类回答常从中间断掉；`routes_lab` 自己
  写了 2400 比默认还小，长策略被截成坏 JSON，报出来的是「没有可解析的 JSON」——那类报错现在
  先看 `truncated`，别把「额度用完被截断」和「回答不合格、解析不出来」混成一句话。
  截断口径没变：判据仍是 `llm.TRUNCATED_FINISH` / `llm.is_truncated()`，被截断的答案不进缓存。
- 缓存窗口和每日调用次数也不进设置，固定在 `CACHE_SECONDS`。
  设置页只留开关、供应商、模型、接口地址、Key。
- `ask()` 的返回除了 `ok` / `text` / `error` / `tokens`，还有 `finish`（供应商的 finish_reason）
  和 `truncated`。截断有两种叫法：OpenAI 风格的 `finish_reason="length"` 和 Anthropic 的
  `stop_reason="max_tokens"`，两个都算（`llm.TRUNCATED_FINISH` / `llm.is_truncated()`）。
  被截断的回答可能带着半截正文，所以报错时要把「额度用完、被截断」和「回答不合格、解析不出来」
  分开说。缓存命中也会带上 `finish` / `truncated`（`_Cache.put` 存 finish），
  被截断的答案根本不进缓存——半截答案缓存下来，只会在 5 分钟里反复命中。
- 策略实验室的生成不要求模型回 JSON：解析在 `ai_candidate.py`，`from_answer` 依次试
  纯 JSON → json 围栏 → python 围栏 → 正文里带括号的 `class Xxx(...)` → 失败，第一个抠到代码的赢。
  最后一段按「第一个带括号的类」切，切到散文里的示例类就会静态检查不通过——取错比整段丢掉值。
  实测模型回的是「散文 + 一个 python 代码块 + 尾部参数表格」，别把这种回答当成「JSON 坏了」。
  - `absolute_strategy_imports`：模型会照抄参考源码的 `from ..base` / `from ...core`，候选是数据
    目录里的独立模块，这两条相对导入解析不了，写盘前换成 `btrader.strategy.base` / `btrader.core`。
  - `sanitize_key`：`key` 撞上已有策略或已有候选就加 `_ai` / `_ai2` 后缀，原名写进候选的 note。
    提示词里也念一遍现有 key，那只是减少重试；真正兜底的是这个函数。
  - 解析只负责把代码捞出来。候选代码仍然要过 `sandbox.check_code`，那是唯一闸门；
    `check_code` 除了结构，还盯三件会让代码根本加载不了的事：相对导入（`from ..base` 在候选
    这种独立模块里解析不了，直接判不通过并给出绝对写法）、基类名必须在模块顶层可见（类继承了
    `Strategy` 却通篇没 import，是最常见的模型错误）、参数构造器同样要可见（用了 `IntParam`
    没导入）。只用 `from x import *` 的写法名字不可知，那种放过。
    批准时 `_register_candidate` 再改写一次类属性 `key = "..."`，所以候选列表上的 key 和最终
    注册的 key 可能差一个后缀。

## 7. AI 交易与 AI 量化

- AI 交易的决策拆在 `ai_pipeline.py`：技术面 → 新闻面 → 多空辩论 → 风控复核 → 裁决。
  流水线只出文本。方向约束、`AIGuard`、下单都在 `engine/ai_trader.py`，分段不能放宽任何一条。
- 深度三档存在 `ai_auto.depth`：quick 一次调用，standard 四段，deep 六段。
  `AITraderConfig.depth` 默认 quick（测试和「决策一次」用），界面默认 standard。
- 裁决段的提示词只有一份，在 `ai_pipeline.DECISION_PROMPT`，`engine/ai_trader` 直接引用。
  改格式要同时改 `parse_decision`。
- AI 交易没有单独的「允许实盘」总闸：`ai_auto` 里没有 `live_enabled`，`AITraderConfig` 也没有。
  删它的理由很直接——设置页八张卡里从来没有这个控件，接口却拿它当启动前提，于是启动必定 400。
  旧的 `config.json` 里残留 `"live_enabled": true` 不会报错：读盘按 dataclass 字段白名单过滤，
  写盘只写 `to_dict()`，下一次保存顺手清掉；`PUT /api/settings` 带上它也走「未知字段忽略」。
  启动条件就三条：AI 助手已启用、这条市场配了 API Key、主网调用方显式传 `confirm_live`
  （测试网不需要）；现货还多一条，`require_stop` 时现货挂不了交易所侧的止损单，启动直接拒。
  真正拦住乱下单的是凭据、`AIGuard`、`RiskManager`、急停/熔断和交易权 `TradeLease`，
  别再补一道「实盘开关」回来。
- 三个页面共用 `main.main > .ai-grid / .quant-grid / .lab-grid` 这套三列骨架：1400 起三列，
  1360 起两列，1024 以下单列交回页面滚动。列内卡片等高靠网格 stretch，卡内列表自己滚，
  空数据用 `.empty-note` / `.cand-empty` 的虚线框，不要再退回 1080 的单列。
  策略页不走这套：`.strategy-page` 是两列，见第 8 节。
- 三个页面各自独立，侧栏进：`AITradePage`（全自动交易）、`AIQuantPage`（候选 → 部署）、
  `StrategyLabPage`（生成与调参）。侧栏现在八项，顺序是交易 / AI 交易 / AI 量化 / 资产 /
  回测 / 策略 / 实验室 / 设置。`StageBar`、`CandidateList`、`MetricGrid`、`ExecutionConsole`
  是共用件，别再各写一份。
- 执行台和 AI 交易共用同一个账户，同时只能开一个，撞上了返回 409。
- 下单预算跟着 `TradeLease` 走（`TradeLease.budget` 是本进程唯一的一份 `TradeBudget`）：
  当日决策次数、每小时订单数、冷却时间。轮询和「立即决策」点一次 new 一个 `AITrader`，
  计数要是留在实例里，点第二下就从零开始数，96 次/日、12 单/时、30 秒冷却等于不存在。
  控制器把同一份 budget 传给每一个实例（`AITrader(budget=)`），别在实例里另起一份。
- 提交前复核：模型调用那几十秒里行情、持仓、策略信号都可能变了。`_recheck_before_submit`
  在 AIGuard 之前重读一遍——策略信号反向或掉成空仓就拒、持仓数量变了就拒（不自己改数量重算）、
  读不到持仓也拒。拒单文案一律以「提交前复核：」开头。只拦新开仓，平仓和观望放行：拦减仓比
  漏减仓更糟，和 RiskManager「只在 entry 上判熔断」同一条理由。复核过之后 AIGuard 与 submit
  用的是最新的持仓与价格，漂移闸、持仓上限、急停看到的都是提交前的数。
  这份持仓必须现读（`AccountService.live_snapshot`，`refresh=True`），不能吃面板那 3/10 秒缓存：
  复核要的就是「模型调用期间有没有成交」，缓存里根本没有。面板（`/api/account`、`/api/portfolio`、
  侧栏资产页）照旧吃缓存，别把它们也改成现读——每 2 秒轮询一次的界面不该每次都去签名读一遍。
  代价是 AI 决策每轮多一次交易对签名读，权益仍走缓存。
- 决策有效期是真闸，不是文档里的承诺：`created_at` 打在「模型返回、解析出决策」那一刻
  （不是这一轮开始），`AIGuard.check` 的「现在」取提交前那一刻，有效期取
  `min(模型给的 ttl_sec, 配置的 decision_ttl_sec)`——配置是上限，模型顶不开。超龄就拒并落审计。
  别把它改回「用构造时刻当 created_at」，那等于 now - created_at 恒为 0，闸门永远是开的。
- 熔断就是当场把交易权还回去：`halt()` 之后 `TradeLease` 立刻释放。熔断的实例本来就不
  会再下单（`step()` 看 state、`_submit()` 看 `_stop_event`、`AIGuard` 看 halted_reason），
  拦住「熔断期间新开仓」的是 `STATE_HALTED` 和 AIGuard，不是租约。别为了拦开仓把整个账户
  锁死——以前 halt 不释放租约，熔断一次自动交易和 AI 量化就再也起不来。
- `start()` 里租约拿到、决策线程却起不来时，state 回 `stopped`、租约放回、记一条
  `start_failed` 审计，异常照旧抛出去。界面显示在跑而实际没有 poller 是最坏的一种状态。
- 下单不改全局市场：`TradingService` 按 `OrderRequest.market_type` 自己解出那个市场的
  客户端（`rest_for`，构造时由 `AppContext.rest_for` 注入，缺省退回 `self.rest`），
  `ctx.market_type` / `settings.market_type` / `config.json` 全程不动。以前下单前会
  `set_market_type` 借一下全局市场（默认还落盘），下一张现货单就把侧栏、图表、设置页一起
  切成现货；后来改成只借不落盘，但并发读设置仍能看到借走那一瞬。别退回那两种做法。
- `account_lock` 在 `TradingService.submit` 上，不是在各引擎里：下单框、`AutoTrader`、
  `AITrader` 都从 `submit` 出去（INV-1），锁放这儿才一次覆盖三路。锁里只放提交那一下
  （读余额持仓、撤旧挂单、下单、挂保护单），模型调用、K 线拉取、`strategy.generate` 一律
  在锁外，否则慢模型会把下单框冻住。并发的第二张单排队等锁，不丢弃也不报错。
- 成交回填 `_reconcile_live_fills` 刻意不拿这把锁：一次同步要对每个市场逐个交易对发签名
  请求，几秒，拿锁等于把下单框挡在轮询后面。账本只有那一个线程写。
- 读持仓失败不等于没有持仓：`_live_held` 读不到就退回本地成交账本
  （`trading._live_ledger()["books"]`），两边都不知道才返回 `None`——平仓和挂保护单报 503
  说实话，`_is_reducing` 返回 False，不冒充减仓去赌交易所的 reduceOnly。以前读失败返回 0，
  卖出不算减仓，熔断或急停期间想平仓都平不掉。

## 8. 策略

- 内置策略在包里：`src/btrader/strategy/builtin/*.py`，跟着程序升级走。用户策略是数据目录
  `%APPDATA%/biTrader/strategies/*.py`（`paths.user_strategies_dir()`）下的普通 `.py` 文件。
  AI 候选在 `strategies/candidates/`，不参与正式注册；批准时才复制进用户目录，并顺手把类属性
  `key = "..."` 改写成最终 key（撞库就带后缀）。策略是本机 Python 代码，权限和本程序相同。
- 加载顺序是先内置、再用户目录。用户目录里的同名 `key` 顶掉内置，这就是「改内置」的实现：
  顶替只在用户目录落盘，包内源码一个字节都不动。两个用户文件用同一个 key 仍然算冲突，
  照旧报「策略 key 重复」并只留先加载的那个。
- 覆盖状态由 registry 现算，不要自己再记一份：`is_builtin(key)` 是「包里有同名实现」（覆盖之后
  仍为真），`is_overridden(key)` 是「现在跑的是用户目录里那份」。`GET /api/strategies` 里的
  `builtin` / `overridden` 就是这两位。`editable` 现在恒为 true（内置也能改），别拿它判断来源。
- 还原是 `POST /api/strategies/{key}/restore`：删掉覆盖文件，内置原版回来。不是覆盖版本返回 400
  「这个策略不是覆盖版本」；一个文件里定义了多条策略时拒绝，要先把文件拆开；删完还有别的文件
  占着这个 key 也返回 400，而不是假装成功。
- 覆盖中的内置不能改名（`POST /api/strategies/{key}/rename` 返回 403）：改名会把内置留在旧 key
  上。要换名字先还原，或者「复制为新策略」。只有纯自定义策略才走「写新文件 → 热加载 → 删旧文件」。
- 删除是 `DELETE /api/strategies/{key}`：纯自定义策略删文件，**覆盖版本删掉就等于还原**（内置原版回来），
  纯内置 400 并指向 restore。和 restore 同一口径：一个文件里还有别的策略就 400 让先拆开，
  不猜作者的文件布局、也不精确改写文件。删完热加载，别的策略不受影响；
  引擎正在跑这条策略时同样 409（`routes._strategy_in_use`）。
- 保存（`PUT /api/strategies/{key}/source`）和还原在引擎正在跑这条策略时返回 409，自动交易和
  AI 交易都算（`routes._strategy_in_use`）。引擎握着的是旧实例，换了文件也要重启引擎才生效，
  别把这个 409 当成多余的检查。
- 保存先过静态检查，写盘后如果加载不通过，会把文件回滚成原样再返回 422。这一步别去掉：坏文件
  留在磁盘上界面上是看不见的（registry 会退回内置），用户只能去数据目录手删。
- 三处入口的分工：侧栏「策略」页（`web/src/pages/StrategiesPage.tsx`）是唯一的源码编辑处；
  回测页的「编辑源码」只跳过去并带上当前策略的 key；策略实验室详情卡的「在策略页编辑」同理，
  带的是候选注册后的 key。三处共用 `components/StrategyEditor.tsx`，`layout="page"` 撑满策略页
  卡片，回测页原来那个源码弹窗已经删了，别再恢复。
- 策略页是两列骨架 `.strategy-page`：列表固定 300px，编辑器吃掉剩下的宽度，宽度上限跟三个 AI 页
  一样走 1400 档。它本来就是两列，所以没有 1360 那一档；断点只有 1024 —— 变单列后代码区改成
  固定 460px 自己滚，CodeMirror 外面那层 `.cm-theme` 的高度要一层层传下去，否则整页被源码撑长。
## 9. 设置的保存与恢复默认

- `config.json` 分段：`risk` / `notify` / `ai` / `ai_auto` / `exec` + 顶层标量 + 两个交易对列表
  `favorites` / `recent_symbols`。`PUT /api/settings` 每个段落都能单独发一个子集，
  只认 dataclass 声明过的字段（多出来的键安静忽略），校验全过才落盘。
- `ai_auto` 是 AI 交易与 AI 量化的运行配置，字段即 `AITraderConfig` 的入参：金额、杠杆、
  决策次数、置信度、决策有效期、漂移闸、冷却、轮询、`require_stop`、`depth`，外加页面
  选过的 `strategy_key` 与 `market_type`（不记的话每次打开都回落到「第一条策略」和全局
  市场）。启动时的优先级是「请求体 > `ai_auto` > 全局」。`confirm_live` 不在这里：那是
  主网下单时当场说一句「这是真钱」，存下来等于把闸掰开。
- `exec` 是执行台最后用过的那三项：`position_pct`（0 到 1 的比例，1 = 全部可用余额，不是
  百分数）、`leverage`（≥ 1）、`poll_seconds`（≥ 1）。默认 1 / 1 / 10，刻意等于组件里原来
  写死的那三个字面值。它们不是风控：真上限在 `risk` 与 `AITrader` 的闸里，改这三项动不了
  任何一条闸。
- `trade` 是下单框按市场记住的杠杆：`{"leverage": {"usdm": 10, "coinm": 5}}`，只存合约
  市场（现货恒 1、不入库，PUT 里写 `"spot"` 直接 400），默认各市场 1。值是 1–125 的
  **整数**（交易所只认整数，1.5 会被币安回 -1102），PUT 按市场合并——只发一个市场时
  另一个不动；校验走和其他数字字段同一套守卫（`_check_number_input`），0 / 126 / 1.5 /
  `"nan"` 各自 400 并点名 `trade.leverage.<市场>`。手改 config.json 写坏的那一项在读盘时
  退回 1（`clean_trade_leverage`），整份配置照样打得开。**真上限仍是 `risk.max_leverage`**：
  下单与预览取 `min(请求值, risk.max_leverage)`，请求体没带 leverage 就用这里记的值
  （`routes.resolve_order_leverage`），被收口时响应里的 `leverage_note` 说清楚是哪一条上限，
  不许静默改数；现货即使传 10 也按 1 下，`POST /api/settings/reset` 认 `trade` 段。
  下单与预览的 `leverage` 声明成 `Any`，在 `resolve_order_leverage` 里先判类型：布尔值直接 400
  ——`float` 字段会把 `true` 悄悄变成 1.0 按 1 倍放行，`leverage: true` 在盘面上不是「1 倍」的意思。
- `favorites` / `recent_symbols`：去重、保序、上限 100（`SYMBOL_LIST_LIMIT`）。PUT 超过上限
  返回 400 并说清给了几个——静默截断会让第 101 个收藏点了没反应；只有手改 `config.json`
  写多了才在读盘时静默截断（整份配置得能打开）。写法按「币/计价」或「币/计价:结算币」判
  （`core.types.symbol_shape_error`），写不成交易对的当场 400；未知交易对**不拦**，换个市场
  之后它可能暂时不在列表里，收藏跨市场，两种写法都留着。
- 数字字段一律只收有限数：`PUT /api/settings` 的 `risk` / `notify` / `ai` / `ai_auto` / `exec` /
  `trade`（杠杆，按 `trade.leverage.<市场>` 点名）在 `setattr` 前后各过一遍 `math.isfinite`
  （`routes._check_number_input` / `_guard_finite`），
  `save()` 用 `json.dump(allow_nan=False)`，读盘每一段都过 `config._coerce_block`。
  以前只有 `type(current)(value)`：`NaN` / `Infinity` 能写进 config.json，之后 `999999 > nan`
  恒为 False，每单上限、持仓上限、决策有效期这些闸全部静默失效；手改出来的 `NaN` 也让整份
  配置再也存不出去。盘上已有的 `NaN` 现在退回默认值并记一条 warning，整份配置照样读得开。
- `POST /api/settings/reset`，body `{"section": "ai_auto"}`，返回重置后的设置。只动点名的
  那一段；`"all"` 也只覆盖 `RESETTABLE_SECTIONS`（risk / notify / ai / ai_auto / exec /
  trade / favorites / recent_symbols）。网络、市场、主题、周期这些顶层设置**不重置**：点一次「恢复
  默认」把用户切好的市场一起换掉，比把参数留着更糟。未知段落 400 并列出可选的段落名。
- `GET /api/tickers?market=&symbols=A,B` 返回 `{items: [{symbol, last, change_pct,
  quote_volume}]}`，`change_pct` 是百分数（1.23 = +1.23%）。一次最多 50 个（超了 400），
  写法与 `/api/ticker` 同一口径（`_check_symbol_shape`：现货不许带结算币、合约必须带），
  所以写错当场 400、不花一次交易所请求。读不到的字段是 `null`，**不是 0**：0 是「恰好不涨
  不跌」，`float(x or 0.0)` 会把这两件事压成一个看不出来的 0.00%。归一在
  `rest.ticker_snapshots`（t62）；客户端没有这个方法就 503 并说清要升级什么，不去猜字段。

## 10. 域名与 HTTPS（只在 Linux 服务器上）

- 三个接口在 `web/routes_deploy.py`：`GET /api/deploy/info`、`POST /api/deploy/check`、
  `POST /api/deploy/tls`。非 Linux 一律 501 并说清，不假装——Windows 上没有 `/etc/caddy`、
  没有 systemd，把 9 个字段填成 null 只会让人以为证书正在签。
- 写 `/etc/caddy/Caddyfile` 与 reload caddy 要 root，而服务是 `bitrader` 用户。所以仓库里有
  `deploy/set-domain.sh`，install.sh 把它以 **root:root 0755 装到 `/usr/local/lib/bitrader/`**，
  并在 `/etc/sudoers.d/bitrader` 只授这一条 NOPASSWD。别把脚本放回 `/opt/bitrader/`：那个目录
  属主就是 bitrader，改得动的脚本 + NOPASSWD sudo 等于把 root 送出去。
- Python 侧只有 `subprocess.run(["sudo", "-n", 脚本, 域名])`：列表参数、不开 shell，域名进不了
  shell。sudoers 没法表达「只允许一个合法域名参数」，所以脚本自己校验参数个数与写法，
  `caddy validate` 不过就还原上一份 Caddyfile。
- `deploy/bitrader.service` 里 `NoNewPrivileges=no` + `ProtectSystem=yes` 是**刻意**的（原来是
  yes / full）：NNP=yes 会让 sudo 直接失败；ProtectSystem=full 把 `/etc` 也挂成只读，root 子进程
  继承同一套挂载，一样写不进 Caddyfile。让步边界就是这一个脚本、这一个文件。
- 读不到的字段一律 null / false 并说清来源：公网 IP 先读数据目录的 `deploy.json`，再读 Caddyfile
  里的 IP 跳转段，最后才问 `https://api.ipify.org`（3 秒超时，失败给 `public_ip: null` 加
  `public_ip_error`）；证书到期时间读
  `/var/lib/caddy/.local/share/caddy/certificates/**/*.crt`（ISO-8601，读不到就是 null）。
- 失败时把 `journalctl -u caddy -n 20 --no-pager`（或 `/var/log/caddy/*.log` 尾部）的**原文**放进
  `log_tail`，脚本输出原样放 `script_output`。**不许自己写一句「可能是 DNS 没生效」冒充原因**：
  接口只说看到了什么（退出码、最后一次探测的错误），原因以 Caddy 日志为准。
- 域名校验两层（`routes_deploy.normalize_domain` 与脚本里的正则）都要留：一层给界面即时反馈，
  一层是 root 脚本自己的保险。IP 直接拒——ACME 不给 IP 发证书，文案里要说清楚。
- 证书由 Caddy 自己走 ACME（HTTP-01）并自动续期；程序不碰证书文件，只读到期时间。


## 11. 在线更新（手动点，不自动更）

- 逻辑在 `core/updater.py`，四个接口在 `web/routes_update.py`：`GET /api/update/info`
  （本地与 GitHub 的差距）、`POST /api/update/apply`（一键更新，返回 `job_id`，后台线程跑）、
  `GET /api/update/status`（进度，重启之后照样读得到）、`POST /api/update/rollback`
  （body `{"confirm": true}`，回到更新前那个提交）。界面上「立即更新」只在落后时能点。
- 仓库里一个 tag 都没有，版本一律按提交 SHA 比，不写版本号比较。远端优先问 GitHub API
  （公开仓库免 token，还能拿到提交标题），失败回落 `git ls-remote`（只读到 SHA，`commits`
  给空数组，`source` 里写明没有标题）。两条都失败时 `remote_sha` 是 null、`error` 是
  原始原因——**这时界面一个字都不许说「已是最新」**。只有「读不到远端」和「数不出差距」才写
  `error`：降级（GitHub 挂了改用 ls-remote、fetch 没成功）不算错，写进去只会把更新按钮
  白白变灰。
- `apply` 的前置检查，任一不过直接返回、**不建任务、不写状态文件**：自动交易（含执行台部署）
  或 AI 交易（含 AI 量化）在跑 → 409；工作树有没提交的改动（`git status --porcelain` 非空）
  → 400；`behind_by == 0` → 400；本地和远端各有新提交（分叉）→ 400 并说清要手动处理。
  探针表是 `routes.RUNNING_ENGINE_PROBES`，由 `build_router` 填一次——控制器建在那儿，
  路由之间没有共享对象，别在 routes_update 里另建一套判断。表里只有这两项：**回测、参数优化
  在跑不拦**（它们跑完就结束，重启丢的是一次结果），文档要如实这么写，别让人以为拦了。
- 流水线：`git fetch --all --prune` → 记 `from_sha` → `git merge --ff-only <remote>/<branch>`
  → `pip install -r requirements.txt`（用 `sys.executable`）→ 只有本机有 npm 且 `web/`
  这次真的有改动才 `npm run build`（服务器没 npm，跳过并在该步 output 写明「服务器不需要
  构建，产物跟着仓库走」）→ 重启。每步记 `name/ok/output/seconds`，任一步失败立刻停、
  `state=failed`、`error` 是那一步的**原始输出**，不替用户编原因。
- 重启：Linux `subprocess.run(["sudo", "-n", "systemctl", "--no-block", "restart", "bitrader"])`
  （列表参数、不开 shell，仓库里装着这一条 NOPASSWD，sudoers 里那一行与这条命令**逐字一致**，
  `--no-block` 也算在内——差一个参数就是 `a password is required`，而那一步看起来仍像成功）；
  Windows 起一个分离的垫片，等本进程放开端口后再拉起 `.venv\Scripts\python.exe run_web.py`，
  然后走 uvicorn 的 `should_exit` 优雅退出。端口跟着真实进程走，见下面那条，别在代码或文档里写死。
  两条启动路径（`python -m btrader.web` 与仓库根的 `run_web.py`）共用
  `web/__main__.build_server` + `serve`，装配与 `updater.set_shutdown_hook` 都在里面；
  谁要再写第三条启动路径，也得走它——自己拼一套 `uvicorn.Config/Server`，
  「更新完重启时优雅退出」这个机制在真实进程里就等于不存在（t86 之前 `run_web.py`
  正是这样，只剩 SIGINT 兜底）。
  **不许 `os._exit`**，Windows 上也不许 `os.kill(os.getpid(), SIGTERM)`——那在 Windows 上是
  TerminateProcess，硬切会把状态文件写坏。
- 重启必须**确认真的起来了**，不许「拉起了就算成功」（t84 修的）。Windows 的垫片只做两件事：
  等端口放开、拉起 `run_web.py`；日志每次一个唯一文件
  （`<数据目录>/logs/restart-<时间>-<随机>.log`），由父进程开好当垫片的 stdout / stderr——
  以前它自己 `open(<仓库>/.run_out.log)`，那个文件正被活着的进程写着（Windows 锁成只读），
  open 就 `PermissionError`；stderr 又给了 DEVNULL，退出码没人看，于是「垫片死了」被当成
  「重启成功」，服务就这么停死了。老进程这边等两段：先等自己放开端口（`RELEASE_TIMEOUT`），
  再等新进程绑上端口（`LISTEN_TIMEOUT`），任何一段超时都算失败，`error` 里带垫片日志路径和
  手动命令（`_manual_hint`）。Linux 那条只看 `sudo -n systemctl --no-block restart` 的返回码
  （非 0 即失败，stdout / stderr 原文照贴）——它**立刻返回**，真正的确认在新进程里做。
- 为什么 Linux 不能在老进程里等重启（t88 的结论，别再改回去）：默认的 `systemctl restart` 要等本
  单元停完才返回，而本单元就是发命令的那个进程——老进程等 systemctl、systemctl 等老进程退出，
  互锁到 `TimeoutStopSec=20` 被 SIGKILL 硬切；而流水线前面几步最坏能跑 2000 秒以上（pip、构建、
  git 都可能慢），在老进程里等重启等于自杀，还会把「重启真的成功了吗」这个判断一起带走。所以改用
  `--no-block`：把重启交给 systemd 就返回，确认挪到新进程——新进程读到「重启中」时自己打一次
  `/api/health`，2xx 才把这一步收成 `done`，超期收成 `failed` 并说清等到什么时候。
- 端口通了不等于服务起来了：只 `connect_ex` 会被「绑了 0.3 秒就关」的进程骗过（端口探测刚通过、
  进程已经死了），所以判据是 `/api/health` 回 2xx。端口探测只用来决定「可以开始问 health 了」。
- 端口跟着真实进程走，不写死：`web/__main__.build_server` 把 `--port` 交给
  `updater.set_web_port()`，垫片与端口探测都用 `web_port()`；拿不到时按 8765 兜底，并在那一步的
  输出里写明「按兜底值算」（t84 之前写死 8765：在 8799 上跑的实例，更新完重启必然
  `Errno 10048` 端口被占，重启步看起来成功、其实没起来）。
- 更新线程**不是 daemon**（`updater._spawn`）：Windows 那条重启要等「自己放开端口 + 新进程绑上
  端口」两段确认，daemon 线程会在 uvicorn 主线程退出时被直接掐掉，「到底起没起来」就没人知道，
  状态又变成一句谎。代价是退出时得等这一轮更新跑完；每一步都有 timeout，不会无限挂着。
- 一次只跑一个更新任务，锁是两层：`JOB_LOCK` 管本进程（`start_job` 里先查 `running_job()`，
  撞上抛 `JobBusy`，两个 POST 把它翻成 409「上一个更新还在跑」）；进程内锁管不了多 worker、
  也管不了两个实例共享同一个数据目录，所以外面还有一层文件锁 `<数据目录>/update.lock`
  （`updater.acquire_update_lock`：`os.open(O_CREAT|O_EXCL)` 建文件，建不出来就是别人握着，
  接口翻成 409。**不用 `flock`**：Windows 上根本没有 `fcntl`，两套机制只会让一台机器能用、
  另一台静默失效。锁文件里写 `pid` / `job_id` / `at`，pid 已经不在了的残留锁算没有、可以接管——
  否则一次断电就把更新永久锁死）。只有进程内锁时，三个进程共用一份数据目录，实测三轮全部
  3/3 都建成了任务，还有一轮 `os.replace` 撞 WinError 5、状态根本没落盘（t88 评审）。
  `update-state.json` 的临时文件名带 pid 与随机后缀：共用一个 `.tmp` 时两个写者会互相把半个
  JSON 换上去。
- 状态落盘在 `<数据目录>/update-state.json`（临时文件 + `os.replace`，原子写）。重启前先把
  `state=running` + `restart_pending` + 端口落到盘上；新进程读到「重启中、记下那个 pid 已经不在了」
  时先自己确认（`/api/health` 2xx 才算重启成），成了就把状态收成 `done` 并补上「重启服务」这一步，
  超期收成 `failed`；否则前端刷新后看到一个永远转圈的 running。
  收尾只认**记下的那个 pid**（`_heal_state`）：那个 pid 还活着就一个字都不改——以前任何别的
  进程读一次状态就把活任务收成 done，还补一条根本没发生过的重启步骤；`restart_pending` 为假而
  pid 不在了，说明上次断在半路，收成 failed 并说清断在哪一步。
- 回滚不改写历史：`git revert --no-edit <from_sha>..HEAD` 把内容退回更新前那个提交，
  只允许在 `state == done` 且有 `from_sha` 时调用，同样走流水线 + 状态 + 重启。
  `from_sha` 还必须像提交 SHA（`updater.is_commit_sha`，`^[0-9a-f]{7,40}$`），不合法 400，
  `git revert` 之前再校验一次——它会被拼进命令行。
- 测试全离线：`tests/test_update.py`（信息收集、五种前置、失败停在那一格、状态收尾、两条重启分支）
  + `tests/test_update_hardening.py`（垫片日志写不了不能当成功、端口没人听要报 failed、
  `JobBusy` 409、外部 pid 不许收尾、`from_sha` 校验、非 daemon 线程）。
- 桌面端不查仓库，只查 Releases（t94 起）：桌面版是 PyInstaller 冻结的 exe，里面没有 `.git`、
  没有 `.venv`、没有 npm，上面那条流水线一步都做不了。所以 `updater.is_desktop_build()`
  （`sys.frozen` 为真，或壳的 `--parent-pid` / `--watch-stdin` 通过 `set_desktop_shell`
  喂进来；`--port 0` **不算**壳——那是 `python -m btrader.web` 的默认值，只说明端口是系统临时分的，
  它单独走 `set_ephemeral_port`，只管 apply / rollback 的拒绝文案）成立时，
  `collect_info` 第一行就 return `desktop_update_info()`：`/api/update/info` 回
  `supported: false` + `desktop` 块（`current` / `latest` / `tag` / `newer` / `download_url` /
  `published_at` / `notes` / `fetched_at` / `cached` / `error`），
  只打一次 `GET /repos/xiaofengxfc/python_jy/releases/latest`（10 秒超时、成功缓存 300 秒），
  **一个 git 子进程都不起**。版本按三元组比（`0.6.10 > 0.6.9`），tag 读不懂就 `newer: null` 并说
  「读不懂」——不许当成最新。界面据此换成桌面卡：三态（有新版给下载链接 / 已最新 / 查不到带
  「重新检查」打 `?refresh=1`），仓库版那三个按钮和「正在读本地与 GitHub 的提交…」一个都不出现。
  桌面端只比对、不下载不改文件——没签名，静默升级会被 SmartScreen 拦。
- 发版走 `packaging/release.py --version X.Y.Z [--build]`：三处版本号（`pyproject.toml`、
  `electron/package.json`、`src/btrader/__init__.py`）必须一致、工作树必须干净、`v<版本>` 这个 tag
  必须还没打过，任一不满足直接退出且什么都不做。顺序是打 tag → 推 tag → 建 Release → 上传
  `dist-electron/biTrader-Setup-<版本>.exe`（流式，1 MiB 一块）→ 打印 `html_url`。token 取
  `GITHUB_TOKEN`，没有就 `git credential fill`，**任何情况下不进日志**。桌面端靠 Release 判断有没有
  新版，所以每次发版**必须把安装包传上去**；Linux 服务器版按提交 SHA 比，不发 Release 也能更新。
  安装包 140+ MB，不进仓库（`dist-electron/` 在 `.gitignore` 里）。细节见 `docs/RELEASE.md`。
- 任何子进程都不许在桌面上开控制台窗口（t96 起）。Windows 上一条铁律：**`DETACHED_PROCESS` 不等于无窗口**——它只是不继承父进程的控制台，console 子进程照样会拿到自己的控制台（实测 `GetConsoleWindow()` 非 0，量测也是这样看到黑框的）。要真正无窗口只有 `CREATE_NO_WINDOW`（0x08000000）。所以：`updater._no_window()` 统一给 `git` / `pip` / 重启垫片两层用；垫片里起 `run_web.py` 是 `CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP`，数值由常量 `hex()` 拼进垫片源码，别写死两份；`electron/main.cjs` 靠 `windowsHide: true`（libuv 自己置 `CREATE_NO_WINDOW`，Node 的 spawn **没有** `creationflags` 这个选项，写了也不生效）；开发机用 `pythonw.exe` 起服务。
  加新的起进程点时照这个来，并在真机上验「可见控制台窗口前后差集为空」——只数 `conhost` 或 `ConsoleWindowClass` 会假阴性（Windows 11 的终端宿主是 `CASCADIA_HOSTING_WINDOW_CLASS`）。
  记住代价：`.venv\Scripts\pythonw.exe` 是 GUI 子系统，没有控制台，`biTrader-backend.exe` 仍是 console 子系统（双击它必开黑窗，只给壳用）。
