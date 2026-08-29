"""Structured failure records kept separate from confirmed facts."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


BUILD_FAILED = "BUILD_FAILED"
TEST_FAILED = "TEST_FAILED"
RUNTIME_ERROR = "RUNTIME_ERROR"
TIMEOUT = "TIMEOUT"
OUTPUT_MISMATCH = "OUTPUT_MISMATCH"
EDIT_TARGET_NOT_FOUND = "EDIT_TARGET_NOT_FOUND"
EDIT_TARGET_AMBIGUOUS = "EDIT_TARGET_AMBIGUOUS"
STALE_EDIT_FAILURE = "STALE_EDIT"
INVALID_EDIT_FAILURE = "INVALID_EDIT"
REGRESSION_DETECTED = "REGRESSION_DETECTED"
TASK_INCOMPLETE = "TASK_INCOMPLETE"
TOOL_BLOCKED = "TOOL_BLOCKED"
TOOL_EXECUTION_FAILED = "TOOL_EXECUTION_FAILED"
UNKNOWN_FAILURE = "UNKNOWN_FAILURE"
DUPLICATE_FAILED_ACTION = "DUPLICATE_FAILED_ACTION"


@dataclass
class FailureEvent:
    id: str
    type: str
    location: dict[str, Any]
    evidence: dict[str, Any]
    related_changeset: str | None
    related_criterion: str | None
    hypothesis: str | None
    attempt: dict[str, Any] | str | None
    diagnostic_hints: list[str]
    fingerprint: str = ""
    repeat_count: int = 1
    step_id: str | None = None
    phase: str = ""
    recovery_result: dict[str, Any] | None = None
    action_signature: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


FAILURE_ANALYSIS_FIELDS = (
    "previous_hypothesis",
    "observed_evidence",
    "previous_attempts",
    "why_previous_attempt_was_insufficient",
    "remaining_possibilities",
    "revised_hypothesis",
    "revised_plan",
)
