"""前端与后端之间的契约。

这一组测试给「前端把地址拼错、界面永远失败」这一类 bug 上锁，全部读源码，
不发任何 HTTP：

* 撤单按钮曾经请求 ``T/api/orders/...``，FastAPI 只回 Not Found，
  界面上的「撤单」永远失败，委托却还挂在交易所。
* 数字输入框里的半截内容（`-`、`1e`）经 Number() 是 NaN，
  JSON.stringify 把它变成 null，后端 int(None) 直接抛 TypeError。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI_DIR = ROOT / "web" / "src"
WEB_PKG = ROOT / "src" / "btrader" / "web"

# 三种字符串字面量：双引号、单引号、模板字符串。
STRING_RE = re.compile(
    r'"([^"\n]*)"'
    r"|'([^'\n]*)'"
    # 模板字符串可能跨行（拼 query 那种），所以不排除换行。
    r"|`([^`]*)`"
)

# 路由有两种写法：装饰器和 add_api_route。
ROUTE_RES = (
    re.compile(r'@(?:app|router)\.(?:get|post|put|delete|patch|websocket)\(\s*[fr]?["\']([^"\']+)["\']'),
    re.compile(r'add_api_route\(\s*[fr]?["\']([^"\']+)["\']'),
)
WILDCARD = "\x00"


def _ui_files() -> list[Path]:
    return sorted(path for path in UI_DIR.rglob("*.ts*") if path.is_file())


def _api_literals() -> dict[str, set[str]]:
    """前端源码里出现的每一处请求路径，映射到写它的文件。"""
    found: dict[str, set[str]] = {}
    for path in _ui_files():
        text = path.read_text(encoding="utf-8")
        for match in STRING_RE.finditer(text):
            literal = next((group for group in match.groups() if group), "")
            # 只要真正发请求的路径：相对导入的 ../api 和厂商域名不算。
            if "/api/" not in literal and literal != "/api":
                continue
            found.setdefault(literal, set()).add(str(path.relative_to(ROOT)))
    return found


def _backend_routes() -> set[str]:
    routes: set[str] = set()
    for path in WEB_PKG.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for pattern in ROUTE_RES:
            routes.update(pattern.findall(text))
    return routes


def _route_pattern(path: str) -> re.Pattern[str]:
    """把 /api/x/{id} 和 /api/x/${...} 都折成一段通配。"""
    skeleton = re.sub(r"\$\{[^}]*\}", WILDCARD, path)
    skeleton = re.sub(r"\{[^}]*\}", WILDCARD, skeleton)
    pieces = [re.escape(piece) for piece in skeleton.split(WILDCARD)]
    return re.compile("^" + "[^/?]+".join(pieces) + "$")


def test_every_api_path_in_the_ui_starts_at_the_root() -> None:
    """路径少了开头的斜杠就是 404：T/api/orders 是这么来的。"""
    offenders = {
        literal: sorted(files)
        for literal, files in _api_literals().items()
        if not literal.startswith("/api/") and literal != "/api"
    }
    assert not offenders, f"这些请求路径不是从根开始的 /api：{offenders}"


def test_every_api_path_the_ui_calls_exists_on_the_backend() -> None:
    """前端调的每个 /api 路径都要有对应的后端路由，否则按钮永远失败。"""
    routes = _backend_routes()
    assert len(routes) > 60, f"没读到后端路由，装饰器格式可能变了：{len(routes)} 条"
    patterns = [_route_pattern(route) for route in routes]

    unmatched: dict[str, list[str]] = {}
    for literal, files in _api_literals().items():
        path = literal.split("?")[0]
        if path.endswith("/") and path != "/api/":
            path = path.rstrip("/")
        if path == "/api/":
            path = "/api"
        if not any(pattern.match(path) for pattern in patterns):
            unmatched[literal] = sorted(files)
    assert not unmatched, f"前端在调后端没有的接口：{unmatched}"


def test_cancel_order_call_targets_the_real_backend_route() -> None:
    """撤单必须是 DELETE /api/orders/{id}，前缀多一个 T 就只剩 Not Found。"""
    source = (UI_DIR / "components" / "BottomTabs.tsx").read_text(encoding="utf-8")
    assert "/api/orders/${encodeURIComponent(order.id)}" in source
    assert "T/api/orders" not in source


def test_numeric_inputs_never_send_a_blank_or_half_typed_value() -> None:
    """空串是 0、半截数字是 NaN，两个都不该写进参数或风控上限。

    Number("") 是 0：清空策略参数会被后端夹到最小值，清空 AI 交易的单笔上限
    等于把上限关掉（后端拿 0 当不限制）。NaN 进 JSON 变成 null，int(None) 接不住。
    """
    param_fields = (UI_DIR / "components" / "ParamFields.tsx").read_text(encoding="utf-8")
    assert 'if (raw === "") return;' in param_fields
    assert "Number.isFinite(" in param_fields
    assert "onChange(spec.name, Number(event.target.value))" not in param_fields

    ai_trade = (UI_DIR / "pages" / "AITradePage.tsx").read_text(encoding="utf-8")
    for field in ("max_order_quote", "max_position_quote"):
        assert f"patch({{ {field}: Number(event.target.value) }})" not in ai_trade
        commit = ai_trade.find(f"patch({{ {field}: next }})")
        assert commit > 0, f"{field} 没有走 next 变量"
        handler = ai_trade[max(0, commit - 520) : commit]
        assert 'if (raw === "") return;' in handler, f"{field} 没拦住空串"
        assert "Number.isFinite(next)" in handler, f"{field} 没拦住 NaN"

BACKTEST_ENGINE = ROOT / "src" / "btrader" / "backtest" / "engine.py"


def _trade_record_keys() -> set[str]:
    """后端 TradeRecord.as_dict() 现在吐哪些键。"""
    source = BACKTEST_ENGINE.read_text(encoding="utf-8")
    block = source.split("def as_dict", 1)[1].split("}", 1)[0]
    return set(re.findall(r'"([a-z_]+)":', block))


def _typescript_fields(name: str) -> set[str]:
    """types.ts 里某个 interface 声明的字段名（注释先剥掉）。"""
    source = (UI_DIR / "types.ts").read_text(encoding="utf-8")
    body = source.split(f"export interface {name} {{", 1)[1].split("}", 1)[0]
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    body = re.sub(r"//[^\n]*", "", body)
    return set(re.findall(r"^\s*([a-z_]+)\??\s*:", body, flags=re.M))


def test_backtest_trade_rows_read_the_fields_the_backend_sends() -> None:
    """成交明细曾经读 timestamp / price / amount，后端一个都不发。

    后端发的是 TradeRecord.as_dict()：entry_time / exit_time / side /
    entry_price / exit_price / qty / pnl / pnl_pct / bars / exit_reason / fee，
    而且 side 是 long / short，不是 buy / sell。
    """
    backend = _trade_record_keys()
    assert backend == {
        "entry_time",
        "exit_time",
        "side",
        "entry_price",
        "exit_price",
        "qty",
        "pnl",
        "pnl_pct",
        "bars",
        "exit_reason",
        "fee",
    }, f"后端 TradeRecord 的字段变了，types.ts 要跟着改：{sorted(backend)}"

    declared = _typescript_fields("BacktestTrade")
    assert declared == backend, f"types.ts 的 BacktestTrade 和后端对不上：{declared ^ backend}"
    types_source = (UI_DIR / "types.ts").read_text(encoding="utf-8")
    assert "trades: BacktestTrade[];" in types_source, "BacktestResult.trades 还挂在模糊类型上"

    page = (UI_DIR / "pages" / "BacktestPage.tsx").read_text(encoding="utf-8")
    for stale in ("trade.timestamp", "trade.price", "trade.amount"):
        assert stale not in page, f"成交明细还在读后端不返回的 {stale}"
    for used in (
        "trade.entry_time",
        "trade.exit_time",
        "trade.entry_price",
        "trade.exit_price",
        "trade.qty",
    ):
        assert used in page, f"成交明细没有读 {used}"
    assert 'trade.side === "short"' in page, "side 是 long / short，不是 buy / sell"

AUTO_ENGINE = ROOT / "src" / "btrader" / "engine" / "auto.py"


def _auto_data_keys() -> set[str]:
    """后端 AutoTrader 报给界面的 K 线供给字段。"""
    source = AUTO_ENGINE.read_text(encoding="utf-8")
    block = source.split("self._data_status", 1)[1].split("}", 1)[0]
    return set(re.findall(r'"([a-z_]+)":', block))


def test_auto_trading_page_shows_the_kline_shortfall_the_backend_reports() -> None:
    """执行台要显示 /api/auto/status 的 data：不够根数时用后端那句话提示。

    引擎在跑不等于在决策：预热不够时它每轮直接 return，日志一个字都没有，
    界面只显示 running=true。这一行是唯一能把原因说出来的地方。
    """
    backend = _auto_data_keys()
    assert backend == {
        "required_bars",
        "fetched_bars",
        "max_bars",
        "short",
        "message",
    }, f"后端 data 字段变了，types.ts 要跟着改：{sorted(backend)}"

    declared = _typescript_fields("AutoDataStatus")
    assert declared == backend, f"types.ts 的 AutoDataStatus 和后端对不上：{declared ^ backend}"

    types_source = (UI_DIR / "types.ts").read_text(encoding="utf-8")
    assert "data: AutoDataStatus | null;" in types_source, "AutoStatus 没挂上 data"

    console = (UI_DIR / "components" / "ExecutionConsole.tsx").read_text(encoding="utf-8")
    assert "status?.data" in console, "执行台没读 status.data"
    assert "data?.short" in console, "执行台没按 short 决定显不显示"
    assert "{data.message" in console, "执行台没把后端的 message 显示出来"
    for used in ("data.fetched_bars", "data.required_bars", "data.max_bars"):
        assert used in console, f"执行台没显示 {used}"

def test_backtest_trade_count_is_not_rendered_with_decimals() -> None:
    """成交笔数是整数：界面上不该出现「15.00 笔」。

    回测结果那排指标共用一个小数位格式，笔数混在里面就带着两位小数，
    读起来像金额。
    """
    page = (UI_DIR / "pages" / "BacktestPage.tsx").read_text(encoding="utf-8")
    assert '["trades", "成交笔数", "int"]' in page, "成交笔数没按整数处理"
    assert 'kind === "int"' in page, "指标渲染没有整数分支"
    assert "fmt(Math.round(value), 0)" in page, "整数分支没有收成 0 位小数"


def test_lab_optimize_says_so_when_no_round_succeeded() -> None:
    """优化每轮都失败时不能报「优化完成」。

    没有 API Key 时后端每轮都记 error，却仍然给出一个用空 metrics 算出来的
    score，界面上就成了「优化完成 + 两轮一样的参数 + 一个分数 + 最优参数」。
    """
    page = (UI_DIR / "pages" / "StrategyLabPage.tsx").read_text(encoding="utf-8")
    types_source = (UI_DIR / "types.ts").read_text(encoding="utf-8")
    assert "error?: string;" in types_source, "OptimizeRound 少了 error 字段"

    assert "function roundFailed(round: OptimizeRound): boolean" in page, "没有判断失败轮次的函数"
    assert "没有一轮跑成功" in page, "全部失败时没有给出提示"
    assert "anyRoundSucceeded" in page, "没有过滤「最优参数」的显示条件"
    assert "{result?.best && anyRoundSucceeded && (" in page, "全失败时还在显示最优参数"
    assert "{round.error || paramsText(round.params)" in page, "轮次表把失败原因挡住了"

def test_wallet_balance_rows_sell_instead_of_closing() -> None:
    """钱包余额行（origin === "wallet"）卖的是余额，不是平仓。

    现货的「持仓」就是钱包余额：后端不带 size 时只平本地成交账本记的数量，
    账本里没有就 400，所以这一行必须把余额显式填进请求里，按钮也不能再写「平仓」。
    """
    panel = (UI_DIR / "components" / "BottomTabs.tsx").read_text(encoding="utf-8")

    # 输入框里显示的和真正发出去的 size 用的是同一个默认值
    assert "value={closeSizes[position.symbol] ?? defaultCloseSize(position)}" in panel
    assert (
        "const typed = parseNumber(closeSizes[position.symbol] ?? defaultCloseSize(position));"
        in panel
    ), "钱包行点了卖出却没把余额带进请求"

    # 只有钱包行取整笔余额，买来的持仓留空（空值 = 全平，语义不变）
    assert 'position.origin === "wallet" ? plainAmount(Math.abs(position.contracts)) : ""' in panel

    # 钱包行「卖出」，账本行仍然是「平仓 / 部分平仓」
    for label in ('"卖出中…"', '"卖出"', '"平仓中…"', '"部分平仓"', '"平仓"'):
        assert label in panel, f"少了按钮文案 {label}"

    # 两张表的表头分开，别让钱包行还顶着「平仓数量」
    assert '"平仓数量",' in panel and '"卖出数量",' in panel


def test_trading_page_explains_a_cross_market_symbol_fallback() -> None:
    """设置里的默认交易对在别的市场不叫这个名字时，退回要说一句。

    ETH/USDT 是现货写法，合约里叫 ETH/USDT:USDT。静默换成 BTC/USDT:USDT
    会让用户以为自己的选择丢了。可用时那一行不能出现。
    """
    page = (UI_DIR / "pages" / "TradingPage.tsx").read_text(encoding="utf-8")

    assert "const [symbolFallback, setSymbolFallback] = useState" in page
    assert "{symbolFallback && (" in page, "说明没有按条件渲染"
    assert "市场不可用，已切到" in page
    assert "settings?.last_symbol && chosen && chosen !== settings.last_symbol" in page
    # 用户自己选、换市场、以及默认交易对可用时都要清掉
    assert page.count("setSymbolFallback(null)") >= 3, "说明不会消失"

def test_conditional_orders_say_they_are_waiting_for_a_trigger() -> None:
    """委托面板里的算法单（止损/止盈/条件单）不该读成「未成交」。

    这些单子在币安是算法单，挂上就是等触发；它们的 price 恒为 0，只有 trigger 有值，
    所以价格列已经回落到 trigger。时间也有缺的时候，缺了要显示「—」而不是 1970 年。
    """
    panel = (UI_DIR / "components" / "BottomTabs.tsx").read_text(encoding="utf-8")

    assert 'order.trigger ? "等待触发" : "未成交"' in panel
    assert "order.price ? fmtPrice(order.price) : order.trigger ? fmtPrice(order.trigger) : \"市价\"" in panel
    assert "order.timestamp" in panel and ': "—",' in panel, "算法单没有时间时会显示 1970"

def test_spot_backtest_cannot_ask_for_shorts() -> None:
    """现货回测不许做空：勾选框置灰，请求里也不发 true。

    后端 _validate_backtest 会把现货的 allow_short 收死成 False、杠杆收死成 1。
    界面还留一个能勾的「允许做空」，用户会以为自己刚跑的是多空策略。
    """
    page = (UI_DIR / "pages" / "BacktestPage.tsx").read_text(encoding="utf-8")

    assert 'allow_short: config.market === "spot" ? false : config.allow_short' in page
    assert "disabled={config.market === \"spot\"}" in page
    assert "现货不能做空" in page
    # 合约还是能勾
    assert "允许做空（仅合约市场生效）" in page

def test_switching_strategy_asks_before_dropping_unsaved_edits() -> None:
    """左边换策略会把右边没保存的改动直接丢掉，得先问一句。

    策略页的列表在页面里、编辑器在组件里，页面拿不到 dirty，只能通过编辑器暴露的
    hasUnsaved() 问一次；「新建」「复制为新策略」同样会覆盖编辑区，也走这条路。
    """
    page = (UI_DIR / "pages" / "StrategiesPage.tsx").read_text(encoding="utf-8")
    editor = (UI_DIR / "components" / "StrategyEditor.tsx").read_text(encoding="utf-8")

    assert "hasUnsaved: () => boolean;" in editor
    assert "hasUnsaved: () => code !== original" in editor

    assert "const confirmDiscard" in page
    assert "window.confirm(`当前策略还有没保存的修改" in page
    # 三个入口都要过这道问句
    assert page.count("confirmDiscard(") >= 3
    assert "if (!confirmDiscard(`切到" in page
    assert 'onClick={() => pick(item.key)}' in page
    assert "onClick={startNew}" in page and "onClick={fork}" in page
