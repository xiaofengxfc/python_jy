"""AI 交易：决策，闸门，下单。

这个模块只做这三件事。它不 import 交易所，也不 import 数据库。唯一的
下单出口是注入的 ``TradingService.submit()``（INV-1）。风控仍然以
``RiskManager`` 为准，``AIGuard`` 只在它前面加一层 AI 自己的收紧（INV-2）。

构造完成后是停止状态。真下单没有单独的开关，只要三条同时成立：AI 助手已启用、
这条市场配了凭据，主网还要调用方显式确认这是真钱（``confirm_live``）；另外未急停
（INV-9）。测试网和主网走同一条路径，只差 ``network`` 指向谁。
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .. import ai, ai_pipeline, paths
from ..core.clock import local_day
from ..core.types import (
    TIMEFRAME_SECONDS,
    MarketType,
    OrderSide,
    OrderType,
    timeframe_seconds,
)
from .trading import OrderRequest, OrderResult

log = logging.getLogger(__name__)

ACTIONS = ("open_long", "open_short", "close_long", "close_short", "hold")
ENTRY_ACTIONS = ("open_long", "open_short")
CLOSE_ACTIONS = ("close_long", "close_short")

STATE_STOPPED = "stopped"
STATE_RUNNING = "running"
STATE_PAUSED = "paused"
STATE_HALTED = "halted"

# 裁决段的输出格式在 ai_pipeline 里，解析和提示词必须同源。
DECISION_PROMPT = ai_pipeline.DECISION_PROMPT

# ------------------------------------------------------------------- cadence
# 决策节奏跟着 K 线周期走，不再按固定秒数轮询：选 1h 就是每根 1h K 线收盘后决策
# 一次。以前固定每 poll_sec（默认 60 秒）跑一整轮，1h 周期下每小时问 60 次模型、
# 拿回 60 个互相打架的方向，还都是按同一根没走完的 K 线做的判断。
#
# 一根 K 线之内只决策一次，判据是「已收盘 K 线的时间戳」，不是墙上时钟：睡过头、
# 系统休眠、进程重启都不会把同一根算两遍，也不会因为本机时钟差几秒就跳过一整根。
#
# 收盘后等 BAR_SETTLE_SECONDS 才去问（整点那一秒读，交易所常常还给的是上一根）。
# 这根 K 线迟迟不来（交易所延迟、或被闸门拦住）就在 BAR_WINDOW_AFTER 这个窗口里
# 按 poll_sec 小步重试；窗口过了就认这一根没数据，等下一根——不会退化成每秒轮询。
BAR_SETTLE_SECONDS = 3.0
BAR_WINDOW_AFTER = 120.0


# --------------------------------------------------------------- data types
@dataclass(frozen=True)
class AIDecision:
    """模型给出的一条决策。schema 里没有 leverage（INV-6）。

    ``created_at`` 是解析出这条决策的时刻，有效期（ttl_sec / decision_ttl_sec）从
    这一刻算到提交为止。
    """

    decision_id: str
    action: str
    symbol: str
    quote: float = 0.0
    price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    confidence: float = 0.0
    ttl_sec: float = 0.0
    rationale: str = ""
    created_at: float = 0.0

    @property
    def is_entry(self) -> bool:
        return self.action in ENTRY_ACTIONS

    @property
    def is_close(self) -> bool:
        return self.action in CLOSE_ACTIONS


@dataclass
class AITraderConfig:
    """一份自包含的运行配置。有没有凭据决定能不能下单，配置本身不设闸。"""

    enabled: bool = False
    confirm_live: bool = False
    network: str = "mainnet"
    symbol: str = "BTC/USDT"
    # 策略只是「参考方向」的来源（策略为辅、AI 为主），但重命名/删除策略时
    # 后端要能认出它，所以还是记下 key。
    strategy_key: str = ""
    market_type: str = MarketType.SPOT.value
    timeframe: str = "15m"
    leverage: float = 1.0
    # 被风控/交易所上限收口时那句话，原样回显给界面（静默改数比报错更糟）。
    leverage_note: str = ""
    max_order_quote: float = 50.0
    max_position_quote: float = 200.0
    max_decisions_per_day: int = 96
    max_orders_per_hour: int = 12
    min_confidence: float = 0.6
    decision_ttl_sec: float = 120.0
    max_price_drift_pct: float = 0.005
    cooldown_sec: float = 30.0
    # 节奏由 timeframe 决定（见上面的 BAR_WINDOW_*）。poll_sec 现在只剩一个作用：
    # 收盘窗口里小步重试的间隔上限。改它动不了「每根 K 线决策一次」这条。
    poll_sec: float = 60.0
    require_stop: bool = True
    # 决策深度：quick 一次调用，standard 四段，deep 六段。引擎默认最省。
    depth: str = "quick"
    # 决策节奏：bar = 跟主周期收盘一次；其余按固定节拍（5m / 15m / ...）。
    # 节奏与主周期是两个独立选项：4h 结构 + 每 15 分钟决策一次是合法组合。
    cadence: str = "bar"
    # 参考周期（除主周期之外的结构周期，最多两个）。
    timeframes: tuple[str, ...] = ()
    # 人确认过的决策准则（ai_policy 当前版本）。空串 = 没有，提示词和改前一致。
    policy_text: str = ""
    # 准则版本号，只用来回填审计：事后要能对上「这条决策是按哪一版准则做的」。
    policy_version: int = 0


@dataclass
class GuardVerdict:
    ok: bool
    reason: str = ""


class TradeBudget:
    """轮询和「立即决策」共用的一份下单预算：当日决策次数、每小时订单数、冷却。

    两个入口各自 new 一个 AITrader（「立即决策」点一次就 new 一次）。计数留在实例里，
    点第二下就从零开始数，96 次/日、12 单/时、30 秒冷却对这条路径等于不存在。挪到这里，
    谁从哪个入口进来都数同一份。读它的是 poller 和接口两处线程，所以全走锁。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._day = ""
        self._decisions = 0
        self._order_times: deque[float] = deque(maxlen=200)
        self._last_order_at = 0.0

    def roll(self, now: float) -> None:
        """跨天清零（本机时区的一天，和风控、模型调用次数同一口径）。"""
        day = local_day(now)
        with self._lock:
            if day == self._day:
                return
            self._day = day
            self._decisions = 0
            self._order_times.clear()
            self._last_order_at = 0.0

    @property
    def decisions_today(self) -> int:
        with self._lock:
            return self._decisions

    def note_decision(self) -> None:
        """记一次真的花出去的模型调用。"""
        with self._lock:
            self._decisions += 1

    def orders_last_hour(self, now: float) -> int:
        with self._lock:
            while self._order_times and now - self._order_times[0] > 3600:
                self._order_times.popleft()
            return len(self._order_times)

    @property
    def last_order_at(self) -> float:
        with self._lock:
            return self._last_order_at

    def note_order(self, now: float) -> None:
        with self._lock:
            self._order_times.append(now)
            self._last_order_at = now


class TradeLease:
    """最小互斥租约：auto / ai / quant 同一时刻只有一个能操作资金（INV-14）。

    预算跟着租约走：拿到同一把租约的引擎，数的是同一份当日决策次数、每小时订单数
    和冷却。控制器给轮询和「立即决策」的本来就是同一把租约，所以两条路径共用一份；
    没接控制器、自己 new 一个 AITrader 的地方（测试、脚本）各自一把，互不干扰。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._owner = ""
        self.budget = TradeBudget()

    @property
    def owner(self) -> str:
        with self._lock:
            return self._owner

    def acquire(self, owner: str) -> bool:
        with self._lock:
            if self._owner and self._owner != owner:
                return False
            self._owner = owner
            return True

    def release(self, owner: str) -> None:
        with self._lock:
            if self._owner == owner:
                self._owner = ""


# ---------------------------------------------------------------- audit log
class _AuditState:
    """一份审计文件在进程内共享的状态：一把锁 + 数到哪儿了。

    锁和计数器按「文件」放，不按「实例」放：轮询和「立即决策」各 new 一个
    AITrader，两个实例一起写同一份文件时必须排同一队、数同一个号。
    """

    __slots__ = ("lock", "last")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.last: int | None = None


_AUDIT_STATE: dict[str, _AuditState] = {}
_AUDIT_STATE_GUARD = threading.Lock()

# 跨进程锁的等待上限。别的进程只在 append 期间持锁（微秒级），等不到就是出事了：
# 那时宁可记一条日志照写，也不能把决策线程挂在这儿。
AUDIT_LOCK_TIMEOUT = 2.0
_AUDIT_LOCK_POLL = 0.01


def _lock_handle(handle: int) -> None:
    """给一个已经打开的句柄加排它锁；拿不到就抛 OSError。"""
    if os.name == "nt":
        import msvcrt

        os.lseek(handle, 0, os.SEEK_SET)
        msvcrt.locking(handle, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_handle(handle: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(handle, 0, os.SEEK_SET)
        msvcrt.locking(handle, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle, fcntl.LOCK_UN)


@contextlib.contextmanager
def _audit_file_lock(path: Path):
    """跨进程互斥（yield 是否真的拿到锁），等待有上限，绝不死等。

    锁的是旁边那个 ``.lock`` 文件，不锁审计文件本身：Windows 的字节锁连读都
    挡住，面板正在读日志时撞上写入就会报错。进程退出时系统收回锁，不会留下僵尸锁。
    """
    handle: int | None = None
    acquired = False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
        deadline = time.monotonic() + max(0.0, AUDIT_LOCK_TIMEOUT)
        while True:
            try:
                _lock_handle(handle)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    log.warning("拿不到审计文件锁，本条序号可能和别的进程撞：%s", path)
                    break
                time.sleep(_AUDIT_LOCK_POLL)
        yield acquired
    finally:
        if handle is not None:
            if acquired:
                try:
                    _unlock_handle(handle)
                except OSError:  # pragma: no cover - 关句柄也会放锁
                    pass
            try:
                os.close(handle)
            except OSError:  # pragma: no cover
                pass


def _audit_state(path: Path) -> _AuditState:
    key = str(Path(path).resolve()).lower()
    with _AUDIT_STATE_GUARD:
        state = _AUDIT_STATE.get(key)
        if state is None:
            state = _AuditState()
            _AUDIT_STATE[key] = state
        return state


def _last_seq_in(path: Path) -> int:
    """文件里最大的 seq；没有就是 0。只读尾部——文件是追加写的，最大号在最后。"""
    try:
        size = path.stat().st_size
    except OSError:
        return 0
    if size <= 0:
        return 0
    chunk = 64 * 1024
    try:
        with path.open("rb") as handle:
            if size > chunk:
                handle.seek(size - chunk)
            text = handle.read().decode("utf-8", errors="ignore")
    except OSError:
        return 0
    best = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue  # 尾部半行、老坏行：都跳过
        if not isinstance(row, dict):
            continue
        try:
            value = int(row.get("seq") or 0)
        except (TypeError, ValueError):
            continue
        best = max(best, value)
    return best


class AuditLog:
    """一份共享审计文件的写入器：seq 在这份文件里单调，跨实例、跨进程都不重号。

    三轮才走到位，记一下为什么：

    1. 最早 seq 是实例内自增（每个新建的 AITrader 从 1 开始数），同一个文件里
       decision 行全是 1、lifecycle 行干脆没有 seq。
    2. 改成按「文件」共享一个进程内计数器：跨实例对了，跨进程还是撞——计数器
       只在进程第一次写时读一次文件尾部，别的进程后来写进来的号它看不见。
    3. 现在每次 append 都在**文件锁**里重读一遍尾部最大号再定号，读写在同一段
       临界区里。代价是每次几十 KB 的顺序读，一小时几十行的量可以忽略。

    进程退出时系统收回文件锁，不会留下僵尸锁；等不到锁（2 秒）就记日志照写，
    绝不把决策线程挂住（INV-15）。重启也不倒退：号是从文件里读出来的，不是从
    内存里带过来的。
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._state = _audit_state(self.path)

    @property
    def lock_path(self) -> Path:
        return self.path.with_name(self.path.name + ".lock")

    @property
    def last_seq(self) -> int:
        with self._state.lock:
            self._state.last = max(int(self._state.last or 0), _last_seq_in(self.path))
            return int(self._state.last or 0)

    def append(self, record: dict[str, Any]) -> dict[str, Any]:
        """补一个 seq 并整行写下去，返回真正写出去的那行。

        seq = 「文件里现有的最大号」+ 1，这一句读、定号、写都在文件锁里完成，
        所以两个进程不会同时读到同一个最大号。_state.last 只当兜底下限用
        （读不到文件时至少不比本进程见过的号小）。
        """
        with self._state.lock:
            row = dict(record)
            line = ""
            with _audit_file_lock(self.lock_path):
                best = max(int(self._state.last or 0), _last_seq_in(self.path))
                self._state.last = best + 1
                row["seq"] = self._state.last
                line = json.dumps(row, ensure_ascii=False) + "\n"
                try:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    # 一次 os.write 落一整行。缓冲写会把一行拆成两次落盘，读的人
                    # 会看到半行——那一行 JSON 就废了，整张事件表跟着解析失败；
                    # 两个进程同时追加时还会互相插进对方行中间。
                    handle = os.open(
                        self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644
                    )
                    try:
                        os.write(handle, line.encode("utf-8"))
                    finally:
                        os.close(handle)
                except OSError:  # pragma: no cover - 磁盘满等
                    log.warning("写入 AI 交易审计失败", exc_info=True)
            return row


def read_audit_rows(
    path: Path, *, since: int = 0, limit: int = 100
) -> list[dict[str, Any]]:
    """读审计文件：seq > since 的最后 limit 行。

    ``since=0`` 就是「全都要」——页面一直这么取，行为不变。老版本写的行没有
    seq（lifecycle 行），它们只在 since=0 时算新行；否则按 seq 增量取的人会
    永远重复拿到同一批老行。
    """
    target = Path(path)
    if not target.exists():
        return []
    count = max(1, int(limit))
    rows: deque[dict[str, Any]] = deque(maxlen=count)
    with target.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue  # 坏行跳过，不毁整张表
            if not isinstance(row, dict):
                continue
            try:
                seq = int(row.get("seq") or 0)
            except (TypeError, ValueError):
                seq = 0
            if since > 0 and seq <= since:
                continue
            rows.append(row)
    return list(rows)

# ------------------------------------------------------------------ parsing
def parse_decision(text: str, *, now: float | None = None) -> tuple[AIDecision | None, str]:
    """把模型文本解析成决策。解析不出来就是失败，调用方必须不下单。

    ``now`` 是「解析出这条决策」的时刻（AIGuard 拿它算有效期），别把这一轮开始的
    时刻传进来——那会让决策凭空变老，也会让有效期闸永远算不出间隔。
    """
    found = ai._find_json_object(text or "")
    if found is None:
        return None, "模型没有返回可解析的 JSON 决策"
    data, _start, _end = found
    try:
        action = str(data.get("action") or "").strip().lower()
        if action not in ACTIONS:
            return None, f"未知动作：{action or '空'}"
        price = data.get("price")
        stop = data.get("stop_loss")
        take = data.get("take_profit")
        decision = AIDecision(
            decision_id=str(data.get("decision_id") or uuid.uuid4().hex[:12]),
            action=action,
            symbol=str(data.get("symbol") or "").strip().upper(),
            quote=float(data.get("quote") or 0.0),
            price=float(price) if price not in (None, "", "null") else None,
            stop_loss=float(stop) if stop not in (None, "", "null") else None,
            take_profit=float(take) if take not in (None, "", "null") else None,
            confidence=float(data.get("confidence") or 0.0),
            ttl_sec=float(data.get("ttl_sec") or 0.0),
            rationale=str(data.get("rationale") or "")[:200],
            created_at=time.time() if now is None else now,
        )
    except (TypeError, ValueError) as exc:
        return None, f"决策字段类型错误：{exc}"
    return decision, ""


def validate_decision(
    decision: AIDecision,
    config: AITraderConfig,
    *,
    reference_price: float | None = None,
) -> list[str]:
    """结构校验。任何一条不过都不允许下单。

    reference_price 是下单那一刻的行情价。模型没给参考价（price 为 null）时，
    止损止盈的方向只能靠它判，否则这两条检查会被整段跳过。
    """
    errors: list[str] = []
    # NaN/inf 和任何数比大小都是假：quote=NaN 时「必须大于 0」「不许超过单笔上限」
    # 「持仓不会超限」全都判不出来，一路走到下单，提交 amount=NaN，还把 NaN 写进
    # 审计 JSONL（不是合法 JSON，前端 JSON.parse 整块都会失败）。
    for label, value in (
        ("下单金额", decision.quote),
        ("参考价", decision.price),
        ("止损价", decision.stop_loss),
        ("止盈价", decision.take_profit),
        ("置信度", decision.confidence),
        ("有效期", decision.ttl_sec),
    ):
        if value is None:
            continue
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            errors.append(f"{label}不是数值")
            continue
        if not math.isfinite(numeric):
            errors.append(f"{label}必须是有限数值")
    if decision.action not in ACTIONS:
        errors.append("动作非法")
    if decision.symbol != config.symbol.upper():
        errors.append(f"交易对不匹配：{decision.symbol or '空'} != {config.symbol}")
    if not (0.0 <= decision.confidence <= 1.0):
        errors.append("置信度必须在 0 到 1 之间")
    if decision.is_entry:
        if decision.quote <= 0:
            errors.append("开仓金额必须大于 0")
        if config.max_order_quote and decision.quote > config.max_order_quote + 1e-9:
            # 超限拒绝而不是截断（INV-3）：截断会让模型以为自己的下单金额生效了。
            errors.append(
                f"单笔金额 {decision.quote:.2f} 超过 AI 上限 {config.max_order_quote:.2f}"
            )
        if config.require_stop and decision.stop_loss is None:
            errors.append("缺少止损价")
        if decision.price is not None and decision.price <= 0:
            errors.append("参考价必须大于 0")
        # 模型不给 price 时用行情价当参考价：下单时 _submit 就是这么取价的，
        # 校验也得用同一个价，否则「多单止损在现价上方」这种会直接漏过去。
        reference = decision.price if decision.price else reference_price
        if decision.stop_loss is not None and reference:
            if decision.action == "open_long" and decision.stop_loss >= reference:
                errors.append("多单止损价必须低于参考价")
            if decision.action == "open_short" and decision.stop_loss <= reference:
                errors.append("空单止损价必须高于参考价")
        # 止盈和止损是对称的两条：多单止盈放在现价下方（空单在上方）会被交易所
        # 立刻触发，要么拒单要么当场按市价了结。止损查了，止盈不能漏。
        if decision.take_profit is not None and reference:
            if decision.action == "open_long" and decision.take_profit <= reference:
                errors.append("多单止盈价必须高于参考价")
            if decision.action == "open_short" and decision.take_profit >= reference:
                errors.append("空单止盈价必须低于参考价")
    if decision.is_close and decision.quote <= 0:
        # 平仓不查金额时，quote=0/负数会变成 amount=0 / amount<0 的平仓单：
        # 交易所一拒，仓还在，审计里却写着 order。
        errors.append("平仓金额必须大于 0")
    if decision.ttl_sec < 0:
        errors.append("有效期不能为负")
    return errors


class AIGuard:
    """AI 专属闸门：在 RiskManager 之前再收紧一层（INV-2/3/4/5/7/12）。"""

    def __init__(self, config: AITraderConfig, risk: Any | None = None) -> None:
        self.config = config
        self.risk = risk

    def check(
        self,
        decision: AIDecision,
        *,
        now: float,
        position_quote: float,
        equity: float,
        decisions_today: int,
        orders_last_hour: int,
        last_order_at: float,
        market_price: float | None,
        kill_switch: bool = False,
        halted_reason: str = "",
    ) -> GuardVerdict:
        c = self.config
        if kill_switch:
            return GuardVerdict(False, "全局急停已开启")
        if halted_reason:
            return GuardVerdict(False, f"风控熔断中：{halted_reason}")
        if decision.action == "hold":
            return GuardVerdict(False, "模型选择观望")
        if not decision.is_entry and not decision.is_close:
            return GuardVerdict(False, "动作不支持")
        if decision.confidence < c.min_confidence:
            return GuardVerdict(False, f"置信度 {decision.confidence:.2f} 低于门槛 {c.min_confidence:.2f}")
        # 有效期：模型给的 ttl_sec 和配置里的 decision_ttl_sec 取更紧的那个，配置是上限。
        # created_at 是解析出决策的时刻，now 是此刻（提交前）——以前 created_at 就是
        # now，这条判断永远不成立，decision_ttl_sec 全库没人读（B1）。
        limits = [value for value in (decision.ttl_sec, c.decision_ttl_sec) if value and value > 0]
        if limits and decision.created_at:
            age = now - decision.created_at
            ttl = min(limits)
            if age > ttl:
                return GuardVerdict(
                    False, f"决策已过期（已过 {age:.0f} 秒，有效期 {ttl:.0f} 秒）"
                )
        if market_price and decision.price:
            drift = abs(market_price - decision.price) / max(decision.price, 1e-9)
            if drift > c.max_price_drift_pct:
                return GuardVerdict(False, f"价格已漂移 {drift:.2%}，超过 {c.max_price_drift_pct:.2%}")
        if c.max_decisions_per_day and decisions_today >= c.max_decisions_per_day:
            return GuardVerdict(False, "已达 AI 当日决策次数上限")
        if c.max_orders_per_hour and orders_last_hour >= c.max_orders_per_hour:
            return GuardVerdict(False, "已达 AI 每小时下单次数上限")
        if last_order_at and c.cooldown_sec and now - last_order_at < c.cooldown_sec:
            return GuardVerdict(False, "AI 下单冷却中")
        if decision.is_entry and c.max_position_quote:
            if position_quote + decision.quote > c.max_position_quote + 1e-9:
                return GuardVerdict(False, "持仓金额将超过 AI 上限")
        if decision.is_entry and self.risk is not None:
            verdict = self.risk.check_order(
                decision.quote,
                position_quote,
                decision.quote,
                leverage=c.leverage,
                entry=True,
                equity=equity,
            )
            if not getattr(verdict, "allowed", True):
                return GuardVerdict(False, f"风控拒绝：{getattr(verdict, 'reason', '')}")
        return GuardVerdict(True)


# ------------------------------------------------------------------ trader
class AITrader:
    """AI 自主交易控制器：poller + 决策 + 闸门 + 审计。"""

    def __init__(
        self,
        config: AITraderConfig,
        *,
        trading: Any,
        llm: Any,
        frame_provider: Callable[[], Any] | None = None,
        frames_provider: Callable[[], dict[str, Any]] | None = None,
        portfolio_provider: Callable[[], dict[str, Any]] | None = None,
        signal_provider: Callable[[], float] | None = None,
        news_provider: Callable[[], str] | None = None,
        audit_path: Path | None = None,
        clock: Callable[[], float] = time.time,
        lease: TradeLease | None = None,
        risk: Any | None = None,
        budget: TradeBudget | None = None,
    ) -> None:
        self.config = config
        self.trading = trading
        self.llm = llm
        self.frame_provider = frame_provider
        # 多周期：返回 {周期: 已收盘 K 线}。给了它就用它，frame_provider 只留给
        # 老调用方（测试与单周期路径）。主周期必须在这个字典里。
        self.frames_provider = frames_provider
        self.portfolio_provider = portfolio_provider
        self.signal_provider = signal_provider
        self.news_provider = news_provider
        self._last_signal: float | None = None
        self.audit_path = audit_path or (paths.logs_dir() / "ai-trade-audit.jsonl")
        # seq 归这份文件所有，不归实例：见 AuditLog 的说明。
        self._audit_log = AuditLog(self.audit_path)
        self.clock = clock
        self.lease = lease or TradeLease()
        # 计数要和别的实例共用：「立即决策」每点一次就是一个新实例（B2）。控制器给
        # 每个实例的是同一把租约，预算挂在租约上，所以两条路径数的是同一份；显式传
        # budget 的（将来控制器自己持有一份也行）优先。
        self.budget = budget if budget is not None else self.lease.budget
        self.guard = AIGuard(config, risk)
        # Unique lease identity: two AITrader instances must not both think
        # they hold the single "ai" slot.
        self._owner_name = f"ai-{uuid.uuid4().hex[:8]}"
        self._lock = threading.RLock()
        self._state = STATE_STOPPED
        self._halted_reason = ""
        self._paused = False
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._seq = 0
        self._events: deque[dict[str, Any]] = deque(maxlen=200)
        self.last_decision: dict[str, Any] | None = None
        # 上一次真正花掉模型调用的那根 K 线的开放时间（毫秒）：「这一根处理过
        # 没有」的去重键。只在真要调模型那一刻前进——模型失败、被闸门拦住都不算
        # 跑过，那根 K 线还在收盘窗口里就还能重试。
        self._last_bar_ts = 0
        # 下一根该收盘的时刻（epoch 秒）。从「刚处理的那根 K 线」推出来：它的
        # 收盘点是 bar_ts + 周期，再下一根就是 + 2 个周期。睡到这儿之前什么都不
        # 用问，这就是「1h 周期 = 每小时决策一次」落到实处的地方。
        self._pending_close = 0.0
        # 固定节拍用另一对锚：已处理的那一片的起点（epoch 秒）与它的编号。
        # 去重键是编号，不是 K 线时间戳——这正是「节奏与周期解耦」的地方：
        # 同一根 1h K 线之内可以按 15m 决策四次，每次拿到的仍是已收盘的那根。
        self._pending_slot = 0.0
        self._last_slot = 0

    # ------------------------------------------------------------- lifecycle
    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def _assistant_blocked(self) -> str:
        """AI 助手能不能真的调用模型；不能就返回一句拒启动的理由。

        以前这里只看交易所凭据：AI 的 Key 没配时启动照样成功，界面显示「运行中」，
        每一轮决策只在审计里写一条「模型调用失败：还没有填写 API Key。」，人以为它在干活。
        没有 Key 就和没启动一样，不如当场说清楚。

        查不到状态就放行（测试替身、离线调用没有 status()），别为了这一道闸把替身全拦了。
        """
        reader = getattr(self.llm, "status", None)
        if reader is None:
            return ""
        try:
            info = reader()
        except Exception:
            return ""
        if not isinstance(info, dict):
            return ""
        if info.get("configured"):
            return ""
        return "AI 助手还没有配 API Key，先在设置里填写后再启动 AI 交易"

    def start(self) -> tuple[bool, str]:
        c = self.config
        if not c.enabled:
            return False, "AI 交易未启用"
        blocked = self._assistant_blocked()
        if blocked:
            return False, blocked
        # The testnet has fake money: rehearsing must not cost an extra click.
        if c.network == "mainnet" and not c.confirm_live:
            return False, "缺少二次确认"
        if c.market_type == MarketType.SPOT.value and c.require_stop:
            # 现货没有交易所侧止损，只有合约能挂 reduce-only 保护单。
            return False, "现货无法挂交易所侧止损，请使用合约或关闭止损要求"
        if self._thread is not None and self._thread.is_alive():
            # 上一轮还在退出（卡在模型调用）。此时清 _stop_event 会让它复活，
            # 变成两个 poller 同时决策。
            return False, "上一轮决策线程仍在退出，请稍后再试"
        with self._lock:
            if self._state == STATE_HALTED:
                return False, f"已熔断：{self._halted_reason}"
            if self._state == STATE_RUNNING:
                return True, "已在运行"
            if not self.lease.acquire(self._owner_name):
                return False, f"交易权已被 {self.lease.owner or '其他'} 占用"
            self._state = STATE_RUNNING
            self._paused = False
            self._stop_event.clear()
        self._audit(
            {"kind": "lifecycle", "event": "start", "network": c.network}
        )
        try:
            thread = threading.Thread(target=self._loop, name="ai-trader", daemon=True)
            thread.start()
        except BaseException:
            # 线程没起来就是没跑：state 和租约都要回滚，否则界面显示在跑、
            # 实际没有 poller，交易权还被一个死引擎占着。
            self._thread = None
            with self._lock:
                self._state = STATE_STOPPED
            self.lease.release(self._owner_name)
            self._audit({"kind": "lifecycle", "event": "start_failed"})
            raise
        self._thread = thread
        return True, "AI 交易已启动"

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3.0)
            if thread.is_alive():
                # 一轮决策可能卡在模型调用上；保留引用，避免紧接着 start()
                # 开出第二个 poller 和新旧两轮决策同时下单。
                log.warning("AI 交易线程未在超时内退出，保留引用")
                self._thread = thread
                self.lease.release(self._owner_name)
                with self._lock:
                    if self._state != STATE_HALTED:
                        self._state = STATE_STOPPED
                self._audit({"kind": "lifecycle", "event": "stop", "slow": True})
                return
        self._thread = None
        with self._lock:
            if self._state != STATE_HALTED:
                self._state = STATE_STOPPED
        self.lease.release(self._owner_name)
        self._audit({"kind": "lifecycle", "event": "stop"})

    def pause(self, paused: bool = True) -> None:
        with self._lock:
            self._paused = bool(paused)
            if self._state == STATE_RUNNING:
                self._state = STATE_PAUSED if paused else STATE_RUNNING
        self._audit({"kind": "lifecycle", "event": "pause" if paused else "resume"})

    def halt(self, reason: str = "人工干预") -> None:
        with self._lock:
            self._halted_reason = reason or "人工干预"
            self._state = STATE_HALTED
            self._paused = False
        # 熔断的引擎不会再交易（step() 看 state、_submit() 看 _stop_event），
        # 所以交易权当场还回去。留着它，auto / quant 甚至下一次启动都会报
        # 「交易权已被占用」，一次熔断就把整个账户锁死。
        # 拦住「熔断期间新开仓」的是 STATE_HALTED 与 AIGuard 的 halted_reason，
        # 不是租约，别把这两件事混成一件。
        self.lease.release(self._owner_name)
        self._audit({"kind": "lifecycle", "event": "halt", "reason": self._halted_reason})

    def kill(self, reason: str = "急停") -> None:
        """急停：熔断 + 停止 poller + 释放交易权（INV-8）。"""
        self.halt(reason)
        self._stop_event.set()
        self.lease.release(self._owner_name)

    # ------------------------------------------------------------------ loop
    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.step()
            except Exception:  # pragma: no cover - poller must never die silently
                log.warning("AI 自主交易轮询失败", exc_info=True)
            # 睡多久算错也不许把 poller 弄死：线程静默退出时界面还显示在跑，
            # 那是最坏的一种状态。算不出来就按 poll_sec 重试。
            try:
                wait = max(1.0, self.next_wait_sec())
            except Exception:
                log.warning("计算下次唤醒时间失败，按 poll_sec 重试", exc_info=True)
                wait = self._poll_seconds()
            self._stop_event.wait(wait)

    def _bar_seconds(self) -> float:
        """这根 K 线有多长（秒）。取不到就是 0，调用方退回旧的定时轮询。"""
        try:
            return float(timeframe_seconds(self.config.timeframe))
        except (KeyError, TypeError, ValueError):
            return 0.0

    def _poll_seconds(self) -> float:
        return max(1.0, float(self.config.poll_sec or 60.0))

    def _cadence_mode(self) -> str:
        """当前节奏：bar（跟主周期）或一个固定周期。认不得的按 bar。

        配置在路由那层已经校验过，这里再兜一次是因为测试与别的调用方会直接
        构造 AITraderConfig：一个写错的 cadence 不该让引擎按 1 秒节拍烧模型。
        """
        mode = (self.config.cadence or "bar").strip()
        if mode == "bar" or mode in TIMEFRAME_SECONDS:
            return mode
        return "bar"

    def _cadence_seconds(self) -> float:
        """一步节拍多长（秒）。0 表示算不出来，调用方退回 poll_sec。"""
        mode = self._cadence_mode()
        if mode == "bar":
            return self._bar_seconds()
        try:
            return float(timeframe_seconds(mode))
        except (KeyError, TypeError, ValueError):
            return self._bar_seconds()

    def _bar_wait(self, clock: float, bar: float, poll: float) -> float:
        """跟主周期：睡到下一根收盘后几秒（见上面的 BAR_SETTLE_* 注释）。"""
        with self._lock:
            pending = self._pending_close
        if pending <= 0:
            # 还没有「已处理的那一根」当锚：短间隔重试，别一上来就睡一整根。
            return min(poll, BAR_WINDOW_AFTER)
        if clock < pending + BAR_SETTLE_SECONDS:
            return max(1.0, pending + BAR_SETTLE_SECONDS - clock)
        if clock <= pending + BAR_WINDOW_AFTER:
            return max(1.0, min(poll, pending + BAR_WINDOW_AFTER - clock))
        next_close = (math.floor(clock / bar) + 1) * bar
        return max(1.0, next_close + BAR_SETTLE_SECONDS - clock)

    def next_wait_sec(self, now: float | None = None) -> float:
        """这一轮之后等多久再看。

        两种节奏：
          * bar（默认）——跟主周期，逻辑与改前逐位相同：还没到收盘点就一觉睡到
            收盘后 BAR_SETTLE_SECONDS；收盘点前后按 poll_sec 小步重试，最远
            BAR_WINDOW_AFTER；窗口过了顺延到墙上的下一根。
          * 固定节拍——睡到下一片开始后 BAR_SETTLE_SECONDS。错过就立刻跑，
            不再有「窗口重试」：节拍是墙上的，下一片自然会把这一片覆盖掉。

        周期不认识时退回旧行为：每 poll_sec 一轮。
        """
        clock = self.clock() if now is None else now
        poll = self._poll_seconds()
        if self._cadence_mode() == "bar":
            bar = self._bar_seconds()
            if bar <= 0:
                return poll
            return self._bar_wait(clock, bar, poll)
        step = self._cadence_seconds()
        if step <= 0:
            return poll
        with self._lock:
            pending = self._pending_slot
        if pending <= 0:
            return min(poll, step)
        target = pending + step + BAR_SETTLE_SECONDS
        if clock < target:
            return max(1.0, target - clock)
        return 1.0

    def next_run_at(self, now: float | None = None) -> float | None:
        """下一次决策的预计时刻（epoch 秒）。界面显示用，算不出来就是 None。"""
        clock = self.clock() if now is None else now
        with self._lock:
            state = self._state
            paused = self._paused
            pending_close = self._pending_close
            pending_slot = self._pending_slot
        if state != STATE_RUNNING or paused:
            return None
        if self._cadence_mode() == "bar":
            bar = self._bar_seconds()
            if bar <= 0:
                return None
            if pending_close <= 0:
                return clock
            if pending_close + BAR_WINDOW_AFTER < clock:
                # 这一根没等到（或已经处理完一段时间了）：算墙上的下一根。
                return (math.floor(clock / bar) + 1) * bar + BAR_SETTLE_SECONDS
            return pending_close + BAR_SETTLE_SECONDS
        step = self._cadence_seconds()
        if step <= 0:
            return None
        if pending_slot <= 0:
            return clock
        return pending_slot + step + BAR_SETTLE_SECONDS

    # ------------------------------------------------------------------ step
    def step(self) -> dict[str, Any] | None:
        """poller 的一轮：只有运行中且未暂停才真正决策。"""
        with self._lock:
            if self._state != STATE_RUNNING or self._paused:
                return None
        return self.decide_once()

    @staticmethod
    def _frame_bar_ts(frame: Any) -> int:
        """已收盘 K 线里最后一根的开放时间（毫秒）；读不到就是 0。

        这就是「这根 K 线处理过没有」的键。用行情自己的时间戳而不是墙上时钟：
        poller 停了再起、机器休眠、本机时钟偏移都不会让同一根决策两遍，也不会整根
        跳过。进程重启会退回 0（这个值没落盘），重启后那根要再决策一次——受冷却与
        每小时订单上限约束，但别在注释或文档里写成「重启也不会重复」。
        """
        try:
            return int(frame["timestamp"].iloc[-1])
        except (KeyError, TypeError, ValueError, IndexError, AttributeError):
            return 0

    def decide_once(self, *, force: bool = False) -> dict[str, Any] | None:
        """同步执行一次「取行情 → 问模型 → 校验 → 闸门 → 下单」。

        与状态机解耦，供测试、``/preview`` 和人工「立即决策」复用。

        同一根 K 线只决策一次：节奏由 K 线周期决定，轮询只是「什么时候醒」。
        这一根跑过就返回 ``None``，连审计都不写——否则时间线上每根 K 线会多一条
        什么都没发生的记录，把真正的那几条挤下去。``force=True`` 给「立即决策」
        用：人点了就该真的跑一次。
        """
        now = self.clock()
        self.budget.roll(now)
        portfolio = self._portfolio()
        market_price = portfolio.get("price")
        frame = None
        frames: dict[str, Any] = {}
        if self.frames_provider is not None:
            # 多周期：主周期必须拿到，参考周期缺了就当没有。
            try:
                frames = self.frames_provider() or {}
            except Exception as exc:
                return self._record(
                    "skip", reason=f"行情不可达：{exc}", decision=None, now=now
                )
            frame = frames.get(self.config.timeframe)
            if frame is None:
                frame = next(iter(frames.values()), None)
        elif self.frame_provider is not None:
            try:
                frame = self.frame_provider()
            except Exception as exc:
                record = self._record(
                    "skip", reason=f"行情不可达：{exc}", decision=None, now=now
                )
                return record
        if frame is None or getattr(frame, "empty", True):
            return self._record("skip", reason="行情数据不可用", decision=None, now=now)

        bar_ts = self._frame_bar_ts(frame)
        mode = self._cadence_mode()
        step = self._cadence_seconds()
        slot = int(math.floor(now / step)) if step > 0 else 0
        if not force:
            with self._lock:
                seen_bar = self._last_bar_ts
                seen_slot = self._last_slot
            if mode == "bar":
                # 只认「更新的那一根」：相等是同一根 K 线（收盘窗口里在重试），变小说明
                # 这次读到的行情比上次还旧——币安不可达时会回落到 Gate 永续镜像，两边的
                # 最后一根收盘 K 线不总是同一根。用相等判断时这种回退会被当成新 K 线，
                # 同一根再调一次模型，还可能在同一个收盘点上下第二单。
                if bar_ts > 0 and seen_bar and bar_ts <= seen_bar:
                    return None
            elif slot > 0 and seen_slot and slot <= seen_slot:
                # 固定节拍：同一片只跑一次。和 K 线时间戳无关——4h 结构配 15m
                # 节拍时，同一根 4h/1h K 线之内本来就该按节拍决策多次。
                return None

        # 策略是参考，不是命令（「策略为辅，AI 为主」）。它只剩两处作用：喂给
        # 流水线当一条输入，以及「不许反着做」。空仓信号不再让这一轮什么都不做
        # ——以前空仓直接 return，AI 一次模型调用都拿不到，等于策略才是决策者。
        signal_target: float | None = None
        if self.signal_provider is not None:
            try:
                signal_target = float(self.signal_provider())
            except Exception as exc:
                return self._record(
                    "skip", reason=f"策略信号不可用：{exc}", decision=None, now=now
                )
        self._last_signal = signal_target

        # 决策次数是模型调用预算：先判上限，避免为一个注定被拒的请求付钱。
        if (
            self.config.max_decisions_per_day
            and self.budget.decisions_today >= self.config.max_decisions_per_day
        ):
            return self._record(
                "skip", reason="已达 AI 当日决策次数上限", decision=None, now=now
            )

        # 花模型调用之前先把「这一根 / 这一片处理过了」记下来：模型调用失败、决策
        # 被闸门拒掉都算跑过，下一轮要等下一根收盘或下一片。不然一次超时就会在同一
        # 个点上反复问，一小时里问坏几次、扣几次额度。
        if bar_ts > 0:
            with self._lock:
                self._last_bar_ts = bar_ts
        if step > 0:
            if mode == "bar":
                bar = step
                if bar_ts > 0:
                    # bar_ts 是刚收盘那根的开放时间：它收盘在 +1 个周期，下一根在
                    # +2 个。时间戳离谱时（单位写成微秒、交易所时钟错、镜像源给了
                    # 未来的 bar）不能照着睡过去：锚最多推到「现在 + 2 根 + 窗口」，
                    # 再远就按墙上的下一根。否则引擎会静默睡到那一天，界面上显示
                    # 「下次 2262 年」，而状态一直是 running。
                    pending = bar_ts / 1000.0 + 2 * bar
                    limit = now + 2 * bar + BAR_WINDOW_AFTER
                    with self._lock:
                        self._pending_close = (
                            pending
                            if pending <= limit
                            else (math.floor(now / bar) + 1) * bar
                        )
            else:
                # 固定节拍：锚就是这一片的起点。同一片重试（模型失败）不会推锚，
                # 所以下一轮还会落在这一片里再试一次；这是刻意的——节拍是墙上的，
                # 一片之内重试比等下一片更接近「每 15 分钟决策一次」的本意。
                with self._lock:
                    self._last_slot = max(self._last_slot, slot)
                    self._pending_slot = float(slot * step)
        pipeline = self._decide(frame, portfolio, signal_target, frames)
        if not pipeline.ok:
            return self._record(
                "skip",
                reason=f"模型调用失败：{pipeline.error or '未知错误'}",
                decision=None,
                now=now,
                trace=pipeline.trace,
            )
        self.budget.note_decision()
        # 时间戳打在「模型返回、解析出决策」这一刻：有效期从决策出生算起，
        # 不是从这一轮开始时算起。
        decision, error = parse_decision(pipeline.decision_text, now=self.clock())
        if decision is None:
            return self._record(
                "skip", reason=error, decision=None, now=now, trace=pipeline.trace
            )
        errors = validate_decision(decision, self.config, reference_price=market_price)
        if errors:
            return self._record(
                "reject", reason="；".join(errors), decision=decision, now=now,
                trace=pipeline.trace,
            )
        # 策略只是参考，唯一的硬约束是「不反着做」：用户选了这条策略，模型却在
        # 相反方向开仓，事后没人解释得清。策略空仓时不拦——那正是「AI 为主」的地方，
        # 方向由模型自己判断。
        if signal_target is not None:
            if signal_target > 0 and decision.action == "open_short":
                return self._record(
                    "reject",
                    reason="策略信号为多，AI 不做反向开空（策略信号只作参考，方向相反的一单不下）",
                    decision=decision,
                    now=now,
                    trace=pipeline.trace,
                )
            if signal_target < 0 and decision.action == "open_long":
                return self._record(
                    "reject",
                    reason="策略信号为空，AI 不做反向开多（策略信号只作参考，方向相反的一单不下）",
                    decision=decision,
                    now=now,
                    trace=pipeline.trace,
                )

        # 模型调用那几十秒里世界可能已经变了：提交前重读行情、持仓、策略信号复核。
        recheck, portfolio, market_price = self._recheck_before_submit(
            decision, portfolio, signal_target
        )
        if not recheck.ok:
            return self._record(
                "reject", reason=recheck.reason, decision=decision, now=now,
                trace=pipeline.trace,
            )

        guard = self.guard.check(
            decision,
            # 闸门看的「现在」也用此刻：有效期判的是「决策出生 → 提交」之间的间隔。
            now=self.clock(),
            position_quote=float(portfolio.get("position_quote") or 0.0),
            equity=float(portfolio.get("equity") or 0.0),
            decisions_today=max(0, self.budget.decisions_today - 1),
            orders_last_hour=self.budget.orders_last_hour(now),
            last_order_at=self.budget.last_order_at,
            market_price=market_price,
            kill_switch=bool(portfolio.get("kill_switch")),
            halted_reason=self._halted_reason or str(portfolio.get("halted_reason") or ""),
        )
        if not guard.ok:
            return self._record(
                "reject", reason=guard.reason, decision=decision, now=now, trace=pipeline.trace
            )

        if decision.action == "hold":
            return self._record(
                "skip", reason="观望", decision=decision, now=now, trace=pipeline.trace
            )

        try:
            order = self._submit(decision, market_price)
        except Exception as exc:
            return self._record(
                "error", reason=f"下单失败：{exc}", decision=decision, now=now,
                trace=pipeline.trace,
            )
        self.budget.note_order(now)
        return self._record(
            "order", reason="", decision=decision, now=now, order=order, trace=pipeline.trace
        )

    # -------------------------------------------------------------- recheck
    @staticmethod
    def _position_base(view: dict[str, Any]) -> float | None:
        """把持仓折成基础币数量。

        持仓金额是「数量 × 价格」，价格一动它就动；直接比金额会把「价格走了 0.2%」
        认成「有人成交了」，那样每一轮都会把自己拒掉。
        """
        try:
            price = float(view.get("price"))
            quote = float(view.get("position_quote") or 0.0)
        except (TypeError, ValueError):
            return None
        if price <= 0:
            return None
        return quote / price

    def _recheck_before_submit(
        self,
        decision: AIDecision,
        portfolio: dict[str, Any],
        signal_target: float | None,
    ) -> tuple[GuardVerdict, dict[str, Any], float | None]:
        """提交前把行情、持仓、策略信号重读一遍（B4）。

        模型调用可能几十秒（deep 六段），这一轮开始时取的方向、持仓、价格到这一刻
        都可能过期。原则只有一条：不确定就不下单，宁可少下一单。

        只拦新开仓。平仓是去风险，拦平仓等于把人锁在仓位里——和 RiskManager 里
        「entry 才判熔断、减仓不拦」是同一条理由。

        返回 (裁决, 最新组合视图, 最新价)；裁决不过时调用方不下单，也不再拿旧值。
        """
        fresh: dict[str, Any] = dict(portfolio)
        fresh_error = ""
        if self.portfolio_provider is not None:
            try:
                data = self.portfolio_provider()
            except Exception as exc:
                fresh_error = f"读不到持仓（{exc}）"
            else:
                if isinstance(data, dict) and data:
                    fresh = data
                else:
                    fresh_error = "读取持仓返回空快照"
        price = fresh.get("price")
        if price is None:
            price = portfolio.get("price")

        if not decision.is_entry:
            # 平仓/观望：数字用最新的，但读不到也放行。拦减仓比漏减仓更糟——和
            # RiskManager 只在 entry 上判熔断是同一条理由。
            return GuardVerdict(True), fresh, price

        if fresh_error:
            return GuardVerdict(False, f"提交前复核：{fresh_error}"), fresh, price

        # 方向：拿最新的策略信号再对一次。读不到、或者和这一单相反，就不下单。
        # 空仓不拦：策略只是参考，空信号本来就允许 AI 自己开仓（见 decide_once）。
        if self.signal_provider is not None:
            try:
                latest_signal = float(self.signal_provider())
            except Exception as exc:
                return (
                    GuardVerdict(False, f"提交前复核：策略信号读不到（{exc}）"),
                    fresh,
                    price,
                )
            wanted = 1.0 if decision.action == "open_long" else -1.0
            if latest_signal * wanted < 0:
                before = 0.0 if signal_target is None else signal_target
                return (
                    GuardVerdict(
                        False,
                        f"提交前复核：策略信号已从 {before:+.0%} 变成 {latest_signal:+.0%}，"
                        f"与 {decision.action} 相反",
                    ),
                    fresh,
                    price,
                )

        # 持仓：数量变了（模型调用期间成交了）就不按旧快照下单，也不自己改数。
        was = self._position_base(portfolio)
        now_qty = self._position_base(fresh)
        if was is None or now_qty is None:
            return (
                GuardVerdict(False, "提交前复核：持仓或价格缺失，算不出当前仓位"),
                fresh,
                price,
            )
        if abs(now_qty - was) > max(1e-9, abs(was) * 1e-6):
            before_quote = float(portfolio.get("position_quote") or 0.0)
            now_quote = float(fresh.get("position_quote") or 0.0)
            return (
                GuardVerdict(
                    False,
                    "提交前复核：持仓在模型调用期间变了"
                    f"（{before_quote:.2f} → {now_quote:.2f} USDT）",
                ),
                fresh,
                price,
            )
        return GuardVerdict(True), fresh, price

    def _submit(self, decision: AIDecision, market_price: float | None) -> OrderResult:
        c = self.config
        price = decision.price or market_price or 0.0
        if price <= 0:
            raise ValueError("没有可用的参考价")
        quote = decision.quote
        amount = quote / price
        # 平空是买入、平多是卖出：方向只由「开/平 + 多/空」决定，漏掉平空就会
        # 用卖单去平空仓——reduce_only 卖单在空头上会被交易所拒单，或者把空头做大。
        side = (
            OrderSide.BUY
            if decision.action in ("open_long", "close_short")
            else OrderSide.SELL
        )
        reduce_only = decision.is_close
        req = OrderRequest(
            symbol=c.symbol,
            side=side.value,
            order_type=OrderType.MARKET.value,
            amount=amount,
            price=price,
            quote_amount=quote,
            reduce_only=reduce_only,
            leverage=c.leverage,
            market_type=MarketType(c.market_type),
            stop_loss=decision.stop_loss,
            take_profit=decision.take_profit,
        )
        # 停止或急停可能发生在模型调用的那几秒里，这时这一单不能再发出去。
        # 按了停止之后还冒出一笔成交，是最不能接受的一类 bug。
        if self._stop_event.is_set():
            raise RuntimeError("已停止，取消本次下单")
        # 唯一下单出口（INV-1）：任何 broker/交易所调用都不得出现在本模块。
        return self.trading.submit(req)

    # ------------------------------------------------------------- reporting
    def _portfolio(self) -> dict[str, Any]:
        if self.portfolio_provider is None:
            return {}
        try:
            data = self.portfolio_provider() or {}
            return data if isinstance(data, dict) else {}
        except Exception:  # pragma: no cover - a broken view must not place orders
            log.warning("读取 AI 组合视图失败", exc_info=True)
            return {}

    def _decide(
        self,
        frame: Any,
        portfolio: dict[str, Any],
        signal_target: float | None,
        frames: dict[str, Any] | None = None,
    ) -> ai_pipeline.PipelineResult:
        """跑一遍流水线。深度、账户和限额都在这里翻译成流水线的输入。"""
        ctx = ai_pipeline.PipelineContext(
            symbol=self.config.symbol,
            timeframe=self.config.timeframe,
            digest=self._digest(frame, frames),
            equity=float(portfolio.get("equity") or 0.0),
            position_quote=float(portfolio.get("position_quote") or 0.0),
            price=portfolio.get("price"),
            signal_target=signal_target,
            max_order_quote=self.config.max_order_quote,
            max_position_quote=self.config.max_position_quote,
            min_confidence=self.config.min_confidence,
            news=self._news(),
            reference_timeframes=tuple(self.config.timeframes),
            policy=self.config.policy_text,
        )
        return ai_pipeline.run(self.llm, ctx, depth=self.config.depth)

    def _digest(self, frame: Any, frames: dict[str, Any] | None = None) -> str:
        try:
            if frames:
                return ai.digest_set(
                    frames,
                    primary=self.config.timeframe,
                    label=self.config.symbol,
                    reference_timeframes=self.config.timeframes,
                )
            return ai.digest(frame, timeframe=self.config.timeframe, label=self.config.symbol)
        except Exception:  # pragma: no cover - digest is best effort
            return ""

    def _news(self) -> str:
        """新闻标题由外部注入；没有就返回空串，流水线会跳过那一段。"""
        if self.news_provider is None:
            return ""
        try:
            return str(self.news_provider() or "")
        except Exception:  # pragma: no cover - 新闻拉不到不影响决策
            return ""

    def _record(
        self,
        outcome: str,
        *,
        reason: str,
        decision: AIDecision | None,
        now: float,
        order: OrderResult | None = None,
        trace: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        order_row = None
        if order is not None:
            try:
                order_row = order.order
            except Exception:  # pragma: no cover
                order_row = None
        record = {
            "at": now,
            "kind": "decision",
            "outcome": outcome,
            "reason": reason,
            "decision_id": decision.decision_id if decision else "",
            "action": decision.action if decision else "",
            "symbol": decision.symbol if decision else self.config.symbol,
            "quote": decision.quote if decision else 0.0,
            "price": decision.price if decision else None,
            "confidence": decision.confidence if decision else 0.0,
            "signal_target": self._last_signal,
            "stop_loss": decision.stop_loss if decision else None,
            "rationale": decision.rationale if decision else "",
            "order_id": (order_row or {}).get("id") if isinstance(order_row, dict) else None,
            "network": self.config.network,
            "state": self.state,
            "depth": self.config.depth,
            "cadence": self._cadence_mode(),
            # 固定节拍下的时间片编号（bar 模式恒 0）。复盘与界面上要能分清
            # 「同一根 K 线上按节拍决策了四次」和「换了四根 K 线」。
            "slot": int(self._last_slot),
            "timeframes": list(self.config.timeframes),
            # 按哪一版准则做的决策。准则换过之后回头看，这一条仍是当时那版。
            "policy_version": int(self.config.policy_version),
            # 这一条决策属于哪根 K 线（毫秒开放时间）：界面上要能说清「这是 1h
            # 那根收盘后做的」，同一根只该有一条。
            "bar_ts": int(self._last_bar_ts),
            "trace": list(trace or []),
        }
        # NaN/inf 不是合法 JSON：写进审计文件会让那一行解析不了，塞进 status 的
        # 事件流还会让前端 JSON.parse 整块失败（一条坏记录毁掉整张表）。
        # 模型回 NaN 时决策本身已经被 validate_decision 拒了，这里只保证记录干净。
        record = {
            key: (None if isinstance(value, float) and not math.isfinite(value) else value)
            for key, value in record.items()
        }
        # seq 在落盘那一下才发出来（跨实例单调），事件流里拿的是写出去的那一行。
        written = self._audit(record)
        self.last_decision = written
        with self._lock:
            self._events.append(written)
        return written

    def _audit(self, record: dict[str, Any]) -> dict[str, Any]:
        """每笔决策或状态变更追加一行，返回补好 seq 的那行。

        写失败绝不能打断交易（INV-15）：seq 已经发出去，缺的那行就是缺了，
        但交易照走。
        """
        written = self._audit_log.append(record)
        with self._lock:
            # 和 status 的读取同一把锁：seq 是事件流的游标，读者拿到它就必须能
            # 拿到 seq 之前的那些行。
            self._seq = max(self._seq, int(written.get("seq") or 0))
        return written

    def status(self, since: int = 0) -> dict[str, Any]:
        now = self.clock()
        self.budget.roll(now)
        # 这些字段由决策线程改（pause/resume/halt/_emit），而 status 被接口线程每几秒
        # 调一次。以前裸读：读到「state 已 halted、halted_reason 还是空」这种半截快照，
        # 界面就只显示一句「已熔断」不说原因；_events 在同一次 list() 里也可能漏掉
        # 刚追加的那几行。
        with self._lock:
            state = self.state
            paused = self._paused
            halted_reason = self._halted_reason
            events = [item for item in list(self._events) if int(item.get("seq") or 0) > since]
            seq = self._seq
        return {
            "state": state,
            "paused": paused,
            "halted_reason": halted_reason,
            "config": {
                "network": self.config.network,
                "symbol": self.config.symbol,
                "strategy": self.config.strategy_key,
                "market_type": self.config.market_type,
                "timeframe": self.config.timeframe,
                "depth": self.config.depth,
                # 节奏与参考周期也是「这一轮到底怎么跑的」的一部分：界面要能
                # 回显，排查时才不用去翻审计。
                "cadence": self._cadence_mode(),
                "cadence_seconds": self._cadence_seconds(),
                "timeframes": list(self.config.timeframes),
                "policy_version": int(self.config.policy_version),
                "max_order_quote": self.config.max_order_quote,
                "max_position_quote": self.config.max_position_quote,
                # 杠杆以前根本不出现在 status 里：界面既没控件也没回显，
                # 用户无法确认这一轮到底按几倍在跑。
                "leverage": self.config.leverage,
                "leverage_note": self.config.leverage_note,
            },
            "counters": {
                "decisions_today": self.budget.decisions_today,
                "orders_last_hour": self.budget.orders_last_hour(now),
                "max_decisions_per_day": self.config.max_decisions_per_day,
                "max_orders_per_hour": self.config.max_orders_per_hour,
            },
            # 节奏：mode=bar 时跟着主周期，否则按固定节拍。界面据此显示
            # 「每 15 分钟一次 · 下次 ≈ 10:00:03」。seconds 为 0 表示周期不认识，
            # 退回旧的定时轮询。
            "cadence": {
                "mode": self._cadence_mode(),
                "seconds": self._cadence_seconds(),
                "bar_seconds": self._bar_seconds(),
                "next_run_at": self.next_run_at(now),
                "last_bar_ts": int(self._last_bar_ts),
                "last_slot": int(self._last_slot),
            },
            "last_decision": self.last_decision,
            "events": events,
            "seq": seq,
        }
