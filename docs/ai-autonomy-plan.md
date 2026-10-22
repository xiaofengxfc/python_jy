# AI 自主化设计

这份文档写 AI 相关的三块功能各自做什么、边界在哪、哪些约束不能动。
代码按这份文档实现，改代码前先确认没有踩到第 6 节的不变量。

| 功能 | 入口 | 主要文件 | 会不会下单 |
|---|---|---|---|
| AI 交易 | 交易页的「AI 交易」面板 | `engine/ai_trader.py`、`ai.py` | 会，模型给决策，闸门放行后下单 |
| AI 量化 | 交易页的执行引擎 + 策略实验室 | `strategy/candidates.py`、`web/routes_lab.py`、`engine/auto.py` | 会，按已批准策略的信号自动下单 |
| AI 助手 | 图表上的 AI 分析、回测页的 AI 过滤 | `ai.py`、`llm.py` | 不会，只读不改仓 |

## 1. 依赖方向

- `engine/ai_trader.py` 只依赖四样东西：`TradingService.submit()`、`RiskManager`、
  `ai.py` 和审计表。它不 import 交易所，也不 import storage。
- `ai.py` 只管提示词和解析，不认识账户和订单。
- `llm.py` 只管收发：多供应商、缓存、审计、限额。它不认识策略和风控。
- 反过来不成立。策略、回测、风控都不 import AI 模块。

## 2. 一笔 AI 决策长什么样

模型只输出一个 JSON 对象：

```json
{
  "action": "open_long | open_short | close_long | close_short | hold",
  "symbol": "BTC/USDT",
  "quote": 50.0,
  "price": 82123.4,
  "stop_loss": 81500.0,
  "take_profit": null,
  "confidence": 0.72,
  "ttl_sec": 120,
  "rationale": "一句话理由"
}
```

字段约束：

| 字段 | 约束 | 不满足时 |
|---|---|---|
| `action` | 上面五个值之一 | 拒绝 |
| `symbol` | 必须等于配置里的交易对 | 拒绝 |
| `quote` | 大于 0，且不超过单笔上限 | 拒绝，不截断 |
| `stop_loss` | 开仓时必填，方向要合理 | 拒绝 |
| `confidence` | 0 到 1 | 拒绝 |
| `ttl_sec` | 不为负 | 拒绝 |

schema 里没有 `leverage`。杠杆由启动参数决定，模型改不了。

## 3. 提示词约束

- 只给已经走完的 K 线。最后一根还在走的 K 线不发给模型。
- 价位必须落在给它的数据区间内，画图前还要过 `ai.validate_levels`。
- 指标先算好，以文本形式发给模型。不发截图。
- 回测复核只给开仓前的 K 线。模型看不到这笔交易后来赚没赚钱。
- 解析不出 JSON 就是失败，调用方不许下单。

## 4. 状态机

| 状态 | 含义 | 怎么离开 |
|---|---|---|
| `stopped` | 默认状态，不轮询 | `start()` |
| `running` | 轮询中，按周期决策 | `pause()`、`stop()`、`halt()`、`kill()` |
| `paused` | 线程还在，但不产生新决策 | `resume()` |
| `halted` | 熔断，直到人工处理 | `stop()` 之后重新 `start()` |

`start()` 会先停掉上一个进程。它还握着共享租约，不停掉新的就拿不到。

## 5. 线程

- 一个 poller 线程 + 一个停止事件。停止事件清掉之后旧线程可能复活，
  所以上一轮没退出时 `start()` 直接拒绝：`上一轮决策线程仍在退出，请稍后再试`。
- 模型调用可能卡十几秒。这几秒里按了停止，这一单不许再发出去。
- 自动交易的收盘简报走 `ai.BriefQueue`，独立线程，队列满就丢，不能阻塞 `_tick()`。
- 查行情失败、策略信号不可用、模型调用失败都只记一条 skip，不抛异常。

## 6. 不变量

改这十五条的任意一条，都要先改这份表和对应的测试。

| 编号 | 约束 | 落在哪 |
|---|---|---|
| INV-1 | AI 订单只能经 `TradingService.submit()` 出去，不允许直连交易所 | `engine/ai_trader.py` |
| INV-2 | 每笔 AI 订单都要过 `RiskManager`，AI 限额是在前面加一层，不是替代 | `AIGuard` |
| INV-3 | 单笔上限取 AI 上限和风控上限的较小值。超限就拒绝，不截断 | `ai.validate_decision`、`AIGuard` |
| INV-4 | 日决策数和小时下单数分别计数，跨日重置 | `AIGuard` |
| INV-5 | 最大持仓：AI 先验，`RiskManager` 再挡一层 | `AIGuard` |
| INV-6 | 杠杆不能由模型改。要改杠杆只能停了再启动 | `AITraderConfig` |
| INV-7 | 熔断和急停优先于一切。熔断时只允许减仓 | `RiskManager.check_order` |
| INV-8 | 急停一个接口可达：`POST /api/risk/kill` 打开急停，停掉自动交易和 AI 交易，再发通知 | `web/routes.py` |
| INV-9 | 主网下单要同时满足：`live_enabled`、`confirm_live`、主网、未急停 | `AITrader.start()` |
| INV-10 | 模型失败或答案读不懂：不下单，保留原仓，也不重试下单 | `_decide_and_trade` |
| INV-11 | 行情不可达就不下单。不拿 Gate 备用行情给实盘单定价 | `frame_provider` |
| INV-12 | 决策有有效期和价格漂移两个闸门，过期或偏离就跳过 | `AIGuard` |
| INV-13 | 现货 + 必须止损：启动就拒绝。现货没有交易所侧 reduce-only 止损 | `AITrader.start()` |
| INV-14 | 自动交易、AI 交易、AI 量化同一时刻只有一个能动钱 | `TradeLease` |
| INV-15 | 每笔决策写一行审计，含拒绝原因。订单在 `orders` 表里能按来源反查 | `ai_trader._audit` |

## 7. 设置字段

存在 `config.json` 的 `ai_auto` 段里。密钥不在配置里，在系统密钥环。

| 字段 | 默认 | 作用 |
|---|---|---|
| `live_enabled` | `false` | 允许真下单的总闸。每次启动还要显式传 `confirm_live` |
| `max_order_quote` | 50 | 单笔上限，单位 USDT |
| `max_position_quote` | 200 | 最大持仓，单位 USDT |
| `max_decisions_per_day` | 96 | 每天最多问模型几次 |
| `max_orders_per_hour` | 12 | 每小时最多下几单 |
| `min_confidence` | 0.6 | 低于这个置信度就跳过 |
| `decision_ttl_sec` | 120 | 决策有效期 |
| `max_price_drift_pct` | 0.005 | 价格漂移超过这个比例就跳过 |
| `cooldown_sec` | 30 | 两次下单之间的冷却 |

## 8. 接口

| 方法 | 路径 | 作用 |
|---|---|---|
| `POST` | `/api/ai/trader/start` | 启动，带交易对、周期、信号策略、`confirm_live` |
| `POST` | `/api/ai/trader/decide` | 立即决策一次，不启动轮询 |
| `POST` | `/api/ai/trader/pause`、`/resume` | 暂停、继续 |
| `POST` | `/api/ai/trader/halt` | 熔断 |
| `POST` | `/api/ai/trader/stop` | 停止，释放交易权 |
| `POST` | `/api/ai/trader/kill` | 急停，同时打开全局风控开关 |
| `GET` | `/api/ai/trader/status`、`/audit` | 状态、审计 |
| `POST` | `/api/ai/lab/candidates` | 让模型生成候选策略 |
| `POST` | `/api/ai/lab/candidates/{id}/review` | 人工批准或拒绝 |
| `POST` | `/api/ai/lab/optimize` | 启动多轮参数优化 |
| `POST` | `/api/backtest/ai-filter` | 回测复核：把每个开仓点交给模型判一次 |

## 9. 策略代码的安全边界

`strategy/sandbox.py` 只做 AST 静态检查，它不是沙箱。真正的安全边界是人工审核，
以及只把审核过的文件复制进正式策略目录。

检查会拦的东西：

- 导入 `os`、`sys`、`subprocess`、`socket`、`requests` 这类模块，以及 `eval`、`exec`、`open`。
- 单个文件超过 60 KB。
- 没有直接继承 `Strategy`，或者没实现 `generate(self, df, params)`。
- 文件名里的 key 和类里的 `key` 不一致。

批准流程：模型写候选 → 静态检查 → 人工在策略实验室看源码和指标 → 批准时复制进策略
目录并触发热加载 → 部署到执行引擎才真正下单。

## 10. 优化循环

`ai_optimize.py` 一轮做四件事：拿当前参数回测，把指标和参数发给模型要新参数，
用同一个 frame 和同一个 `BacktestConfig` 再回测，指标改善且回撤不变差才接受。
上限是轮次和模型调用次数，两者任一用尽就停。

## 11. 测试

| 文件 | 覆盖 |
|---|---|
| `tests/test_ai.py` | 提示词、解析、价位校验、失败降级 |
| `tests/test_ai_trader.py` | 决策 schema、闸门、状态机、审计 |
| `tests/test_strategy_lab.py` | 静态检查拦得住什么，优化循环的接受规则 |
| `tests/test_routes_lab.py` | 候选生成、人工审核、优化任务 |
| `tests/test_web_protection.py` | 条件单与保护单接口 |
| `tests/test_engine_exclusion.py` | 三个主体互斥 |

全部离线跑。模型用 `FakeLLM` 替身，密钥环用 `monkeypatch` 顶掉。
