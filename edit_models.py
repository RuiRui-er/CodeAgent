"""Data structures for confidence-aware structured editing."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


APPLIED = "APPLIED"
UNVERIFIED = "UNVERIFIED"
VERIFIED = "VERIFIED"
PARTIALLY_VERIFIED = "PARTIALLY_VERIFIED"
REGRESSED = "REGRESSED"
ROLLBACK_NONE = "NONE"
UNDONE = "UNDONE"
CHECKPOINT_ROLLED_BACK = "CHECKPOINT_ROLLED_BACK"
TARGET_NOT_FOUND = "TARGET_NOT_FOUND"
AMBIGUOUS_TARGET = "AMBIGUOUS_TARGET"
STALE_EDIT = "STALE_EDIT"
INVALID_EDIT = "INVALID_EDIT"
BLOCKED = "BLOCKED"
FAILED = "FAILED"


@dataclass(frozen=True)
class StructuredEditRequest:
    file: str
    operation: str
    intent: str
    symbol: str | None = None
    anchor: str | None = None
    old_block: str | None = None
    new_block: str | None = None
    candidate_id: str | None = None


@dataclass(frozen=True)
class EditCandidate:
    id: str
    file: str
    symbol: str | None
    start: int
    end: int
    line_range: str
    context: str

    def public(self) -> dict[str, Any]:
        value = asdict(self)
        value.pop("start")
        value.pop("end")
        return value


@dataclass(frozen=True)
class ResolvedEdit:
    file: str
    operation: str
    intent: str
    symbol: str | None
    start: int
    end: int
    before: str
    after: str
    resolution: str


@dataclass(frozen=True)
class ChangeSet:
    id: str
    file: str
    symbol: str | None
    operation: str
    intent: str
    before: str
    after: str
    apply_status: str
    verification_status: str
    rollback_status: str
    step_id: str | None
    phase: str
    checkpoint_base: str | None
    start: int
    context_before: str
    context_after: str
    related_acceptance_criteria: list[str] = field(default_factory=list)
    related_verification_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
