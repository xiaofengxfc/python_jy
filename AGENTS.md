# 项目约定

biTrader：后端 Python + FastAPI，在 `src/btrader/`。前端 React + TS，在 `web/`。
新会话先读这份文件，再改代码。

## 1. 改完要让它生效

| 改了什么 | 要做什么 |
|---|---|
| `web/src/**`（TS、TSX、CSS） | 在 `web` 目录执行 `npm run build`。产物直接写进 `src/btrader/web/static` |
| `src/btrader/**`（Python） | 重启后端，没有热重载 |
| 两者都改 | 先 build，再重启 |
| `notify.cmd` 和文档 | 不用重启 |

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

提交前必须全绿。测试不依赖本机网关：`tests/conftest.py` 已经关掉
`WeChatNotifier._preflight`。

## 3. 改代码的注意事项

- 图表用 TradingView lightweight-charts，代码在 `web/src/chart/`。
  指标算法在 `indicators.ts`，画线图元在 `primitives.ts`。
  指标算法是从手绘 canvas 版搬过来的，不要顺手优化。数值一变，图就和历史截图对不上。
- 含中文的源文件只用编辑器改。不要用 `Set-Content` 或 `Out-File`，
  这两个命令会按 GBK 写文件，中文变乱码，之后就是 SyntaxError。
- `cmd` 脚本要 CRLF 加纯 ASCII。否则 `rem` 行会被当成命令执行。
- 前端只读磁盘上的静态产物。`index.html` 带 `no-store`，改完刷新页面就行。

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

- 推送通道默认是企业微信智能机器人，API 模式加长连接。本机 OpenClaw 网关主动连
  `wss://openws.work.weixin.qq.com`。没有回调 URL，不用扫码，登录态不会过期。
  凭证在 `~/.openclaw/openclaw.json` 的 `channels.wecom.{botId,secret}`，
  发送命令带 `--channel wecom --target <成员 userid>`。
- 设置页也支持企业微信自建应用 API 直连，填 CorpID、AgentId、Secret，
  直接调 `qyapi.weixin.qq.com`，不用 OpenClaw。Secret 只存系统密钥环。
  `notify.mode=openclaw|wecom_api` 决定走哪条通道，默认 `openclaw`。
- 一次 `openclaw message send` 约 30 秒，时间花在 Node 启动和网关往返上。
- 网关冷启动要 50 秒以上，`START_TIMEOUT` 设成 180 秒。
- 企微机器人只能推到已存在的会话。目标成员要先给机器人发过一条消息，
  否则接口返回 `errcode 93006 invalid chatid`。目标填错也是这个码。
- 网关由终端启动时（`state=external`），本程序只能用 `openclaw gateway restart` 重启它。

## 6. AI 助手

改 `btrader/llm.py` 和 `btrader/ai.py` 之前，先读 README 的「AI 助手」一节。

- 密钥在系统密钥环，service 叫 `biTrader-ai`，不进 `config.json`。
  `AISettings` 只存供应商、模型、上限和开关。
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
