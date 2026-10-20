# 项目约定（任何新会话都必须遵守）

biTrader：Python/FastAPI 后端（`src/btrader/`）+ React/TS 前端（`web/`）。
这份文件是**无记忆环境下的运行手册**：新开一个上下文、完全不知道之前聊过什么，
照这里做就能正确开发和汇报。

## 1. 每轮任务结束，必须推送一条微信通知

用户要求：任务结束或出问题，自动发微信。执行方式只有一个命令：

```bat
notify.cmd --title "回测完成" "BTCUSDT 1h，收益 +2.1%，回撤 13.5%"
notify.cmd "部署失败：退出码 1"
notify.cmd --title 任务完成 "修复了主网/测试网 API 串号" --line "改动：routes.py"
```

- 退出码：`0` 已发送，`1` 发送失败，`2` 用法错误。失败不影响任务本身。
- **不依赖任何环境变量或当前目录**：`notify.cmd` 自己设置 `PYTHONPATH` 并调用
  `.venv` 的解释器；推送设置（目标、账号、开关）存在
  `%APPDATA%\biTrader\config.json`，与对话记忆无关。
- 长任务中途失败时也要推一条，说明失败原因。
- 如果 `notify.cmd` 返回 1：先确认网关在跑
  （`POST http://127.0.0.1:8765/api/install/openclaw/gateway`），再重试一次。

应用自身的任务（自动交易启停、回测/优化完成或失败）**不需要手动推送**：
`routes.py` 的 `_notify_task` 已经接好，受「任务状态」开关控制。

## 2. 改完必须让它生效

| 改了什么 | 必须做的事 |
|---|---|
| `web/src/**`（TS/TSX/CSS） | `cd web; npm run build`（产物直接写 `src/btrader/web/static`） |
| `src/btrader/**`（Python） | 重启后端（见下），没有热重载 |
| 两者都改 | 先 build，再重启 |
| `notify.cmd` / 文档 | 无需重启 |

后端重启（Windows，PowerShell）：

```powershell
Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue |
  ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
Start-Process -FilePath ".venv\Scripts\python.exe" `
  -ArgumentList "run_web.py","--host","127.0.0.1","--port","8765" `
  -WorkingDirectory "D:\github_xm\python_jy" -WindowStyle Hidden
```

Node 不在 PATH 时：`$env:Path = "D:\tool\node;" + $env:Path`。

## 3. 测试

```powershell
.venv\Scripts\python.exe -m pytest -q
```

提交前必须全绿。测试不得依赖本机网关：`tests/conftest.py` 已经把
`WeChatNotifier._preflight` 关掉。

## 4. 代码/编辑注意事项

- 含中文的源文件只用编辑器工具改，**不要**用 `Set-Content`/`Out-File`
  （会用 GBK 写成乱码，导致 SyntaxError）。
- `cmd` 脚本必须 CRLF + 纯 ASCII，否则 `rem` 行会被当命令执行。
- 前端只读磁盘上的静态产物；`index.html` 是 `no-store`，改完刷新页面即可。

## 5. 已验证的现实约束（别重新踩）

- 推送通道是**企业微信智能机器人（API 模式 + 长连接）**：本机 OpenClaw 网关
  主动连 `wss://openws.work.weixin.qq.com`，没有回调 URL、没有扫码、不会过期。
  凭证在 `~/.openclaw/openclaw.json` 的 `channels.wecom.{botId,secret}`；
  发送用 `--channel wecom --target <成员 userid>`。
- 单次 `openclaw message send` 约 **30 秒**（Node 启动 + 网关往返）。
- 网关冷启动要 **50 秒以上**，`START_TIMEOUT` 已调到 180 秒。
- 企微机器人只能在**已存在的会话**里推送：目标成员必须先给机器人发过一条消息，
  否则接口返回 `errcode 93006 invalid chatid`（目标写错也是这个码）。
- 网关若由终端启动（`state=external`），本程序只能通过
  `openclaw gateway restart` 重启它。

## 6. AI 助手（`btrader/llm.py` + `btrader/ai.py`）

- 密钥在系统密钥环（service `biTrader-ai`），**不进 `config.json`**；
  `AISettings` 只存供应商/模型/上限/开关。
- 分工：`llm.py` 只管收发（多供应商、缓存、审计、限额），`ai.py` 只管提示词与解析，
  两者都不碰交易。改提示词只动 `ai.py`。
- 不可破坏的约束（改之前先读 README「AI 助手」一节）：
  - 模型**不进** `strategy.generate()` / `run_backtest()` 的循环，也不下单；
  - 回测复核只给**开仓前**的 K 线，`ai.entry_window` 是唯一入口；
  - 模型给的价位必须过 `ai.validate_levels` 才能画到图上；
  - 调用失败/答案读不懂时默认**保留**交易（不能因为超时就"策略变好"）；
  - 自动交易简报走 `ai.BriefQueue`（独立线程、队列满即丢弃），不能阻塞 `_tick()`。
- 网络注意：币安合约域名在本机不可达，AI 端点统一走 `routes._market_frame`，
  它和 `/api/klines` 一样会回落到 Gate 永续行情。别在 AI 路由里直接调
  `ctx.klines.fetch`，否则图表能用而"分析"500。
- 测试在 `tests/test_ai.py`，全部离线：`FakeLLM` 替身 + `monkeypatch` 密钥环。
