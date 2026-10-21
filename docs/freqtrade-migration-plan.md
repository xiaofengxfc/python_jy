# biTrader 组件迁移计划（Freqtrade + 成熟开源组件）

> 结论先说：自动交易/回测整体迁移到 **Freqtrade 2026.9**（独立进程 + REST/WS），
> 策略编辑用 **CodeMirror 6**，K 线图用 **KLineChart**，AI 分析正文用
> **react-markdown** 渲染。biTrader 继续负责手动交易、纸面账户、统一风控、
> AI 助手和企业微信通知。Freqtrade 以**独立进程**运行，不把 GPL 代码复制进
> 本仓库，避免许可证污染。

## 1. 现有组件盘点与成熟项目对照

| biTrader 组件 | 现状 | 成熟开源项目 | 许可证 | 迁移方式 |
| --- | --- | --- | --- | --- |
| 自动交易引擎 | `engine/auto.py` 自研轮询 + 调仓 | **Freqtrade** 55.7k★ / Jesse 8.7k★ / Hummingbot 20.6k★ | GPL-3.0 / MIT / Apache-2.0 | Freqtrade 独立进程，biTrader 通过 REST/WS 控制 |
| 回测引擎 | `backtest/engine.py` 自研事件驱动 | **Freqtrade Backtesting/Hyperopt** / VectorBT / Backtrader | GPL-3.0 / Apache-2.0 / GPL-3.0 | 用 Freqtrade `webserver` REST 或 CLI，结果映射到现有页面 |
| 策略框架 | `strategy/base.py` 自定义 `Strategy.generate()` | Freqtrade `IStrategy` | GPL-3.0 | 新策略统一写 Freqtrade 格式；旧策略保留只读兼容 |
| 策略编辑 | 仅有只读代码预览 | **CodeMirror 6** 7.8k★ / Monaco 46.9k★ | MIT / MIT | CodeMirror 6 + Python 语法高亮，保存后重载 |
| K 线图 | `Chart.tsx` 72KB 自绘 Canvas | **KLineChart** 4.2k★ / TradingView Lightweight Charts 17.6k★ | Apache-2.0 / Apache-2.0 | KLineChart 替换自绘图表（内置指标/画线） |
| AI 分析正文 | 纯文本 `white-space: pre-wrap` | **react-markdown** 15.9k★ + rehype-sanitize | MIT | Markdown 渲染、代码块、表格 |
| 表格 | 自研 `Table.tsx` | **TanStack Table** 28.5k★ | MIT | 需要排序/筛选/虚拟滚动时替换 |
| 权益曲线 | 自研 `LineChart.tsx` | Lightweight Charts / ECharts | Apache-2.0 | 回测结果页替换 |
| 表单/校验 | 手写 state | react-hook-form + zod | MIT | 设置页与回测参数页逐步替换 |
| 状态/请求 | 手写 fetch + useState | TanStack Query | MIT | 自动交易轮询与回测任务缓存 |
| 桌面壳 | Electron | Electron | MIT | 保持 |
| 企业微信通知 | 自研 OpenClaw 网关 | — | — | 保持 |

### 选型理由

- **Freqtrade**：加密交易领域最成熟的开源机器人，原生支持 Binance/Gate/OKX 等，
  自带 dry-run、实盘、回测、Hyperopt、REST API、WebSocket、策略热加载。
  它的策略模型（`populate_indicators` / `populate_entry_trend` /
  `populate_exit_trend`）正好覆盖“策略出信号 + AI 复核”的需求。
- **Jesse**（MIT）是许可证更宽松的备选，但生态、REST API、回测报告不如
  Freqtrade 完整；如果未来必须闭源分发，再评估切换。
- **Hummingbot** 偏做市/高频，不适合当前“K 线策略 + 回测”的产品形态。
- **NautilusTrader** 偏机构级低延迟，接入成本高。
- **KLineChart** 内置 MA/EMA/BOLL/MACD/RSI/KDJ 等指标和画线工具，能直接替掉
  当前 72KB 的自绘图表；Lightweight Charts 更轻但指标要自己算。
- **CodeMirror 6** 比 Monaco 体积小、无需 CDN、离线桌面可用，适合策略编辑。

## 2. 目标架构

```
React 前端
├─ /api/*            biTrader FastAPI（手动交易、资产、AI 分析、风控、通知）
└─ /api/ft/*         Freqtrade 代理（新增）
      │
      ├─ freqtrade trade       127.0.0.1:18081  REST + WS（自动交易、持仓、日志）
      └─ freqtrade webserver   127.0.0.1:18082  REST（回测、Hyperopt、历史结果）
            │
            └─ user_data/
                 ├─ strategies/     策略源码（编辑器写入）
                 ├─ data/<exchange>/ 本地 K 线（由 biTrader 导出）
                 └─ backtest_results/
```

### 进程与安全

- Freqtrade 使用**独立 venv**（`%APPDATA%\biTrader\freqtrade-venv`），不污染
  biTrader 的 `.venv`，也避免 GPL 代码进入本仓库。
- 只监听 `127.0.0.1`；用户名/密码/JWT secret 由 biTrader 首次启动时随机生成，
  保存在 `%APPDATA%\biTrader\config.json` 的 `freqtrade` 段，不发给浏览器。
- 前端只调用 biTrader 的 `/api/ft/*`，由后端代理登录、刷新 token、转发 WS；
  浏览器永远拿不到 Freqtrade 的密码。
- 自动交易与手动交易共用同一交易所账户时，**同一时刻只允许一个写单主体**：
  Freqtrade 运行时，biTrader 手动下单页面提示“自动交易占用中”，或由
  `TradeLease` 统一协调。

## 3. 数据与策略

### 3.1 数据桥

本机 `api.binance.com` / `fapi.binance.com` 不可达，Gate 可达。Freqtrade 支持
`gate` 交易所，因此：

- 回测数据：从 biTrader 的 `KlineStore`（已带 Gate 永续回落）导出
  `user_data/data/<exchange>/<PAIR>-<tf>.json`，格式为
  `[[timestamp_ms, open, high, low, close, volume], ...]`。
- 实盘/纸面：Freqtrade 配置的交易所由设置页选择 `binance` 或 `gate`；
  默认按设置里的代理与网络可达性选择，Binance 不通时提示改用 Gate 或配置代理。
- 交易对映射：Freqtrade 用 `BTC/USDT`，文件名用 `BTC_USDT-1h.json`。

### 3.2 策略格式

新策略统一为 Freqtrade `IStrategy`：

```python
from freqtrade.strategy import IStrategy

class MyStrategy(IStrategy):
    timeframe = "1h"
    minimal_roi = {"0": 0.1}
    stoploss = -0.05
    startup_candle_count = 30

    def populate_indicators(self, dataframe, metadata): ...
    def populate_entry_trend(self, dataframe, metadata): ...
    def populate_exit_trend(self, dataframe, metadata): ...
```

策略编辑器保存前做 AST 检查：禁止 `os/sys/subprocess/socket/requests/urllib/
eval/exec/open` 等；只允许 `freqtrade.*`、`pandas`、`numpy`、`talib` 等白名单。
保存后调用 `freqtrade list-strategies` 或重启 `webserver` 完成热加载。

## 4. AI 自主交易拆分

### 4.1 AI 自主交易（策略出信号 + AI 复核）

- Freqtrade 运行选定策略，策略产生 `enter_long/enter_short` 信号。
- 策略基类 `AIConfirmStrategy` 在 `confirm_trade_entry` 中调用 biTrader 本地
  `POST /api/ai/confirm-entry`：
  - 入参：策略名、交易对、方向、当前价、指标摘要、账户与风控快照。
  - 出参：`{"approve": bool, "stake_mult": 0.0-1.0, "reason": "..."}`。
- AI **只能否决或缩小仓位，不能凭空开仓**；策略没有信号时 AI 无机会下单。
- AI 调用失败/超时：默认 **拒绝新开仓**（fail-closed），已有持仓的止损/止盈
  仍由 Freqtrade 执行，不受影响。
- 所有确认写入 `logs/ai-confirm-audit.jsonl`。

### 4.2 AI 自主量化（AI 生成/调参 → 回测 → 自动执行）

- AI 生成 Freqtrade 策略代码（或对现有策略参数做多轮优化）。
- 静态检查 → Freqtrade 回测 → 展示收益/回撤/夏普/成交数。
- 人工点击“批准并部署”后写入 `user_data/strategies/`，再启动 Freqtrade 自动交易。
- **绝不自动把候选策略上线实盘**；实盘仍需 `live_enabled + confirm_live + 主网 + 未急停`。
- AI 量化不逐笔调用模型，交易可复现；模型只参与策略生成/调参阶段。

### 4.3 互斥

`TradeLease` 扩展为三主体：`auto`（Freqtrade 策略自动）、`ai_trade`（AI 复核）、
`ai_quant`（AI 量化）。同一时刻只有一个持有租约；急停会停止全部三者和
Freqtrade 进程。

## 5. 分阶段实施

| 阶段 | 内容 | 验收 | 预估 |
| --- | --- | --- | --- |
| P0 | 调研、选型、Freqtrade 独立 venv 安装与最小回测验证 | `freqtrade backtesting` 用本地数据跑通；REST `/ping`、`/token/login`、WS 订阅跑通 | 0.5 天（已完成验证） |
| P1 | UI 快修：AI 分析内嵌到底部 Tab；下单区底部按钮不再被裁切；AI 文本 Markdown 渲染 | 1680×1000 与 1366×768 截图无遮挡；买入按钮完整可见 | 1 天 |
| P2 | 策略编辑器：源码读取/新建/保存/校验/热加载；回测页与实验室复用 | 修改一个用户策略后，回测页下拉立即出现新参数；非法代码被拒 | 2–3 天 |
| P3 | Freqtrade 交易进程：`FreqtradeSupervisor` + `/api/ft/*` 代理 + WS 代理；自动交易页改为 Freqtrade 状态/持仓/启停/平仓 | 纸面启动、停止、强制平仓、日志流、持仓与收益正确 | 3–4 天 |
| P4 | Freqtrade 回测：数据桥 + `/api/v1/backtest` + 历史结果；回测页替换 | 单次回测、参数优化、历史记录三 Tab 全走 Freqtrade | 3–4 天 |
| P5 | AI 拆分：`AIConfirmStrategy` + `/api/ai/confirm-entry`；AI 量化生成/审批/部署 | AI 只能否决策略信号；AI 量化候选不自动上线 | 2–3 天 |
| P6 | K 线图替换为 KLineChart；表格/Markdown/CodeMirror 收尾 | 指标、画线、AI 价位叠加与现有功能对齐 | 2–3 天 |

## 6. 兼容与回滚

- 旧接口 `/api/auto/*`、`/api/ai/trader/*`、`/api/backtest/*` **保留**，
  前端在 Freqtrade 不可用时自动回退到旧引擎；设置页显示当前引擎。
- 迁移期间两套引擎不共享同一账户写权限；Freqtrade 未启动时旧引擎可用，
  Freqtrade 启动后旧自动交易按钮禁用。
- 所有新表/文件都在 `%APPDATA%\biTrader\freqtrade\` 下，删除该目录即可回滚。
- 每次迁移阶段结束跑全量 `pytest` 与 `npm run build`，并截屏关键页面。

## 7. 风险与待确认

1. **打包分发**：Freqtrade 需要独立 Python 环境；当前 PyInstaller 后端 exe
   无法自己创建 venv。开发模式可直接用本机 Python；正式安装包需要
   “首次运行安装 Freqtrade”或随包附带 Python。需要确认是否接受安装时联网下载。
2. **GPL-3.0**：Freqtrade 只以独立进程提供 HTTP/WS，不复制源码；若未来要闭源
   分发，需法务确认。
3. **币安网络**：本机 Binance 不可达，回测用 Gate 本地数据；实盘/纸面实时交易
   需要代理或切换 Gate。页面会明确显示当前数据源。
4. **双账户/双数据库**：Freqtrade 有自己的 SQLite；自动交易持仓/收益从 Freqtrade
   REST 读，手动交易仍读 biTrader。资产页需要合并展示时，以“自动/手动”分组。
5. **策略不兼容**：旧 `btrader.strategy.Strategy` 不能直接在 Freqtrade 运行；
   内置策略需要改写为 Freqtrade 模板，旧策略保留只读回测兼容。

## 8. 先做的无争议项

1. AI 分析从悬浮窗改为交易页底部「AI 分析」Tab，正文宽度随面板，Markdown 渲染。
2. 下单区取消固定视口高度裁切，订单簿固定高度、下单卡片按内容展开，买入按钮
   永远在文档流内。
3. 策略源码 API + 编辑器（P2），这是回测/Freqtrade 迁移的前置能力。

## 9. 本轮已完成（2026-10-21）

- `web/src/components/BottomTabs.tsx`：新增 `AI 分析` Tab；`aiPanel` 由交易页注入，
  不再使用悬浮窗；面板高度随 Tab 扩展，正文可滚动。
- `web/src/pages/TradingPage.tsx`：移除 `FloatingWindow`；图工具栏「AI 分析」按钮
  改为跳转底部 Tab；AI 价位仍以可编辑画线叠加到图上。
- `web/src/components/MarkdownText.tsx` + `react-markdown` + `remark-gfm`：
  AI 分析/新闻/持仓复核按 Markdown 渲染，代码块、列表、表格可读。
- `web/src/styles.css`：`.trading` 取消固定视口高度；`.trading-rail` 不再
  `overflow:hidden`；订单簿固定 232px，下单卡片按内容展开，页面滚动；AI Tab
  打开时图表让出高度。
- `src/btrader/strategy/source.py` + `src/btrader/web/routes_strategy.py`：
  用户策略源码读取/校验/新建/保存/热加载；内置策略只读，可复制为绝对导入的
  新用户策略；所有写入限制在 `%APPDATA%\biTrader\strategies`。
- `web/src/components/StrategyEditor.tsx`：CodeMirror 6 + Python 语法高亮，
  校验、保存并重载、新建、复制内置策略；回测页「编辑源码」弹窗已接入。
- `tests/test_strategy_source.py`：7 个离线测试，覆盖模板、保存、更新、非法导入、
  key 不一致、内置只读、内置复制。
- `src/btrader/freqtrade.py`：`FreqtradeBridge` + `FreqtradeClient`。负责独立
  venv 检测、配置生成、凭证生成、`freqtrade trade` 子进程启停、REST 登录/代理、
  策略文件读取/保存、CLI 回测与结果映射。
- `src/btrader/web/routes_freqtrade.py`：`/api/ft/status|start|stop|strategies|
  profit|trades|forceexit|backtest`。
- `web/src/components/FreqtradeCard.tsx`：交易页自动交易区新增 Freqtrade 引擎卡片，
  显示安装/运行状态、交易所/端口/交易对、策略选择、启动纸面/停止/日志。
- 本机验证：Freqtrade 2026.9 + Gate 纸面进程可启动/停止；`/api/ft/start`、
  `/api/ft/profit`、`/api/ft/stop` 全链路通过；CLI 回测经 bridge 映射为现有
  `BacktestResult`（示例策略 15 笔、收益/回撤/夏普正常）。
- 全量 `pytest` 通过；`npm run build` 通过。
- 数据桥：`FreqtradeBridge.write_ohlcv()` + `POST /api/ft/data`，回测页新增
  `Freqtrade` 标签：策略/交易对/周期/时间范围 → 同步 K 线 → CLI 回测 →
  指标、权益曲线、成交明细（`web/src/components/FreqtradeBacktestPanel.tsx`）。
- AI 拆分：`AITradingPanel` 两个标签：
  - `AI 自主交易`：先由所选策略产出 target，模型只复核/否决方向并决定
    quote；新增 `signal_provider`，反向开仓直接拒绝，空仓信号不调用模型。
  - `AI 自主量化`：候选策略列表 → 代码检查/回测状态 → 人工批准 → 部署为正式
    策略并以纸面模式自动执行（`AIQuantPanel.tsx`）。
- 本机把 Freqtrade venv 复制到 `%APPDATA%\biTrader\freqtrade\venv`，默认路径即可
  识别；仍保留 `freqtrade.python` 覆盖项给开发模式。
- `/ws/ft` 代理：后端用 sidecar 的 JWT 登录 Freqtrade RPC WebSocket，把
  status/entry/exit/protection 原样转给浏览器；token 不进入页面。
- 自动交易页默认引擎切到 `Freqtrade`，旧引擎保留为显式“旧引擎”回退；两套
  引擎启动时互斥，避免同时操作同一账户。
- AI 量化部署加入回测硬门槛：保存候选策略 → 运行 btrader 回测 → 必须至少
  成交且总收益为正 → 记录指标 → 才启动纸面自动交易；否则阻止并显示原因。
- Freqtrade 策略编辑：回测页可直接读取/保存 `user_data/strategies/*.py`，
  保存做 Python 语法和 `IStrategy` 继承检查，重启 sidecar 后生效。

## 10. 下一步（P4）

- 用 Freqtrade 回测结果（而不是 btrader 回测）驱动 AI 量化候选的
  `record_backtest`；策略源码格式需要从 btrader `Strategy` 转到 Freqtrade
  `IStrategy`。
- 自动交易页把 Freqtrade 的 `/profit`、`/status`、`/forceexit` 全部接进
  持仓表（当前已接持仓/平仓，日志和权益曲线仍以 btrader 为主）。
- 安装包/venv 分发：当前开发模式依赖本机 Python；后续做首次运行安装器。

