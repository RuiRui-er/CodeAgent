"""Structured state used to rebuild the model context."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


PLANNING = "PLANNING"
EXECUTING = "EXECUTING"
VERIFYING = "VERIFYING"
DEBUGGING = "DEBUGGING"
DONE = "DONE"
FAILED = "FAILED"
PHASES = {PLANNING, EXECUTING, VERIFYING, DEBUGGING, DONE, FAILED}


@dataclass(frozen=True)
class AcceptanceCriterion:
    id: str
    description: str
    criticality: str
    verification_mode: str
    evidence_type: str
    verification_method: str


@dataclass(frozen=True)
class VerificationCheck:
    id: str
    description: str
    verification_mode: str
    evidence_type: str
    verification_method: str
    command: list[str] | None
    baseline_required: bool
    related_acceptance_criteria: list[str]


@dataclass(frozen=True)
class ExecutionStep:
    step_id: str
    description: str
    step_kind: str
    suggested_tools: list[str]
    related_acceptance_criteria: list[str]
    expected_change_files: list[str] = field(default_factory=list)
    related_verification_ids: list[str] = field(default_factory=list)


@dataclass
class AgentState:
    original_task: str
    current_phase: str = PLANNING
    task_understanding: str = ""
    acceptance_criteria: list[AcceptanceCriterion] = field(default_factory=list)
    verification_contract: list[VerificationCheck] = field(default_factory=list)
    baseline: list[dict[str, Any]] = field(default_factory=list)
    execution_plan: list[ExecutionStep] = field(default_factory=list)
    current_step: str | None = None
    clarification_needed: str | None = None
    confirmed_facts: list[str] = field(default_factory=list)
    completed_steps: list[str] = field(default_factory=list)
    failed_attempts: list[dict[str, Any]] = field(default_factory=list)
    failure_evidence: list[dict[str, Any]] = field(default_factory=list)
    recent_actions: list[dict[str, Any]] = field(default_factory=list)
    relevant_files: list[str] = field(default_factory=list)
    relevant_symbols: list[str] = field(default_factory=list)
    change_sets: list[dict[str, Any]] = field(default_factory=list)
    current_checkpoint: dict[str, Any] | None = None
    verification_result: dict[str, Any] | None = None
    manual_confirmation_items: list[str] = field(default_factory=list)
    human_evidence: list[dict[str, Any]] = field(default_factory=list)
    failed_finish_attempts: int = 0
    finish_guardrail_active: bool = False
    verification_sequence: int = 0
    failure_history: list[dict[str, Any]] = field(default_factory=list)
    current_failure: dict[str, Any] | None = None
    repeated_failure_count: int = 0
    consecutive_failure_fingerprint: str | None = None
    consecutive_failure_count: int = 0
    replan_attempts_by_fingerprint: dict[str, int] = field(default_factory=dict)
    no_progress_replan_count: int = 0
    no_progress_fingerprint: str | None = None
    replan_reason: str | None = None
    failure_analysis: dict[str, Any] | None = None
    needs_user_confirmation: bool = False
    verification_mode: str | None = None
    phase_history: list[dict[str, Any]] = field(default_factory=list)
    planning_validation_failures: int = 0
    planning_repair_attempts: int = 0
    planning_repair_success: bool = False
    planning_frozen: bool = False
    required_evidence_types: list[str] = field(default_factory=list)
    require_all_baselines: bool = False
    max_execution_plan_steps: int | None = None
    required_sanity_command_fragment: str | None = None

    def planning_snapshot(self) -> dict[str, Any]:
        return asdict(self)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "current_phase" and "current_phase" in self.__dict__:
            raise AttributeError("current_phase is lifecycle-owned; use AgentOrchestrator.transition()")
        super().__setattr__(name, value)

    def add_fact(self, fact: str) -> None:
        if fact and fact not in self.confirmed_facts:
            self.confirmed_facts.append(fact)

    def add_relevant_file(self, path: str) -> None:
        if path and path not in self.relevant_files:
            self.relevant_files.append(path)

    def add_relevant_symbol(self, symbol: str) -> None:
        if symbol and symbol not in self.relevant_symbols:
            self.relevant_symbols.append(symbol)

    def add_action(self, action: dict[str, Any], keep: int = 5) -> None:
        self.recent_actions.append(action)
        self.recent_actions[:] = self.recent_actions[-keep:]

    def complete_current_step(self) -> None:
        if not self.current_step:
            return
        if self.current_step not in self.completed_steps:
            self.completed_steps.append(self.current_step)
        ids = [step.step_id for step in self.execution_plan]
        try:
            index = ids.index(self.current_step)
        except ValueError:
            return
        self.current_step = ids[index + 1] if index + 1 < len(ids) else None

    def current_execution_step(self) -> ExecutionStep | None:
        return next((step for step in self.execution_plan if step.step_id == self.current_step), None)

    def remaining_execution_steps(self) -> list[ExecutionStep]:
        completed = set(self.completed_steps)
        return [step for step in self.execution_plan if step.step_id not in completed and step.step_id != self.current_step]

    def execution_plan_complete(self) -> bool:
        return bool(self.execution_plan) and self.current_step is None and not self.remaining_execution_steps()

    def next_verification_ref(self) -> str:
        self.verification_sequence += 1
        return f"verification_{self.verification_sequence:03d}"
