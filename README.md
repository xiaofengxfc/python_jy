# biTrader · 币安桌面交易工作台

基于 Python + FastAPI 的加密货币桌面交易软件，首期适配 **币安**（现货、U 本位合约、币本位合约）。
集行情看盘、手动下单、事件驱动回测、参数优化、纸面交易与自动交易于一体。

> ⚠️ **风险提示**：加密资产交易风险极高。本项目默认使用纸面交易；切换到实盘前请先完成回测与纸面验证，
> 并配置风控限制。因使用本软件造成的任何损失由使用者自行承担。

---

## 功能一览

| 模块 | 说明 |
| --- | --- |
| 交易 | K 线图（Canvas）、指标叠加、订单簿、成交价、手动市价/限价下单 |
| 资产 | 实盘余额与合约持仓、纸面账户权益/盈亏 |
| 回测 | 事件驱动回测（信号收盘产生、下一根开盘成交）、手续费/滑点/杠杆/止损止盈 |
| 参数优化 | 参数网格搜索，按夏普/收益/回撤等排序 |
| 自动交易 | 新 K 线收盘后计算目标仓位并自动调仓，支持纸面与实盘 |
| 设置 | API 凭据（系统密钥环）、主网/测试网、风控限制、策略插件目录 |

支持市场：`spot`（现货）、`usdm`（U 本位永续）、`coinm`（币本位合约），
每个市场独立选择，行情与交易接口由 ccxt 统一封装。

### 数量单位说明

| 市场 | 界面填写 | 交易所实际接收 | 换算 |
| --- | --- | --- | --- |
| 现货 / U 本位 | 币数量（如 `0.01 BTC`） | 币数量 | 1:1（U 本位乘合约面值） |
| 币本位 | 币数量（如 `0.01 BTC`） | 张数 | `张数 = 币数量 × 价格 ÷ 每张美元面值` |

币本位每张合约代表固定美元面值（BTCUSD 永续为 100 USD，由交易所定义），
下单时自动向下取整为整张；不足 1 张会被明确拒绝，不会放大或缩小订单。
在「交易」页选择币本位合约时，下单卡片会显示当前交易对的换算提示与预计张数。

## 快速开始

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows
pip install -r requirements.txt
python run.py
```

也可以只启动后端、自己用浏览器打开（前端改动刷新即可，无需重启）：

```bash
python run_web.py
```

或安装为包后使用命令：

```bash
pip install -e .
bitrader
```

数据默认保存在 `%APPDATA%\biTrader`（可用环境变量 `BITRADER_DATA_DIR` 覆盖）：

```
biTrader/
├─ config.json        # 界面/风控设置（不含密钥）
├─ bitrader.db        # 订单、成交、回测记录
├─ cache/klines/      # K 线 Parquet 缓存
├─ logs/              # 运行日志
└─ strategies/        # 用户策略插件目录
```

## API 配置

1. 在币安创建 API Key，**只勾选「读取」与「现货/合约交易」，绝对不要开启提现权限**。
2. 建议绑定 IP 白名单。
3. 打开「设置 → 币安 API 凭据」填入并保存，凭据存放在 Windows 凭据管理器（keyring），
   不会写入 `config.json`。
4. 点击「测试连接」验证权限。

## 使用流程（建议）

```
回测（调参、看回撤） → 纸面交易（真实行情、模拟资金） → 实盘（小资金、开风控）
```

- 回测页：选择市场/交易对/周期 → 加载历史数据 → 设置成本与风控参数 → 开始回测。
- 参数优化页：为参数填写候选值（如 `5,10,20`），选择排序指标后运行。
- 回测完成后点击「用此参数开始纸面交易」，参数会自动带入自动交易页（默认纸面模式）。
- 自动交易：只在**新 K 线收盘后**计算一次信号，避免同一根 K 线重复下单。

## 策略插件

在「设置 → 策略插件」中打开目录，放入 `.py` 文件即可。最小模板：

```python
import pandas as pd
from btrader.strategy.base import Strategy, IntParam


class MyStrategy(Strategy):
    key = "my_strategy"
    name = "我的策略"
    params_spec = [IntParam("period", "均线周期", 20, 5, 200)]

    def generate(self, df: pd.DataFrame, params: dict) -> pd.DataFrame:
        ma = df["close"].rolling(params["period"]).mean()
        target = (df["close"] > ma).astype(float)      # 1.0 多 / 0.0 空仓 / -1.0 空
        return pd.DataFrame({"target": target}, index=df.index)
```

完整示例见 `strategies/example_strategy.py`。注意：策略是本地 Python 代码，
拥有与本程序相同的系统权限，请只运行你自己信任的策略。

## 风控

所有下单（手动与自动）都会经过 `RiskManager`：

- 单笔最大金额、单品种最大持仓、最大杠杆
- 当日最大亏损、当日最大交易次数、连续亏损熔断
- 实盘下单二次确认、全局急停开关

风控状态显示在界面顶部/设置页。命中限制的订单会被拒绝并在界面提示原因。

## 打包 Windows 后端

```bat
packaging\build_dev.bat
```

产物：`dist\biTrader-backend\biTrader-backend.exe`（后端进程，由 Electron 外壳拉起）。

前端资源由后端从 `src/btrader/web/static` 提供，打包时一并复制进产物。

## 项目结构

```
src/btrader/
├─ core/          配置、凭据、模型、指标、SQLite 存储
├─ exchange/      ccxt REST 封装、K 线缓存、交易对
├─ strategy/      策略基类、插件加载、内置策略
├─ backtest/      事件驱动回测引擎、参数网格搜索
├─ engine/        风控、纸面撮合、下单服务、自动交易线程
└─ web/           FastAPI 后端、行情 WebSocket、前端静态资源
```

## 测试

```bash
python -m pytest
```

测试覆盖指标计算、策略信号、回测记账恒等式、风控、纸面撮合与 UI 离屏构建。
