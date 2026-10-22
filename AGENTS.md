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
Start-Process -FilePath ".venv\Scripts\python.exe" `
  -ArgumentList "run_web.py","--host","127.0.0.1","--port","8765" `
  -WorkingDirectory (Get-Location) -WindowStyle Hidden
```

Node 不在 PATH 时，先把 Node 目录加进去。

## 2. 测试

```powershell
.venv\Scripts\python.exe -m pytest -q
```

提交前必须全绿。测试不访问网络：`tests/conftest.py` 把 `notify._post_json` 换成
一个直接报错的替身，暂存文件也指向临时目录。要验证发送的测试自己替换它。

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
- 权益基线存在 `account.baseline.v1:<network>:<market>`，只有全钱包读会写。
  单交易对读只继承。如果让单交易对读也写，第一次读的那一对会把偏小的金额
  冻成「起始资金」。
- 现货没有 reduce-only 触发单。止损、止盈、条件单只在合约市场挂到交易所侧。
  现货请求会返回明确文案：`PUT /api/positions/protection` 和
  `POST /api/orders/conditional`。
- 保护单接口是重挂语义。先调 `cancel_protective_orders`，只撤该交易对的止损止盈，
  不会撤掉挂着的限价单。条件单的类型由触发价和现价的相对位置决定，不要自己猜。
- SQLite 里 `mode='paper'` 的旧成交还在库里，界面只显示 `live` 的成交。

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

- 密钥在系统密钥环，service 叫 `biTrader-ai`，不进 `config.json`。
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
- 输出额度是两个常量，都在 `llm.py`：`OUTPUT_TOKENS`（4000）给问答、分析、简报这类普通回答；
  `OUTPUT_TOKENS_CODE`（8000）给写代码这类长回答，策略实验室生成一个完整模块走它。
  推理模型先把额度花在思考上，写策略的额度给小了，JSON 会从中间断掉。
  改额度只动这两个常量，别在调用点写数字。`routes_lab` 曾经自己写了 2400，比默认值还小，
  长策略被截成坏 JSON，报出来的是「没有可解析的 JSON」，文案还让用户去设置里调一个不存在的项。
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
