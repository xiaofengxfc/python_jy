"""AI 候选策略的落盘与人工审核。

候选先写 ``%APPDATA%/biTrader/strategies/candidates/``。审核通过时由
``routes_lab`` 复制为正式策略并触发热加载。候选目录本身仍不 import，
也不直接参与交易。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import paths
from .base import Strategy
from .registry import StrategyRegistry
from .sandbox import CheckReport, check_code

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"

_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,120}$")


@dataclass
class Candidate:
    id: str
    created_at: float
    strategy_key: str
    name: str
    base_key: str = ""
    model: str = ""
    prompt_sha: str = ""
    source_sha256: str = ""
    status: str = STATUS_PENDING
    checks: dict[str, Any] = field(default_factory=dict)
    backtest: dict[str, Any] = field(default_factory=dict)
    note: str = ""
    reviewer: str = ""
    registered_key: str = ""
    strategy_path: str = ""

    @property
    def code_path(self) -> Path:
        return Path(self.__dict__["_dir"]) / f"{self.id}.py"

    def to_dict(self) -> dict[str, Any]:
        data = {k: v for k, v in self.__dict__.items() if not k.startswith("_")}
        return data


class CandidateError(RuntimeError):
    pass


def json_safe(metrics: dict[str, Any]) -> dict[str, Any]:
    """只留能写进 JSON 的指标值。

    ``json.dumps`` 默认把 NaN / inf 写成 ``NaN`` / ``Infinity``：文件写得下去，
    ``json.loads`` 也读得回来，但接口响应用的序列化不接受这两个字面量，于是
    一条坏指标能让整个 ``/api/ai/strategies/candidates`` 从此 500，用户只能去数据
    目录手改 JSON。坏值在这里丢掉，界面读到的就是「—」。
    """
    clean: dict[str, Any] = {}
    for name, value in dict(metrics).items():
        try:
            number = float(value)
        except (TypeError, ValueError):
            clean[str(name)] = value  # 交易对、周期这类文本原样保留
            continue
        if not math.isfinite(number):
            continue
        clean[str(name)] = value
    return clean


class CandidateStore:
    """File-backed candidate store. Every path is confined to the candidate dir."""

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = (directory or paths.strategy_candidates_dir()).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ paths
    def _path(self, candidate_id: str, suffix: str) -> Path:
        if not _SAFE_ID.match(candidate_id):
            raise CandidateError(f"非法候选 id：{candidate_id}")
        path = (self.directory / f"{candidate_id}{suffix}").resolve()
        if path.parent != self.directory:
            raise CandidateError("候选路径越界")
        return path

    # ----------------------------------------------------------------- create
    def create(
        self,
        code: str,
        *,
        strategy_key: str,
        name: str = "",
        base_key: str = "",
        model: str = "",
        prompt_sha: str = "",
        note: str = "",
    ) -> Candidate:
        report: CheckReport = check_code(code)
        slug = re.sub(r"[^a-z0-9_]", "_", (strategy_key or "strategy").lower())[:32] or "strategy"
        candidate_id = f"cand-{time.strftime('%Y%m%d-%H%M%S')}-{slug}"
        # Same-second collisions are possible in tests; add a short suffix.
        if self._path(candidate_id, ".py").exists():
            candidate_id = f"{candidate_id}-{int(time.time() * 1000) % 1000:03d}"
        candidate = Candidate(
            id=candidate_id,
            created_at=time.time(),
            strategy_key=strategy_key,
            name=name or strategy_key,
            base_key=base_key,
            model=model,
            prompt_sha=prompt_sha,
            source_sha256=hashlib.sha256(code.encode("utf-8")).hexdigest(),
            status=STATUS_PENDING if report.ok else STATUS_REJECTED,
            checks=report.as_dict(),
            note=note,
        )
        code_path = self._path(candidate_id, ".py")
        candidate.__dict__["_dir"] = str(self.directory)
        tmp = code_path.with_suffix(".py.tmp")
        tmp.write_text(code, encoding="utf-8")
        tmp.replace(code_path)
        self._write(candidate)
        return candidate

    # ------------------------------------------------------------------- read
    def _write(self, candidate: Candidate) -> None:
        meta = candidate.to_dict()
        meta["code_file"] = f"{candidate.id}.py"
        path = self._path(candidate.id, ".json")
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    def list(self) -> list[Candidate]:
        items: list[Candidate] = []
        for path in sorted(self.directory.glob("cand-*.json"), reverse=True):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            items.append(self._from_dict(data))
        return items

    def get(self, candidate_id: str) -> Candidate:
        path = self._path(candidate_id, ".json")
        if not path.exists():
            raise CandidateError(f"候选不存在：{candidate_id}")
        return self._from_dict(json.loads(path.read_text(encoding="utf-8")))

    def read_code(self, candidate_id: str) -> str:
        path = self._path(candidate_id, ".py")
        if not path.exists():
            raise CandidateError(f"候选代码不存在：{candidate_id}")
        return path.read_text(encoding="utf-8")

    # ---------------------------------------------------------- instantiate
    def load_strategy(self, candidate_id: str) -> Strategy:
        """Instantiate a candidate strategy without touching the registry.

        A candidate may be backtested before it is approved, but it must not
        join the shared strategy list: the source is copied into a throwaway
        folder under a unique module name and loaded by a private
        ``StrategyRegistry``, so the live list stays exactly as the user left
        it and a broken candidate cannot disable a working strategy.
        """
        code = self.read_code(candidate_id)
        report = check_code(code)
        if not report.ok:
            issues = report.as_dict()["issues"][:3]
            problems = "；".join(item["message"] for item in issues)
            raise CandidateError(f"静态检查未通过，不能回测：{problems}")
        folder = Path(tempfile.mkdtemp(prefix="btrader-cand-"))
        try:
            stem = "cand_" + hashlib.sha1(candidate_id.encode("utf-8")).hexdigest()[:12]
            (folder / f"{stem}.py").write_text(code, encoding="utf-8", newline="\n")
            registry = StrategyRegistry()
            registry.discover([folder])
            # ``discover`` also loads the built-ins; only the keys that came
            # from the throwaway folder belong to this candidate.
            keys = [key for key in registry.keys if registry.source_path(key) is not None]
            if not keys:
                detail = "\n".join(registry.errors[-2:]) or "没有找到 Strategy 子类"
                raise CandidateError(f"候选代码无法加载：{detail}")
            declared = str(self.get(candidate_id).strategy_key or "")
            return registry.get(declared if declared in keys else keys[0])
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def rename_registered_key(self, old_key: str, new_key: str) -> int:
        """Keep the candidate records pointing at a renamed strategy file."""
        changed = 0
        for candidate in self.list():
            if candidate.registered_key != old_key:
                continue
            candidate.registered_key = new_key
            if candidate.strategy_path:
                candidate.strategy_path = str(
                    Path(candidate.strategy_path).with_name(f"{new_key}.py")
                )
            self._write(candidate)
            changed += 1
        return changed

    def _from_dict(self, data: dict[str, Any]) -> Candidate:
        known = {f for f in Candidate.__dataclass_fields__}
        candidate = Candidate(**{k: v for k, v in data.items() if k in known})
        candidate.__dict__["_dir"] = str(self.directory)
        return candidate

    # ----------------------------------------------------------------- review
    def review(
        self, candidate_id: str, *, approve: bool, note: str = "", reviewer: str = ""
    ) -> Candidate:
        candidate = self.get(candidate_id)
        if approve and not candidate.checks.get("ok"):
            raise CandidateError("静态检查未通过，不能批准")
        candidate.status = STATUS_APPROVED if approve else STATUS_REJECTED
        candidate.note = note
        candidate.reviewer = reviewer
        self._write(candidate)
        return candidate

    def record_backtest(self, candidate_id: str, metrics: dict[str, Any]) -> Candidate:
        candidate = self.get(candidate_id)
        candidate.backtest = json_safe(metrics)
        self._write(candidate)
        return candidate

    def mark_registered(
        self, candidate_id: str, *, key: str, strategy_path: str
    ) -> Candidate:
        """Remember the user strategy file created when a candidate is approved."""
        candidate = self.get(candidate_id)
        candidate.registered_key = key
        candidate.strategy_path = strategy_path
        self._write(candidate)
        return candidate


_STORE: CandidateStore | None = None
_STORE_LOCK = threading.Lock()


def default_store() -> CandidateStore:
    """Process-wide candidate store.

    The lab, the backtest route and the AI quant panel all read the same
    directory; one instance keeps their writes from racing on the JSON
    sidecar files.
    """
    global _STORE
    if _STORE is None:
        with _STORE_LOCK:
            if _STORE is None:
                _STORE = CandidateStore()
    return _STORE
