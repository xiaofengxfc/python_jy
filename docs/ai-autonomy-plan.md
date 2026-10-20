# biTrader AI 自主化改造设计（Phase 2–4）

| 项 | 内容 |
| --- | --- |
| 状态 | 设计稿（本轮只改文档：不改源码/前端、不 commit、不重启服务） |
| 范围 | ①自动交易并入交易页 ②AI 自主交易（纸面+实盘）与「量化」区 ③策略代码生成/多轮回测反思/实时调参 |
| 依据 | 共享任务 `task-6`；用户已确认的优先级 ①–④（①AI 浮窗+画板由其他队友并行，本文不设计） |
| 目标读者 | 实现 Phase 2/3/4 的工程师、review 的 Lead、以及之后接手的会话 |
| 相关代码基线 | `TradingService`(src/btrader/engine/trading.py) / `RiskManager`(risk.py) / `PaperBroker`(paper.py) / `AutoTrader`(auto.py) / `routes.py` / `llm.py` / `ai.py` / `core/config.py` / `strategy/registry.py` / `web/src/pages/*.tsx` |

> 本文所有路径都是仓库相对路径。涉及行号时以当前 HEAD 为准（示例：`routes.py:323`），
> 实现前请重新定位；行号只是"从这里开始读"的提示，不是契约。

---

## 0. 需求 → 文档章节对照

| 用户/任务要求 | 本文位置 |
| --- | --- |
| Phase 2 自动交易合并，验收标准与风险 | §4（含 §3 总览） |
| Phase 3 AI 自主交易 + 量化，验收标准与风险 | §5 |
| Phase 4 策略代码生成 / 多轮反思 / 实时调参，验收标准与风险 | §6 |
| 文件级改动清单、路由、dataclass 字段、前端组件、payload 示例 | §4.3 / §5.7–§5.9 / §6.5–§6.6 |
| 安全与风控不变量（风控不可绕过、限额、熔断、急停、实盘二次确认、失败降级、审计、预算、脱敏） | §5.6（INV-1…INV-15） |
| AI 决策 schema、提示词约束、状态机、线程模型 | §5.2–§5.5 |
| 策略代码沙箱边界、人工审核、reload | §6.1–§6.2 |
| 多轮回测循环的轮数/预算/缓存/失败恢复 | §6.3 |
| 实时调参的边界（已审核参数、限幅、留痕、回滚） | §6.4 |
| README / 文档更新段落 + pytest 离线测试清单（每阶段 ≥8） | §7 / §8 |
| 明确「本轮不做」 | §9 |
| README 硬约束「AI 不下单」如何受控修改（含建议与理由） | §1.3 + §7（裁决） |

---

## 1. 结论摘要（先看这 5 分钟）

1. **Phase 2 是纯前端合并，后端零改动。** 把 `AutoPage.tsx` 拆成 `AutoTraderPanel.tsx` + `useAutoStatus.ts`，挂到交易页底部 Tab「自动」，页头加运行徽章，`AutoPage.tsx` 与 `PageKey "auto"` 在验收通过后的同一 PR 删除。`/api/auto/*` 契约不动。
2. **Phase 3 新增 `src/btrader/engine/ai_trader.py`，AI 决策只允许经 `TradingService.submit()` 这一条出口。** `RiskManager` 仍是唯一权威；AI 专属限额（单笔/日笔数/置信度/冷却）是**前置收紧**，不是替代。`ai.py` 继续只做"提示词 + 解析 + 校验"，不碰交易。
3. **「AI 不下单」这条 README 硬约束不删除，而是分层改写：** 保留「AI 永不进入 `strategy.generate()` / `run_backtest()` 循环」这条**执行内核确定性**不变量；把"不下单"改成"**不直接下单，只经 AI 下单网关**"。裁决与理由见 §7。
4. **Phase 4 的候选策略代码永不自动 import。** AI 只能写 `%APPDATA%\biTrader\strategies\candidates\`；静态检查（AST 白名单/黑名单 + 长度上限）→ 子进程超时回测 → **人工审核（sha256 校验）** → 复制进正式策略目录并 `reload`。静态检查+子进程只是降低风险，**人工审核才是安全边界**；强隔离（AppContainer/Job Object）放 Phase 5。
5. **三个会自动动钱的主体（策略自动、AI 自主、AI 量化）互斥。** 新增 `TradeLease`，同一时刻只允许一个持有；新增一键急停 `POST /api/risk/kill`（目前 `kill_switch` 只能通过 `PUT /api/settings` 改，见 `routes.py:661`）。所有新能力默认关闭；实盘 = 设置级开关 + 启动时 `confirm_live` + 主网 + 未急停，四重条件缺一不下单。

---

## 2. 现状基线（设计依据，读码结论）

### 2.1 下单与风控链路（事实）

- **唯一出口**：`src/btrader/engine/trading.py:313` `TradingService.submit(OrderRequest) -> OrderResult`。
  纸面走 `PaperBroker.place_order`（`paper.py:94`），实盘走 `self.rest.create_order`；两条路都在 `submit` 内先调
  `precheck()`（`trading.py:219`）→ `RiskManager.check_order()`（`risk.py:142`）。
- **风控闸门**（`risk.py:142-180`）：`kill_switch`、`halted_reason`（连续亏损熔断）、当日最大交易次数、
  当日最大亏损、单笔上限（`max_order_quote` 与 `max_order_pct` 取紧）、最大持仓、最大杠杆。
  只拦"开仓"（`entry=True`），**永远不拦 reduce-only 平仓**——这是刻意设计，别改。
- **状态持久化**：`RISK_STATE_KEY = "risk.state.v1"`（`risk.py:19`）；`clear_halt()` 通过移动 `pnl_baseline/trades_baseline` 给"新额度"（`risk.py:236`）。
- **纸面账户**：`PaperBroker` 有 `place_order / mark / check_protections / place_conditional / snapshot / restore`；
  SL/TP guard 存在 `account.protections` 且随快照持久化（`paper.py:47`）。
- **保护单**：纸面由 `PaperBroker` 自己触发；实盘由 `TradingService._arm_live_protection`（`trading.py:459`）
  挂交易所侧 stop/take_profit，失败只返回 `warnings`，**不回滚已成交的开仓**。

### 2.2 自动交易与线程模型（事实）

- `AutoTrader`（`engine/auto.py:53`）线程名 `auto-trader`；`_tick()`（`auto.py:153`）取行情（`use_cache=False`）→
  丢弃未收盘 K 线 → `strategy.generate` → `clamp_target` → `_rebalance()` → `trading.submit`。
  `_live_position_qty()` 返回 `None` 时**跳过本轮**（`auto.py:276`，防"读失败→当成空仓→重复开仓"）。
- `AutoController`（`routes.py:274`）是**单槽**所有者；事件流 `EventLog`（500 条，`routes.py:247`）；
  AI 简报走 `self.briefs = ai.BriefQueue()`（`routes.py:288`），`_on_candle` 只 `submit` 不等待（`routes.py:415`）。
- 回测/优化/AI 过滤走 `JobStore`（`routes.py:453`，内存 40 个）+ 独立 `threading.Thread`，完成后 `_notify_task`。
- 后端进程内还有两条常驻线程（`web/app.py`）：纸面撮合 `paper-reconciler`（`RECONCILE_SECONDS=2`，`app.py:1395`）
  和实盘成交回填（`LIVE_SYNC_SECONDS=5`，`app.py:1401`）。
- 锁：`_State.locks[market]`（ccxt 客户端不可并发，`app.py:64`）、`_State.account_lock`（纸面账户，`app.py:67`）。

### 2.3 AI 能力现状（事实）

- `llm.py`：多供应商（`PROVIDERS`）、密钥环（`KEYRING_SERVICE="biTrader-ai"`）、TTL 缓存（`_Cache`）、
  每日预算（`DEFAULT_DAILY_LIMIT=300`）、审计 `logs/ai-audit.jsonl`（2MB 滚动，`llm.py:276`）。
  **`LLMService.ask()` 永不抛异常**，失败返回 `{"ok": False, "error": ...}`——这是全项目依赖的约定。
- `ai.py`：纯函数。`digest()`（摘要）、`validate_levels()`（价位区间校验）、`entry_window()`（只给开仓前 K 线）、
  `filter_trades()` / `judge_entry()`（回测 AI 过滤）、`trade_metrics` / `compare_runs`、
  `BriefQueue`（`ai.py:810`，独立线程、队列满即丢弃）、`briefing()`。
- 现有 AI 路由：`/api/ai/status|key|test|analyze|news|review|prejudge|audit|describe|suggest` 与
  `/api/backtest/ai-filter`。**没有任何路由能产生订单。**

### 2.4 前端结构（事实）

- `App.tsx:13` `PageKey = "trading" | "portfolio" | "auto" | "backtest" | "settings"`；
  `Sidebar.tsx:73 LABELS` 与 `ICONS` 同步维护。
- `TradingPage.tsx`：`.trading` 两栏 = `.trading-main`（symbol-strip / Chart / BottomTabs）+ `.trading-rail`
  （`AIPanel` / 盘口 / `OrderTicket`）。已有实盘确认 modal（`TradingPage.tsx:456-491`）。
- `BottomTabs.tsx:22` `Tab = "open" | "trades" | "positions" | "news"`，固定高度 + 可折叠。
- `AutoPage.tsx`（438 行）独立页：策略/市场/周期/模式选择、参数、启停、最近信号、运行日志；每 2 秒
  `GET /api/auto/status?since=` 增量拉事件（`AutoPage.tsx:64-77`）；末尾有一行遗留 `export { fmtPrice }`（`AutoPage.tsx:438`）。

### 2.5 现状缺口（编号供后续引用）

| # | 缺口 | 证据 |
| --- | --- | --- |
| G1 | 自动交易与图表割裂；切页丢上下文；且启动会自动改全局市场（`ctx.set_market_type`） | `routes.py:323`、`AutoPage.tsx` 独立页 |
| G2 | 实盘"二次确认"名不副实：只是下拉框 + 红条，`confirm_live: mode === "live"` 恒真 | `AutoPage.tsx:104` |
| G3 | 没有一键急停路由，`kill_switch` 只能整包 `PUT /api/settings` 改 | `routes.py:661` |
| G4 | 没有"AI 产生订单"的模块；`ai.py` 只读 | README「AI 不下单」 |
| G5 | 策略无法热重载；`StrategyRegistry.discover` 只在启动调用一次 | `context.py:82`；`registry.py:29` 无锁 |
| G6 | 没有候选代码目录/审核流；`user_strategies_dir()` 里的文件会被直接 import | `registry.py:67` |
| G7 | 没有多轮"回测→反思→调参"循环；`grid_search` 是穷举，AI 只做单次过滤 | `routes.py:1461`、`backtest/engine.py:409` |
| G8 | 没有参数修订/回滚；`AutoTrader` 的 `config.params` 只读 | `auto.py:176` |
| G9 | 没有 AI 交易审计；`llm` 审计只记 transport | `llm.py:276` |
| G10 | 自动/量化/AI 三个主体未来会并发操作同一账户，没有互斥机制 | 现状只有 `AutoController` 单槽 |

---

## 3. 里程碑总览

| 阶段 | 目标 | 主要交付 | 预估 | 依赖 | 发布门槛（Gate） |
| --- | --- | --- | --- | --- | --- |
| **Phase 2** | 自动交易并入交易页 | 前端重构 + 删除 `AutoPage`；后端 0 改动 | 1–2 人日 | 无 | `npm run build` 通过；手动清单 §4.7 全过；`pytest` 全绿 |
| **Phase 3** | AI 自主交易（纸面+实盘）+「量化」区 | `engine/ai_trader.py`、`engine/ai_quant.py`、`ai.py` 决策函数、设置项、`web/routes_ai.py`、前端 3 个面板、`/api/risk/kill` | 8–12 人日 | Phase 2（页面骨架）| §5.11 全过；新增离线测试 ≥8；实盘默认关闭 |
| **Phase 4** | 策略代码生成 / 多轮回测反思 / 实时调参 | `strategy/sandbox.py`、`strategy/candidates.py`、`ai_optimize.py`、`web/routes_lab.py`、策略实验室页、`AutoTrader.update_params` | 12–18 人日 | Phase 3（量化会话、lease、审计） | §6.8 全过；新增离线测试 ≥12；候选永不自动启用 |
| **Phase 5（不做）** | 强隔离、多交易对、更多数据源 | — | — | — | 见 §9 |

**阶段纪律**：每个 Phase 独立可发布、可回滚。Phase 2 不改 Python，回滚只需还原前端；Phase 3/4 的新能力
全部默认关闭（`enabled=False`），回滚 = 关开关 + `git revert` 一个 PR，不依赖数据迁移
（新增 `settings_kv` 键都带版本号，旧版本读到未知键会忽略）。

---

## 4. Phase 2：自动交易并入交易页

### 4.1 目标 / 非目标

**目标**
- 交易页内完成"看盘 → 配置策略 → 启停自动交易 → 看日志"闭环，不需要切页。
- 复用现有 `/api/auto/*`，后端零改动（接口契约冻结，见 §4.4）。
- 顺手修 G2（实盘二次确认）与 G1 的隐式改市场副作用。

**非目标**
- 不做 AI 自主交易（Phase 3）；不改 `AutoTrader` 引擎；不改自动交易参数含义；
- 不把「自动交易」保留成第二个入口（删除后 Sidebar 只剩 交易/资产/回测/设置 四项）。

### 4.2 信息架构（放在哪、为什么）

```
┌ 交易页 ─────────────────────────────────────────────────────────────┐
│ 页头：交易  现货  [实时]  [纸面|实盘]  [市场…]  ● 自动运行中 [启动/停止] ← 新增徽章+按钮
├──────────────────────────────────────────┬──────────────────────────┤
│ symbol-strip                              │ AIPanel（已有，浮窗化由队友做）│
│ Chart                                     │ 盘口                      │
│                                           │ OrderTicket              │
├──────────────────────────────────────────┤                          │
│ BottomTabs：委托 | 成交 | 持仓 | 自动 | 量化(Phase 3) | 新闻        │
│   └─「自动」= AutoTraderPanel（策略/参数/启停/信号/日志，整宽）      │
└──────────────────────────────────────────┴──────────────────────────┘
```

**决策：底部 Tab，不选右侧栏 Tab。** 理由：
1. rail 宽约 320–360px，`AutoPage` 的表单是 4 列 `form-grid`（市场/交易对/K线周期/运行模式）+ 参数网格，
   塞进 rail 会挤成一列滚动条，且和 `OrderTicket` 抢视觉焦点；
2. 底部 Tab 是**整宽、固定高度、可折叠**（`BottomTabs.tsx:33`），与「委托/成交/持仓」同层，语义就是"执行区"；
3. 右侧栏保留 AI 分析（用户要求①的浮窗化正好由队友处理），自动交易不应占掉 rail。

页头徽章 + 启停按钮放在 `page-head`：自动交易是一个**模式**（在跑/没跑），不是一张表，必须随时可见。

### 4.3 文件级改动清单

| 文件 | 动作 | 内容 |
| --- | --- | --- |
| `web/src/components/AutoTraderPanel.tsx` | **新增**（约 300 行） | 从 `AutoPage.tsx:137-433` 整体搬入：策略/市场/周期/模式选择、`ParamFields`、投入比例、轮询间隔、杠杆、最近信号、运行日志、实盘红条 + **确认 modal** |
| `web/src/hooks/useAutoStatus.ts` | **新增**（约 90 行） | 单一轮询源：`status`、`seq`、`refresh()`、`start(payload)`、`stop()`、`error/notice`；2s 增量拉取（复用 `AutoPage.tsx:64-77` 逻辑）；`document.visibilityState !== "visible"` 时暂停轮询 |
| `web/src/pages/TradingPage.tsx` | 修改 | (a) 引入 `useAutoStatus` + `AutoTraderPanel`，把 `status/config/running` 传给页头与 BottomTabs；(b) 页头加徽章与启停按钮；(c) 删除自动交易面板对 `market/symbol/timeframe` 的重复选择，改为从本页 props 传入 |
| `web/src/components/BottomTabs.tsx` | 修改 | `Tab` 增 `"auto"`；`TABS` 增 `["auto", "自动"]`（`BottomTabs.tsx:22-29`）；新增 prop `auto?: ReactNode`，仅在传入时渲染该 Tab；计数徽标只对 委托/成交/持仓 生效 |
| `web/src/App.tsx` | 修改 | `PageKey` 去掉 `"auto"`（`App.tsx:13`）；删除 `AutoPage` import 与 `{page === "auto"}` 分支（`App.tsx:9,93`） |
| `web/src/components/Sidebar.tsx` | 修改 | `ICONS`/`LABELS` 去掉 `auto` 项（`Sidebar.tsx:40-51,73-79`） |
| `web/src/pages/AutoPage.tsx` | **删除** | 迁移完成后同一 PR 删除；注意它末尾的 `export { fmtPrice }`（`AutoPage.tsx:438`）是遗留，一并清掉 |
| `web/src/styles.css` | 修改 | 追加 `.auto-panel`（`form-grid` 在窄屏折行）、`.auto-feed`（复用现有 `.feed/.feed-row`）、页头 `.auto-badge` 三个小类；不新建样式表 |
| `src/btrader/web/routes.py` | **不改** | `/api/auto/status|start|stop` 与 `AutoStartPayload` 保持原样（契约冻结） |
| `docs/`、`README.md` | Phase 2 完成后改 | 见 §7 |

> 前端构建约定（AGENTS.md §2）：改 `web/src/**` 必须 `cd web; npm run build`，产物写 `src/btrader/web/static`；
> Node 不在 PATH 时先 `$env:Path = "D:\tool\node;" + $env:Path`。

### 4.4 组件契约与接口（后端契约冻结）

`AutoTraderPanel` props：

```ts
interface AutoTraderPanelProps {
  market: string;          // 来自 TradingPage，不再本地选择
  symbol: string;
  timeframe: string;
  running: boolean;        // 来自 useAutoStatus
  busy: boolean;
  onStart: (payload: AutoStartPayload) => Promise<void>;
  onStop: () => Promise<void>;
  status: AutoStatus | null;
}
```

`useAutoStatus({ enabled }: { enabled: boolean })` 返回：

```ts
{
  status: AutoStatus | null;
  seq: number;                      // 增量事件游标
  error: string; notice: string;
  refresh: (incremental: boolean) => Promise<void>;
  start: (payload: AutoStartPayload) => Promise<void>;
  stop: () => Promise<void>;
}
```

**接口 payload（与现有 `AutoPage.tsx:92-106` 完全一致，禁止改字段名）**：

```json
POST /api/auto/start
{
  "symbol": "BTC/USDT", "market": "spot", "timeframe": "15m",
  "strategy": "ma_cross", "params": {"fast": 10, "slow": 30},
  "position_pct": 1.0, "leverage": 1.0, "poll_seconds": 10,
  "mode": "paper", "confirm_live": false
}
```

```json
GET /api/auto/status?since=42
{
  "running": true,
  "config": {"symbol":"BTC/USDT","market":"spot","timeframe":"15m","strategy":"ma_cross",
             "params":{"fast":10,"slow":30},"mode":"paper","position_pct":1.0,"leverage":1.0},
  "last_signal": {"target":1.0,"reason":"收盘价 61,234.5000","when":"21:45:02"},
  "seq": 57,
  "events": [{"seq":57,"kind":"signal","level":"SIGNAL","message":"目标仓位 +100% · 收盘价 61,234.5000","when":"21:45:02"}]
}
```

`POST /api/auto/stop` → `{"ok": true, "status": {...}}`（同 `routes.py:1187`）。

### 4.5 迁移步骤（每步可独立验证、可单独回滚）

**Step 1 — 抽组件（后端不动，页面还在）**
1. 新建 `useAutoStatus.ts`，把 `AutoPage.tsx:64-83` 的轮询搬进去（含 `seq` 合并 `events.slice(-400)`）。
2. 新建 `AutoTraderPanel.tsx`，把 JSX 从 `AutoPage.tsx:137-433` 搬进去；`market/symbol/timeframe` 改 props；
   策略列表仍由面板自己 `GET /api/strategies` 拉（一次）。
3. `AutoPage.tsx` 变成 12 行 wrapper：`<AutoTraderPanel market="spot" symbol="BTC/USDT" ... />`（保持旧页可达）。
4. 验证：`npm run build`；旧「自动交易」页功能不变（启动/停止/日志/信号）。

**Step 2 — 挂进交易页**
5. `BottomTabs` 加 `auto` Tab 与 `auto?: ReactNode`；`TradingPage` 用同一个 `useAutoStatus` 实例，
   把面板传给 `BottomTabs`，页头渲染状态徽章 + 启停按钮。
6. 验证：交易页内启动 → 图表不中断 → 切到「委托」Tab 后事件仍在累积（因为 hook 挂在页面级）；
   两个入口同时打开旧页时，只允许一处 `setInterval`（旧页在 Step 3 会删，短暂并存可接受，但事件游标各自独立）。

**Step 3 — 删旧入口**
7. `App.tsx` 删 `PageKey "auto"` 分支与 import；`Sidebar.tsx` 删 `auto` 的 `ICONS`/`LABELS`；删除 `AutoPage.tsx`。
8. 验证：`npm run build`；`grep -r "AutoPage" web/src` 为空；侧栏 4 项；刷新页面直达交易页。

**Step 4 — 清理与收尾**
9. 删 `AutoPage.tsx:438` 遗留导出；确认 `types.ts` 的 `AutoStatus` 未变；把 `styles.css` 里只被 AutoPage 用到的样式合并。
10. 更新 README（§7），跑 `pytest -q`。

### 4.6 顺带修的两个小问题

1. **实盘二次确认补齐（G2）**：`AutoTraderPanel` 选择 `live` 时不立刻生效，弹 modal（照抄
   `TradingPage.tsx:456-491` 的结构，文案改成"自动交易会持续下单"），确认后才把 `confirm_live: true` 发出；
   未确认时 `confirm_live: false`。后端已经会 400（`routes.py:308`），前端不再"替用户确认"。
2. **隐式改市场（G1）**：现在 `/api/auto/start` 会 `ctx.set_market_type(market)`（`routes.py:323`）。
   并入交易页后 `market` 就是当前图表市场，调用变成幂等，副作用自然消失；**后端这行保留**（兼容 API 直调与 curl 用户），
   但前端保证传入的就是当前页面市场。

### 4.7 验收标准

- [ ] 交易页底部出现「自动」Tab，表单/参数/启停/信号/日志与旧自动交易页**逐项一致**（对照清单 6 项）。
- [ ] 页头徽章 2 秒内反映 running 变化；启动/停止不需要刷新页面。
- [ ] 启动后切到「委托/成交/持仓」Tab，再切回「自动」，事件流不丢、不重复（`seq` 正确）。
- [ ] 实盘模式必须先过 modal 确认；未确认时请求里 `confirm_live=false`，后端返回 400 并显示原因。
- [ ] 自动交易运行中，切换 K 线周期/交易对不打断自动交易（其标的由启动时锁定）。
- [ ] `npm run build` 通过；`grep -r "AutoPage" web/src` 与 `grep -rn "\"auto\"" web/src/App.tsx` 均为空。
- [ ] 后端 `/api/auto/*` 响应字段无变化：`tests/test_auto.py` + 新增契约测试全绿。
- [ ] 小窗口（≤1366×768）下交易页不出现横向滚动条（自动面板 `form-grid` 折行）。

### 4.8 风险与缓解

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| 双轮询（旧页 + 新页）短时并存 | 事件重复/请求翻倍 | Step 3 同一 PR 内删除旧页；hook 内加 `document.visibilityState` 判断 |
| 底部 Tab 变多（6 个）在小屏挤压 | 体验差 | 只给 委托/成交/持仓 显示计数；自动/量化用"运行中"圆点代替数字；`panel-toggle` 仍可折叠 |
| 删除 `PageKey "auto"` 破坏外部书签 | 低 | 前端无路由，`PageKey` 只是内存状态；无 URL 影响 |
| 用户肌肉记忆（侧栏第二项消失） | 低 | 「自动」Tab 在交易页默认可见（不折叠），README 使用流程更新 |
| `AutoTraderPanel` 内部再拉一次 `/api/strategies` | 冗余请求 | 面板只在挂载时拉一次；与 BacktestPage 的拉取互不影响 |

---

## 5. Phase 3：AI 自主交易 + 「量化」区

### 5.1 模块边界与依赖方向

```
web/routes_ai.py        (HTTP：payload 校验、lease、控制器)
        │
        ├──> engine/ai_trader.py     AI 自主交易（poller + decision queue + guard + audit）
        │         │
        │         ├──> engine/trading.py ──> engine/risk.py ──> core/storage.py
        │         │                     └──> engine/paper.py（仅 paper 分支内部）
        │         └──> ai.py（纯函数：prompt/parse/validate）──> llm.py（transport）
        │
        └──> engine/ai_quant.py      AI 控制量化（包一层 AutoTrader + 参数修订）
                  └──> engine/auto.py（执行策略）──> engine/trading.py
```

**硬性依赖规则（review 时逐条检查）：**

1. `engine/ai_trader.py` **不得** `import` `exchange.rest` / `engine.paper` / `core.storage`；
   它只依赖注入进来的 `TradingService`、`LLMService`、以及两个 provider 回调。
2. AI 模块**不得**调用 `paper.place_order` / `rest.create_order`——只能 `trading.submit()`。
3. `ai.py` 不 import 任何 `engine.*`（保持可离线测试的纯函数层）；`AIDecision` dataclass 放在 `ai.py`。
4. `engine/ai_quant.py` 不直接下单，只通过 `AutoTrader` 执行；`AutoTrader` 仍是唯一策略执行者。
5. `web/routes_ai.py` 只做参数校验/租约/转发，不写业务判断（判断全部下沉到可测函数）。

### 5.2 AI 自主交易决策 schema

**输出（模型必须只输出这一个 JSON 对象）**

```json
{
  "action": "open",
  "symbol": "BTC/USDT",
  "side": "buy",
  "quote": 50.0,
  "price": 61234.5,
  "stop_loss": 60400.0,
  "take_profit": 62800.0,
  "confidence": 0.72,
  "reason": "4h 均线多头排列，1h 回踩 MA20 未破，ATR 1.8% 的止损空间"
}
```

**字段契约**

| 字段 | 类型 | 约束 | 用途 |
| --- | --- | --- | --- |
| `action` | `"hold" \| "open" \| "close"` | 必填；未知值 → 视为 `hold` | 决策动作 |
| `symbol` | string | 必须等于本次会话配置的交易对（v1 单标的） | 防串标的 |
| `side` | `"buy" \| "sell"` | `open` 必填；现货做空 → 拒绝 | 方向 |
| `quote` | number | `open` 必填，`0 < quote ≤ min(ai_auto.max_order_quote, 风控单笔上限)` | 名义金额（USDT） |
| `price` | number | 可省略；给了就必须在最近收盘价 ±2% 内 | 限价/市价参考价 |
| `stop_loss` | number | `require_stop_loss` 时必须给；落在摘要高低区间内、方向正确、距开仓价 0.1%–10% | 止损（纸面 guard / 实盘 reduce-only stop） |
| `take_profit` | number | 可选，同上 | 止盈 |
| `confidence` | number | `0–1`；`< ai_auto.min_confidence` → 降级为 `hold` | 置信度门槛 |
| `reason` | string | 非空、≤200 字，进审计与事件流 | 可解释性 |

`close` 时 `quote/side/price` 忽略，按当前持仓全平（`reduce_only=True`，方向由持仓决定）。

**解析与校验（纯函数，全部可离线测试）**

`src/btrader/ai.py` 新增：

```python
@dataclass
class AIDecision:
    action: str
    symbol: str
    side: str = ""
    quote: float = 0.0
    price: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    confidence: float = 0.0
    reason: str = ""
    decision_id: str = ""
    decided_at: float = 0.0
    reference_price: float = 0.0

def decision_prompt(*, symbol, market_label, timeframe, digest_text, position,
                    risk, caps, mode) -> str
def propose_decision(llm, *, symbol, timeframe, frame, position, risk, caps,
                     mode) -> dict          # 返回 llm.ask 的 dict + decision/errors
def parse_decision(text: str) -> tuple[dict | None, str]   # 复用 _find_json_object(ai.py:568)
def validate_decision(payload, *, symbol, frame, last_price, caps, position_contracts,
                      allow_short, require_stop_loss) -> tuple[AIDecision | None, list[str]]
```

`parse_decision` 复用现有 `_find_json_object`（`ai.py:568`）从"带散文/代码块"的回答里取第一个 JSON；
取不到 → 返回 `(None, "没有找到 JSON")`，**不是异常**。

**校验规则（V1–V12，全部"失败即不下单"，不做部分下单）**

| # | 规则 | 失败处理 |
| --- | --- | --- |
| V1 | `action ∈ {hold, open, close}` | 未知 → 按 `hold`，记 `reason=未知动作` |
| V2 | `symbol == 配置标的` | 拒绝，记错误 |
| V3 | `side ∈ {buy, sell}`；现货 `sell` 必须有持仓且 `quote ≤ 持仓名义` | 拒绝 |
| V4 | `quote ∈ (0, cap]`；`cap = min(ai.max_order_quote, 风控有效单笔上限)` | 拒绝（不自动截断，避免"悄悄放大/缩小"） |
| V5 | `price` 缺省 → 用最近收盘价；给了 → `|price/last-1| ≤ 0.02` | 拒绝 |
| V6 | `require_stop_loss and action=="open"` → 必须给 `stop_loss` | 拒绝 |
| V7 | 多头 `stop_loss < entry*(1-0.001)` 且 `> entry*0.90`；空头镜像；`take_profit` 同理反向 | 拒绝 |
| V8 | 止损/止盈必须落在摘要窗口 `[low_N, high_N]` 内（复用 `validate_levels` 同款区间思想） | 拒绝 |
| V9 | `confidence ∈ [0,1]`；`< min_confidence` → `hold` | 降级（不算错误） |
| V10 | `reason` 非空、截断 200 字 | 允许通过，审计记原文截断 |
| V11 | 决策时延 `now - decided_at ≤ decision_ttl_seconds`（默认 120s） | 过期 → 跳过，记 `stale` |
| V12 | 提交前最新价与决策时参考价偏离 `> max_price_drift_pct`（默认 0.5%） | 跳过，记 `price_drift` |

> **"宁可不下单"原则**：任何一条 V 规则失败都只记录、不重试、不"去掉止损再下单"。
> 这与现有 `validate_levels`（`ai.py:246`，"宁可少画一条线"）和 `_parse_verdict`（`ai.py:664`，读不懂默认保留交易）的取向一致：
> 在**下单侧**必须更保守——读不懂 = 不交易。

### 5.3 提示词约束

`ai.py` 新增：

```python
DECISION_SYSTEM = (
    "你是一个受严格风控约束的自动交易决策器，服务于一个真实在跑的交易系统。"
    "你只能根据下面给出的事实判断：已收盘 K 线摘要、当前持仓、风控状态、本次允许的限额。"
    "禁止编造事实里没有的价位、指标或新闻；事实不足以支撑一笔交易时必须输出 hold。"
    "只输出一个 JSON 对象，不要输出解释、Markdown 或代码块标记。"
)
```

`decision_prompt` 的结构（**发给模型的全部内容**）：

```
【决策任务】{symbol} {market_label} {timeframe}；运行模式：{mode}
【行情事实】（btrader/ai.py digest 的输出，只含已收盘 K 线）
{digest_text}
【当前持仓】{position_summary | "空仓"}
【风控状态】日内盈亏 {day_pnl}；日内成交 {day_trades} 笔；熔断：{是/否}；急停：{是/否}
【本次允许】单笔 ≤ {cap} USDT；置信度 ≥ {min_confidence}；必须止损：{是/否}；允许做空：{是/否}
【输出 schema】
{"action":"hold|open|close","symbol":"...","side":"buy|sell","quote":0,"price":0,
 "stop_loss":0,"take_profit":0,"confidence":0.0,"reason":"≤80 字"}
【规则】
1. action=hold 时其余字段可省略。
2. quote 不得大于限额；price 必须在最近收盘价 ±2% 内。
3. stop_loss/take_profit 必须在上面摘要出现的最高/最低价之间，方向必须正确。
4. 任何字段拿不准 → hold。
```

约束落地为代码，而不是只写在提示词里：
- **只能从摘要判断**：prompt 由 `ai.digest()` 生成，模型看不到原始 DataFrame、看不到未来 K 线（`_closed_only` 同款过滤在取行情时做，见 §5.5）；
- **不得编造价位**：V7/V8 在本地校验，模型给错价位 = 拒单；
- **置信度阈值才下单**：V9；
- **持仓隐私**：只有 `ai.share_position = true` 时才把持仓数字放进 prompt（沿用现有开关语义，`routes.py:787`）；
  AI 自主交易启动时**强制要求**该开关（否则模型无法判断是否该平仓）——启动接口直接 400；
- **不允许模型看到 API 余额明细**：`PositionView` 只给 side/contracts/entry/upnl，不给 cash 明细（现金只用于本地 sizing，不进 prompt）。

缓存键（防重复计费）：

```
ai-trader|{symbol}|{timeframe}|{last_closed_ts}|{mode}|{position_side}
ai-quant|reflect|{strategy}|{symbol}|{tf}|{last_closed_ts}|{params_hash}
ai-quant|choose|{idea_hash}|{catalogue_hash}
ai-lab|gen|{idea_hash}|{base_key}
ai-lab|reflect|{params_hash}|{metrics_hash}
```

**关键点**：`last_closed_ts` 进 key，保证"一根 K 线最多一次决策"，同时**绝不跨 K 线复用决策**。

### 5.4 状态机

`AITraderState`（`engine/ai_trader.py`）：

```
idle ──start──> running ──pause──> paused ──resume──> running
  ▲                │  │                │
  │                │  └──stop──────────┴──stop──> idle
  │                │
  │                ├──风控熔断/急停/连续失败达阈值──> halted ──(clear_halt + 手动 reset)──> idle
  │                └──未捕获异常/行情连续失败──────> error  ──(手动 reset)──────────────> idle
  └──────────────────────────────────stop──────────────────────────────────────────────┘
```

| 当前 | 事件 | 下一状态 | 副作用 |
| --- | --- | --- | --- |
| idle | `start`（通过四重实盘条件） | running | 起 poller 线程；`TradeLease.acquire("ai")`；发通知 |
| running | `pause` | paused | 不再入队决策；**已有持仓保留**（不自动平仓，避免暂停变成市价砸盘） |
| paused | `resume` | running | 从**下一根收盘 K 线**开始决策（不回补） |
| running | `stop` | idle | 停线程；`lease.release("ai")`；持仓保留、保护单保留 |
| running | `risk.snapshot().halted` 或 `kill_switch` | halted | 只停止新开仓；**允许 reduce-only 平仓**（与 `RiskManager` 的 entry 语义一致） |
| running | 模型连续失败 `max_consecutive_failures` 次 | error | 停止决策；发通知；不再重试直到手动 reset |
| running | 行情连续失败 `max_consecutive_failures` 次 | error | 同上（行情不可达时交易等于盲飞） |
| halted/error | `stop` / `reset` | idle | 清错误计数；不清风控状态 |
| 任意 | 进程重启 | **idle** | 绝不自动恢复；`settings_kv` 里保存 `halted_reason/error` 供页面提示 |

**持久化**：`settings_kv` 键 `ai.trader.state.v1`，只存 `{state, reason, at, stopped_symbol, mode}`；
重启后页面显示"上次因 X 停止"，必须人工重新启动。

### 5.5 线程模型（不阻塞 `_tick()`）

```
ai-trader 轮询线程（poll_seconds=10）
  └─ 取已收盘 K 线（frame_provider，内部持 state.locks[market]）
       ├─ 没有新收盘 K 线 → 直接返回（绝大多数轮次）
       └─ 有新 K 线 → 检查 state/config/预算/冷却
             └─ queue.submit({"run": _decide_and_trade})   ← 立即返回，队列满=丢弃
                          │
                          ▼
                 ai-decision 工作线程（BriefQueue 自带的 daemon 线程）
                   llm.ask(decision_prompt)        ← 可能 20–40s，绝不进 poller
                   → parse_decision → validate_decision → guard.check
                   → 取最新价复核漂移(V12) → trading.submit()
                   → 写 logs/ai-trade-audit.jsonl → 事件流 → 通知
```

实现复用 `ai.BriefQueue`（`ai.py:810`）——它已经是"独立线程 + 队列满即丢弃 + 异常只记 last_error"。
建议给它加一个可选线程名参数（默认不变，向后兼容）：

```python
class BriefQueue:
    def __init__(self, size: int = 4, name: str = "ai-brief") -> None: ...
```

`AITrader` 用 `ai.BriefQueue(size=1, name="ai-decision")`（size=1：同一时刻只允许一个决策在飞，天然去重）。

**Provider 注入（让 engine 保持 Qt/HTTP 无关、可离线测试）**

```python
# routes_ai.py 里构造，闭包内持锁与兜底逻辑
def frame_provider() -> pd.DataFrame:
    with state.locks[market]:
        df = ctx.klines.fetch(market, symbol, timeframe, limit=300, use_cache=False)
    return _closed_only(df, timeframe)        # routes.py:1335 已有

def portfolio_provider(refresh_price: bool = False) -> PortfolioView:
    ...  # paper: paper.account（持 state.account_lock）；live: rest.fetch_balance/fetch_positions
    ...  # 读失败 → 抛 PortfolioUnavailable（引擎捕获后跳过本轮，绝不当成空仓）
```

**锁清单**

| 锁 | 保护对象 | 谁持有 |
| --- | --- | --- |
| `AITrader._lock` (RLock) | 状态/配置/last_decision/计数器 | poller 与 decision 线程 |
| `ai.BriefQueue._lock` | 决策队列 | `submit()` / `_drain()` |
| `_State.account_lock` | 纸面账户读写 | provider、`/api/portfolio`、撮合循环 |
| `_State.locks[market]` | ccxt 客户端（不可并发） | `frame_provider`、下单路由、撮合循环 |
| `TradeLease._lock` | 三主体互斥 | 三个 controller 的 start/stop |
| `RiskManager._lock` | 风控状态 | 所有 `check_*`/`record_*` |

**失败降级表（"模型失败不交易，保留原仓"）**

| 故障 | 行为 | 用户可见 |
| --- | --- | --- |
| `llm.ask` 返回 `ok=False`（超时/限额/网络） | 不下单；`failures += 1`；持仓与保护单不动；达阈值 → state=error | 事件流 `WARN AI 决策失败：…`（错误文本经脱敏） |
| 返回非 JSON / 缺字段 | 同上，reason=`解析失败` | 同上 |
| 校验 V1–V10 不通过 | 不下单；记为 `rejected` 决策（**不算失败计数**，避免模型格式差就停摆） | 事件流 `INFO 决策被校验拒绝：<规则>` |
| `RiskManager` 拒绝 | 不下单；事件流给出中文原因（`OrderRejected`） | `WARN 风控拒绝：已达当日最大交易次数` |
| 行情取不到 | 跳过本轮；连续失败 → error | `WARN 行情不可用，暂停决策` |
| `trading.submit` 抛非 `OrderRejected` | 记 `error` 决策 + audit；**不重试**（重试可能重复下单） | `ERROR 下单异常` + 微信通知（节流 600s） |
| 实盘开仓后保护单 `warnings` 非空 | 立即尝试 reduce-only 平仓；平仓也失败 → state=halted + 通知 | `ERROR 保护单未挂上，已尝试平仓/需人工处理` |
| 进程被杀 | 持仓与交易所侧保护单仍在（实盘）；重启后 state=idle | 页面提示"上次运行被中断" |

### 5.6 安全与风控不变量（INV 表）

| # | 不变量 | 实现位置 | 测试 |
| --- | --- | --- | --- |
| **INV-1** | **AI 订单只能经 `TradingService.submit()`**，禁止任何模块直连 broker/交易所 | `engine/ai_trader.py` 只 import `engine.trading` | `test_ai_trader_never_calls_broker_directly` |
| **INV-2** | 每笔 AI 订单必过 `RiskManager`（`precheck` + `check_order`），AI 限额是**前置收紧**不是替代 | 由 INV-1 保证；`AIGuard` 在 submit 前另加 AI 专属闸门 | `test_risk_rejection_blocks_ai_order`、`test_ai_cap_tighter_than_risk_cap` |
| **INV-3** | 单笔上限 = `min(ai_auto.max_order_quote, 风控有效单笔上限)`；超限**拒绝**而不是截断 | `ai.validate_decision` V4 + `AIGuard` | `test_quote_above_cap_is_rejected_not_clamped` |
| **INV-4** | 日限额：`ai_auto.max_decisions_per_day`（模型调用）与 `ai_auto.max_orders_per_hour`（下单）双计数，跨日重置 | `AITrader._counters`（持久化 `ai.trader.counters.v1`） | `test_daily_decision_limit_stops_before_llm_call` |
| **INV-5** | 最大持仓：`ai_auto.max_position_quote` 前置 + `RiskManager.max_position_quote` 兜底 | `AIGuard` | `test_position_cap_blocks_adding` |
| **INV-6** | 杠杆不可由 AI 改：决策 schema 无 leverage 字段；杠杆在启动时固定，改需 stop→start | `AITraderConfig.leverage` | `test_decision_has_no_leverage_effect` |
| **INV-7** | 熔断（`halted_reason`）与急停（`kill_switch`）优先于一切；halted 时仍允许 reduce-only | 沿用 `RiskManager.check_order(entry=...)` 语义 | `test_halted_blocks_open_allows_close` |
| **INV-8** | 急停一键可达：`POST /api/risk/kill` 设 `kill_switch` + 停 auto/ai/quant + 通知 | `routes_ai.py` + `RiskSettings.kill_switch` | `test_kill_switch_stops_all_traders` |
| **INV-9** | 实盘四重条件：`ai_auto.live_enabled` + `confirm_live=true` + 主网 + 未急停，缺一不下单 | `AIController.start` | `test_live_start_requires_all_four` |
| **INV-10** | 模型失败/答案读不懂 → **不下单、保留原仓**；不重试下单 | `_decide_and_trade` | `test_model_failure_places_no_order_and_keeps_position` |
| **INV-11** | 行情不可达（币安域）→ 不下单；**不**用 Gate 备用行情给实盘单定价 | `frame_provider` 抛错 → 跳过；显式不用 `_market_frame` | `test_no_decision_when_market_unreachable` |
| **INV-12** | 决策新鲜度：TTL + 价格漂移双闸门，过期/偏离即跳过 | V11/V12 | `test_stale_decision_is_skipped`、`test_price_drift_skips_order` |
| **INV-13** | 实盘现货 + 必须止损 → 启动即拒绝（现货没有交易所侧 reduce-only 止损，`trading.py:471`） | `AIController.start` 校验 | `test_live_spot_requires_futures_for_stops` |
| **INV-14** | 三主体互斥：auto / ai / quant 同一时刻只有一个在动钱 | `TradeLease` | `test_lease_blocks_second_trader` |
| **INV-15** | 审计必写且必可追溯：每笔决策一行，含拒绝原因；订单可在 `orders` 表按 `origin=ai` 反查 | `ai_trader._audit` + `OrderRequest.origin` | `test_trade_audit_has_required_fields` |

**额外数据字段：`OrderRequest.origin`**

`trading.py:59` 的 dataclass 增加向后兼容字段：

```python
@dataclass
class OrderRequest:
    ...
    origin: str = "manual"     # manual | auto | ai
```

`OrderResult` 同步带 `origin`；`db.save_order(...)` 的 `raw` JSON 里天然含它（`storage.py:116`），
因此**不需要改表结构**；审计与排障用 `SELECT raw FROM orders WHERE raw LIKE '%"origin": "ai"%'`。

### 5.7 设置结构（dataclass 字段）

新增到 `src/btrader/core/config.py`（与 `RiskSettings` 同文件；**不要**塞进 `llm.AISettings`，那是 transport 层）：

```python
@dataclass
class AIAutoSettings:
    """AI 自主交易。默认全关；实盘另有独立闸门。"""
    enabled: bool = False
    live_enabled: bool = False
    symbol: str = "BTC/USDT"
    market_type: str = "spot"
    timeframe: str = "15m"
    poll_seconds: float = 10.0
    min_confidence: float = 0.65
    max_order_quote: float = 50.0
    max_position_quote: float = 200.0
    max_decisions_per_day: int = 20
    max_orders_per_hour: int = 4
    max_consecutive_failures: int = 3
    decision_ttl_seconds: float = 120.0
    max_price_drift_pct: float = 0.005
    require_stop_loss: bool = True
    allow_short: bool = False
    daily_call_limit: int = 60
    dry_run: bool = False          # true 时只预览不下单

@dataclass
class AIQuantSettings:
    """AI 控制量化：执行由 AutoTrader，AI 只选策略/调已审核参数。"""
    enabled: bool = False
    live_enabled: bool = False
    strategy: str = ""
    params: dict[str, Any] = field(default_factory=dict)   # 已审核基线
    mode: str = "manual"           # manual | assisted | auto
    tune_enabled: bool = False
    tune_max_step_pct: float = 0.25
    tune_min_interval_bars: int = 4
    max_tunes_per_day: int = 6
    reflection_interval_bars: int = 4
    daily_call_limit: int = 40

@dataclass
class AIStrategyLabSettings:
    """策略实验室：候选生成 + 多轮回测反思。永远不自动启用候选。"""
    enabled: bool = False
    max_source_bytes: int = 20_000
    max_rounds: int = 5
    max_calls_per_job: int = 20
    max_seconds_per_job: float = 600.0
    min_trades: int = 20
    min_improvement: float = 0.05
    max_drawdown_worse: float = 0.02
    validate_timeout_seconds: float = 60.0
```

`AppSettings` 追加三个字段：`ai_auto`、`ai_quant`、`ai_lab`（都 `field(default_factory=...)`）。

`ConfigStore._load()`（`config.py:70`）同步改造（与 `risk/notify/ai` 完全同款）：

```python
ai_auto_raw = raw.pop("ai_auto", {}) or {}
ai_quant_raw = raw.pop("ai_quant", {}) or {}
ai_lab_raw = raw.pop("ai_lab", {}) or {}
...
settings.ai_auto = AIAutoSettings(**{k: v for k, v in ai_auto_raw.items()
                                     if k in {f for f in AIAutoSettings.__dataclass_fields__}})
# ai_quant / ai_lab 同理
```

`Context._attach_settings()`（`context.py:198`）**无需**改：AI 控制器每次从 `ctx.settings.ai_auto` 读取
（`PUT /api/settings` 会整对象替换，`routes.py:669-672`），**禁止在构造时捕获嵌套对象**——这是踩过的坑。

`routes.py` 的 `SettingsPayload` 追加三个可选 dict + 三个 `_apply_*` 校验器（与 `_apply_ai` 同款，
`routes.py:1284`），并加跨字段校验：

- `ai_auto.enabled` 要求 `ai.enabled` 与 `ai.share_position`；
- `ai_auto.live_enabled` 要求 `ai_auto.enabled`；
- `ai_quant.enabled` 要求 `ai.enabled`；`ai_quant.mode` ∈ `{manual, assisted, auto}`；
- `ai_lab.enabled` 要求 `ai.enabled`；
- 所有数值上限做 `>0` 校验（`max_order_quote <= 0` 直接 400）。

### 5.8 路由与 payload / 响应示例

新增 `src/btrader/web/routes_ai.py`（`build_ai_router(get_state, controllers) -> APIRouter`，
由 `build_router` 里 `router.include_router(...)` 挂载）。

**AI 自主交易**

```json
POST /api/ai/trader/start
{
  "symbol": "BTC/USDT", "market": "spot", "timeframe": "15m",
  "mode": "paper", "confirm_live": false, "dry_run": false,
  "leverage": 1.0, "poll_seconds": 10
}
→ 200 {"ok": true, "status": {"state": "running", "config": {...}, "seq": 0, "events": []}}
→ 400 {"detail": "AI 自主交易需要先允许把持仓发送给模型（设置 → AI 助手）"}
→ 409 {"detail": "策略自动交易正在运行，请先停止"}
```

```json
GET /api/ai/trader/status?since=12
{
  "state": "running",
  "state_reason": "",
  "config": {"symbol":"BTC/USDT","market":"spot","timeframe":"15m","mode":"paper",
             "dry_run":false,"min_confidence":0.65,"max_order_quote":50.0},
  "last_decision": {
    "decision_id": "d-7f3a1c",
    "at": 1761000000.0,
    "action": "open", "side": "buy", "quote": 50.0, "price": 61234.5,
    "stop_loss": 60400.0, "take_profit": 62800.0, "confidence": 0.72,
    "reason": "4h 均线多头排列…",
    "outcome": "filled",          // filled | rejected | valid_but_hold | error | stale | drift
    "outcome_reason": "",
    "order_id": "paper-9c2f…"
  },
  "counters": {"decisions_today": 6, "orders_today": 2, "failures": 0, "last_call_at": 1761000000.0},
  "risk": {"day_pnl": -12.3, "halted": false, "kill_switch": false},
  "seq": 31,
  "events": [{"seq":31,"kind":"decision","level":"INFO","message":"开多 50 USDT · 置信度 0.72","when":"21:45:02"}]
}
```

```json
POST /api/ai/trader/stop          → {"ok": true, "status": {...}}
POST /api/ai/trader/pause         {"paused": true}   → {"ok": true, "status": {...}}
POST /api/ai/trader/halt          {"reason": "人工干预"} → {"ok": true, "status": {"state": "halted"}}
POST /api/ai/trader/preview       → {"decision": {...}, "valid": true, "errors": [],
                                     "prompt_chars": 1820, "tokens": 640, "cached": false}
GET  /api/ai/trader/audit?limit=100 → {"items": [ ...见 §5.6 审计字段... ]}
```

**量化（AI 控制）**

```json
POST /api/ai/quant/start
{
  "strategy": "ma_cross", "params": {"fast": 10, "slow": 30},
  "symbol": "BTC/USDT", "market": "spot", "timeframe": "15m",
  "mode": "paper", "position_pct": 0.5, "leverage": 1.0,
  "confirm_live": false, "tune_mode": "assisted"
}
→ 200 {"ok": true, "status": {...}}

GET /api/ai/quant/status
{
  "state": "running",
  "config": {"strategy":"ma_cross","symbol":"BTC/USDT","mode":"paper","tune_mode":"assisted"},
  "params": {"fast": 10, "slow": 30},
  "baseline": {"fast": 10, "slow": 30},
  "revisions": [{"rev": 3, "at": 1761000000, "source": "ai",
                 "before": {"fast": 10, "slow": 30}, "after": {"fast": 12, "slow": 30},
                 "reason": "趋势强但假突破多，缩短快线", "accepted": true}],
  "last_reflection": {"at": 1761000000, "text": "近 4 根 K 线震荡收敛…", "suggestion": "hold"},
  "seq": 8, "events": [...]
}

POST /api/ai/quant/tune      {"apply": true} → {"candidate": {"fast": 12, "slow": 30},
                                                "revision": {"rev": 3, ...}, "rejected_reason": ""}
POST /api/ai/quant/rollback  {"rev": 2} → {"ok": true, "params": {...}, "revision": {...}}
GET  /api/ai/quant/revisions?limit=50 → {"items": [...]}
```

**急停（新增，Phase 3 的一部分但独立于 AI）**

```json
POST /api/risk/kill
{"on": true, "confirm": true, "reason": "手动急停"}
→ 200 {"ok": true, "risk": {...snapshot()...}, "stopped": ["auto", "ai", "quant"]}
→ 400 {"detail": "打开急停需要 confirm=true"}
POST /api/risk/kill {"on": false, "confirm": true}   // 只解除急停，不自动重启任何交易
```

### 5.9 文件级改动清单

| 文件 | 动作 | 内容 |
| --- | --- | --- |
| `src/btrader/engine/ai_trader.py` | **新增**（~420 行） | `AITraderState`、`PortfolioView`、`AITraderConfig`、`AIGuard`、`AITrader`（poller/queue/state machine/audit）、`PortfolioUnavailable` |
| `src/btrader/engine/ai_quant.py` | **新增**（~260 行） | `ParamRevision`、`QuantSession`（包 `AutoTrader`）、`propose_tune` / `apply_tune` / `rollback` / `revisions` |
| `src/btrader/ai.py` | 修改（+~220 行） | `AIDecision`、`DECISION_SYSTEM`、`decision_prompt`、`propose_decision`、`parse_decision`、`validate_decision`；`BriefQueue.__init__` 加 `name` 参数；新增 `reflect_tuning` / `reflection_note` / `choose_strategy` |
| `src/btrader/engine/auto.py` | 修改（+~25 行） | `self._config_lock`；`update_params(params, reason)`；`_tick` 内快照 params（`auto.py:176`） |
| `src/btrader/core/config.py` | 修改（+~60 行） | 三个 dataclass + `AppSettings` 字段 + `_load` 重建 |
| `src/btrader/core/types.py` | 可选 | `RunMode` 已够用，不需要改 |
| `src/btrader/engine/trading.py` | 修改（+3 行） | `OrderRequest.origin: str = "manual"`（`trading.py:59`）；`OrderResult` 带 `origin` |
| `src/btrader/llm.py` | 修改（+~30 行） | 抽出 `append_audit(filename, record)`（`_append_audit` 改调它，`llm.py:276`）；`redact(text)`；滚动逻辑复用 |
| `src/btrader/web/controllers.py` | **新增**（~260 行） | 从 `routes.py` **机械搬出** `EventLog`/`JobStore`/`AutoController`（不改行为），并新增 `TradeLease`、`AIController`、`AIQuantController`、`StrategyLabController` |
| `src/btrader/web/routes.py` | 修改 | `build_router` 里创建 `lease` + 三个 controller，`include_router(build_ai_router(...))`；`SettingsPayload` 三个 dict；`_apply_ai_auto/_ai_quant/_ai_lab`；`POST /api/risk/kill` 放在此处（与 `/api/risk` 同段，`routes.py:678`） |
| `src/btrader/web/routes_ai.py` | **新增**（~420 行） | 上表 15 条路由 + 对应 pydantic payload（`AITraderStartPayload` 等） |
| `src/btrader/context.py` | 修改（+2 行） | 无需 attach；确认 `AIController` 持有 `ctx.settings` 引用即可 |
| `src/btrader/paths.py` | 修改（+6 行） | `strategy_candidates_dir()` / `logs` 复用（候选目录 Phase 4 用，先加不亏） |
| `web/src/types.ts` | 修改（+~90 行） | `AIDecisionOutcome`、`AITraderStatus`、`AIDecisionEvent`、`AIQuantStatus`、`AIQuantRevision`、`AIAutoSettings`、`AIQuantSettings`、`AIStrategyLabSettings`；`AppSettings` 加三字段 |
| `web/src/components/AITraderCard.tsx` | **新增**（~120 行） | rail 卡片：状态徽章、模式、今日决策/下单、[启动/暂停/停止/急停]、"预览一次" |
| `web/src/components/AITraderPanel.tsx` | **新增**（~260 行） | 底部 Tab「自动」里的 AI 自主交易卡片：配置、事件流、决策历史、dry-run 开关、审计表 |
| `web/src/components/AIQuantPanel.tsx` | **新增**（~240 行） | 底部 Tab「量化」：策略选择、基线参数、tune_mode、revision 列表、回滚、反思记录 |
| `web/src/components/BottomTabs.tsx` | 修改 | 加 `autoPanel/quantPanel` props 与 `"quant"` Tab |
| `web/src/pages/TradingPage.tsx` | 修改 | 挂 rail 卡片与两个底部面板；页头增加"AI 运行中"标记 |
| `web/src/pages/SettingsPage.tsx` | 修改（+~120 行） | AI 助手卡（`SettingsPage.tsx:626`）后插 `AI 自主交易` 卡；风控参数卡（`SettingsPage.tsx:1467`）后插 `量化与实验室` 卡 |
| `tests/test_ai_trader.py` | **新增** | §8.2 |
| `tests/test_ai_quant.py` | **新增** | §8.2 |

### 5.10 前端布局

```
交易页底部 Tab「自动」                     交易页底部 Tab「量化」
┌ AutoTraderPanel（Phase 2，策略自动）┐   ┌ AIQuantPanel（AI 控制量化）────────┐
│ 策略/参数/启停/信号/日志            │   │ 策略选择 + 参数（基线）           │
├ AI 自主交易（Phase 3）─────────────┤   │ 调参模式 [manual|assisted|auto]   │
│ 状态机徽章 · 模式 · 提示词预览      │   │ 当前参数 vs 基线（高亮差异）      │
│ [启动][暂停][停止][急停]            │   │ 修订历史表 + [回滚]              │
│ 最近决策表（时间/动作/金额/结果）   │   │ 最近反思文本 + 采纳/拒绝          │
│ 事件流（复用 .feed/.feed-row）      │   └──────────────────────────────────┘
└────────────────────────────────────┘
右侧 rail 卡片 AITraderCard：只显示状态 + 最近 1 条决策 + 急停（运行中才出现，否则不占位）
```

### 5.11 验收标准

- [ ] 纸面模式下 AI 连续跑 ≥20 根 K 线：每次决策都可追溯到 `logs/ai-trade-audit.jsonl` 一行，且与 `orders` 表一一对应。
- [ ] 每笔 AI 订单都经过 `RiskManager`：把 `max_daily_trades` 调到 1，第二笔被拒且事件流给出风控中文原因。
- [ ] 急停按钮点下后 ≤2 个 tick 内停止新开仓；当前持仓不被强平；`kill_switch=true` 持久化，重启仍生效。
- [ ] 模型断开（FakeLLM `ok=False`）时：不下单、原仓不动、事件流有 `AI 决策失败`、连续 3 次后 state=error。
- [ ] 置信度 0.5 的 `open` 决策被降级为 `hold`，不产生订单。
- [ ] 止损在错误一侧的决策被拒（V7），不产生订单。
- [ ] 实盘启动四重条件逐个缺失时均返回 400/409，且错误文案指明缺哪一个。
- [ ] 实盘现货 + 必须止损 → 启动被拒（INV-13）。
- [ ] auto 正在运行时启动 AI → 409；反之亦然（TradeLease）。
- [ ] 量化会话：AI 把参数从 10 改成 13（在 +25% 内）→ 修订表新增一行；改成 20（+100%）→ 被限幅到 12 或拒绝，且记录原因。
- [ ] 回滚到 rev1 后，`AutoTrader` 下一个 tick 使用 rev1 参数（不重启会话）。
- [ ] 设置页三个新卡片可保存、可读回；`PUT /api/settings` 后无需重启生效。
- [ ] 新增 `tests/test_ai_trader.py` / `test_ai_quant.py` 全绿，且不访问网络、不依赖密钥环。

### 5.12 风险与缓解

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| 模型在真实行情里"看上去很合理"的错误决策 | 真金白银亏损 | 默认关闭；纸面先行；单笔上限默认 50 USDT；置信度 0.65；必须止损；日笔数 4/小时 |
| 决策延迟（20–40s）后价格已变 | 成交价偏离预期 | TTL 120s + 漂移 0.5% 双闸门；市价单本就按最新价成交，滑点由 `slippage_bps` 反映 |
| 实盘保护单挂失败 | 裸仓 | INV：立即尝试平仓，失败则 halted + 微信通知（复用 `trading.py:441` 的 warnings） |
| 与策略自动交易同时运行造成双重曝光 | 仓位翻倍 | `TradeLease` 互斥 + 测试 |
| 三主体互斥导致用户困惑（"为什么启动不了"） | 体验 | 409 文案明确写"先停止 X"；前端按钮置灰并显示当前占用者 |
| prompt 中含持仓数字被第三方模型看到 | 隐私 | 复用 `ai.share_position`；AI 自主交易启动强制要求开启并在设置页说明 |
| 额度被一根 K 线的重试烧光 | 成本 | 缓存键按 K 线；`max_consecutive_failures` 停止；`daily_call_limit` 独立于全局 300 |
| 新增设置让 `config.json` 变复杂 | 维护 | 全部带默认值；旧配置缺字段自动用默认（`_load` 已知字段过滤） |

---

## 6. Phase 4：策略代码自优化 + 多轮回测反思 + 实时调参

### 6.1 候选代码沙箱边界

**目录（Windows 实际路径）**

```
%APPDATA%\biTrader\strategies\            ← 正式策略目录（registry 从这里 import）
├─ candidates\                            ← 新：AI 候选（永不 import）
│  ├─ cand-20261020-214500-momo.py
│  ├─ cand-20261020-214500-momo.json      ← 元数据（状态/哈希/检查报告/回测摘要）
│  └─ rejected\                           ← 被拒绝的候选（留档，不删）
└─ example_strategy.py
```

`paths.strategy_candidates_dir()` 新增（`paths.py:57` 旁）：

```python
def strategy_candidates_dir() -> Path:
    path = user_strategies_dir() / "candidates"
    path.mkdir(parents=True, exist_ok=True)
    return path
```

**写入规则**
1. 候选文件名 `cand-{YYYYMMDD-HHMMSS}-{slug}.py`，`slug` 由 AI 给的 key 净化（`[^a-z0-9_]` → `_`，截 32）。
2. **绝不写正式目录**；`CandidateStore.create()` 只接受 `candidates/` 下的路径。
3. 元数据 sidecar（同目录 `.json`）：`id, created_at, strategy_key, name, base_key, model, prompt_sha, source_sha256, status, checks, backtest, note`。
4. 写文件用"临时文件 + `replace`"（与 `config.py:96` 同款），避免半截文件被读到。

**静态检查（`src/btrader/strategy/sandbox.py`，纯函数）**

```python
BANNED_MODULES = {
    "os", "sys", "subprocess", "socket", "requests", "urllib", "http", "https",
    "ftplib", "telnetlib", "smtplib", "shutil", "pathlib", "tempfile", "glob",
    "ctypes", "multiprocessing", "threading", "asyncio", "importlib", "builtins",
    "pickle", "marshal", "code", "gc", "inspect", "signal", "platform", "webbrowser",
}
ALLOWED_MODULES = {"math", "statistics", "numpy", "pandas", "typing", "dataclasses",
                   "btrader.strategy.base"}
BANNED_NAMES = {"eval", "exec", "compile", "open", "input", "breakpoint", "globals",
                "locals", "vars", "__import__", "getattr", "setattr", "delattr",
                "memoryview", "exit", "quit"}
MAX_SOURCE_BYTES = 20_000
MAX_LINES = 600

@dataclass
class CheckReport:
    ok: bool
    errors: list[str]
    warnings: list[str]
    strategy_key: str
    imports: list[str]

def check_source(source: str, *, max_bytes: int = MAX_SOURCE_BYTES,
                 known_keys: set[str] | None = None) -> CheckReport: ...
```

检查项（按顺序，**只 `ast.parse`，绝不 import**）：

| # | 检查 | 失败信息 |
| --- | --- | --- |
| C1 | `len(source.encode()) <= max_bytes` | `源码超过 20000 字节上限` |
| C2 | 行数 ≤ 600 | `源码超过 600 行上限` |
| C3 | `ast.parse` 成功 | `语法错误：第 N 行` |
| C4 | 所有 `Import`/`ImportFrom` 的根模块 ∈ `ALLOWED_MODULES` | `禁止导入 os（安全边界）` |
| C5 | 不存在 `import *` | `禁止 import *` |
| C6 | 不存在 `Name.id ∈ BANNED_NAMES` | `禁止使用 eval/open 等内置（安全边界）` |
| C7 | 不存在 `Attribute.attr.startswith("__")`（`self.__init__` 也不需要） | `禁止访问双下划线属性` |
| C8 | 存在且仅存在一个 `Strategy` 子类，含 `generate` 方法 | `必须定义恰好一个 Strategy 子类并实现 generate` |
| C9 | `key`/`name` 是字符串常量；`key` 合法标识符 | `策略必须声明字符串 key` |
| C10 | `key` 不与内置/已加载策略冲突（`known_keys`） | `策略 key 与现有策略冲突` |
| C11 | `params_spec` 只包含 `IntParam/FloatParam/BoolParam/ChoiceParam` 调用 | `params_spec 只能使用内置参数构造器` |
| C12 | 不存在 `while True`（无界循环） | `禁止无限循环` |
| C13 | 不存在 `time.sleep`（连 `time` 都不在白名单内，C4 已覆盖） | — |

**子进程校验（降低风险，不是安全边界）**

```python
def validate_in_subprocess(source: str, frame_npz: Path, *,
                           timeout: float = 60.0) -> dict:
    """在独立 `python -I` 进程里跑一次回测，超时杀进程。返回 metrics 或错误。"""
```

实现要点：
- 写入 `tempfile.mkdtemp()`，`subprocess.run([sys.executable, "-I", "-c", RUNNER], cwd=tmp, timeout=timeout, capture_output=True, creationflags=CREATE_NO_WINDOW)`；
- `RUNNER`：用 `importlib.util.spec_from_file_location` 加载候选 → 实例化 → 用 `datasets`（父进程写入的 `.npz`）调用 `run_backtest` → 打印 `{"ok": true, "metrics": {...}}`；
- 父进程只解析 stdout 最后一行 JSON；超时 → `Popen.kill()` + 判失败；
- **诚实说明**：`-I` 只是"隔离 site-packages 路径 + 无用户环境"，**不是**真正的权限沙箱。静态检查 C4–C7 是主要防线；最终防线是**人工审核**。强隔离（Windows AppContainer / 低权限令牌 / Job Object + 网络禁用）放 Phase 5。

**人工审核流（唯一启用路径）**

```
AI 生成源码 → sandbox.check_source() 失败 → 落盘为 status=invalid（页面可见，不可审核通过）
             └ 通过 → 落盘 status=pending → 人工在「策略实验室」查看 diff/检查报告/回测摘要
                        ├ [拒绝] → status=rejected，移入 candidates/rejected/
                        └ [批准] → 重跑静态检查 + 重算 sha256 == 用户确认时的 sha256
                                    ├ 不一致 → 409「文件在审核期间被修改，请重新检查」
                                    └ 一致 → 复制到 user_strategies_dir()/{key}.py
                                             → StrategyRegistry.reload()（原子换表）
                                             → status=approved，写 logs/ai-strategy-audit.jsonl
```

**`reload` 的原子性（G5）**

`registry.py` 现在 `discover()` 会先 `self._strategies.clear()`（`registry.py:29-31`），
在并发请求下存在"读到空表"窗口。改为：

```python
class StrategyRegistry:
    def __init__(self):
        self._lock = threading.RLock()
        ...
    def reload(self, extra_dirs: list[Path] | None = None) -> None:
        fresh = StrategyRegistry()          # 全新实例，不碰 self
        fresh.discover(extra_dirs or self._dirs)
        with self._lock:
            self._strategies, self.errors = fresh._strategies, fresh.errors
```

`get()/keys/meta_list` 读 `self._strategies` 时持锁（或读局部引用），保证换表瞬间旧引用仍可用。

### 6.2 人工审核 UI 契约

`web/src/pages/StrategyLabPage.tsx`（新页面，Sidebar 第 3 项「实验室」，替换原「自动交易」的位置）：

- 列表：`GET /api/ai/strategies/candidates` → 卡片/表格（key、状态、创建时间、检查通过/失败数、回测指标摘要）。
- 详情：代码只读展示（带行号）、静态检查报告（每条 C1–C12 的通过/失败与原因）、与 base 策略的 diff、
  子进程回测结果（收益/回撤/交易数/胜率）。
- 操作：`[校验]`（触发子进程回测 job）、`[批准并启用]`（需再次输入策略 key 确认 + 传 `expected_sha256`）、`[拒绝]`（可填理由）。
- 顶部警示条：**「候选代码在批准前不会被执行；批准后它拥有与本程序相同的系统权限」**（与 README 现有措辞一致）。

### 6.3 多轮「回测 → 反思 → 调参 → 再回测」循环

`src/btrader/ai_optimize.py`（顶层模块，避免 `strategy ↔ backtest` 循环 import）：

```python
@dataclass
class OptimizeRound:
    round: int
    params: dict[str, Any]
    metrics: dict[str, float]
    score: float
    accepted: bool
    reason: str
    reflection: str
    tokens: int = 0
    elapsed: float = 0.0
    error: str = ""

def optimize_params(llm, strategy, frame, base_params, config, *,
                    bounds: dict[str, list[Any]], rounds: int, metric: str,
                    min_improvement: float, max_drawdown_worse: float,
                    max_calls: int, deadline: float,
                    progress=None, should_stop=None) -> dict: ...
```

**算法（确定性外壳 + 模型只提候选）**

```
1. 基线：run_backtest(frame, strategy, validate_params(base_params), config)
   → best = (params, metrics, score)
2. for round in 1..rounds:
   a. 预算检查：calls<max_calls、now<deadline、should_stop()、连续无改善<2
   b. 提示词：只喂"已算好的"指标（score、收益、回撤、胜率、交易数、最近 10 笔 pnl 方向）+ 当前参数 + 参数上下界
      → 模型只输出 {"params": {...}, "hypothesis": "...", "reason": "..."}
   c. 解析失败/越界 → 记 error，continue（不计入失败上限 3 次则继续）
   d. clamp：validate_params + 每次相对上一轮变化 ≤ 50%（防跳飞）
   e. run_backtest → metrics（**同一个 frame、同一个 config**，保证可比）
   f. 接受条件：score 提升 ≥ min_improvement 且 max_drawdown 不烂于 best 的 (1 + max_drawdown_worse)
   g. 记录 OptimizeRound；接受则 best = 本轮
3. 返回 {"ok": True, "rounds": [...], "best": {...}, "best_metrics": {...},
        "calls": n, "stopped": "rounds|budget|timeout|no_improvement|cancel|error"}
```

`score`：由 `metric` 决定（`sharpe|total_return|profit_factor|...`），直接取 `BacktestResult.metrics[metric]`
（`backtest/engine.py:336 compute_metrics`）。

**预算与上限**

| 项 | 默认 | 说明 |
| --- | --- | --- |
| 轮数 | 5（`ai_lab.max_rounds`） | 5 轮后必停 |
| 模型调用 | 20/任务 | 每轮最多 1 次反思 + 1 次重试 |
| 墙钟 | 600s | 到期标记 `stopped=timeout` |
| 样本 | `trades >= 20` | 不足 → 结果标注"样本不足，不建议采纳"，**仍可查看** |
| 连续无改善 | 2 轮 | 提前停 |
| 连续失败 | 3 次 | 提前停 |

**缓存**：`llm` 层缓存键 `ai-lab|reflect|{params_hash}|{metrics_hash}`；
同一组参数+同一组指标在 TTL 内不重复计费（模型原样重复回答时直接复用）。
**帧缓存**：一次 job 只取一次 K 线（`frame` 在 job 内存里复用），回测本身是纯 CPU、可重跑。

**失败恢复**：
- job 状态持久化 `settings_kv ai.lab.jobs.v1`（最多 20 条：id/status/params/rounds/created_at）；
- 进程重启时，`status=running` 的 job 在启动时被标为 `interrupted`，**不自动续跑**（避免半夜烧钱）；
- 每轮完成后立即写盘，崩溃最多丢 1 轮；
- `cancel` 通过 `threading.Event` 传给 `should_stop`，当前回测结束即停（回测是同步函数，无法中断，但一轮 < 1s）。

**应用结果**：`POST /api/ai/optimize/{job_id}/apply` 把 `best.params` 写成
`ai_quant.params` 的新修订（`source="lab"`），**不自动启动实盘**；页面提示"已写入量化基线，需人工启动"。

### 6.4 实时调参边界（AI 量化）

只允许：

1. **已审核参数**：`AIQuantSettings.params` 的 key 必须 ∈ `strategy.params_spec` 的 name 集合；
   未知 key 直接丢弃（不是报错，避免模型输出噪声导致会话停摆）。
2. **限幅**：每个数值参数单次变化 ≤ `tune_max_step_pct`（默认 25%），且必须落在 `ParamSpec.minimum/maximum` 内；
   `choice` 参数只允许在 `choices` 内换值。越界 → 钳制或拒绝，记录 `clamped`。
3. **频率**：两次调参间隔 ≥ `tune_min_interval_bars` 根收盘 K 线；每日 ≤ `max_tunes_per_day` 次。
4. **留痕**：每次变更写 `ParamRevision`（rev 自增）到 `settings_kv ai.quant.revisions.v1`（保留 200 条）
   与 `logs/ai-quant-audit.jsonl`（含 before/after/reason/metrics/来源）。
5. **回滚**：`POST /api/ai/quant/rollback {rev}` 恢复该 rev 的 params，并写一条 `source="rollback"` 的修订；
   回滚不重启会话。
6. **执行**：通过 `AutoTrader.update_params()` 热更新（不 stop/start，避免丢失 `_current_target` 造成额外调仓）：

```python
# engine/auto.py 修改
def update_params(self, params: dict[str, Any], *, reason: str = "") -> None:
    with self._config_lock:
        self.config.params = dict(params)
        self._params_rev += 1
    self._emit_log("INFO", f"参数已更新（{reason}）")
```

`_tick` 里改为：`params = dict(self.config.params)`（持锁取快照）→ `self.strategy.generate(closed, params)`。

7. **模式**：
   - `manual`：AI 只给建议，`tune` 接口返回候选，**必须** `apply=true` 才写入；
   - `assisted`：同上，但界面高亮"AI 建议你改成 X"；
   - `auto`：在限幅/频率/每日上限内自动写入（默认关闭，需显式选）。

### 6.5 路由与 payload / 响应示例

**候选策略（routes_lab.py）**

```json
POST /api/ai/strategies/generate
{"idea": "波动率突破，ATR 通道入场", "base_strategy": "ma_cross",
 "symbol": "BTC/USDT", "market": "usdt", "timeframe": "1h", "bars": 800}
→ {"job_id": "ab12cd34ef56", "status": "running"}

GET /api/ai/strategies/candidates?status=pending
{"items": [{"id":"cand-20261020-214500-momo","strategy_key":"momo_atr",
            "name":"ATR 动量","status":"pending","created_at":1761000000,
            "sha256":"9f2c…","checks_ok":true,"checks_errors":[],
            "backtest":{"trades":86,"total_return":0.184,"max_drawdown":-0.093,
                        "win_rate":0.52,"sharpe":1.31}}]}

GET /api/ai/strategies/candidates/{id}
{"candidate": {...}, "source": "import pandas as pd\n...", "checks": [...],
 "diff": "...", "backtest": {...}}

POST /api/ai/strategies/candidates/{id}/validate → {"job_id": "..."}   // 子进程回测
POST /api/ai/strategies/candidates/{id}/review
{"approve": true, "note": "检查通过，纸面跑一周", "expected_sha256": "9f2c…"}
→ {"ok": true, "strategy_key": "momo_atr", "reloaded": true, "keys": ["ma_cross","momo_atr",...]}
→ 409 {"detail": "文件在审核期间被修改，请重新检查"}

POST /api/ai/strategies/reload → {"ok": true, "keys": [...], "errors": []}
```

**多轮优化**

```json
POST /api/ai/optimize
{"strategy":"ma_cross","base_params":{"fast":10,"slow":30},
 "symbol":"BTC/USDT","market":"spot","timeframe":"1h","bars":800,
 "rounds":5,"metric":"sharpe",
 "bounds":{"fast":[5,10,20,30],"slow":[30,50,80]}}
→ {"job_id":"...","status":"running"}

GET /api/ai/optimize/{job_id}
{"id":"...","status":"done","progress":1.0,
 "result":{"best":{"fast":20,"slow":80},"best_metrics":{"sharpe":1.62,"total_return":0.21,
           "max_drawdown":-0.11,"trades":64},"calls":4,"stopped":"no_improvement",
           "rounds":[{"round":1,"params":{"fast":20,"slow":50},"metrics":{...},
                      "accepted":true,"reason":"夏普 1.42 > 1.31",
                      "reflection":"趋势延续性强，建议拉长慢线"}]}}

POST /api/ai/optimize/{job_id}/cancel → {"ok": true}
POST /api/ai/optimize/{job_id}/apply  → {"ok": true, "params": {...}, "revision": {"rev": 4, ...}}
```

**实时反思**

```json
GET /api/ai/quant/reflections?limit=20
{"items":[{"at":1761000000,"text":"近 4 根 K 线振幅收窄，趋势策略易被来回打",
           "suggestion":"hold","params_hash":"…","tokens":420}]}
```

### 6.6 文件级改动清单

| 文件 | 动作 | 内容 |
| --- | --- | --- |
| `src/btrader/strategy/sandbox.py` | **新增**（~260 行） | `CheckReport`、`check_source`（C1–C13）、`validate_in_subprocess`、常量表 |
| `src/btrader/strategy/candidates.py` | **新增**（~280 行） | `Candidate`、`CandidateStore`（create/list/get/set_status/approve/reject/cleanup）、哈希与原子写 |
| `src/btrader/strategy/registry.py` | 修改（+~30 行） | `_lock`、`reload()` 原子换表、`get/keys/meta_list` 加锁 |
| `src/btrader/ai_optimize.py` | **新增**（~300 行） | `OptimizeRound`、`optimize_params`、`reflect_tuning` 的调用编排、预算/停止条件 |
| `src/btrader/ai.py` | 修改（+~140 行） | `generate_strategy`、`reflection_prompt`、`choose_strategy`、`parse_params_object` |
| `src/btrader/llm.py` | 修改 | `append_audit("ai-strategy-audit.jsonl", ...)` 复用（Phase 3 已抽） |
| `src/btrader/paths.py` | 修改 | `strategy_candidates_dir()`（Phase 3 可先加） |
| `src/btrader/web/routes_lab.py` | **新增**（~300 行） | 上表 10 条路由 + payload |
| `src/btrader/web/controllers.py` | 修改 | `StrategyLabController`（JobStore + cancel event + 持久化） |
| `src/btrader/web/routes.py` | 修改 | include lab router |
| `web/src/pages/StrategyLabPage.tsx` | **新增**（~260 行） | 三个 Tab：候选策略 / 多轮优化 / 调参历史 |
| `web/src/components/CandidateReview.tsx` | **新增**（~220 行） | 代码查看、检查报告、diff、批准/拒绝 |
| `web/src/components/OptimizeRounds.tsx` | **新增**（~160 行） | 轮次表、反思文本、apply |
| `web/src/App.tsx` / `Sidebar.tsx` | 修改 | `PageKey` 加 `"lab"`，侧栏加「实验室」 |
| `web/src/types.ts` | 修改（+~60 行） | `StrategyCandidate`、`OptimizeJob`、`OptimizeRound`、`QuantRevision` |
| `tests/test_strategy_sandbox.py` / `test_ai_optimize.py` | **新增** | §8.3 |

### 6.7 前端：策略实验室页

```
实验室（侧栏第 3 项）
┌ Tab：候选策略 | 多轮优化 | 调参历史 ─────────────────────────────┐
│ 候选策略：列表（状态筛选）+ 详情抽屉                              │
│   ├ 左：源码（等宽、行号、只读）                                  │
│   ├ 中：静态检查报告（C1–C13，失败项红色）+ 与 base 的 diff        │
│   ├ 右：回测摘要（收益/回撤/交易数/胜率）+ [校验][批准][拒绝]     │
│ 多轮优化：表单（策略/参数/轮数/指标/边界）→ 轮次表 + 反思文本     │
│   └ [应用为量化基线]（不自动启动实盘）                            │
│ 调参历史：revisions 表（rev/时间/来源/before→after/原因/回滚）     │
└──────────────────────────────────────────────────────────────────┘
```

### 6.8 验收标准

- [ ] `import os` / `import subprocess` / `open(` / `eval(` / `__import__` 的候选**全部**被判 invalid，且落盘不覆盖已有文件。
- [ ] 候选只出现在 `strategies/candidates/`；`strategies/*.py` 在没有"批准"动作前不新增任何文件。
- [ ] 批准流程：sha256 不一致 → 409；一致 → 复制 + `reload`，`GET /api/strategies` 立即出现新 key（不重启后端）。
- [ ] 被拒绝的候选移入 `candidates/rejected/`，不参与 registry。
- [ ] 子进程校验超时（构造 `while True` 之外的慢循环）→ 60s 内被杀，job 标记 error，不影响后端。
- [ ] 多轮优化：轮数/预算/墙钟三个上限各自单独生效（构造 FakeLLM 每轮都给新参数）。
- [ ] 优化过程中关闭开关/取消 → 当前轮结束后停止，已完成的轮次保留。
- [ ] 进程重启后 `running` 的 job 变 `interrupted`，不自动续跑。
- [ ] 实时调参：超过 ±25% 被钳制；未知参数被丢弃；每日上限生效；每次变更可在 3 秒内的修订表看到。
- [ ] 回滚后 `AutoTrader` 用旧参数（不重启、`_current_target` 不变）。
- [ ] 新增 `tests/test_strategy_sandbox.py` / `test_ai_optimize.py` 全绿且离线。

### 6.9 风险与缓解

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| 模型生成恶意/资源耗尽代码 | 本机数据被读/进程被拖死 | 静态白名单 + 子进程超时 + 人工审核；强化隔离留 Phase 5 |
| 审核者"看都没看"点通过 | 等于自动执行 | UI 必须展示 diff + 检查报告；批准需二次输入 key；文案警告权限 |
| 多轮回测过拟合 | 上线即亏 | 最少交易数、回撤不恶化、min_improvement、只输出建议不自动启用 |
| 优化烧钱 | 成本 | 轮数/调用/墙钟三重上限 + 缓存 + 默认关闭 |
| 热调参导致状态不一致 | 目标仓位跳变 | 只改 params 不改 position_pct/leverage；间隔 ≥4 根 K 线；回滚即时 |
| registry 热重载与并发读 | 500 | 原子换表 + 锁 + 测试 |
| 候选 key 冲突 | 覆盖内置策略 | C10 + 批准时再查一次 |

---

## 7. 与 README 硬约束「AI 不下单」的关系（裁决）

### 7.1 裁决

**保留"执行内核确定性"这条硬约束，修改"AI 不下单"这句表述。**

具体是三条：

1. **保留（永不修改）**：AI 不进入 `strategy.generate()`，不进入 `run_backtest()` 循环，不参与每一根 K 线的信号计算。
   回测结果必须可复现；`ai-filter` 的对比之所以有意义，正是因为两次跑的是同一套确定性信号（见 `routes.py:1519` 注释）。
2. **保留**：AI 的分析/复核/简报仍然只读，不产生订单。
3. **新增受控例外**：AI 可以"发起订单"，但只能经 **AI 下单网关**（`AITrader`）→ `TradingService.submit()` → `RiskManager`。
   网关默认关闭；纸面/实盘各有独立开关；实盘四重条件；每单都有 AI 专属收紧限额；随时可急停。

### 7.2 为什么建议保留「不进 generate()/回测循环」（理由）

1. **可复现性**：回测是本项目所有决策的地基（参数优化、AI 过滤对比、历史回放）。模型进循环 = 每次跑结果不同，
   `ai.compare_runs()` 的"过滤前后"对比立刻失去意义——两个 run 不再是"同一策略、同数据、唯一差异是过滤"。
2. **成本与延迟不可控**：`generate()` 每根 K 线调用一次，800 根 = 800 次模型调用；`AutoTrader._tick()` 在
   交易线程里，模型 20–40s 的延迟会直接变成"漏掉的止损"（这正是 `BriefQueue` 存在的理由，`ai.py:810`）。
3. **风控只有一条咽喉**：全部限额都在 `RiskManager`，而它只在 `TradingService.submit()` 里被调用。
   如果允许"模型直接生成信号→引擎照单执行"，信号就绕过了风控的语义（模型可以说"100% 仓位"）。
   保留"策略输出 target，引擎负责执行与风控"的分层，才能保证任何来源的订单都过同一道闸。
4. **可审计粒度**：按 K 线一根一次决策可以完整落盘（`ai-trade-audit.jsonl`），按 bar 每根调用无法审。
5. **与现有设计一致**：`suggest_strategy` 明确只返回"已有策略的参数字典"，注释写着"Letting a model author a
   strategy file would hand it the same privileges as the backend"（`ai.py:502`）——同一个思路，
   Phase 4 也遵守：AI 写代码 → 人工审核 → 变成普通确定性策略，**运行时模型不在场**。

一句话版本：**"模型只在两次确定性执行之间做决策，永不进入执行内部。"**（决策可以影响"做什么"，不能影响"怎么算"。）

### 7.3 新的边界表述（README 用）

> - **AI 不进入策略内核**。模型永远不在 `strategy.generate()` 或回测循环里：一次回测的每一根 K 线都由确定性代码算出，
>   所以结果可以复现，也不会因为模型今天状态不同而变。
> - **AI 不会直接下单**。所有订单只有一条出口：`TradingService` → `RiskManager`。
>   手动下单、策略自动交易、AI 自主交易**都走这一条**，因此单笔/日限额、最大持仓、杠杆、熔断、急停对三者同时生效。
> - **AI 自主交易是受控例外**：默认关闭；纸面与实盘是两个独立开关；实盘还要求主网、显式二次确认、未急停；
>   AI 有比通用风控更紧的专属限额；模型失败、答案读不懂、行情取不到时**一律不下单、保留原仓**；随时可一键急停。
> - **AI 写策略代码必须人审**。候选只写到 `strategies/candidates/`，静态检查 + 子进程回测 + 人工批准后才进入正式目录；
>   批准后它就是一个普通的确定性策略，运行时模型不参与。

### 7.4 README 逐段替换要点

| 段落 | 现状 | 替换要点 |
| --- | --- | --- |
| 「功能一览」表（README:13-21） | 「自动交易」独立一行；无 AI 自主/量化/实验室 | `自动交易` → `交易页内嵌自动交易（策略/参数/启停/日志）`；新增三行：`AI 自主交易`（纸面+实盘，受控）、`量化`（AI 控制策略参数）、`策略实验室`（候选代码审核 + 多轮优化，Phase 4） |
| 「AI 助手」硬约束列表（README:104-125） | 第一条「**AI 不下单**。订单只由策略和风控产生…」 | 换成 §7.3 四段；其余五条保留，补一条"AI 自主交易的决策与订单都写 `logs/ai-trade-audit.jsonl`" |
| 「风控」段（README:161-169） | 「所有下单（手动与自动）都会经过 RiskManager」 | 改成「所有下单（手动、策略自动、AI 自主）都会经过 `RiskManager`」；补：AI 专属限额（单笔/日笔数/置信度/冷却）；急停按钮位置（交易页页头 + 设置页）；实盘四重条件 |
| 数据目录树（README:59-68） | `└─ strategies/ # 用户策略插件目录` | 补 `strategies/candidates/`（AI 候选，未审核不加载）、`logs/ai-trade-audit.jsonl`、`logs/ai-quant-audit.jsonl`、`logs/ai-strategy-audit.jsonl` |
| 「使用流程」（README:127-137） | 回测 → 纸面 → 实盘 | 中间插入 AI 自主交易/量化的建议顺序：先用纸面 AI 跑 ≥1 周 → 看审计与回撤 → 再考虑实盘；候选策略必须先纸面验证 |
| 「策略插件」（README:138-159） | 放入 `.py` 即可加载 | 补「AI 生成的候选不在这里，永不自动加载；审核通过才会被复制进来」；补 UI 路径（实验室页） |
| 「项目结构」（README:183-193） | engine/ 描述 | 补 `engine/ai_trader.py`、`engine/ai_quant.py`、`strategy/sandbox.py`、`strategy/candidates.py`、`ai_optimize.py` |
| 「测试」（README:195-203） | 覆盖指标/回测/风控/AI 解析 | 补「AI 交易网关、量化调参、候选沙箱、多轮优化」四类；强调全部离线（FakeLLM/StubTrading） |

### 7.5 AGENTS.md 增补（§6「AI 助手」之后加一节）

要点（实现阶段写进去）：

- **下单边界**：AI 相关模块只允许 `trading.submit()` 下单；禁止 `import` `exchange.rest` / `engine.paper`；
  新增模块的依赖方向见 `docs/ai-autonomy-plan.md §5.1`。
- **设置位置**：AI 自主/量化/实验室的设置都在 `core/config.py`（`AIAutoSettings` 等），**不要**加到 `llm.AISettings`
  （transport 层只放供应商/预算/缓存）；控制器每次读 `ctx.settings.*`，不要捕获嵌套对象。
- **三个新审计文件**：`logs/ai-trade-audit.jsonl`、`logs/ai-quant-audit.jsonl`、`logs/ai-strategy-audit.jsonl`；
  写之前先过 `llm.redact()`，禁止把 API Key/完整 prompt 写进去。
- **候选策略**：只写 `strategies/candidates/`；任何"自动加载候选"的改动都是回归。
- **前端**：改了 `web/src/components/AutoTraderPanel.tsx` 等也要 `npm run build`（同 §2 表格）。
- **测试**：`tests/test_ai_trader.py` / `test_ai_quant.py` / `test_strategy_sandbox.py` / `test_ai_optimize.py`
  必须离线：`FakeLLM` + `StubTrading`（记录 submit 调用）+ `tmp_path` 重定向 `BITRADER_DATA_DIR`。

---

## 8. pytest 离线测试清单

**通用基建（先做，三阶段共用）**

- `tests/conftest.py` 增加自动 fixture：`tmp_path` 下设置 `BITRADER_DATA_DIR`（`paths._base_dir()` 读环境变量，
  `paths.py:12`），保证不碰真实 `%APPDATA%`。
- `tests/helpers.py` 增加 `FakeLLM`（已有形态见 `tests/test_ai.py:29`，可上移到 helpers 供三个新文件复用）、
  `StubTrading`（记录 `submit` 调用、可配置返回/抛 `OrderRejected`）、`StubFrameProvider`、`StubLease`。
- 所有新测试不得访问网络、不得依赖密钥环、不得起后端（沿用 `tests/test_auto.py` 的 Stub 风格）。

### 8.1 Phase 2（`tests/test_web_auto_merge.py` + 既有回归）

| # | 测试 | 断言 |
| --- | --- | --- |
| 1 | `test_auto_page_removed` | `web/src/pages/AutoPage.tsx` 不存在 |
| 2 | `test_auto_panel_component_exists` | `AutoTraderPanel.tsx` 存在且含 `export default` |
| 3 | `test_trading_page_mounts_auto_panel` | `TradingPage.tsx` 含 `AutoTraderPanel` 且把 `auto` 传给 `BottomTabs` |
| 4 | `test_bottom_tabs_has_auto_tab` | `BottomTabs.tsx` 含 `["auto", "自动"]` |
| 5 | `test_sidebar_and_app_have_no_auto_page` | `Sidebar.tsx`/`App.tsx` 不再含 `"auto"` 页面项 |
| 6 | `test_shared_auto_status_hook_used_once` | `useAutoStatus.ts` 存在；`AutoTraderPanel.tsx` import 它；`TradingPage.tsx` 不再自行 `setInterval` 拉 `/api/auto/status` |
| 7 | `test_auto_start_payload_fields_frozen` | `AutoStartPayload` 字段集合与文档 §4.4 一致（防止合并时改坏契约） |
| 8 | `test_auto_status_shape` | `AutoController.status()` 返回 `running/config/last_signal/seq/events`（fake ctx） |
| 9 | `test_auto_start_rejects_live_on_testnet` | `AutoController.start` 在 testnet + live 抛 400（`routes.py:302`） |
| 10 | `test_auto_start_requires_confirm_live` | 缺 `confirm_live` 抛 400 |
| 11 | `test_auto_start_requires_credentials_for_live` | 无 key 抛 400 |
| 12 | `test_auto_stop_when_not_running_is_noop` | `stop()` 不抛 |

### 8.2 Phase 3（`tests/test_ai_trader.py` / `tests/test_ai_quant.py`）

**决策解析与校验（纯函数，先写）**

| # | 测试 | 断言 |
| --- | --- | --- |
| 1 | `test_parse_decision_from_prose_and_fence` | 带解释/```json 也能取出对象 |
| 2 | `test_parse_decision_returns_none_without_json` | 返回 `(None, 原因)`，不抛 |
| 3 | `test_unknown_action_becomes_hold` | V1 |
| 4 | `test_confidence_below_threshold_downgrades` | V9 → `hold`，不产生订单 |
| 5 | `test_stop_loss_wrong_side_rejected` | V7 |
| 6 | `test_level_outside_range_rejected` | V8 + 不画/不下单 |
| 7 | `test_quote_above_cap_rejected_not_clamped` | V4 |
| 8 | `test_spot_sell_without_position_rejected` | V3 |

**网关行为（`AITrader` + Stub）**

| # | 测试 | 断言 |
| --- | --- | --- |
| 9 | `test_ai_trader_never_calls_broker_directly` | `StubTrading.submit_calls == 1`；`PaperBroker.account.orders` 为空（未被直接调用） |
| 10 | `test_risk_rejection_blocks_ai_order` | `kill_switch=True` → 无订单，决策 `outcome=rejected` |
| 11 | `test_model_failure_places_no_order_and_keeps_position` | `FakeLLM(ok=False)`；持仓数量不变 |
| 12 | `test_market_unreachable_skips_decision` | provider 抛错 → 不调用 `FakeLLM` |
| 13 | `test_stale_decision_is_skipped` | `decided_at` 超 TTL |
| 14 | `test_price_drift_skips_order` | 漂移 > 0.5% |
| 15 | `test_daily_decision_limit_stops_before_llm_call` | 计数达上限后 `FakeLLM.prompts` 不增长 |
| 16 | `test_live_start_requires_all_four` | 四个条件逐个缺失 → 400/409（参数化 4 例） |
| 17 | `test_live_spot_with_stop_rejected` | INV-13 |
| 18 | `test_halted_blocks_open_allows_close` | 熔断时 `open` 拒绝、`close` 允许（reduce-only） |
| 19 | `test_lease_blocks_second_trader` | auto 占用时 ai start → 409，反之亦然 |
| 20 | `test_trade_audit_has_required_fields` | 每笔决策一行，含 §5.6 列出的字段 |
| 21 | `test_state_machine_transitions` | idle→running→paused→running→stop→idle；异常→error |
| 22 | `test_restart_leaves_state_idle` | 持久化里是 running，新实例读出 idle + 上次原因 |

**量化**

| # | 测试 | 断言 |
| --- | --- | --- |
| 23 | `test_tune_clamped_to_max_step` | 10→20 被钳到 12（25%） |
| 24 | `test_unknown_param_dropped` | 模型多给 `foo` 不写入 |
| 25 | `test_tune_interval_enforced` | 间隔内再次 tune 被拒 |
| 26 | `test_max_tunes_per_day` | 第 N+1 次被拒 |
| 27 | `test_revision_recorded_and_rollback` | 回滚后 params == rev1，且新增一条 rollback 修订 |
| 28 | `test_auto_trader_update_params_next_tick` | 更新后 `strategy.generate` 收到新 params |
| 29 | `test_quant_llm_failure_keeps_params` | 参数不变，事件流有失败记录 |
| 30 | `test_live_quant_requires_live_enabled` | 默认关 → 400 |

### 8.3 Phase 4（`tests/test_strategy_sandbox.py` / `tests/test_ai_optimize.py`）

**沙箱静态检查**

| # | 测试 | 断言 |
| --- | --- | --- |
| 1 | `test_valid_minimal_strategy_passes` | ok，key 正确 |
| 2 | `test_import_os_rejected` | errors 含"禁止导入 os" |
| 3 | `test_import_subprocess_rejected` | 同上 |
| 4 | `test_open_eval_dunder_rejected` | `open(...)`、`eval(...)`、`__import__` 各自被拒 |
| 5 | `test_oversize_source_rejected` | 字节数与行数两个上限分别命中 |
| 6 | `test_syntax_error_rejected` | 报行号 |
| 7 | `test_missing_generate_rejected` | C8 |
| 8 | `test_duplicate_key_rejected` | C10（传入 `known_keys`） |
| 9 | `test_params_spec_must_use_builtin` | C11 |
| 10 | `test_infinite_loop_rejected` | C12 |
| 11 | `test_check_never_imports_candidate` | 用一个会在 import 时写文件的模块，断言文件未创建（只 parse） |

**候选存储与审核**

| # | 测试 | 断言 |
| --- | --- | --- |
| 12 | `test_candidate_written_only_to_candidates_dir` | 正式目录没有新文件 |
| 13 | `test_approve_copies_and_reloads` | tmp 目录下批准后 `registry.keys` 含新 key |
| 14 | `test_approve_sha_mismatch_rejected` | 409 风格异常 |
| 15 | `test_rejected_candidate_not_registered` | registry 不含 |
| 16 | `test_invalid_candidate_cannot_be_approved` | status=invalid → 拒绝批准 |
| 17 | `test_subprocess_timeout_reported` | monkeypatch runner → 返回 error，不抛 |

**多轮优化**

| # | 测试 | 断言 |
| --- | --- | --- |
| 18 | `test_stops_at_max_rounds` | 轮数 = min(配置, 模型愿意给的) |
| 19 | `test_budget_calls_cap` | `FakeLLM.prompts` 数 ≤ max_calls |
| 20 | `test_no_improvement_early_stop` | 2 轮不提升即停，`stopped=no_improvement` |
| 21 | `test_rejected_params_keep_best` | best 不被差参数覆盖 |
| 22 | `test_drawdown_worse_rejects` | 收益高但回撤恶化 → rejected |
| 23 | `test_llm_failure_continues_then_stops` | 连续 3 次失败 → `stopped=error` |
| 24 | `test_reflection_prompt_contains_only_computed_metrics` | 不出现原始 DataFrame / 未来数据 |
| 25 | `test_same_params_cached` | 同参数+同指标不重复调用模型（`FakeLLM` 调用数） |
| 26 | `test_interrupted_job_marked_after_restart` | 持久化里 running → 启动时 interrupted |
| 27 | `test_apply_writes_revision` | 应用后 `AIQuantSettings.params` = best，新增 source=lab 修订 |
| 28 | `test_cancel_stops_after_current_round` | `stopped=cancel`，已完成轮次保留 |

**门槛**：`pytest -q` 全绿；新增用例总耗时 < 30s；不新增网络/密钥环/子进程（除沙箱测试里的 monkeypatch 替身）。

---

## 9. 本轮不做 / 后续里程碑

| 项 | 为什么这轮不做 | 建议阶段 |
| --- | --- | --- |
| AI 分析浮窗 + 画板画线 | 已由队友并行（`FloatingWindow.tsx`、`drawingsStore.ts`），本文不重复设计 | 进行中 |
| 强隔离沙箱（AppContainer / 低权限令牌 / Job Object / 禁网） | 工程量大、需要打包配合；静态检查+子进程+人审已能覆盖主要风险 | Phase 5 |
| 多交易对/多标的并行 AI 交易 | 风控、lease、审计都要按标的扩展；先做单标的验证正确性 | Phase 5+ |
| AI 自主修改风控参数（限额/杠杆/熔断阈值） | **永不允许**：模型不能提高自己的权限 | 永不 |
| AI 自动开关实盘 | 不允许：实盘开关只能人工 + 二次确认 | 永不 |
| 自动批准候选策略 | 不允许：审核是唯一信任边界 | 永不 |
| 新闻/链上/资金费率接进决策 prompt | 需要数据管道与新的隐私评估；先验证纯 K 线摘要的决策质量 | Phase 5 |
| 多轮回测并行跑（多 job 并发） | 现有 JobStore 是内存表，并发会抢 CPU 且烧钱；先串行 | Phase 5 |
| 模型微调/训练、向量库、RAG | 与"确定性内核"定位无关，成本高 | 不做 |
| 多账户 / 多交易所 | 与本次需求无关 | 不做 |
| 移动端 / Electron 外壳改动 | 不涉及 | 不做 |
| 把最优回测参数一键上实盘 | 高风险；只允许"应用到量化基线 + 人工启动"，且默认纸面 | 不做（保持） |
| 自动交易页作为独立路由保留 | 与需求②冲突；迁移完成即删 | Phase 2 内 |

---

## 10. 附录

### A. 改动文件总表（按阶段）

| 阶段 | 新增 | 修改 | 删除 |
| --- | --- | --- | --- |
| Phase 2 | `web/src/components/AutoTraderPanel.tsx`、`web/src/hooks/useAutoStatus.ts`、`tests/test_web_auto_merge.py` | `web/src/pages/TradingPage.tsx`、`web/src/components/BottomTabs.tsx`、`web/src/App.tsx`、`web/src/components/Sidebar.tsx`、`web/src/styles.css` | `web/src/pages/AutoPage.tsx` |
| Phase 3 | `src/btrader/engine/ai_trader.py`、`src/btrader/engine/ai_quant.py`、`src/btrader/web/controllers.py`、`src/btrader/web/routes_ai.py`、`web/src/components/AITraderCard.tsx`、`AITraderPanel.tsx`、`AIQuantPanel.tsx`、`tests/test_ai_trader.py`、`tests/test_ai_quant.py` | `ai.py`、`engine/auto.py`、`engine/trading.py`、`core/config.py`、`llm.py`、`paths.py`、`web/routes.py`、`web/src/types.ts`、`TradingPage.tsx`、`BottomTabs.tsx`、`SettingsPage.tsx` | — |
| Phase 4 | `strategy/sandbox.py`、`strategy/candidates.py`、`ai_optimize.py`、`web/routes_lab.py`、`web/src/pages/StrategyLabPage.tsx`、`web/src/components/CandidateReview.tsx`、`OptimizeRounds.tsx`、`tests/test_strategy_sandbox.py`、`tests/test_ai_optimize.py` | `strategy/registry.py`、`ai.py`、`controllers.py`、`routes.py`、`App.tsx`、`Sidebar.tsx`、`types.ts` | — |

### B. 建议常量（集中定义，便于调）

| 常量 | 位置 | 值 | 说明 |
| --- | --- | --- | --- |
| `AI_DECISION_CACHE_TTL` | `ai.py` | 600s | 与 `llm.DEFAULT_CACHE_SECONDS` 一致 |
| `AI_AUDIT_LIMIT_BYTES` | `llm.py` | 2MB | 复用现有滚动策略 |
| `AI_TRADE_MAX_DECISION_AGE` | `ai_trader.py` | 120s | 决策新鲜度硬上限 |
| `AI_TRADE_DEFAULT_QUOTE` | `config.py` | 50 USDT | 单笔默认上限（保守） |
| `TUNE_MAX_STEP_PCT` | `config.py` | 0.25 | 单次调参最大幅度 |
| `LAB_MAX_SOURCE_BYTES` | `config.py` | 20_000 | 候选源码上限 |
| `LAB_VALIDATE_TIMEOUT` | `config.py` | 60s | 子进程回测上限 |
| `LEGACY_PAGE_AUTO` | — | 删除 | Phase 2 后不存在 |

### C. 术语

| 术语 | 含义 |
| --- | --- |
| 执行内核确定性 | 模型不参与 `generate()`/`run_backtest()`，每根 K 线由确定性代码计算 |
| AI 下单网关 | `AITrader`：唯一允许把模型决策转成 `OrderRequest` 的模块 |
| 前置收紧 | AI 专属限额在 `TradingService.submit` 之前拦截，与 `RiskManager` 是"与"关系 |
| TradeLease | auto/ai/quant 三主体互斥租约 |
| 候选（candidate） | AI 生成、未审核、放在 `candidates/`、永不 import 的策略源码 |
| 修订（revision） | 一次参数变化的不可变记录，可回滚 |
| 热调参 | 不重启会话地修改已审核参数 |

### D. 关键代码索引（实现前对照）

| 用途 | 位置 |
| --- | --- |
| 唯一下单出口 | `src/btrader/engine/trading.py:313` `TradingService.submit` |
| 风控闸门 | `src/btrader/engine/risk.py:142` `check_order` |
| 纸面撮合/保护单 | `src/btrader/engine/paper.py:94 / 301 / 333` |
| 自动交易循环 | `src/btrader/engine/auto.py:153` `_tick` |
| 自动交易控制器/事件流/简报 | `src/btrader/web/routes.py:274 / 247 / 288` |
| 回测/优化/AI 过滤任务 | `src/btrader/web/routes.py:1396 / 1461 / 1519` |
| 后台撮合线程 | `src/btrader/web/app.py:1404` |
| AI 摘要/校验/简报队列 | `src/btrader/ai.py:51 / 246 / 810` |
| LLM 传输/缓存/预算/审计 | `src/btrader/llm.py:311 / 199 / 415 / 276` |
| 设置/密钥环约定 | `src/btrader/core/config.py:19,38`、`src/btrader/llm.py:37` |
| 策略加载 | `src/btrader/strategy/registry.py:29` |
| 前端自动交易页 | `web/src/pages/AutoPage.tsx`（将被删除） |
| 前端交易页/底部面板 | `web/src/pages/TradingPage.tsx:495`、`web/src/components/BottomTabs.tsx:22` |
| 前端 AI 分析卡（Phase 3 参考样式） | `web/src/components/AIPanel.tsx` |

---

### 附：Phase 2 第一步（给 Lead 的落地建议）

**文件级第一步**：新建 `web/src/hooks/useAutoStatus.ts`，把 `AutoPage.tsx:64-83` 的轮询原样迁过去；
然后新建 `web/src/components/AutoTraderPanel.tsx`，把 `AutoPage.tsx:137-433` 的 JSX 迁进去、
`market/symbol/timeframe` 改成 props。此时 `AutoPage.tsx` 缩成 wrapper，页面行为零变化。
这一步不改 Python、不删任何东西，`npm run build` 通过即可验证——是整条链路上风险最低的落点。
