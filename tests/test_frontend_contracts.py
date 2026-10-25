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
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[1]
UI_DIR = ROOT / "web" / "src"
WEB_PKG = ROOT / "src" / "btrader" / "web"

# 扫描器：逐字符走一遍源码，认出「会变成请求路径的字面量」。
#
# 以前这里是一条正则 `` `([^`]*)` ``，遇到嵌套模板就翻车：
#
#     await api<Portfolio>(`/api/portfolio${view ? `?market=${view}` : ""}`);
#
# 匹配到第一个反引号就收，切出 `/api/portfolio${view ? `，于是报「前端在调后端
# 没有的接口」——前端没写错，是扫描器不会数层级。现在按字符扫：模板里遇到 ${
# 就进表达式上下文，表达式里再遇到反引号又进下一层，一路配到最外层结束。
#
# 顺带把三件事理清（以前都靠运气）：
#   * 注释整段跳过：注释里写着 "/api/old" 不是调用；
#   * 散文里提到 /api 的字符串不算调用，但 T/api/orders 这种拼错的路径要抓；
#   * `"/api/strategies/" + encodeURIComponent(k) + "/rename"` 拼出来的路径也检查
#     （以前只看得到 "/api/strategies/"，后缀写错一样能过）。
HOLE = "${}"


class Piece(NamedTuple):
    start: int
    end: int
    text: str


def _scan_code(text: str, index: int, pieces: list[Piece], *, in_expression: bool) -> int:
    """扫一段代码。in_expression=True 时停在配对的 } 上（模板的 ${...} 里）。"""
    depth = 0
    while index < len(text):
        char = text[index]
        if text.startswith("//", index):
            newline = text.find("\n", index)
            index = len(text) if newline < 0 else newline + 1
            continue
        if text.startswith("/*", index):
            close = text.find("*/", index + 2)
            index = len(text) if close < 0 else close + 2
            continue
        if char in "\"'":
            index = _scan_plain(text, index, char, pieces)
            continue
        if char == "`":
            index = _scan_template(text, index, pieces)
            continue
        if in_expression:
            if char == "{":
                depth += 1
            elif char == "}":
                if depth == 0:
                    return index + 1
                depth -= 1
        index += 1
    return index


def _scan_plain(text: str, index: int, quote: str, pieces: list[Piece]) -> int:
    """普通字符串：扫到同种引号或行尾为止。"""
    cursor = index + 1
    buffer: list[str] = []
    while cursor < len(text):
        char = text[cursor]
        if char == "\\":
            buffer.append(text[cursor : cursor + 2])
            cursor += 2
            continue
        if char == quote:
            pieces.append(Piece(index, cursor + 1, "".join(buffer)))
            return cursor + 1
        if char == "\n":
            break
        buffer.append(char)
        cursor += 1
    pieces.append(Piece(index, cursor, "".join(buffer)))
    return cursor


def _scan_template(text: str, index: int, pieces: list[Piece]) -> int:
    """模板字符串：静态片段攒成一条，${...} 折成一个洞，里面的代码再扫一遍。"""
    cursor = index + 1
    buffer: list[str] = []
    while cursor < len(text):
        char = text[cursor]
        if char == "\\":
            buffer.append(text[cursor : cursor + 2])
            cursor += 2
            continue
        if char == "`":
            pieces.append(Piece(index, cursor + 1, "".join(buffer)))
            return cursor + 1
        if text.startswith("${", cursor):
            buffer.append(HOLE)
            # 表达式里的模板/字符串各自入列：内层的 `/api/x` 也是一条路径
            cursor = _scan_code(text, cursor + 2, pieces, in_expression=True)
            continue
        buffer.append(char)
        cursor += 1
    pieces.append(Piece(index, len(text), "".join(buffer)))
    return len(text)


def _scan_strings(text: str) -> list[Piece]:
    pieces: list[Piece] = []
    _scan_code(text, 0, pieces, in_expression=False)
    return pieces


def _looks_like_api_call(skeleton: str) -> bool:
    """这条字面量像不像一个请求路径（而不是散文里提到的 /api）。"""
    value = skeleton.lstrip()
    while value.startswith(HOLE):
        value = value[len(HOLE) :]
    # 前缀里不含点：./api 和 ../api 是相对导入，不是请求路径。
    return bool(re.match(r"[\w$\-]*/api(?:/|$|\?|\{|:)", value))


def _is_concat_gap(gap: str) -> bool:
    """两个字面量之间只有「+ 一个表达式 +」：它们拼的是同一条路径。"""
    core = gap.strip()
    return core.startswith("+") and core.endswith("+")


def _api_skeletons(text: str) -> list[str]:
    """源码里出现的请求路径骨架（洞写成 ${}），按出现顺序、去重。"""
    ordered = sorted(_scan_strings(text), key=lambda piece: (piece.start, -piece.end))
    inner = [
        piece
        for piece in ordered
        if any(
            other is not piece and other.start <= piece.start and piece.end <= other.end
            for other in ordered
        )
    ]
    outer = [piece for piece in ordered if piece not in inner]
    merged: list[Piece] = []
    for piece in outer:
        if merged:
            gap = text[merged[-1].end : piece.start]
            if _is_concat_gap(gap):
                merged[-1] = Piece(
                    merged[-1].start, piece.end, merged[-1].text + HOLE + piece.text
                )
                continue
        merged.append(piece)
    found: list[str] = []
    # 嵌套模板里的静态片段单独过一遍：拼 query 的那截也可能自带 /api
    for piece in merged + inner:
        if _looks_like_api_call(piece.text) and piece.text not in found:
            found.append(piece.text)
    return found


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
        for skeleton in _api_skeletons(text):
            found.setdefault(skeleton, set()).add(str(path.relative_to(ROOT)))
    return found


def _backend_routes() -> set[str]:
    routes: set[str] = set()
    for path in WEB_PKG.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for pattern in ROUTE_RES:
            routes.update(pattern.findall(text))
    return routes


def _path_variants(path: str) -> list[str]:
    """一条骨架可能对应的几种真实路径。

    ${} 洞里既可能是一个路径段（/api/backtest/jobs/${}），也可能是整条查询串
    （`/api/portfolio${view ? `?market=${view}` : ""}` 里就是）。两种都算：
    后面那截被丢掉的写法也要试一遍，不然「拼 query 的模板」会一直被判成不存在的接口。
    """
    variants = [path]
    trimmed = path
    while trimmed.endswith(HOLE):
        trimmed = trimmed[: -len(HOLE)]
        if trimmed.endswith("/"):
            trimmed = trimmed.rstrip("/")
        if trimmed and trimmed not in variants:
            variants.append(trimmed)
    return variants


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
        variants = _path_variants(path)
        if not any(pattern.match(variant) for pattern in patterns for variant in variants):
            unmatched[literal] = sorted(files)
    assert not unmatched, f"前端在调后端没有的接口：{unmatched}"


def _routes_match(path: str) -> bool:
    """这条骨架能不能对上一个真实后端路由（和主检查同口径：先砍掉查询串）。"""
    patterns = [_route_pattern(route) for route in _backend_routes()]
    path = path.split("?")[0]
    if path.endswith("/") and path != "/api/":
        path = path.rstrip("/")
    if path == "/api/":
        path = "/api"
    return any(
        pattern.match(variant)
        for pattern in patterns
        for variant in _path_variants(path)
    )


def test_nested_template_strings_are_scanned_whole() -> None:
    """模板里再套模板：按层级配到最外层，不能碰到第一个反引号就收。

    这行真实代码曾经被切成半截（反引号包着的 /api/portfolio${view ? 反引号），于是
    报「前端在调后端没有的接口」。要求：外层前缀 /api/portfolio 要认出来，模板里
    任何一个静态路径片段都要检查——动态的不等于整条跳过。
    """
    source = (
        '  const next = await api<Portfolio>(`/api/portfolio${view ? `?market=${view}` : ""}`);\n'
    )
    skeletons = _api_skeletons(source)
    assert '/api/portfolio${}' in skeletons, skeletons
    assert not [item for item in skeletons if '`' in item or ' ? ' in item], skeletons
    assert _routes_match('/api/portfolio${}')

    # 模板里再套一个「后端没有的接口」：不能因为它在洞里就放过
    nested_missing = (
        '  const x = await api(`/api/nonexistent${view ? `?market=${view}` : ""}`);\n'
    )
    assert '/api/nonexistent${}' in _api_skeletons(nested_missing)
    assert not _routes_match('/api/nonexistent${}')


def test_the_real_portfolio_fetch_is_scanned_as_a_plain_path() -> None:
    """PortfolioPage 里那一行真源码（现在已经是 query 写法）也要被整条认出来。"""
    text = (UI_DIR / 'pages' / 'PortfolioPage.tsx').read_text(encoding='utf-8')
    skeletons = _api_skeletons(text)
    portfolios = [item for item in skeletons if item.startswith('/api/portfolio')]
    assert portfolios, skeletons
    for item in portfolios:
        assert '`' not in item, f"扫出了半截模板：{item}"
        assert item.split('?')[0].startswith('/api/portfolio')
    assert any(_routes_match(item) for item in portfolios), portfolios


def test_comments_and_prose_are_not_api_calls() -> None:
    """注释里、散文里出现的 /api 不是调用；拼错的路径仍然是调用。"""
    source = "\n".join(
        [
            '  // 以前这里写的是 "/api/fake-in-comment"，别当成调用',
            '  /* "/api/fake-in-block-comment" */',
            '  const note = "already /api/ticker was the old path";',
            '  const broken = "T/api/orders";',
            '  const real = await api("/api/positions/close");',
        ]
    )
    skeletons = _api_skeletons(source)
    assert '/api/fake-in-comment' not in skeletons, skeletons
    assert '/api/fake-in-block-comment' not in skeletons, skeletons
    assert not [item for item in skeletons if item.startswith('already ')], skeletons
    # 拼错的路径（少了开头的斜杠）必须留住：test_..._starts_at_the_root 要抓它
    assert 'T/api/orders' in skeletons, skeletons
    assert '/api/positions/close' in skeletons
    assert _routes_match('/api/positions/close')


def test_concatenated_paths_are_checked_with_their_suffix() -> None:
    """字符串拼出来的路径要拼成一条再查。

    以前只看得到 /api/strategies/ 这一截，后缀写成 /rename2 也能过：真接口是
    DELETE /api/strategies/{key} 和 POST /api/strategies/{key}/rename。
    """
    source = (
        '  await api("/api/strategies/" + encodeURIComponent(key) + "/rename", { method: "POST" });\n'
    )
    skeletons = _api_skeletons(source)
    assert '/api/strategies/${}/rename' in skeletons, skeletons
    assert _routes_match('/api/strategies/${}/rename')
    # 后缀写错就该红：证明这条检查真的看后缀
    assert not _routes_match('/api/strategies/${}/rename2')


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

    # 输入框里显示的和真正发出去的 size 用的是同一个默认值。键按「行」记
    # （rowKey = symbol + origin）：现货同一对会同时出账本行与自有余额行，
    # 只按 symbol 记的话两行共用一个输入框。
    assert "value={closeSizes[rowKey(position)] ?? defaultCloseSize(position)}" in panel
    assert (
        "const typed = parseNumber(closeSizes[rowKey(position)] ?? defaultCloseSize(position));"
        in panel
    ), "钱包行点了卖出却没把余额带进请求"

    # 只有钱包行取整笔余额，买来的持仓留空（空值 = 全平，语义不变）
    assert 'position.origin === "wallet" ? plainAmount(sizeOf(position)) : ""' in panel

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
    # 句子本身搬到了共用件 SymbolFallbackNote（见 test_cross_market_symbol_fallback_speaks_up_on_three_pages），
    # 这里只钉住「退回时才算出来、三处共用、可用时清掉」。
    assert "<SymbolFallbackNote" in page, "说明没有渲染"
    assert "market={market}" in page
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

def test_custom_strategies_can_be_deleted_from_the_page() -> None:
    """自定义策略要能从界面上删掉（DELETE /api/strategies/{key}）。

    以前只有新建/复制，删不掉自己造出来的文件，只能去数据目录手删。删除会真删文件，
    所以先 confirm；覆盖版本删除等于还原，那条路是「还原内置」，纯内置后端 400 指向 restore。
    """
    editor = (UI_DIR / "components" / "StrategyEditor.tsx").read_text(encoding="utf-8")
    page = (UI_DIR / "pages" / "StrategiesPage.tsx").read_text(encoding="utf-8")

    assert 'method: "DELETE"' in editor
    assert "onDeleted?.(key)" in editor
    assert "{detail && !isNew && !detail.builtin && (" in editor, "纯自定义策略才出删除按钮"
    assert "window.confirm(" in editor
    assert '"remove"' in editor
    # 删除后的反馈在页面这一层（refresh 会清掉编辑器里的通知）
    assert "已删除「" in page


def test_pages_do_not_spin_forever_when_the_backend_is_down() -> None:
    """后端没起来时：设置页要说读不到并自己重试，交易页别拿 0 冒充行情。

    实测（网络层全拒绝）三处都是错的：设置页永远「正在读取设置…」，交易页图表永远
    「加载中…」，24h 高低量/最新价印成 0.000000。
    """
    app = (UI_DIR / "App.tsx").read_text(encoding="utf-8")
    page = (UI_DIR / "pages" / "TradingPage.tsx").read_text(encoding="utf-8")
    settings = (UI_DIR / "pages" / "SettingsPage.tsx").read_text(encoding="utf-8")

    # 设置读取失败：留错误、并排一次重试
    assert "setSettingsError(" in app
    assert "window.setTimeout(() => void load(), 5000)" in app
    assert 'settingsError ? "读不到设置" : "正在读取设置…"' in settings
    assert "读取设置失败：" in settings

    # 没有交易对时不要停在加载态
    assert "if (!symbol) {" in page and "setChartLoading(false);" in page
    # 没有行情时显示「—」，不是 0
    assert "const hasQuote = Boolean(ticker) || lastPrice > 0;" in page
    assert '{high > 0 ? fmtPrice(high) : "—"}' in page
    assert '{low > 0 ? fmtPrice(low) : "—"}' in page
    assert "{volume > 0 ? (" in page

def test_cross_market_symbol_fallback_speaks_up_on_three_pages() -> None:
    """默认交易对跨市场被换掉时，三处都要说出来，而且说的是同一句话。

    交易页行情头、回测页交易对下拉、设置页默认交易对，都会在「列表里没有原来那一对」时
    退到列表第一项。以前只有交易页说，另两处静默换掉。三处共用
    components/SymbolFallbackNote.tsx，页面里不许再各写一段同样的文案。
    """
    note = (UI_DIR / "components" / "SymbolFallbackNote.tsx").read_text(encoding="utf-8")
    assert "if (!from || !to || from === to) return null;" in note, "可用时也会显示"
    assert "市场不可用，已切到" in note
    assert 'layout === "form"' in note

    for rel in ("pages/TradingPage.tsx", "pages/BacktestPage.tsx", "pages/SettingsPage.tsx"):
        page = (UI_DIR / rel).read_text(encoding="utf-8")
        assert 'import SymbolFallbackNote from "../components/SymbolFallbackNote"' in page, rel
        assert "<SymbolFallbackNote" in page, rel
        assert "市场不可用，已切到" not in page, f"{rel} 又自己写了一套文案"
        assert "setSymbolFallback(null)" in page, f"{rel} 的说明不会消失"

    for rel in ("pages/BacktestPage.tsx", "pages/SettingsPage.tsx"):
        assert 'layout="form"' in (UI_DIR / rel).read_text(encoding="utf-8"), rel

def test_portfolio_page_can_switch_market() -> None:
    """资产页要能切市场，页头要说清看的是哪个账户。

    以前 /api/portfolio 不带 market，永远读设置的默认市场：装了现货 Key 的人
    在资产页上根本看不到现货账户，页头也不写是哪个市场。实测现货账户权益 39 万、
    合约账户 4999，页面只有一个数字时没人分得清。
    """
    page = (UI_DIR / "pages" / "PortfolioPage.tsx").read_text(encoding="utf-8")
    app = (UI_DIR / "App.tsx").read_text(encoding="utf-8")

    assert "defaultMarket" in page and "defaultMarket={settings?.market_type" in app
    assert "const [market, setMarket] = useState<string | null>(null);" in page
    assert "query({ market: view })" in page, "读的还是默认市场"
    assert "}账户 · 每 5 秒刷新" in page, "页头没说市场"
    assert "onClick={() => setMarket(item.value)}" in page

def test_selling_a_wallet_row_keeps_the_balance_prefilled() -> None:
    """钱包行卖完之后，数量框要回到「整笔余额」，不能留一个空框。

    实测（桩账户，钱包 0.997 ETH）：预填 0.997 → 点「卖出」→ 请求体 size=0.997 正确，
    但成交后输入框变成空串，行还是钱包行、按钮还是「卖出」；再点一次请求体就只剩
    {"symbol","market"}，现货后端回 400「本地成交账本里没有该交易对…请显式指定 size」。
    空串把钱包行的默认值顶掉了，所以成交后要删键而不是写空串。
    """
    source = (UI_DIR / "components" / "BottomTabs.tsx").read_text(encoding="utf-8")
    assert "delete next[rowKey(position)];" in source
    assert '[position.symbol]: ""' not in source, "成交后又把数量写成空串了"


def test_refreshing_the_strategy_list_cannot_drop_unsaved_edits() -> None:
    """策略页的「刷新」不能把没保存的改动换掉。

    刷新本身只换列表；只有当手上这条已经不在列表里（磁盘上被删、被改名）时，选中项
    才会挪到第一条、编辑器跟着换成另一条源码。这条路径以前不问题就换，和切换/新建/
    复制三条一样处理：先 hasUnsaved() 再 window.confirm，用户不肯就让手上的原样留着。
    """
    page = (UI_DIR / "pages" / "StrategiesPage.tsx").read_text(encoding="utf-8")
    assert "onClick={() => void reload()}" in page, "刷新按钮没走守卫"
    assert "const reload = async () => {" in page
    assert "confirmDiscard(`刷新（" in page
    assert page.count("confirmDiscard(") >= 4, "切策略/新建/复制/刷新四条都要问"
    # 用户拒绝时只换列表，不动编辑器
    assert "      setStrategies(items);" in page and "      return;" in page


def test_spot_pnl_says_it_is_booked_quantity_only() -> None:
    """现货行的浮动盈亏只按账本数量算，界面必须说明白。

    钱包余额里混着用户自己充的币：买进来 0.0041 而钱包有 1.0041 时，盈亏是 0.02
    （按 0.0041 算），可数量列写的是 1.0041，两个数摆一起像算错了。后端为此给了
    unrealized_basis / ledger_size 两个字段，界面不读它们就是「界面说的和后端做的不一样」。
    """
    fmt = (UI_DIR / "format.ts").read_text(encoding="utf-8")
    types = (UI_DIR / "types.ts").read_text(encoding="utf-8")
    assert "export function ledgerBasisNote(" in fmt
    assert 'unrealized_basis !== "ledger"' in fmt
    assert "Math.abs(booked - Math.abs(position.contracts)) < 1e-9" in fmt, "数量一致时不该加说明"
    assert "账本口径 ${fmt(booked, 6)}" in fmt, "浮盈旁边要说清是账本口径"
    assert 'unrealized_basis?: "" | "ledger";' in types
    assert "ledger_size?: number;" in types
    for rel in ("components/BottomTabs.tsx", "pages/PortfolioPage.tsx"):
        page = (UI_DIR / rel).read_text(encoding="utf-8")
        assert "ledgerBasisNote(position) && (" in page, rel


def test_order_ticket_refuses_orders_below_the_venue_minimums() -> None:
    """低于交易所最小量/最小名义额的单不发出去。

    实测 0.002 ETH（4.77 USDT）被交易所按 NOTIONAL 拒（-1013），界面以前把这发出去、
    再把交易所报文念一遍。preview 现在带 min_amount / min_notional，下单框据此先拦。
    """
    ticket = (UI_DIR / "components" / "OrderTicket.tsx").read_text(encoding="utf-8")
    types = (UI_DIR / "types.ts").read_text(encoding="utf-8")
    assert "min_amount?: number | null;" in types and "min_notional?: number | null;" in types
    assert "const minimumProblem = (amount: number): string => {" in ticket
    assert "if (tooSmall) return setFlash({ tone: \"err\", text: tooSmall });" in ticket
    # 预检必须在 fetch 之前：否则单已经发出去了才提示
    assert ticket.index("const tooSmall = minimumProblem(amount);") < ticket.index('api<{\n        order: Record<string, unknown>;')
    assert "交易所最低" in ticket, "下单框要显示交易所的最低下单额度"


def test_a_slow_strategy_source_cannot_overwrite_the_newer_one() -> None:
    """连着点两条策略，慢回来的那条不能盖掉新选中的那条。

    读源码是异步的：先点 bollinger（慢 4.5 秒）再点 ma_cross，旧回执落地后编辑器
    显示的是 bollinger 的源码和文件名，而「保存」PUT 的是当前选中的 ma_cross——
    A 的代码被写进 B 的文件。无头浏览器实测：左边高亮 ma_cross，栏头写着「布林带回归」，
    代码是 key = "bollinger"。修法是每次读源码发一个号，回执对不上号就丢掉。
    """
    editor = (UI_DIR / "components" / "StrategyEditor.tsx").read_text(encoding="utf-8")
    assert "const loadSeq = useRef(0);" in editor
    assert "const id = ++loadSeq.current;" in editor
    guard = editor.index("if (id !== loadSeq.current) return;")
    assert guard < editor.index("setDetail(data);"), "回执要先对号再落地"
    assert guard < editor.index("setError(caught instanceof Error"), "过期回执的报错也不该弹"
    assert "if (id === loadSeq.current) setBusy(\"\");" in editor, "旧回执不该把新的加载态清掉"


def test_a_slow_ai_status_cannot_flip_the_state_chip() -> None:
    """AI 交易状态轮询重叠时，旧响应不能盖掉新响应。

    后端慢的时候上一条还没回来下一条已经发出：实测页面先显示「已停止」（新回执），
    6 秒后旧的「运行中」落地把它盖回去，两秒后才被下一轮纠正——自动交易停着，
    界面说在跑。同样的竞态在回测进度、执行台上都存在，这里先按状态那一处收口。
    """
    page = (UI_DIR / "pages" / "AITradePage.tsx").read_text(encoding="utf-8")
    assert "const statusSeq = useRef(0);" in page
    assert "const id = ++statusSeq.current;" in page
    assert page.index("if (id !== statusSeq.current) return;") < page.index("setStatus(next);")


def test_ai_filter_table_reads_the_keys_the_backend_sends() -> None:
    """AI 过滤那张表读的键，得是 ai.filter_trades 真的塞进 judged 里的。

    judged 的每一行是 {**trade.as_dict(), "verdict": ..., "reason": ...}——
    所以除了 TradeRecord 的字段，只多 verdict / reason 两个。表里读的键要是超出这两个集合，
    那几列就永远是空的（成交明细当初就是这么坏掉的）。kept 也必须是数量：后端把 kept_count
    改名发出去，换成数组的话界面那一格会把列表渲染出来。
    """
    ai_source = (ROOT / "src" / "btrader" / "ai.py").read_text(encoding="utf-8")
    block = ai_source.split("judged: list[dict[str, Any]] = []", 1)[1].split("if progress:", 1)[0]
    sniffed = set(re.findall(r'"([a-z_]+)":', block))
    assert {"verdict", "reason"} <= sniffed, f"judged 行的字段变了：{sorted(sniffed)}"

    allowed = _trade_record_keys() | sniffed
    page = (UI_DIR / "pages" / "BacktestPage.tsx").read_text(encoding="utf-8")
    judged_block = page.split("rows={aiFilter.judged", 1)[1].split("empty=", 1)[0]
    used = set(re.findall(r"item\.([a-z_]+)", judged_block))
    assert used, "AI 过滤表一个字段都没读，说明这一块被改坏了"
    assert used <= allowed, f"AI 过滤表读了后端不给的字段：{sorted(used - allowed)}"

    routes = (ROOT / "src" / "btrader" / "web" / "routes.py").read_text(encoding="utf-8")
    assert '"kept": judged["kept_count"]' in routes, "kept 要发数量，别把 kept 数组发出去"
    assert "kept: number;" in (UI_DIR / "types.ts").read_text(encoding="utf-8")


def test_portfolio_page_shows_a_placeholder_before_the_first_snapshot() -> None:
    """资产页第一次读回来之前不能把账户画成 0。

    /api/portfolio 走整钱包现读，实测要 5 秒上下；这段时间 data 是 null，以前每个
    数字都落到 fmt(undefined ?? 0) —— 总权益 0.00 / 可用余额 0.00 / 成交 0 /「还没有成交记录」，
    而账户里其实有 4,999.65 USDT 和 34 笔成交。无头浏览器实测：1.2 秒时全是 0，9 秒后真数字。
    修法是没数据时显示「—」和「正在读取账户…」，别拿 0 冒充。
    """
    page = (UI_DIR / "pages" / "PortfolioPage.tsx").read_text(encoding="utf-8")
    assert "const loading = data === null;" in page
    for metric in ("data?.equity", "data?.cash", "data?.realized_pnl"):
        guarded = f'{{loading ? "—" : fmt({metric} ?? 0, 2)}}'
        assert guarded in page, f"{metric} 没走加载占位"
        assert f"{{fmt({metric} ?? 0, 2)}}" not in page, f"{metric} 还会画成 0"
    assert page.count("正在读取账户…") >= 3, "空列表在加载时也要说在读取，不能写「还没有」"
    assert "成交 {loading ? \"—\" : trades.length}" in page
    assert "全部委托 {loading ? \"—\" : orders.length}" in page


def test_backtest_page_exposes_funding_and_liquidation_costs() -> None:
    """回测页要能开关资金费与强平，并把结果说清楚。

    后端把这两样算成真成本之后，界面如果还写着「不会强平、也不算资金费」，
    用户会以为关掉就是默认口径。所以：三个请求字段要发出去、两个指标要显示、
    现货下置灰并说明原因、成交明细要认得出强平、后端给的 warnings 不能吞。
    """
    engine = (ROOT / "src" / "btrader" / "backtest" / "engine.py").read_text(encoding="utf-8")
    for key in ("funding_paid", "liquidations"):
        assert f'metrics["{key}"]' in engine, f"后端不再算 {key} 了，界面也要跟着改"
    routes = (ROOT / "src" / "btrader" / "web" / "routes.py").read_text(encoding="utf-8")
    for field in ("funding: bool = True", "liquidate: bool = True", "maintenance_margin_rate: float"):
        assert field in routes, f"请求字段 {field} 不见了"

    page = (UI_DIR / "pages" / "BacktestPage.tsx").read_text(encoding="utf-8")
    # 三个控件都在（现货是置灰，不是藏起来）
    for control in ('id="bt-funding"', 'id="bt-liquidate"', 'id="bt-mmr"'):
        assert control in page, f"参数区少了 {control}"
    assert page.count('disabled={config.market === "spot"}') >= 2, "现货下资金费与强平要置灰"
    assert "config.market === \"spot\" || !config.liquidate || config.leverage <= 1" in page, (
        "维持保证金率只在强平打开且杠杆 > 1 时可用"
    )
    assert "现货没有资金费率，杠杆恒为 1 也不会强平" in page, "现货下要说清为什么不生效"
    # 三个字段真的发出去
    assert 'funding: config.market === "spot" ? false : config.funding,' in page
    assert 'liquidate: config.market === "spot" ? false : config.liquidate,' in page
    assert "maintenance_margin_rate: config.maintenance_margin_rate," in page
    # 两个指标有格子：资金费是钱（带符号），强平是次数
    assert '["funding_paid", "资金费", "money"],' in page
    assert '["liquidations", "强平", "int"],' in page
    assert "kind === \"money\"" in page, "资金费要按金额画并留正负号"
    # 成交明细认得出强平
    assert 'liquidation: "强平",' in page
    assert "<ReasonLabel reason={trade.exit_reason} />" in page
    # warnings 不吞
    assert "result.warnings" in page
    assert 'className="banner warn"' in page
    assert "warnings?: string[];" in (UI_DIR / "types.ts").read_text(encoding="utf-8")


def test_the_three_ai_pages_also_explain_a_cross_market_fallback() -> None:
    """AI 交易 / AI 量化 / 策略实验室换市场时也不能静默换掉交易对。

    这三页的标的初值取自 settings.last_symbol。设置里存的是现货写法（BTC/USDT）时,
    合约市场的列表里没有这一项，代码就退到列表第一项，一个字都不说 —— 和交易页以前
    一样的毛病。三处共用 components/SymbolFallbackNote.tsx 那一行，可用时不出现。
    """
    note = (UI_DIR / "components" / "SymbolFallbackNote.tsx").read_text(encoding="utf-8")
    assert "市场不可用，已切到" in note

    for rel in ("pages/AITradePage.tsx", "pages/AIQuantPage.tsx", "pages/StrategyLabPage.tsx"):
        page = (UI_DIR / rel).read_text(encoding="utf-8")
        assert 'import SymbolFallbackNote from "../components/SymbolFallbackNote";' in page, rel
        assert "<SymbolFallbackNote" in page, f"{rel} 退回时不说一句"
        assert "const symbolRef = useRef(symbol);" in page, f"{rel} 的退回判定用的是旧值"
        assert (
            "setSymbolFallback(known || !to || to === from ? null : { from, to });" in page
        ), f"{rel} 没有在退回时算出说明"
        assert "setSymbolFallback(null);" in page, f"{rel} 手动选了之后说明不会消失"
        assert "市场不可用，已切到" not in page, f"{rel} 又自己写了一套文案"


def test_the_view_lives_in_the_url_hash() -> None:
    """刷新不该弹回交易页：视图存在 URL hash 里，后退前进也要跟得上。

    以前 const [page, setPage] = useState<PageKey>("trading") 纯内存，按 F5 必回交易页。
    现在首屏从 hash 读、换页写 hash，hash 认不出来回交易页，且不许用整页 reload。
    """
    app = (UI_DIR / "App.tsx").read_text(encoding="utf-8")
    assert "const [page, setPage] = useState<PageKey>(pageFromHash);" in app
    assert "function pageFromHash(): PageKey" in app
    for key in ("trading", "ai-trade", "ai-quant", "portfolio", "backtest", "strategy", "lab", "settings"):
        assert f'"{key}",' in app, f"PAGE_KEYS 少了 {key}"
    # 认不出来的 hash 回交易页
    assert 'PAGE_KEYS.find((key) => key === raw) ?? "trading"' in app
    # 前进后退：hashchange 与 popstate 都要听
    assert 'window.addEventListener("hashchange", sync)' in app
    assert 'window.addEventListener("popstate", sync)' in app
    # 换页写 hash（进历史），首屏归一化用 replace 不塞历史
    assert "window.location.hash = want;" in app
    assert "window.history.replaceState(null, \"\", `#/${clean}`);" in app
    # 侧栏与页面内部跳转都要走 go，不能直接 setPage 绕过 hash
    assert "onPick={go}" in app
    assert "onNavigate={(next) => go(next)}" in app
    assert "location.reload(" not in app


def test_the_24h_change_is_formatted_in_one_place_for_three_views() -> None:
    """24 小时涨幅三处同款：+1.23% / -0.45%，涨 up、跌 down，null 留空不显示 0。

    格式只有 web/src/tickers.ts 一份：fmtChangePct / changeTone。选择器行、收藏与最近用过、
    交易页标题都调它，别再各写一遍 toFixed(2)。数据走 GET /api/tickers，拿不到就整列不显示。
    """
    tickers = (UI_DIR / "tickers.ts").read_text(encoding="utf-8")
    assert "export function fmtChangePct" in tickers
    assert "value > 0 ? \"+\" : \"\"" in tickers
    assert "value.toFixed(2)" in tickers
    assert "if (value == null || !Number.isFinite(value)) return \"\";" in tickers
    assert "export function changeTone" in tickers
    assert 'return value > 0 ? "up" : "down";' in tickers
    # 接口形状与降级：读不到就返回空表，界面不显示、也不报错
    assert "/api/tickers?${query({ market, symbols: chunk.join(\",\") })}" in tickers
    # 空表兜底：没有可问的交易对就返回空表（分批之后每批自己 catch，不再是一个大 try）
    assert "if (!wanted.length) return {};" in tickers
    # 后端一次最多收 50 个，前端不能一把塞 300 条进去（超了 400，整批都没了）：
    # 50 一批、最多 6 批，串行发（见 test_the_ticker_batches_cover_the_whole_list）
    assert "const MAX_SYMBOLS = 50;" in tickers

    picker = (UI_DIR / "components" / "SymbolPicker.tsx").read_text(encoding="utf-8")
    assert 'import { changeTone, fmtChangePct } from "../tickers";' in picker
    assert "fmtChangePct(quotes[symbol]?.change_pct ?? null)" in picker
    assert "changeTone(quotes[symbol]?.change_pct ?? null)" in picker

    page = (UI_DIR / "pages" / "TradingPage.tsx").read_text(encoding="utf-8")
    assert 'import { changeTone, fmtChangePct, loadTickers } from "../tickers";' in page
    assert "const changePct = ticker?.change_pct ?? null;" in page, "读不到涨幅时不能压成 0"
    assert "{fmtChangePct(changePct)}" in page
    assert "toFixed(2)}%" not in page, "标题又自己写了一遍格式"


def test_favorites_come_from_settings_with_a_star_toggle() -> None:
    """收藏不再写死：读 settings.favorites，星标增删，收藏在上、「最近用过」最多 8 条。

    口径是跟账户走，所以不写 localStorage；空的时候要说明怎么加。
    """
    picker = (UI_DIR / "components" / "SymbolPicker.tsx").read_text(encoding="utf-8")
    assert "const FAVORITES" not in picker, "写死的收藏还在"
    assert "favorites?: string[];" in picker and "recent?: string[];" in picker
    assert "onToggleFavorite?: (symbol: string) => void;" in picker
    assert 'aria-pressed={favorited(symbol)}' in picker
    assert 'className={`picker-star${favorited(symbol) ? " on" : ""}`}' in picker
    assert "还没有收藏，点右侧星标添加" in picker
    assert "最近用过" in picker
    assert "const RECENT_LIMIT = 8;" in picker
    assert "localStorage" not in picker, "收藏不能写进 localStorage"

    page = (UI_DIR / "pages" / "TradingPage.tsx").read_text(encoding="utf-8")
    assert "settings?.favorites ?? []" in page
    assert "settings?.recent_symbols ?? []" in page
    assert 'api("/api/settings", { method: "PUT", body: JSON.stringify({ favorites: next }) })' in page
    assert "onToggleFavorite={toggleFavorite}" in page
    assert "favorites={favorites}" in page and "quotes={quotes}" in page
    assert "const RECENT_LIMIT = 8;" in page

    types = (UI_DIR / "types.ts").read_text(encoding="utf-8")
    assert "favorites?: string[];" in types
    assert "recent_symbols?: string[];" in types
    assert "export interface TickerQuote {" in types


def test_recent_symbols_go_back_to_the_account_too() -> None:
    """「最近用过」也存后端（settings.recent_symbols），刷新还在，不写 localStorage。"""
    page = (UI_DIR / "pages" / "TradingPage.tsx").read_text(encoding="utf-8")
    assert "settings?.recent_symbols ?? []" in page
    assert "recent_symbols: next" in page
    assert 'body: JSON.stringify({ recent_symbols: next })' in page
    assert "const RECENT_LIMIT = 8;" in page


def test_two_stars_clicked_at_once_both_stick() -> None:
    """连点两个星标不能只剩一个：setState 还没重渲染时要读 ref 里的最新值。"""
    page = (UI_DIR / "pages" / "TradingPage.tsx").read_text(encoding="utf-8")
    assert "const favoritesRef = useRef(favorites);" in page
    assert "favoritesRef.current = favorites;" in page
    assert "const recentRef = useRef(recent);" in page
    assert "const current = favoritesRef.current;" in page
    assert "favoritesRef.current = next;" in page
    assert "recentRef.current.filter((item) => item !== pair)" in page


def test_the_close_box_shows_how_much_would_actually_be_closed() -> None:
    """现货账本行的「平仓数量」占位要用账本数量，不能用钱包余额。

    实测（t49，测试网真单）：钱包里有自己充的 1 ETH，程序又买了 0.0025，持仓表数量列是钱包事实
    1.002500、数量框占位也写 1.002500，但点「平仓」只卖掉账本那 0.0025 —— 界面说的和后端做的
    不是一回事。钱包行照旧预填整笔余额（那是「卖出」的语义）。
    """
    tabs = (UI_DIR / "components" / "BottomTabs.tsx").read_text(encoding="utf-8")
    assert "const closeHint = (position: Position) =>" in tabs
    # 数量一律是基础币：contracts 是交易所口径的张（币本位 10 张 = 0.02 BTC）
    assert "const sizeOf = (position: Position) => Math.abs(position.size_base ?? position.contracts);" in tabs
    assert "position.ledger_size || position.size_base || position.contracts" in tabs
    assert "placeholder={fmt(closeHint(position), 6)}" in tabs
    assert "placeholder={fmt(Math.abs(position.contracts), 6)}" not in tabs
    # 钱包行还是预填整笔余额
    assert 'position.origin === "wallet" ? plainAmount(sizeOf(position)) : "";' in tabs


def test_a_fill_refreshes_the_panel_with_a_live_read() -> None:
    """成交回执之后用 refresh=1 现读，把面板从「钱包余额」追到账本口径。

    缓存里那一行成交后最长 10 秒还是钱包余额，面板会把刚买的持仓画成「卖出 / 预填整笔余额」。
    下单成交、撤单、平仓三条路都要追。
    """
    page = (UI_DIR / "pages" / "TradingPage.tsx").read_text(encoding="utf-8")
    assert "refresh: freshPair && symbol ? 1 : undefined" in page, "现读那一读没带 refresh=1"
    assert "const refreshAfterFill" in page
    assert 'row.origin === "ledger"' in page, "没等这一对变成账本行就收工"
    assert "onFilled={() => void refreshAfterFill()}" in page, "下单框成交后没追"
    assert "void refreshAfterFill();" in page, "撤单/平仓之后没追"
    tabs = (UI_DIR / "components" / "BottomTabs.tsx").read_text(encoding="utf-8")
    assert "onChanged?.();" in tabs, "撤单 / 平仓 / 卖出都要走 run() 才能触发追新"


def test_the_order_ticket_blocks_below_the_minimum_before_sending() -> None:
    """最小下单量 / 最小名义额在本地拦下来，提示写清是多少，不发给交易所拒。

    预览多返回 min_amount / min_notional；缺字段或 null 就当没有（不猜、不拦）。
    """
    ticket = (UI_DIR / "components" / "OrderTicket.tsx").read_text(encoding="utf-8")
    assert "const minimumProblem = (amount: number): string =>" in ticket
    # 只认当前这张票的 preview：防抖窗口里上一份的数字会把合法的单误拦
    assert "const currentPreview = preview && previewFor === previewTicket ? preview : null;" in ticket
    assert "currentPreview?.min_amount ?? null" in ticket and "currentPreview?.min_notional ?? null" in ticket
    assert 'setPreviewFor([market, symbol, side, orderType, sizeMode, size].join("|"));' in ticket
    assert "if (!minAmount && !minNotional) return \"\";" in ticket, "缺字段时要当没有"
    assert "最小下单数量" in ticket and "最小下单金额" in ticket, "提示里没说清最小是多少"
    # 两条提交路径（市价/限价、触发单）都拦一次
    assert ticket.count("const tooSmall = minimumProblem(amount);") == 2
    assert 'if (tooSmall) return setFlash({ tone: "err", text: tooSmall });' in ticket
    # 不用点提交就能看见
    assert "const minimumWarning = useMemo" in ticket
    assert '{minimumWarning && <div className="flash err">{minimumWarning}</div>}' in ticket


def test_the_spot_unrealized_pnl_says_which_basis_it_uses() -> None:
    """现货浮盈按账本算：持仓面板与资产页都要说一句，别当成钱包余额的盈亏。"""
    fmt_ts = (UI_DIR / "format.ts").read_text(encoding="utf-8")
    assert "export function ledgerBasisNote" in fmt_ts
    assert 'if (position.unrealized_basis !== "ledger") return "";' in fmt_ts
    assert "账本口径 " in fmt_ts
    for name in ("components/BottomTabs.tsx", "pages/PortfolioPage.tsx"):
        src = (UI_DIR / name).read_text(encoding="utf-8")
        assert "ledgerBasisNote(position)" in src, f"{name} 没说浮盈是账本口径"


def test_a_spot_pair_can_be_two_rows_at_once() -> None:
    """现货同一交易对两行：账本行（平仓）与自有余额行（卖出），数量框互不串。

    后端把 `contracts` 拆成账本数量与自有余额之后，两行会同时出现。数量框以前按
    symbol 记，两行共用一个输入：在钱包行填 1，账本行也跟着显示 1，点哪一行都按这个数走。
    """
    tabs = (UI_DIR / "components" / "BottomTabs.tsx").read_text(encoding="utf-8")
    assert "const rowKey = (position: Position)" in tabs, "数量框要按行记，不能只按 symbol"
    assert "${position.symbol}|${position.origin" in tabs
    assert "closeSizes[rowKey(position)]" in tabs
    assert "closeSizes[position.symbol]" not in tabs, "两行会共用一个输入框"
    assert "run(`close:${rowKey(position)}`" in tabs, "忙碌标记也要按行"
    # origin 决定按钮文案：账本行平仓、自有余额行卖出
    assert 'const wallet = position.origin === "wallet";' in tabs
    assert '"卖出"' in tabs and '"平仓"' in tabs
    # 自有余额行预填差额（defaultCloseSize 只给 wallet 行数量），账本行占位用账本数量
    assert 'position.origin === "wallet" ? plainAmount(sizeOf(position)) : ""' in tabs
    assert "placeholder={fmt(closeHint(position), 6)}" in tabs
    # 两组分开渲染：账本行进持仓表，自有余额行进收起的那一组
    assert 'positions.filter((item) => item.origin !== "wallet")' in tabs
    assert 'positions.filter((item) => item.origin === "wallet")' in tabs
    assert "另有 {walletPositions.length} 项钱包余额" in tabs


def test_the_bottom_panel_height_is_dragged_and_remembered() -> None:
    """底部面板高度可拖、双击回默认、记在 localStorage；图表高度不许跟着变。"""
    tabs = (UI_DIR / "components" / "BottomTabs.tsx").read_text(encoding="utf-8")
    assert "const PANEL_DEFAULT_HEIGHT = 320;" in tabs, "默认高度要抬到能看三级标题"
    assert "const PANEL_HEIGHT_KEY = " in tabs
    assert "biTrader.bottomPanelHeight" in tabs
    assert "window.localStorage.setItem(PANEL_HEIGHT_KEY" in tabs
    assert "style={collapsed ? undefined : { flexBasis: panelHeight }}" in tabs
    assert 'className="panel-resizer"' in tabs
    assert "onPointerDown={startResize}" in tabs
    assert "onDoubleClick={() => setPanelHeight(PANEL_DEFAULT_HEIGHT)}" in tabs, "双击要回默认"
    assert 'window.addEventListener("pointermove", move)' in tabs
    # 高度不进 config.json：那里没有这个键
    assert "config.json" in tabs.split("storageKey")[0] or "不进 config.json" in tabs
    css = (UI_DIR / "styles.css").read_text(encoding="utf-8")
    assert ".panel-resizer {" in css and "cursor: row-resize" in css
    assert "flex: 0 0 320px;" in css, "面板默认高度还是 208"
    # 图表高度仍是自己的 clamp()，与面板无关
    assert ".trading-main > .card:nth-child(2)" in css
    assert "height: clamp(420px, calc(100vh - 330px), 780px)" in css, "图表高度被面板带跑了"


def test_the_ai_analysis_can_open_in_a_floating_window() -> None:
    """分析正文给一个「放大」，开成可拖可关的浮窗，浮窗里不设 max-height。"""
    panel = (UI_DIR / "components" / "AIPanel.tsx").read_text(encoding="utf-8")
    assert 'import FloatingWindow from "./FloatingWindow";' in panel
    assert "放大" in panel
    assert "onClick={() => setExpanded(true)}" in panel
    assert "open={expanded}" in panel and "onClose={() => setExpanded(false)}" in panel
    assert "storageKey=" in panel
    assert 'className="ai-text ai-text-floating"' in panel
    css = (UI_DIR / "styles.css").read_text(encoding="utf-8")
    assert ".ai-text-floating {" in css and "max-height: none" in css


def test_the_run_config_saves_itself_and_can_go_back_to_defaults() -> None:
    """AI 交易与执行台的运行配置：改动即存（防抖 500ms）+ 恢复默认（只重置这一段）。"""
    saver = (UI_DIR / "settingsAutoSave.ts").read_text(encoding="utf-8")
    assert "export const SAVE_DEBOUNCE_MS = 500;" in saver
    assert "window.setTimeout(() => void flush(), SAVE_DEBOUNCE_MS)" in saver
    assert '"/api/settings", {' in saver
    assert 'method: "PUT"' in saver
    assert 'body: JSON.stringify({ [section]: body })' in saver
    assert '"/api/settings/reset"' in saver
    assert 'method: "POST", body: JSON.stringify({ section })' in saver
    # 回执是 {ok, section, settings}：直接当 AppSettings 用会拿到 undefined，界面就退回前端兜底值。
    assert "result.settings ?? result" in saver
    page = (UI_DIR / "pages" / "AITradePage.tsx").read_text(encoding="utf-8")
    assert 'createSectionSaver("ai_auto", {' in page
    assert "saver.queue(" in page, "AI 交易的运行配置没存"
    # 存成功要把服务器那份回填给 App：App 只在启动时读一次设置，不回填的话
    # 执行台改了杠杆再去 AI 量化部署，部署请求里还是旧的 settings.exec.leverage。
    assert "onSaved?.(saved)" in saver, "回执没带出服务器那份设置"
    assert "onSettingsChange?.({ ai_auto: saved.ai_auto })" in page
    assert 'resetSection("ai_auto")' in page
    assert "恢复默认" in page
    assert "只重置这一段" in page and "主题" in page, "二次确认要写清只重置运行配置"
    window = (UI_DIR / "components" / "ExecutionConsole.tsx").read_text(encoding="utf-8")
    assert 'createSectionSaver("exec", {' in window
    assert "settings?: AppSettings | null;" in window, "执行台没读设置的 exec 段"
    assert "readExec(settings)" in window
    assert "onSettingsChange?.({ exec: saved.exec })" in window, "执行台存完没回填给 App"
    assert 'resetSection("exec")' in window
    assert "恢复默认" in window
    types = (UI_DIR / "types.ts").read_text(encoding="utf-8")
    assert "export interface ExecSettings" in types and "exec?: ExecSettings;" in types


def _deploy_card_source() -> str:
    """设置页「域名与 HTTPS」那一整块：状态、三个请求、整张卡的 JSX。

    只切 JSX 会漏掉写在卡片上面的请求与二次确认；整段切到文件尾又会把别处的
    注释（那里写着「可能存着」这类普通叙述）算进来，所以两块各取一段拼起来。
    """
    page = (UI_DIR / "pages" / "SettingsPage.tsx").read_text(encoding="utf-8")
    start = page.index("const [deploy, setDeploy] = useState<DeployInfo | null>(null);")
    stop = page.index("const loadSettings = useCallback(", start)
    title = '<span className="card-title">域名与 HTTPS</span>'
    assert title in page, "状态有了，卡片呢"
    return page[start:stop] + page[page.index(title) :]


def test_the_deploy_card_calls_the_three_endpoints() -> None:
    """三个接口都用上：读状态、检查解析、签证书。"""
    card = _deploy_card_source()
    for path in ("/api/deploy/info", "/api/deploy/check", "/api/deploy/tls"):
        assert path in card, f"{path} 没被调用"
    assert 'method: "POST"' in card, "检查与签发都得是 POST"


def test_the_deploy_card_greys_out_instead_of_hiding_the_buttons() -> None:
    """501（Windows/macOS）时整卡置灰：按钮还在但点不动，后端那句说明原样显示。

    不能画一个能点的按钮让人点完才看到 501——那等于让人白点两次。
    """
    card = _deploy_card_source()
    assert "deployUnavailable" in card, "没有「这台机器用不了」的开关"
    assert "disabled={deployUnavailable" in card, "501 时按钮还点得动"
    assert "{deployError}" in card, "后端那句说明没有原样显示"
    for word in ("可能", "也许", "大概是", "估计是"):
        assert word not in card, f"卡片里自己编了解释：{word}"


def test_the_deploy_card_shows_the_log_tail_verbatim() -> None:
    """失败时把 Caddy 日志与脚本输出原样贴出来：pre 里整段，不截断、不转述。"""
    card = _deploy_card_source()
    assert "{deployTls.log_tail}" in card, "log_tail 没原样渲染"
    assert "{deployTls.script_output}" in card, "script_output 没原样渲染"
    assert "<pre" in card, "日志要放在 pre 里"
    assert 'className="deploy-log"' in card, "日志块没有 deploy-log 这个类"
    assert "log_tail.slice(" not in card and "script_output.slice(" not in card, "截断了日志"
    css = (UI_DIR / "styles.css").read_text(encoding="utf-8")
    assert ".deploy-log {" in css and "overflow: auto" in css, "日志块不能滚动"


def test_the_certificate_confirmation_mentions_the_interruption() -> None:
    """签发前的二次确认要说清：改 Caddyfile、reload caddy、会短暂中断。"""
    card = _deploy_card_source()
    assert "window.confirm(" in card, "签发之前没有二次确认"
    confirm = card[card.index("window.confirm("):]
    confirm = confirm[: confirm.index(");") + 2]
    assert "reload" in confirm, "没说要 reload caddy"
    assert "中断" in confirm, "没说会短暂中断"
    assert "Caddyfile" in confirm, "没说改的是 Caddyfile"


def test_the_deploy_card_explains_where_the_public_ip_came_from() -> None:
    """公网 IP 要连来源一起显示，并说清 A 记录指到它；拿不到就显示后端给的原因。"""
    card = _deploy_card_source()
    assert "public_ip_source" in card
    assert "public_ip_error" in card
    assert "A 记录" in card, "没说要指到这个地址"
    assert "cert_expires" in card, "没显示证书到期时间"
    assert "placeholder=" in card, "没有域名输入框"


def test_the_ticker_batch_only_asks_for_the_current_market() -> None:
    """批量涨幅只问「与当前市场同写法」的交易对。

    收藏与最近用过是跨市场的（现货写法 [BTC/USDT, ETH/USDT] 在 usdm 下也留着），
    以前原样拼进 /api/tickers；后端按市场收死写法，一个不对就整批 400，
    于是 300 行涨幅、收藏、最近用过全空，可标题那处涨幅还单独显示着。
    """
    src = (UI_DIR / "tickers.ts").read_text(encoding="utf-8")
    assert "export function symbolFitsMarket(" in src, "没有按市场判写法的函数"
    assert "symbols.filter((item) => symbolFitsMarket(market, item))" in src, "拼请求前没筛"
    assert src.index("symbolFitsMarket(market, item)") < src.index("await api<"), "筛选要排在发请求之前"


def test_a_favorite_from_another_market_is_shown_but_not_sent() -> None:
    """别的市场的收藏照常显示在列表里，只是不参与批量请求。

    现货写法一个冒号都没有；合约是一个冒号 + 结算币（USDT/USDC 归 U 本位，其余归币本位，
    交割的 -YYMMDD 先切掉）；BTC/USDT:USDT:USDT 这种多段写法谁都不算。
    """
    src = (UI_DIR / "tickers.ts").read_text(encoding="utf-8")
    assert "const colons = " in src and "colons > 1" in src, "多段写法没挡"
    assert 'if (!colons) return market === "spot";' in src, "现货/合约的分界不对"
    assert 'return market !== "spot" && marketForSymbol(text) === market;' in src, "合约没按结算币对市场"
    assert "export function marketForSymbol(" in src
    assert "USDT" in src and "USDC" in src and "coinm" in src
    assert "-\\d{6}" in src, "交割的到期日没切掉"
    page = (UI_DIR / "pages" / "TradingPage.tsx").read_text(encoding="utf-8")
    assert "favorites={favorites}" in page and "recent={recent}" in page, "收藏和最近用过要照常显示"
    assert "loadTickers(market, quotesWanted, { signal: controller.signal })" in page, "批量请求要带上市场与中止信号"


def test_a_failed_batch_leaves_the_change_blank_and_says_why_in_the_console() -> None:
    """整批仍然失败时：名字还在、涨幅留空、console 留一条原因；标题涨幅不受影响。"""
    src = (UI_DIR / "tickers.ts").read_text(encoding="utf-8")
    assert "console.warn" in src, "整批失败一句话都不留，查都没法查"
    assert "return {};" in src, "失败要返回空表，不能抛到界面"
    picker = (UI_DIR / "components" / "SymbolPicker.tsx").read_text(encoding="utf-8")
    assert "quotes[symbol]?.change_pct ?? null" in picker, "拿不到涨幅要留空"
    assert 'className="picker-pct faint"' in picker, "留空要留成空格子"
    page = (UI_DIR / "pages" / "TradingPage.tsx").read_text(encoding="utf-8")
    assert "const changePct = ticker?.change_pct ?? null;" in page, "标题的涨幅要单独取"


def test_the_panel_resizer_is_a_real_click_target_inside_the_panel() -> None:
    """拖拽把手要真的按得到：整块落在 .panel 的可见区内，别再挂到裁剪外面。

    实测过：把手 top=725、面板 top=731（负 margin 顶出去 6px），.panel 又是
    overflow: hidden，整块被裁掉，elementFromPoint 落到 trading-main 上，
    真鼠标拖 150px 面板一动不动；合成事件绕过命中测试才「能拖」。
    """
    css = (UI_DIR / "styles.css").read_text(encoding="utf-8")
    start = css.index("\n.panel-resizer {")
    block = css[start : css.index("\n.panel-resizer::after {")]
    assert "margin-top: -" not in block, "把手又被负 margin 顶到 overflow 外面了"
    assert "flex: 0 0 12px;" in block or "flex: 0 0 10px;" in block, "可点高度不够"
    assert "cursor: row-resize;" in block
    assert "z-index:" in block, "把手要压在内层内容之上"
    panel = css[css.index("\n.panel {") : css.index("\n.panel.collapsed {")]
    assert "overflow: hidden;" in panel, "列表滚动靠它，别为了把手改成 visible"


def test_the_ticker_batches_cover_the_whole_list() -> None:
    """300 行列表要问全：50 个一批、最多 6 批，串行发。

    后端 /api/tickers 一次只收 50 个（超了 400），以前只发一批，于是 300 行里后 250 行
    全年 24 小时涨幅那一格是空的。这里钉住批数与分批边界，也钉住「别一次并发打 6 个」。
    """
    src = (UI_DIR / "tickers.ts").read_text(encoding="utf-8")
    assert "const MAX_SYMBOLS = 50;" in src, "单批上限变了？后端就是 50"
    assert "const MAX_BATCHES = 6;" in src, "最多 6 批 = 300 行"
    assert "slice(0, MAX_SYMBOLS * MAX_BATCHES)" in src, "截断要按 6 批算，不是 50 个"
    assert "const batches = Math.min(MAX_BATCHES, Math.ceil(wanted.length / MAX_SYMBOLS));" in src
    assert "wanted.slice(index * MAX_SYMBOLS, (index + 1) * MAX_SYMBOLS)" in src, "分批边界不对"
    assert "Promise.all" not in src, "别并发打 6 个请求"


def test_the_ticker_cache_lists_ten_seconds_per_market() -> None:
    """结果缓存 10 秒、键带交易对集合：面板每 2 秒轮询一次，不能每次都重发 6 个请求，
    也不能让第二个调用方（交易对列表）命中一份缺项的快照。"""
    src = (UI_DIR / "tickers.ts").read_text(encoding="utf-8")
    assert "const CACHE_TTL_MS = 10_000;" in src
    # 键要带这一批交易对：只按市场记的话，第二个调用方会命中缺项的缓存、一条都不发
    assert "function bucketKey(market: string, wanted: string[]): string {" in src
    assert "const key = bucketKey(market, wanted);" in src
    assert "Date.now() - cached.at < CACHE_TTL_MS" in src, "没看时间戳就返回缓存"
    assert "cache.set(key," in src, "拿到了不写缓存，等于每次都重发"
    assert "const inflight = new Map<string," in src, "同一批交易对要串成一个序列"


def test_a_closed_page_stops_the_remaining_batches() -> None:
    """面板关掉 / 组件卸载：中止后面的批次，别让请求在后台继续跑。"""
    src = (UI_DIR / "tickers.ts").read_text(encoding="utf-8")
    assert "signal?: AbortSignal;" in src
    assert "if (signal?.aborted) break;" in src, "批次之间没检查中止标志"
    assert "if (!signal?.aborted && (failures < batches" in src, "被中止的半份快照不该写缓存"
    page = (UI_DIR / "pages" / "TradingPage.tsx").read_text(encoding="utf-8")
    assert "const controller = new AbortController();" in page
    assert "loadTickers(market, quotesWanted, { signal: controller.signal })" in page
    assert "quotesAbort.current?.abort();" in page, "卸载 / 关面板没有中止"
    assert "symbols.slice(0, 300)" in page, "只问前 50 个的话，后 250 行永远空着"
    # 当前交易对、收藏、最近用过仍排在最前面，第一屏一定有数
    assert "() => [symbol, ...favorites, ...recent, ...symbols.slice(0, 300)" in page
    # 面板关掉之后一条都不再发：定时刷新只在面板开着时挂
    assert "if (!pickerOpen) {" in page and "window.setInterval(refreshQuotes, 20000)" in page
    assert "onOpenChange={setPickerOpen}" in page
    picker = (UI_DIR / "components" / "SymbolPicker.tsx").read_text(encoding="utf-8")
    assert "onOpenChange?: (open: boolean) => void;" in picker
    assert "onOpenChange?.(open);" in picker, "面板开关状态没透出去"
    # 中止是调用方不要了，不是失败：别往 console 里刷一条原因
    assert "if (signal?.aborted) break;" in src


def test_one_failed_batch_leaves_only_its_rows_blank() -> None:
    """某一批失败只让那一批的行留空，已经拿到的结果不清空。"""
    src = (UI_DIR / "tickers.ts").read_text(encoding="utf-8")
    assert "第 ${index + 1}/${batches} 批失败" in src, "失败要写清是第几批、共几批"
    assert "chunk.length" in src, "要写清这批问了多少个"
    assert "merged[item.symbol] = item;" in src, "结果要合并到同一张表里，别每批一张"
    # 批内的 catch 只记一条日志，不许 return / 清空
    head = src.index("async function fetchBatches(")
    body = src[head:]
    catch_start = body.index("} catch (caught) {")
    catch_end = body.index("}\n  }", catch_start)
    caught = body[catch_start:catch_end]
    assert "return" not in caught, "批失败就整批返回，前面拿到的全丢了"
    assert "merged" not in caught, "批失败时动了已拿到的结果"
    assert "console.warn" in caught, "失败不留原因，整列空白没法查"


# ---------------------------------------------------------------- t79 杠杆与在线更新


def _order_ticket_source() -> str:
    return (UI_DIR / "components" / "OrderTicket.tsx").read_text(encoding="utf-8")


def _update_card_source() -> str:
    """设置页「软件更新」那一块：状态、四个请求、整张卡的 JSX。"""
    page = (UI_DIR / "pages" / "SettingsPage.tsx").read_text(encoding="utf-8")
    start = page.index("const [update, setUpdate] = useState<UpdateInfo | null>(null);")
    end = page.index("const loadSettings = useCallback(", start)
    title = '<span className="card-title">软件更新</span>'
    assert title in page, "状态有了，卡片呢"
    return page[start:end] + page[page.index(title) :]


def test_the_order_ticket_shows_leverage_only_on_futures() -> None:
    """杠杆栏只在合约上出现：现货没有借币这回事，整栏不显示。"""
    src = _order_ticket_source()
    assert "{market !== \"spot\" && (" in src, "杠杆栏没有按市场收死"
    assert 'id="leverage-input"' in src
    assert "min={1}" in src and "max={MAX_LEVERAGE}" in src and "step={1}" in src
    assert "const MAX_LEVERAGE = 125;" in src, "整数 1–125，上限写在常量里"
    assert "杠杆" in src


def test_the_leverage_is_saved_to_the_trade_section() -> None:
    """初值取 settings.trade.leverage[market]，改动即存（createSectionSaver("trade")）。"""
    src = _order_ticket_source()
    assert 'createSectionSaver("trade", {' in src
    assert "trade?.leverage" in src, "没读设置的 trade 段"
    assert "queue({ leverage:" in src, "改动没落盘"
    assert "String(Number.isFinite" in src or "Number.isFinite" in src, "拿不到就当 1"
    saver = (UI_DIR / "settingsAutoSave.ts").read_text(encoding="utf-8")
    assert '"trade"' in saver, "SettingsSection 里没有 trade"
    # 写盘失败要说一声，而且重读设置不能盖掉还没落盘的杠杆：
    # 「我改了、刷新又回 1」正是这两条一起缺的样子。
    assert "onError" in saver and "get pending()" in saver, "写盘失败被静默吞掉"
    assert "tradeSaver.current.pending" in src, "重读设置把用户刚敲的杠杆打回去了"
    types = (UI_DIR / "types.ts").read_text(encoding="utf-8")
    assert "export interface TradeSettings" in types and "trade?: TradeSettings;" in types


def test_both_order_bodies_carry_leverage() -> None:
    """下单预览与下单两个 body 都带 leverage；现货不发这个字段。"""
    src = _order_ticket_source()
    field = '...(market === "spot" ? {} : { leverage: leverageValue }),'
    assert src.count(field) >= 2, f"只有 {src.count(field)} 个 body 带了杠杆"
    preview = src.index('"/api/orders/preview"')
    submit = src.index('api<{', src.index("const send = async"))
    assert field in src[:submit], "预览的 body 没带杠杆"
    assert field in src[submit:], "下单的 body 没带杠杆"


def test_the_over_limit_leverage_is_announced_not_clamped() -> None:
    """超过 risk.max_leverage 只说一句「上限 X 倍」，不偷偷改成上限值。"""
    src = _order_ticket_source()
    assert "倍，超过会按上限处理" in src
    assert "leverageValue > maxLeverage" in src, "没跟 risk.max_leverage 比"
    assert "max_leverage" in src, "没读 risk.max_leverage"
    assert "Math.min(leverage" not in src and "Math.min(parsed" not in src, "偷偷夹到上限了"


def test_the_update_card_calls_the_four_endpoints() -> None:
    """软件更新卡：读信息、立即更新、轮询进度、回滚。"""
    card = _update_card_source()
    for path in ("/api/update/info", "/api/update/apply", "/api/update/status", "/api/update/rollback"):
        assert path in card, f"{path} 没被调用"
    assert "{ confirm: true }" in card, "回滚要显式确认"


def test_the_update_button_needs_something_to_update() -> None:
    """落后才可点：behind_by 是 0 时按钮点不动。"""
    card = _update_card_source()
    assert "typeof update.behind_by === \"number\" &&" in card, "没按 behind_by 收口（读不到时也不许放行）"
    assert "update.behind_by > 0" in card
    assert "disabled={!canUpdate" in card, "按钮没跟 canUpdate 走"


def test_the_update_card_spells_out_the_three_blocked_states() -> None:
    """三种不可更新状态：本地新、已分叉、error 非空（贴后端原话）。"""
    card = _update_card_source()
    assert "本地比远端新，先 push" in card
    assert "已分叉，需要手动处理" in card
    assert "ahead_by" in card and "behind_by" in card
    assert "{update.error}" in card, "后端原话没原样显示"


def test_the_update_steps_are_rendered_verbatim() -> None:
    """每步 {name, ok, output, seconds}：失败那步的 output 原文贴进 pre，不截断不转述。"""
    card = _update_card_source()
    assert "step.name" in card and "step.ok" in card and "step.seconds" in card
    assert "{step.output}" in card, "步骤输出没原样渲染"
    assert "<pre" in card and 'className="deploy-log"' in card, "输出要放在可滚的 pre 里"
    assert "step.output.slice(" not in card, "截断了输出"
    assert "updateJob?.steps" in card or "steps.map(" in card, "没有逐步列出"


def test_the_update_card_survives_the_restart() -> None:
    """更新会重启服务：轮询断了就等 /api/health 回来，60 秒没回来给手动命令。"""
    card = _update_card_source()
    assert "/api/health" in card, "没等 /api/health"
    assert "60" in card and "秒" in card
    assert "手动拉起：" in card, "60 秒没回来要给手动命令（优先用后端给的那条）"
    assert "已更新完成，当前提交" in card


def test_the_update_confirms_say_what_they_do() -> None:
    """更新与回滚各有一道二次确认，写清是否会停服务、失败停在哪一步。"""
    card = _update_card_source()
    assert "window.confirm(" in card
    first = card[card.index("window.confirm("):]
    first = first[: first.index(");") + 2]
    for word in ("自动交易", "停", "失败", "步"):
        assert word in first, f"确认文案里少了「{word}」"
    rest = card[card.index("window.confirm(") + len(first):]
    rollback = rest[rest.index("window.confirm("):]
    rollback = rollback[: rollback.index(");") + 2]
    assert "回到更新前的提交" in rollback
    assert "rollback" in rollback.lower() or "/api/update/rollback" in rest

# ---------------------------------------------------------------- t85 收口说明与更新卡收尾


def test_the_leverage_note_from_the_server_is_shown() -> None:
    """后端 leverage_note 写着「不许静默改数」：预览要显示，成交后也要留着。"""
    src = _order_ticket_source()
    assert "leverage_note" in src, "后端给的收口说明一个字都没引用"
    assert 'const leverageHint = leverageNoticeText || preview?.leverage_note || "";' in src
    assert "setLeverageNotice(" in src, "成交回执里的 leverage_note 没留下来"
    assert "setLeverageNotice(result.leverage_note" in src, "下单回执里的说明没取"
    assert "setLeverageNotice(" in src and src.count("setLeverageNotice(") >= 2, "上一单的说明没清"
    assert "{leverageTip}" in src, "算了不显示等于没做"
    assert "const leverageTip = leverageHint || maxLeverageHint;" in src
    assert "leverage_note.slice(" not in src, "说明被截断了"
    types = (UI_DIR / "types.ts").read_text(encoding="utf-8")
    assert "leverage_note" in types, "OrderPreview 上没有这个字段"


def test_the_unknown_leverage_cap_says_so_instead_of_nothing() -> None:
    """读不到 risk.max_leverage 就按「不知道上限」处理，给中性提示，不静默。"""
    src = _order_ticket_source()
    assert "maxLeverageState" in src
    assert src.count('remember("unknown", null)') >= 2, "只有一条路记「不知道」"
    assert "风控上限还没读到" in src, "读不到就什么都不说"
    assert 'if (maxLeverageState !== "unknown") return "";' in src
    assert "|| maxLeverageHint" in src, "中性提示没接进那条提示里"
    hook = src[src.index("const loadTradeSettings = useCallback(") :]
    hook = hook[: hook.index("  }, [market]);")]
    assert "catch {" in hook, "读设置的路径没有兜底分支"
    assert 'remember("unknown", null)' in hook, "抛出异常时又静默了"


def test_the_leverage_cap_follows_the_settings_page() -> None:
    """设置页改了 risk.max_leverage，下单框要跟着变，不是挂载时读一次就定住。"""
    src = _order_ticket_source()
    assert "const loadTradeSettings = useCallback(" in src
    assert "[loadTradeSettings, market]" in src, "换市场/重进面板不重读设置"
    assert 'window.addEventListener("focus", refresh)' in src, "从设置页回来不重读"
    assert 'document.addEventListener("visibilitychange", refresh)' in src
    assert 'window.removeEventListener("focus", refresh)' in src, "监听没拆"
    assert 'document.removeEventListener("visibilitychange", refresh)' in src


def test_the_update_polling_stops_when_the_page_unmounts() -> None:
    """更新要重启服务，轮询会一直转：卸载即停，重启期间的重试也要停得住。"""
    card = _update_card_source()
    assert "new AbortController()" in card, "轮询没有可中止的开关"
    assert "updateAbort.current?.abort()" in card, "卸载时没停"
    assert card.count("signal.aborted") >= 2, "读进度与等 /api/health 两个循环都得看中止标记"
    assert "signal: controller.signal" in card or "signal," in card, "请求没带 signal"
    assert "useEffect(() => () => {" in card, "没有卸载清理"


def test_the_manual_restart_command_follows_the_platform() -> None:
    """手动命令按平台给：后端给 platform 就用，没给就按 navigator.userAgent 兜底。"""
    card = _update_card_source()
    assert "platform" in card, "没看后端给的平台"
    assert "navigator.userAgent" in card, "后端没给时没兜底"
    assert "sudo systemctl status bitrader && sudo journalctl -u bitrader -n 50" in card, "Linux 上给了 Windows 的命令"
    types = (UI_DIR / "types.ts").read_text(encoding="utf-8")
    assert types.count("platform?: string | null;") >= 2, "UpdateInfo / UpdateStatus 都没这个字段"


def test_the_update_rows_do_not_show_an_unknown_gap_as_zero() -> None:
    """ahead_by / behind_by 是 null 时数不出差距：显示「—」，别拿 0 冒充「没有差距」。"""
    card = _update_card_source()
    assert "const countText = (value: number | null | undefined) =>" in card
    assert "update.behind_by ??" not in card, "还有地方拿 0 顶替未知"
    assert "update.ahead_by ??" not in card
    assert "countText(update.behind_by)" in card and "countText(update.ahead_by)" in card
    assert "落后 {countText(update.behind_by)} 个 / 领先 {countText(update.ahead_by)} 个" in card
    # 未知时不许说「已是最新」，也不许判分叉
    assert "update.behind_by === 0" in card
    assert "if (ahead == null || behind == null) return \"\";" in card


def test_the_restart_pending_is_shown_as_waiting() -> None:
    """状态里 restart_pending 还在：说明在等新进程起来，别一概按 running 画。"""
    card = _update_card_source()
    assert "restart_pending" in card
    assert "正在等新进程起来" in card
    assert "UPDATE_FINAL_STATES.includes(String(updateJob?.state" in card, "done 之后还挂着旧标记时别乱说在等"
    types = (UI_DIR / "types.ts").read_text(encoding="utf-8")
    assert "restart_pending?: boolean | null;" in types, "UpdateStatus 上没有这个字段"


def test_the_leverage_note_belongs_to_one_ticket_only() -> None:
    """上一条回执说明只对刚才那张票有效：换市场 / 换交易对 / 改杠杆 / 改数量都要清掉。

    usdm 那张票的「这次按 10x 下单」留在现货票上，等于给一张没发生的单作证；
    数量从 100 改成 500 之后那句「按 10x 下单」也一样在骗人。
    """
    src = _order_ticket_source()
    assert "const ticketKey = (quantity: string) =>" in src, "没有「这张票」的记号"
    ticket = src[src.index("const ticketKey = (quantity: string) =>") :]
    ticket = ticket[: ticket.index(";")]
    for part in ("${market}", "${symbol}", "${leverageValue}", "${quantity}"):
        assert part in ticket, f"票的记号没算上 {part}"
    assert "const ticket = ticketKey(size);" in src
    assert "leverageNotice.ticket === ticket" in src, "上一条说明没跟票对号"
    assert "const leverageNoticeText =" in src
    assert "const leverageTip = leverageHint || maxLeverageHint;" in src
    # 提交开始时照旧先清（下一单失败也别把上一条留在屏上）
    assert src.count("setLeverageNotice(") >= 2, "提交开始与回执落地这两处都要动它"
    assert 'ticket: ticketKey("")' in src, "记账用的还是没清空的那张票，说明刚落地就被自己擦掉"
    # 现货不显示杠杆栏，换过去同样得作废
    assert '"spot"' in ticket or "market" in ticket


def test_each_market_keeps_its_own_leverage_cap_hint() -> None:
    """上限读数按市场各记一份：切市场先回到「还在读」，别拿 usdm 的结果去说 coinm。"""
    src = _order_ticket_source()
    assert "capByMarket" in src
    assert "const capReading = capByMarket[market];" in src, "读数没按市场取"
    assert "const maxLeverage = capReading?.cap ?? null;" in src
    assert 'const maxLeverageState: "loading" | "known" | "unknown" = capReading?.state ?? "loading";' in src, (
        "新市场没有「还在读」这一档：上一次的读数会直接顶着用"
    )
    assert 'if (maxLeverageState !== "unknown") return "";' in src
    hook = src[src.index("const loadTradeSettings = useCallback(") :]
    hook = hook[: hook.index("  }, [market]);")]
    assert "const target = market;" in hook, "读数没记在发起这次读的市场名下"
    assert "setCapByMarket((prev) => ({ ...prev, [target]: { state, cap } }))" in hook
    assert 'remember("known", cap)' in hook
    assert hook.count('remember("unknown", null)') >= 2, "只有一条路记「不知道」"

# ---------------------------------------------------------------- t92 更新失败详情与真实端口、未成交不说成交


def test_the_update_status_is_read_as_soon_as_the_page_opens() -> None:
    """重启失败、服务刚回来时打开设置页：挂载就要无条件读一次盘上的状态。

    只在点过「立即更新」之后才有东西看，等于让人在最需要的那一刻看空白。
    """
    card = _update_card_source()
    assert 'api<UpdateStatus>("/api/update/status")' in card, "挂载那次没读盘上的状态（不带 job_id 的那条）"
    assert "const loadUpdateStatus = useCallback(" in card, "没有单独那次「读盘上的状态」"
    assert "void loadUpdateStatus();" in card, "没有在挂载时读一次"
    assert "!UPDATE_FINAL_STATES.includes(state)" in card, "不是「还在跑才进轮询」"
    assert "void watchUpdate(" in card, "正在跑的任务没有接着轮询"
    assert "上一次更新停在失败的那一步" in card, "读回一个失败任务时不说一句"


def test_a_failed_job_on_disk_is_shown_in_full() -> None:
    """失败任务的 error 与每步输出整段显示，连同 from_sha / to_sha / port 一起画出来。"""
    card = _update_card_source()
    assert "{updateJob.error}" in card, "失败原因的后端原话没显示"
    assert "{step.output}" in card, "失败那一步的原始输出没显示"
    assert "updateJob.error.slice(" not in card, "把失败原因截断了"
    assert "step.output.slice(" not in card, "把步骤输出截断了"
    assert "updateJob.from_sha" in card and "updateJob.to_sha" in card, "起始 / 目标提交没画"
    assert "updateJob?.port" in card, "端口没从状态里读"
    assert "{jobTrace.state}" in card and "{jobTrace.port}" in card, "现场那几行没渲染"


def test_the_manual_restart_command_takes_the_real_port() -> None:
    """手动命令：后端给的那条优先；自己拼的端口取状态里记的；只有拿不到才落 8765 并写明。"""
    card = _update_card_source()
    assert "手动拉起：" in card, "后端给的命令没取"
    assert "const backendManual = useMemo(" in card, "没有从失败输出里抠那条命令"
    assert "const updatePort =" in card and "updateJob?.port" in card, "端口没从状态里取"
    assert "run_web.py --host ${updateHost} --port ${port}" in card, "自己拼的那条没跟着状态里的端口走"
    assert "8765" in card and "按默认" in card, "落到 8765 时没写明是兜底值"
    assert "run_web.py --host 127.0.0.1 --port 8765" not in card, "端口还写死 8765"


def test_an_unfilled_order_is_not_reported_as_filled() -> None:
    """限价单刚受理（filled 0、status open）不是「买入成功」：按回执说「已挂单，等待成交」。"""
    src = _order_ticket_source()
    assert "order.filled" in src, "没看回执里的 filled"
    assert "order.status" in src, "没看回执状态"
    assert "filledQty > 0" in src, "没按 filled 分档"
    assert "已挂单，等待成交" in src, "未成交也要说清挂上去了、还没成交"
    assert "已提交，状态未知" in src, "状态读不懂就要照实说不知道"
    assert "买入成功" not in src and "卖出成功" not in src, "受理还写成「成功」"
    assert "买入成交" in src and "卖出成交" in src, "成交那一档也要说人话"

# ---------------------------------------------------------------- t95 桌面端只查 Releases


def _desktop_branch() -> str:
    """桌面卡那一段源码（卡内标记之间）：supported === false 时才渲染的内容。"""
    card = _update_card_source()
    begin = card.index("{/* desktop-card-begin */}")
    split = card.index("{/* desktop-card-split */}")
    end = card.index("{/* desktop-card-end */}")
    assert begin < split < end, "桌面卡与服务器版的标记顺序不对"
    return card[begin:split]


def _server_branch() -> str:
    """服务器版那一段（git 那套）：桌面态里一个都不许渲染。"""
    card = _update_card_source()
    split = card.index("{/* desktop-card-split */}")
    end = card.index("{/* desktop-card-end */}")
    return card[split:end]


def test_the_desktop_card_has_none_of_the_repository_buttons() -> None:
    """桌面安装版里没有 git 仓库：三个按钮、那句「正在读…」、状态轮询一个都不许出现。"""
    desk = _desktop_branch()
    assert "立即更新" not in desk, "桌面卡里还有「立即更新」"
    assert "回滚" not in desk, "桌面卡里还有「回滚」"
    assert "检查更新" not in desk, "桌面卡里还有「检查更新」"
    assert "正在读本地与 GitHub 的提交" not in desk, "桌面卡里还有那句「正在读本地与 GitHub 的提交…」"
    assert "loadUpdateStatus" not in desk, "桌面卡还在读 /api/update/status"
    assert "软件更新" in _update_card_source(), "标题还是「软件更新」"


def test_the_desktop_card_says_which_version_is_out() -> None:
    """有新版：说出远端版本与自己的版本，给下载外链；没挂安装包也要说清去哪看。"""
    desk = _desktop_branch()
    assert "GitHub 上已经有" in desk, "有新版时没说远端是哪个版本"
    assert "你现在是" in desk, "没说本机是哪个版本"
    assert "下载新安装包" in desk
    assert 'target="_blank"' in desk and 'rel="noreferrer"' in desk, "外链要用新窗口打开"
    assert "这个 Release 没有挂安装包" in desk, "Release 没挂安装包时没给替代说法"
    page = (UI_DIR / "pages" / "SettingsPage.tsx").read_text(encoding="utf-8")
    assert "https://github.com/xiaofengxfc/python_jy/releases" in page, "没挂安装包时要去 Release 页面看"
    assert "RELEASES_PAGE" in desk, "页面上的链接没走那个常量"
    assert "{desktopTag}" in desk and "{desktopCurrent}" in desk, "版本号写死了，没取后端给的那几个字段"
    assert "{desktopDownload}" in desk or "desktop?.download_url" in desk, "下载链接没取后端给的"


def test_the_desktop_card_tells_latest_apart_from_unknown() -> None:
    """已最新与查不到是两回事：newer 是 null（比不出来）必须算查不到，不许算已最新。"""
    card = _update_card_source()
    desk = _desktop_branch()
    assert "已是最新" in desk, "已最新那一档没文案"
    assert "查不到 GitHub 上的版本" in desk, "查不到那一档没文案"
    assert "重新检查" in desk, "查不到时要能重新检查"
    assert 'refresh ? "/api/update/info?refresh=1" : "/api/update/info"' in card, "重新检查没打 ?refresh=1"
    assert "desktop.newer === true" in card and "desktop.newer === false" in card
    assert card.count(': "unknown"') >= 2, "null / 缺字段 / 有 error 都要算「查不到」"
    assert "不下载不改文件" in desk, "卡底要写清这里只比对、不下载不改文件"


def test_the_server_card_is_untouched_when_supported_is_missing() -> None:
    """服务器版不许被改坏：supported 缺失或 true 时按钮、那句「正在读…」、轮询照旧。"""
    card = _update_card_source()
    assert "const desktopMode = Boolean(update && update.supported === false);" in card, (
        "只有后端明确说 false 才切桌面，别拿「没这个字段」当桌面"
    )
    server = _server_branch()
    for word in ("检查更新", "立即更新", "回滚", "正在读本地与 GitHub 的提交"):
        assert word in server, f"服务器版少了「{word}」"
    assert "info?.supported !== false" in card, "桌面态还在轮询 /api/update/status"


def test_the_desktop_types_are_declared() -> None:
    """/api/update/info 的桌面字段要有类型，别拿 any 顶。"""
    types = (UI_DIR / "types.ts").read_text(encoding="utf-8")
    for field in ("supported", "mode", "version", "desktop"):
        assert f"{field}?" in types, f"UpdateInfo 少了 {field}"
    for field in ("current", "latest", "tag", "newer", "download_url", "published_at", "notes"):
        assert field in types, f"desktop 块少了 {field}"

# ---------------------------------------------------------------- t101 三态都能重新检查、缓存要说实话、主框架导航守住


def test_the_desktop_card_offers_a_refresh_in_every_state() -> None:
    """有新版 / 已最新 / 查不到 三档都要能立刻重查：刚发完版不该只能等 300 秒缓存。"""
    desk = _desktop_branch()
    assert desk.count("重新检查") == 1, "按钮该有一份、三态共用；只有一份说明它没挂在某一档里"
    marker = desk.index("{/* desktop-refresh-row */}")
    for state in ("newer", "latest", "unknown"):
        assert desk.index(f'desktopState === "{state}"') < marker, f"{state} 那一档该在共用按钮之前收尾"
    after_unknown = desk[desk.index('{desktopState === "unknown" && ('):]
    assert after_unknown.index("</>") < after_unknown.index("重新检查"), (
        "「重新检查」还被关在查不到那一档里，有新版 / 已最新时点不到"
    )
    row = desk[marker:]
    row = row[: row.index("</div>")]
    assert "loadUpdate(true)" in row, "重新检查要打 ?refresh=1"


def test_the_unknown_desktop_state_still_gives_the_download_link() -> None:
    """tag 读不懂时 newer 是 null，但 Release 上挂着安装包：这条兜底链接必须能到达。"""
    desk = _desktop_branch()
    unknown = desk[desk.index('{desktopState === "unknown" && ('):]
    assert "比不出新旧" in unknown, "比不出新旧时没说可以自己下"
    assert "{desktopDownload}" in unknown, "兜底链接没取后端给的 download_url"
    assert 'target="_blank"' in unknown and 'rel="noreferrer"' in unknown
    assert "desktopDownloadNote" in desk, "download_note 没显示"
    assert "{desktopDownloadNote}" in unknown, "后端给的 download_note 没原样贴出来"


def test_the_desktop_card_says_cached_when_the_answer_came_from_cache() -> None:
    """缓存命中就说「缓存于」：把 5 分钟前抓的回包写成「刚查的」就是骗人。"""
    card = _update_card_source()
    desk = _desktop_branch()
    assert "desktop?.cached" in card or "desktop.cached" in card, "没看 cached 标记"
    assert "desktop?.fetched_at" in card, "没取真的抓取时间 fetched_at"
    assert "缓存于 " in card and "5 分钟内不再问 GitHub" in card, "缓存命中时没有说法"
    assert "查于 " in card, "没有缓存时仍要说「查于」"
    assert "{desktopStamp}" in desk, "算了不显示等于没做"
    types = (UI_DIR / "types.ts").read_text(encoding="utf-8")
    for field in ("download_note", "fetched_at", "cached"):
        assert field in types, f"UpdateDesktop 少了 {field}"


def test_the_shell_guards_the_main_frame_navigation() -> None:
    """主框架自己的导航也要守：拖个文件进来不能把整页导航到 file://。"""
    main = (ROOT / "electron" / "main.cjs").read_text(encoding="utf-8")
    assert 'on("will-navigate"' in main, "主框架导航没有守卫"
    nav = main[main.index("const allowedOrigins"):]
    nav = nav[: nav.index("void win.loadURL")]
    assert "event.preventDefault()" in nav, "越界的导航没挡住"
    assert "127.0.0.1" in nav and "localhost" in nav, "只放行回环地址"
    assert "allowedOrigins" in nav or "origin" in nav, "按 origin 判，别按前缀猜"
    assert "file:" not in nav and "data:" not in nav, "不许出现放行 file:/data: 的判断"
    # 壳自己的初始加载不能被挡：守卫只挂在 will-navigate 上，不碰 loadURL
    assert 'void win.loadURL(target);' in main


# ------------------------------------------------ 前端复审 W1–W7（方案 A）


def test_a_mainnet_close_asks_first_and_sends_confirm_live() -> None:
    """主网平仓必须带 confirm_live：不带它后端直接 400，仓位一动不动。

    实测（主网 ctx）：POST /api/positions/close 回
    「主网下单需要二次确认（confirm_live=true）。测试网不需要。」，仓位还是 0.02 BTC。
    界面上的单行平仓、部分平仓、钱包行卖出、全部平仓，在主网一条都走不通。
    """
    tabs = (UI_DIR / "components" / "BottomTabs.tsx").read_text(encoding="utf-8")
    assert 'if (network !== "mainnet") return true;' in tabs, "测试网不该弹窗"
    assert "const confirmClose = (what: string): boolean => {" in tabs
    assert 'confirm_live: network === "mainnet",' in tabs
    assert tabs.count('confirm_live: network === "mainnet",') == 2, "单行平仓与全部平仓都要带"
    assert "!confirmClose(" in tabs, "两个入口都要先问一句"
    # 数量框和 held 上限也按基础币收口，否则币本位会按 500 倍的数量下单
    assert "const held = sizeOf(position);" in tabs


def test_the_execution_console_can_start_on_mainnet() -> None:
    """/api/auto/start 在主网要求 confirm_live。执行台以前从来没带过：主网启动必 400，
    而同一个引擎从 AI 量化页的部署按钮却能启动。
    """
    console = (UI_DIR / "components" / "ExecutionConsole.tsx").read_text(encoding="utf-8")
    assert 'const live = settings?.network === "mainnet";' in console
    assert "confirm_live: live," in console, "请求体里没有 confirm_live"
    assert "window.confirm(" in console, "主网启动要先问一句"


def test_position_quantities_are_base_coins_not_contracts() -> None:
    """界面上的数量一律读 size_base：contracts 是交易所口径的张。

    币本位 BTC/USD:BTC 一张 100 USD：0.02 BTC 的仓位 contracts = 10。前端把 10 当
    BTC 用，100% 按钮填 10（= 5,000 张），部分平仓按 10 做 reduceOnly 被交易所直接拒，
    资产页数量列比真实大 500 倍（1000SHIB 那种一张 1000 币的更大）。
    """
    types = (UI_DIR / "types.ts").read_text(encoding="utf-8")
    assert "size_base?: number | null;" in types, "Position 上没有基础币数量"
    for name in (
        "components/BottomTabs.tsx",
        "components/ProtectionDialog.tsx",
        "pages/PortfolioPage.tsx",
        "pages/TradingPage.tsx",
    ):
        src = (UI_DIR / name).read_text(encoding="utf-8")
        assert "size_base" in src, f"{name} 还在拿 contracts 当币用"
    page = (UI_DIR / "pages" / "TradingPage.tsx").read_text(encoding="utf-8")
    assert "held.size_base ?? held.contracts" in page, "下单框的数量预算没换成基础币"
    # 权益兜底别自己拿数量乘价（合约数量是张），直接用后端算好的市值
    assert "sum + (position.quote_value || 0)" in page
    portfolio = (UI_DIR / "pages" / "PortfolioPage.tsx").read_text(encoding="utf-8")
    assert "fmt(Math.abs(position.size_base ?? position.contracts), 6)" in portfolio


def test_a_half_armed_guard_is_reported_as_half_armed() -> None:
    """保护单回执只报真的挂上的那条腿，并带上 warnings。

    后端一条腿失败时 protection 里的 stop_loss / take_profit 回 0.0 并在 warnings 里说明；
    前端如果拿请求值拼文案，那一侧的仓位其实没有任何交易所侧保护，界面却写「已挂到持仓」。
    """
    for name in ("components/OrderTicket.tsx", "components/ProtectionDialog.tsx"):
        src = (UI_DIR / name).read_text(encoding="utf-8")
        assert "result?.protection?.stop_loss" in src, f"{name} 没读回执里真的挂上的止损"
        assert "result?.protection?.take_profit" in src, f"{name} 没读回执里真的挂上的止盈"
        assert "warnings" in src, f"{name} 没显示 warnings"


def test_a_stale_preview_cannot_block_a_valid_order() -> None:
    """preview 有 220ms 防抖：数量刚改、预览还没追上时，旧数字不许参与判定。

    实测：先填 1（太小）、迅速改成 100，在 preview 追上之前点提交，会被按
    「这一单只有 1.00」误拦。
    """
    ticket = (UI_DIR / "components" / "OrderTicket.tsx").read_text(encoding="utf-8")
    assert "const currentPreview = preview && previewFor === previewTicket ? preview : null;" in ticket
    assert "const tooSmall = minimumProblem(amount);" in ticket
    # 结论行与 AI 预判也只认当前这张票
    assert "currentPreview.allowed" in ticket
    assert "if (!currentPreview) return;" in ticket
    assert "disabled={prejudgeBusy || !currentPreview}" in ticket
    # 过期时明确说一句，别把上一单的「风控通过」留在屏上
    assert "改动了，正在重算…" in ticket


def test_the_percent_buttons_multiply_by_the_leverage() -> None:
    """交易页「25%」是**保证金**比例：合约 10 倍下应下 2.5 倍权益的名义额。

    以前 applyRatio 只做 available * ratio，杠杆没进这个式子：填 10 倍、点 25%，
    实际只占掉 2.5% 保证金，用户看到的是「设了杠杆，点 25% 没加杠杆」。
    现货没有杠杆、合约卖出已有多头是平仓，这两种都不能乘。
    """
    ticket = _order_ticket_source()
    assert "const ratioSize = (ratio: number) => available * ratio * ratioLeverage;" in ticket
    assert "const amount = ratioSize(ratio);" in ticket
    assert "title={ratioTitle(ratio)}" in ticket
    assert 'if (market === "spot") return 1;' in ticket
    assert 'if (side === "sell" && positionQty > 0) return 1;' in ticket
    assert "return leverageValue > 1 ? leverageValue : 1;" in ticket
    # 按钮上要把「保证金 → 名义额」这笔账写出来
    assert "作保证金 × " in ticket and "名义额" in ticket

