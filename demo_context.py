"""Offline demonstration of phase-aware context selection."""

from pathlib import Path

from agent_state import (
    DEBUGGING,
    EXECUTING,
    PLANNING,
    VERIFYING,
    AcceptanceCriterion,
    AgentState,
    ExecutionStep,
    VerificationCheck,
)
from context_manager import ContextManager


def main() -> None:
    workspace = Path(__file__).parent / "demo_project"
    manager = ContextManager(workspace)
    state = AgentState(original_task="Fix divide and verify the result.")
    state.acceptance_criteria = [
        AcceptanceCriterion("AC-1", "divide(10, 2) returns 5", "CRITICAL", "AUTO", "TARGET", "Run test")
    ]
    state.verification_contract = [
        VerificationCheck("V-1", "Calculator test", "AUTO", "TARGET", "Run unittest", ["python", "-m", "unittest", "-v"], True, ["AC-1"])
    ]
    state.execution_plan = [
        ExecutionStep("STEP-1", "Inspect divide", ["read_file"], ["AC-1"]),
        ExecutionStep("STEP-2", "Fix and test divide", ["apply_patch", "run_command"], ["AC-1"]),
    ]
    state.current_step = "STEP-1"

    print("\n=== DEMO 1: planning discovery ===")
    state.add_fact("Project contains calculator.py and test_calculator.py.")
    state.add_action({"tool": "list_files", "observation": "2 source files found"})
    manager.build_messages(state, "Planning prompt")

    print("\n=== DEMO 2: executing current step ===")
    state.set_phase(EXECUTING)
    state.add_relevant_file("calculator.py")
    state.add_relevant_symbol("divide")
    state.add_action({"tool": "read_file", "observation": "calculator.py read from disk"})
    manager.build_messages(state, "Execution prompt")

    print("\n=== DEMO 3: verification failure ===")
    state.set_phase(DEBUGGING)
    state.failed_attempts.append({"attempt": "python -m unittest -v", "reason": "divide returned 0.2"})
    state.failure_evidence.append({"exit_code": 1, "stderr": "AssertionError: 0.2 != 5"})
    state.add_action({"tool": "run_command", "observation": {"exit_code": 1}})
    manager.build_messages(state, "Debugging prompt")

    print("\n=== DEMO 4: verification success ===")
    state.set_phase(VERIFYING)
    state.add_fact("Updated calculator.py in the workspace.")
    state.completed_steps.extend(["STEP-1", "STEP-2"])
    state.add_action({"tool": "run_command", "observation": {"exit_code": 0, "stdout": "OK"}})
    manager.build_messages(state, "Verification prompt")


if __name__ == "__main__":
    main()
