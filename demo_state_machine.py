"""Offline state-machine closure demo; no model or workspace mutation."""

from agent_events import (
    FINAL_VERIFIED,
    FINISH_REQUESTED,
    INCREMENTAL_VERIFIED,
    MAX_STEPS_REACHED,
    PLAN_READY,
    REPLAN_REQUIRED,
    TARGET_FAILED,
    TOOL_FAILED,
    VERIFICATION_REGRESSED,
    VERIFICATION_REQUESTED,
    VERIFICATION_UNVERIFIED,
    AgentEvent,
)
from agent_orchestrator import AgentOrchestrator
from agent_state import DEBUGGING, EXECUTING, PLANNING, VERIFYING, AgentState


def show(label, state, orchestrator, event):
    result = orchestrator.transition(state, event)
    print(label, "=>", result.to_dict())


def main():
    orchestrator = AgentOrchestrator()

    lifecycle = AgentState("demo")
    show("plan ready", lifecycle, orchestrator, AgentEvent(PLAN_READY, "plan available"))
    show("finish", lifecycle, orchestrator, AgentEvent(FINISH_REQUESTED, "final verification required"))
    show("final verified", lifecycle, orchestrator, AgentEvent(FINAL_VERIFIED, "environment evidence passed", {"mode": "FINAL"}))

    incremental = AgentState("incremental", current_phase=EXECUTING)
    show("incremental request", incremental, orchestrator, AgentEvent(VERIFICATION_REQUESTED, "step check"))
    show("incremental verified", incremental, orchestrator, AgentEvent(INCREMENTAL_VERIFIED, "step passed", {"mode": "INCREMENTAL"}))

    target = AgentState("target", current_phase=VERIFYING)
    show("target failed", target, orchestrator, AgentEvent(TARGET_FAILED, "critical target failed", {"mode": "FINAL"}))

    repeated = AgentState("repeat", current_phase=EXECUTING)
    show("ordinary failure", repeated, orchestrator, AgentEvent(TOOL_FAILED, "same test failed"))
    show("repeated failure", repeated, orchestrator, AgentEvent(REPLAN_REQUIRED, "repeated failure fingerprint"))

    regressed = AgentState("regression", current_phase=VERIFYING)
    recovery = {"status": "UNDONE", "change_set_id": "change_0007"}
    show("regression after existing recovery", regressed, orchestrator, AgentEvent(
        VERIFICATION_REGRESSED, "new regression detected", {"mode": "FINAL", "recovery_result": recovery}
    ))

    unverified = AgentState("unverified", current_phase=VERIFYING)
    show("unverified", unverified, orchestrator, AgentEvent(
        VERIFICATION_UNVERIFIED, "critical evidence unavailable", {"mode": "FINAL"}
    ))
    print("UNVERIFIED can DONE:", unverified.current_phase == "DONE")

    maximum = AgentState("max steps", current_phase=DEBUGGING)
    show("max steps", maximum, orchestrator, AgentEvent(MAX_STEPS_REACHED, "MAX_AGENT_STEPS reached"))


if __name__ == "__main__":
    main()
