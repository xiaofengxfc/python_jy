"""AI 候选策略的落盘与人工审核（Phase 4）。

候选永远只写 ``%APPDATA%/biTrader/strategies/candidates/``，**绝不 import、
绝不自动复制进正式策略目录**。审核通过只是把状态改为 ``approved``；启用仍由
人工在策略实验室里执行（Phase 4-B 接线）。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import paths
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

    @property
    def code_path(self) -> Path:
        return Path(self.__dict__["_dir"]) / f"{self.id}.py"

    def to_dict(self) -> dict[str, Any]:
        data = {k: v for k, v in self.__dict__.items() if not k.startswith("_")}
        return data


class CandidateError(RuntimeError):
    pass


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
        candidate.backtest = dict(metrics)
        self._write(candidate)
        return candidate
