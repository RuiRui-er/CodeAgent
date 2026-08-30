"""Single authority for Coding Agent lifecycle phase transitions."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from agent_events import (
    CONTINUE_EXECUTION,
    EDIT_APPLIED,
    EDIT_FAILED,
    FINAL_PARTIAL,
    FINAL_VERIFIED,
    FINISH_REQUESTED,
    INCREMENTAL_PARTIAL,
    INCREMENTAL_VERIFIED,
    MAX_STEPS_REACHED,
    PLAN_BLOCKED_BY_USER_INTENT,
    PLAN_READY,
    REPLAN_REQUIRED,
    TARGET_FAILED,
    TOOL_FAILED,
    UNRECOVERABLE_FAILURE,
    USER_CONFIRMATION_REQUIRED,
    VERIFICATION_REGRESSED,
    VERIFICATION_REQUESTED,
    VERIFICATION_UNVERIFIED,
    AgentEvent,
)
from agent_state import DEBUGGING, DONE, EXECUTING, FAILED, PLANNING, VERIFYING, AgentState


FINAL = "FINAL"
INCREMENTAL = "INCREMENTAL"
TERMINAL_PHASES = {DONE, FAILED}


@dataclass(frozen=True)
class TransitionResult:
    previous_phase: str
    event: str
    next_phase: str
    reason: str
    pause_autonomous_loop: bool
    needs_user_confirmation: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Explicit table for all non-global lifecycle transitions.
ALLOWED_TRANSITIONS = {
    (PLANNING, PLAN_READY): EXECUTING,
    (PLANNING, PLAN_BLOCKED_BY_USER_INTENT): PLANNING,
    (EXECUTING, FINISH_REQUESTED): VERIFYING,
    (DEBUGGING, FINISH_REQUESTED): VERIFYING,
    (EXECUTING, VERIFICATION_REQUESTED): VERIFYING,
    (DEBUGGING, VERIFICATION_REQUESTED): VERIFYING,
    (EXECUTING, TOOL_FAILED): DEBUGGING,
    (DEBUGGING, TOOL_FAILED): DEBUGGING,
    (EXECUTING, EDIT_FAILED): DEBUGGING,
    (DEBUGGING, EDIT_FAILED): DEBUGGING,
    (EXECUTING, EDIT_APPLIED): EXECUTING,
    (DEBUGGING, EDIT_APPLIED): EXECUTING,
    (DEBUGGING, CONTINUE_EXECUTION): EXECUTING,
    (VERIFYING, INCREMENTAL_VERIFIED): EXECUTING,
    (VERIFYING, INCREMENTAL_PARTIAL): EXECUTING,
    (VERIFYING, FINAL_VERIFIED): DONE,
    (VERIFYING, FINAL_PARTIAL): VERIFYING,
    (VERIFYING, VERIFICATION_REGRESSED): DEBUGGING,
    (VERIFYING, TARGET_FAILED): DEBUGGING,
    (VERIFYING, VERIFICATION_UNVERIFIED): VERIFYING,
    (DEBUGGING, REPLAN_REQUIRED): PLANNING,
}


class AgentOrchestrator:
    def __init__(self, failed_finish_limit: int = 2):
        self.failed_finish_limit = max(1, int(failed_finish_limit))

    def transition(self, state: AgentState, event: AgentEvent) -> TransitionResult:
        previous = state.current_phase
        if previous in TERMINAL_PHASES:
            raise ValueError(f"terminal phase {previous} cannot handle {event.type}")

        if event.type in {MAX_STEPS_REACHED, UNRECOVERABLE_FAILURE}:
            next_phase = FAILED
        elif event.type == USER_CONFIRMATION_REQUIRED:
            next_phase = previous
        else:
            try:
                next_phase = ALLOWED_TRANSITIONS[(previous, event.type)]
            except KeyError as exc:
                raise ValueError(f"illegal transition: {previous} + {event.type}") from exc

        needs_confirmation = state.needs_user_confirmation
        pause = False
        if event.type in {PLAN_BLOCKED_BY_USER_INTENT, USER_CONFIRMATION_REQUIRED, VERIFICATION_UNVERIFIED, FINAL_PARTIAL}:
            needs_confirmation = True
            pause = True
        elif event.type == FINAL_VERIFIED:
            needs_confirmation = False

        if event.type == FINISH_REQUESTED:
            state.verification_mode = FINAL
        elif event.type == VERIFICATION_REQUESTED:
            state.verification_mode = event.payload.get("mode", INCREMENTAL)
        elif event.type in {FINAL_VERIFIED, FINAL_PARTIAL}:
            state.failed_finish_attempts = 0
            state.finish_guardrail_active = False
        elif (
            event.type in {VERIFICATION_REGRESSED, TARGET_FAILED, VERIFICATION_UNVERIFIED}
            and event.payload.get("mode", state.verification_mode) == FINAL
        ):
            state.failed_finish_attempts += 1
            state.finish_guardrail_active = state.failed_finish_attempts >= self.failed_finish_limit

        if event.type == REPLAN_REQUIRED:
            state.replan_reason = event.reason or event.payload.get("reason") or "repeated failure"
            state.failure_analysis = None
        elif event.type == PLAN_READY:
            state.replan_reason = None

        state.current_phase = next_phase
        state.needs_user_confirmation = needs_confirmation
        record = {
            "from": previous,
            "event": event.type,
            "to": next_phase,
            "reason": event.reason,
        }
        state.phase_history.append(record)
        result = TransitionResult(previous, event.type, next_phase, event.reason, pause, needs_confirmation)
        self._log(result)
        return result

    @staticmethod
    def _log(result: TransitionResult) -> None:
        print("\n[Transition]", flush=True)
        print(result.previous_phase, flush=True)
        print(f"-- {result.event} -->", flush=True)
        print(result.next_phase, flush=True)
        if result.reason:
            print(f"Reason: {result.reason}", flush=True)
