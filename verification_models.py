"""Explainable result models for evidence-gated verification."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


PASS = "PASS"
FAIL = "FAIL"
CRITERION_UNVERIFIED = "UNVERIFIED"
NOT_APPLICABLE = "NOT_APPLICABLE"


@dataclass(frozen=True)
class CriterionResult:
    criterion_id: str
    status: str
    evidence_type: str
    evidence_source: str
    command: list[str] | None
    exit_code: int | None
    summary: str
    details: str
    verification_ids: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class HumanEvidence:
    criterion_id: str
    accepted: bool
    evidence: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class VerificationResult:
    mode: str
    overall_status: str
    criterion_results: list[dict[str, Any]]
    target_results: list[dict[str, Any]]
    regression_results: list[dict[str, Any]]
    sanity_results: list[dict[str, Any]]
    baseline_failures: list[str]
    current_failures: list[str]
    new_failures: list[str]
    verified_critical: list[str]
    unverified_critical: list[str]
    failed_critical: list[str]
    manual_items: list[str]
    evidence_summary: str
    overall_reason: str = ""
    unverified_reasons: list[dict[str, Any]] = field(default_factory=list)
    human_evidence: list[dict[str, Any]] = field(default_factory=list)
    recovery_result: dict[str, Any] | None = None
    checkpoint_result: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
