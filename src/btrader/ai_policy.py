"""AI 交易的决策准则：一段人确认过的文本 + 参数补丁 + 版本历史。

为什么单独存一份、而不是直接塞进 config.json：准则改一次要能回滚、要能看是谁在
什么时候改的、改之前长什么样。config.json 记的是「当前状态」，这里记的是「变更史」。

补丁只落在 ai_auto 这一段的字段上。那里的每个字段本来就是 AITraderConfig 的入参，
校验走 PUT /api/settings 同一条路（routes._apply_ai_auto），所以「AI 提议」和
「人手动改」不会走出两套口径。

不在白名单里的三样东西，是这道闸本身，不给文本、也不给模型：
- 主网启动时的 confirm_live：每次启动人当场说一句，存下来就等于把闸掰开；
- 急停 / 熔断：写进准则里等于允许用一段文本关掉它；
- risk 段（风控上限、杠杆上限）：那是所有下单路径共用的硬顶，不是 AI 的策略参数。
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
import uuid
from pathlib import Path
from typing import Any

from . import paths

log = logging.getLogger(__name__)

POLICY_FILENAME = "ai-policy.json"
POLICY_TEXT_LIMIT = 4000
POLICY_NOTE_LIMIT = 500
POLICY_HISTORY_LIMIT = 50

# 补丁能碰的字段，全部在 ai_auto 这一段里。enabled 不在里面：它不是一个闸，
# 改它只是挪一下「下次打开时开关的状态」，写进准则里反而看不出来。
PATCH_FIELDS: tuple[str, ...] = (
    "max_order_quote",
    "max_position_quote",
    "max_decisions_per_day",
    "max_orders_per_hour",
    "min_confidence",
    "decision_ttl_sec",
    "max_price_drift_pct",
    "cooldown_sec",
    "poll_sec",
    "leverage",
    "require_stop",
    "depth",
    "cadence",
    "timeframes",
    "strategy_key",
    "market_type",
    "symbol",
    "timeframe",
)

BOOL_FIELDS = ("require_stop",)
INT_FIELDS = ("max_decisions_per_day", "max_orders_per_hour")
TEXT_FIELDS = ("depth", "cadence", "strategy_key", "market_type", "symbol", "timeframe")
LIST_FIELDS = ("timeframes",)

PATCH_LABELS: dict[str, str] = {
    "max_order_quote": "单笔上限（USDT）",
    "max_position_quote": "持仓上限（USDT）",
    "max_decisions_per_day": "每日决策次数",
    "max_orders_per_hour": "每小时订单数",
    "min_confidence": "最小置信度",
    "decision_ttl_sec": "决策有效期（秒）",
    "max_price_drift_pct": "价格漂移闸",
    "cooldown_sec": "冷却时间（秒）",
    "poll_sec": "轮询间隔（秒）",
    "leverage": "杠杆",
    "require_stop": "必须带止损",
    "depth": "决策深度",
    "cadence": "决策节奏",
    "timeframes": "参考周期",
    "strategy_key": "策略",
    "market_type": "市场",
    "symbol": "交易对",
    "timeframe": "主周期",
}


class PolicyError(ValueError):
    """补丁不合法。路由把它原样翻成 400 的 detail。"""


def policy_path() -> Path:
    return paths.data_dir() / POLICY_FILENAME


def _lock(path: Path):
    """跨进程互斥，复用审计那套文件锁（Windows 字节锁 / Linux flock）。"""
    from .engine.ai_trader import _audit_file_lock

    return _audit_file_lock(path.with_name(path.name + ".lock"))


def blank_state() -> dict[str, Any]:
    return {
        "version": 0,
        "text": "",
        "updated_at": 0.0,
        "author": "",
        "overrides": {},
        "history": [],
    }


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def load(path: Path | None = None) -> dict[str, Any]:
    """读当前准则与历史。文件不在、读不了、解析不了，都退回空白并记一条日志。

    这里的容错和 config.json 同一条理由：一份坏文件不该让整个 AI 交易页打不开。
    历史坏了不影响当前准则，当前准则坏了就只剩一段空文本（提示词里少一段而已）。
    """
    target = Path(path) if path is not None else policy_path()
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return blank_state()
    except OSError as exc:
        log.warning("读不到决策准则文件 %s：%s", target, exc)
        return blank_state()
    try:
        data = json.loads(raw)
    except ValueError:
        log.warning("决策准则文件解析不了（%s），按空白处理", target)
        return blank_state()
    if not isinstance(data, dict):
        return blank_state()

    state = blank_state()
    try:
        state["version"] = max(0, int(data.get("version") or 0))
    except (TypeError, ValueError):
        state["version"] = 0
    state["text"] = str(data.get("text") or "")[:POLICY_TEXT_LIMIT]
    state["updated_at"] = _as_float(data.get("updated_at"))
    state["author"] = str(data.get("author") or "")[:80]
    overrides = data.get("overrides")
    if isinstance(overrides, dict):
        state["overrides"] = {str(k): v for k, v in overrides.items() if str(k) in PATCH_FIELDS}
    history = data.get("history")
    if isinstance(history, list):
        state["history"] = [item for item in history if isinstance(item, dict)][
            :POLICY_HISTORY_LIMIT
        ]
    return state


def save(state: dict[str, Any], path: Path | None = None) -> None:
    """原子写：临时文件名带 pid + 随机后缀，写完 os.replace 换上去。"""
    target = Path(path) if path is not None else policy_path()
    payload = {
        "version": int(state.get("version") or 0),
        "text": str(state.get("text") or ""),
        "updated_at": _as_float(state.get("updated_at")),
        "author": str(state.get("author") or ""),
        "overrides": dict(state.get("overrides") or {}),
        "history": list(state.get("history") or [])[:POLICY_HISTORY_LIMIT],
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with _lock(target):
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            os.replace(tmp, target)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:  # pragma: no cover - 被别的进程抢先删了
            pass


def _label(key: str) -> str:
    return PATCH_LABELS.get(key, key)


def _clean_number(value: Any, key: str) -> float:
    if isinstance(value, bool):
        raise PolicyError(f"「{_label(key)}」要数值，不要 true/false")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise PolicyError(f"「{_label(key)}」要数值，收到 {value!r}") from None
    if not math.isfinite(number):
        raise PolicyError(f"「{_label(key)}」不能是 NaN / Infinity")
    if number < 0:
        raise PolicyError(f"「{_label(key)}」不能是负数")
    return number


def _clean_one(key: str, value: Any) -> Any:
    if key in LIST_FIELDS:
        if not isinstance(value, (list, tuple)):
            raise PolicyError(f"「{_label(key)}」要数组，收到 {value!r}")
        return [str(item or "").strip() for item in value if str(item or "").strip()]
    if key in BOOL_FIELDS:
        if not isinstance(value, bool):
            raise PolicyError(f"「{_label(key)}」只能是 true / false")
        return value
    if key in TEXT_FIELDS:
        text = str(value or "").strip()
        if not text:
            raise PolicyError(f"「{_label(key)}」不能是空字符串")
        return text
    number = _clean_number(value, key)
    if key in INT_FIELDS:
        if abs(number - round(number)) > 1e-9:
            raise PolicyError(f"「{_label(key)}」要整数，收到 {number}")
        return int(round(number))
    return number


def clean_overrides(values: Any) -> dict[str, Any]:
    """严格清洗：任何一个不合法的键都抛 PolicyError。

    严格是有意的。静默丢掉一个键，用户会以为「AI 说它改了」，实际那一项没动。
    """
    if values is None:
        return {}
    if not isinstance(values, dict):
        raise PolicyError("补丁必须是一个对象：{字段: 新值}")
    cleaned: dict[str, Any] = {}
    for raw_key, value in values.items():
        key = str(raw_key).strip()
        if key not in PATCH_FIELDS:
            raise PolicyError(
                f"「{key}」不在可改范围里。可改：{'、'.join(PATCH_FIELDS)}"
            )
        cleaned[key] = _clean_one(key, value)
    return cleaned


def coerce_overrides(values: Any) -> tuple[dict[str, Any], list[str]]:
    """宽松清洗：能用的留下，用不了的写一句原因。给复盘用。

    模型给的补丁常常一半合法一半离谱，整块丢掉等于白跑一次复盘；apply 那一步
    仍会走严格版本，被留下的这些还要再过一遍设置校验。
    """
    cleaned: dict[str, Any] = {}
    rejected: list[str] = []
    if values is None:
        return cleaned, rejected
    if not isinstance(values, dict):
        return cleaned, ["补丁不是一个对象"]
    for raw_key, value in values.items():
        key = str(raw_key).strip()
        if key not in PATCH_FIELDS:
            rejected.append(f"{key}：不在可改范围里")
            continue
        try:
            cleaned[key] = _clean_one(key, value)
        except PolicyError as exc:
            rejected.append(str(exc))
    return cleaned, rejected


def apply_version(
    state: dict[str, Any],
    *,
    text: str,
    overrides: dict[str, Any],
    note: str,
    author: str,
    before: dict[str, Any],
    after: dict[str, Any],
    findings: list[dict[str, Any]] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """生成下一版（纯函数，不落盘）。回滚也走这里：历史是一条线，不回写。"""
    edited = (text or "").strip()
    if len(edited) > POLICY_TEXT_LIMIT:
        raise PolicyError(
            f"准则最多 {POLICY_TEXT_LIMIT} 字，现在 {len(edited)} 字"
        )
    version = int(state.get("version") or 0) + 1
    stamp = float(now if now is not None else time.time())
    entry = {
        "version": version,
        "at": stamp,
        "author": (author or "").strip()[:80] or "人工",
        "note": (note or "").strip()[:POLICY_NOTE_LIMIT],
        "text": edited,
        "overrides": dict(overrides or {}),
        "before": dict(before or {}),
        "after": dict(after or {}),
        "findings": [item for item in (findings or []) if isinstance(item, dict)][:20],
    }
    history = [entry, *list(state.get("history") or [])][:POLICY_HISTORY_LIMIT]
    return {
        "version": version,
        "text": edited,
        "updated_at": stamp,
        "author": entry["author"],
        "overrides": dict(overrides or {}),
        "history": history,
    }


def find_version(state: dict[str, Any], version: int) -> dict[str, Any] | None:
    for entry in state.get("history") or []:
        try:
            if int(entry.get("version") or 0) == int(version):
                return entry
        except (TypeError, ValueError):
            continue
    return None


def public_view(state: dict[str, Any]) -> dict[str, Any]:
    """给界面的形状：当前那条 + 历史摘要（不含 before/after 两份完整配置）。"""
    history = []
    for entry in state.get("history") or []:
        history.append(
            {
                "version": entry.get("version"),
                "at": entry.get("at"),
                "author": entry.get("author"),
                "note": entry.get("note"),
                "overrides": entry.get("overrides") or {},
            }
        )
    return {
        "version": state.get("version") or 0,
        "text": state.get("text") or "",
        "updated_at": state.get("updated_at") or 0.0,
        "author": state.get("author") or "",
        "overrides": state.get("overrides") or {},
        "fields": [
            {"key": key, "label": PATCH_LABELS.get(key, key)}
            for key in PATCH_FIELDS
        ],
        "history": history,
    }
