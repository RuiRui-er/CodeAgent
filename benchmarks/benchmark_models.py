"""Serializable benchmark task, variant, evidence, and run result models."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    category: str
    description: str
    initial_commit: str
    critical_criteria: list[str]
    noncritical_criteria: list[str]
    expected_files: list[str]
    hidden_test_command: list[str]
    notes: str = ""


@dataclass(frozen=True)
class VariantConfig:
    name: str
    context_mode: str
    structured_edit_enabled: bool
    evidence_gate_enabled: bool
    failure_memory_enabled: bool
    implemented: bool = False


VARIANTS = {
    "full": VariantConfig("full", "phase_aware", True, True, True, True),
    "baseline_full_history": VariantConfig("baseline_full_history", "full_history", True, True, True),
    "baseline_recent_n": VariantConfig("baseline_recent_n", "recent_n", True, True, True),
    "baseline_exact_replace": VariantConfig("baseline_exact_replace", "phase_aware", False, True, True),
    "baseline_no_evidence_gate": VariantConfig("baseline_no_evidence_gate", "phase_aware", True, False, True),
    "baseline_no_failure_memory": VariantConfig("baseline_no_failure_memory", "phase_aware", True, True, False),
}


@dataclass(frozen=True)
class HiddenEvaluationResult:
    success: bool
    command: list[str]
    exit_code: int | None
    stdout: str
    stderr: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RunResult:
    run_id: str
    task_id: str
    variant: str
    agent_final_phase: str
    agent_verification_status: str | None
    hidden_success: bool
    false_success: bool
    steps: int
    llm_calls: int
    context_chars_total: int
    context_chars_avg: float
    context_chars_max: int
    context_chars_p95: int
    repeated_actions: int
    failure_events: int
    regressions: int
    changeset_undos: int
    checkpoint_rollbacks: int
    successful_recoveries: int
    replans: int
    final_checkpoint: str | None
    termination_reason: str
    wrong_location_edits: int | None
    ambiguous_edit_rejections: int
    stale_edit_rejections: int
    planning_validation_failures: int
    planning_repair_attempts: int
    planning_repair_success: bool
    hidden_evaluation: dict[str, Any]
    initial_commit: str
    variant_config: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TrajectoryRecorder:
    entries: list[dict[str, Any]] = field(default_factory=list)
    context_sizes: list[int] = field(default_factory=list)
    llm_calls: int = 0

    def record(self, **entry: Any) -> None:
        self.entries.append({"step": len(self.entries) + 1, **entry})

    def record_context(self, chars: int, phase: str | None, action: str | None) -> None:
        self.llm_calls += 1
        self.context_sizes.append(chars)
        self.record(phase=phase, event="LLM_CALL", tool=None, action=action, context_chars=chars)
