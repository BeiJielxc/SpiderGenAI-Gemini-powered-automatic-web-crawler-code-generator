"""Structured evidence contracts shared by specialists, gates and memory.

The orchestration layer never treats a bare ``success=True`` as proof.  Every
stage records assertions, candidate identifiers and artifact references so a
later runtime failure can be traced back to the decision that introduced it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional


VALID_EVIDENCE_STATUSES = {"proposed", "verified", "rejected", "stale", "confirmed"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_candidate_id(stage: str, payload: Any) -> str:
    """Return a stable short id for a candidate within a task run."""
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:10]
    prefix = (stage or "candidate").strip().lower().replace("_", "-")[:12]
    return f"{prefix}-{digest}"


@dataclass
class EvidenceAssertion:
    check: str
    expected: Any
    observed: Any
    passed: bool
    artifact_ref: Optional[str] = None
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class EvidenceCandidate:
    candidate_id: str
    kind: str
    value: Any
    status: str = "proposed"
    confidence: float = 0.0
    provenance: Dict[str, Any] = field(default_factory=dict)
    assertions: List[EvidenceAssertion] = field(default_factory=list)
    risk_flags: List[str] = field(default_factory=list)
    rejection_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        out = asdict(self)
        out["assertions"] = [item.to_dict() for item in self.assertions]
        return out


@dataclass
class StageEvidence:
    stage: str
    status: str = "proposed"
    confidence: float = 0.0
    selected_candidate_id: Optional[str] = None
    candidates: List[EvidenceCandidate] = field(default_factory=list)
    assertions: List[EvidenceAssertion] = field(default_factory=list)
    artifact_refs: List[str] = field(default_factory=list)
    assumptions: List[str] = field(default_factory=list)
    risk_flags: List[str] = field(default_factory=list)
    rejected_candidates: List[Dict[str, Any]] = field(default_factory=list)
    recommended_next_stage: Optional[str] = None
    created_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        status = self.status if self.status in VALID_EVIDENCE_STATUSES else "proposed"
        return {
            "stage": self.stage,
            "status": status,
            "confidence": max(0.0, min(1.0, float(self.confidence))),
            "selected_candidate_id": self.selected_candidate_id,
            "candidates": [item.to_dict() for item in self.candidates],
            "assertions": [item.to_dict() for item in self.assertions],
            "artifact_refs": list(dict.fromkeys(self.artifact_refs)),
            "assumptions": self.assumptions,
            "risk_flags": self.risk_flags,
            "rejected_candidates": self.rejected_candidates,
            "recommended_next_stage": self.recommended_next_stage,
            "created_at": self.created_at,
        }


@dataclass
class ValidationReport:
    gate: str
    passed: bool
    failure_type: Optional[str] = None
    summary: str = ""
    assertions: List[EvidenceAssertion] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)
    rollback_target: Optional[str] = None
    created_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        out = asdict(self)
        out["assertions"] = [item.to_dict() for item in self.assertions]
        return out


@dataclass
class AttributionDecision:
    failure_type: str
    suspected_stages: List[Dict[str, Any]]
    rollback_target: str
    retry_candidate_id: Optional[str] = None
    confidence: float = 0.0
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RuntimeReport:
    success: bool
    task_id: str
    workdir: str
    script_path: str
    output_files: List[str] = field(default_factory=list)
    record_count: int = 0
    stage_counts: Dict[str, int] = field(default_factory=dict)
    schema_valid: bool = False
    date_fill_rate: float = 0.0
    url_valid_rate: float = 0.0
    exit_code: int = 0
    timed_out: bool = False
    error: Optional[str] = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    created_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def merge_stage_evidence(
    current: Optional[Dict[str, Any]],
    evidence: StageEvidence,
) -> Dict[str, Any]:
    merged = dict(current or {})
    merged[evidence.stage] = evidence.to_dict()
    return merged


def artifact_ids_from_tool_log(entries: Iterable[Dict[str, Any]]) -> List[str]:
    refs: List[str] = []
    for entry in entries or []:
        artifacts = entry.get("artifacts") if isinstance(entry, dict) else None
        if not isinstance(artifacts, dict):
            continue
        for value in artifacts.values():
            if isinstance(value, dict) and value.get("artifact_id"):
                refs.append(str(value["artifact_id"]))
    return list(dict.fromkeys(refs))


__all__ = [
    "AttributionDecision",
    "EvidenceAssertion",
    "EvidenceCandidate",
    "RuntimeReport",
    "StageEvidence",
    "ValidationReport",
    "artifact_ids_from_tool_log",
    "make_candidate_id",
    "merge_stage_evidence",
]
