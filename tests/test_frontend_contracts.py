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
