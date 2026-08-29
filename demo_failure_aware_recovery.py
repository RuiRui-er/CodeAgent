"""Offline demonstration of structured failure memory and recovery decisions."""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from agent_state import EXECUTING, AcceptanceCriterion, AgentState, ExecutionStep
from context_manager import ContextManager
from failure_classifier import FailureClassifier
from failure_recovery import FailureRecovery
from tool_executor import ToolExecutor


class DemoCheckpointManager:
    pending_changesets: list[str] = []

    def can_rollback(self) -> bool:
        return False

    def rollback_last_stable(self, state, reason):
        raise AssertionError("ordinary demo failures must not invent a rollback")


class DemoTools:
    def __init__(self, root: Path):
        self.root = root
        self.checkpoint_manager = DemoCheckpointManager()


def make_state() -> AgentState:
    state = AgentState("fix empty parser input", current_phase=EXECUTING)
    state.acceptance_criteria = [
        AcceptanceCriterion("AC1", "empty input parses", "CRITICAL", "AUTO", "TARGET", "frozen parser test")
    ]
    state.execution_plan = [ExecutionStep("STEP1", "fix parser", ["apply_patch", "run_command"], ["AC1"])]
    state.current_step = "STEP1"
    return state


def main() -> None:
    root = Path(__file__).parent / ".demo_failure_runtime" / uuid.uuid4().hex
    root.mkdir(parents=True)
    try:
        state = make_state()
        classifier = FailureClassifier()
        examples = [
            ("build", "run_command", {}, {"status": "FAILED", "command": "cmake --build .", "exit_code": 1, "stdout": "", "stderr": "compiler error: missing symbol"}),
            ("test", "run_command", {}, {"status": "FAILED", "command": "python -m unittest test_parser.py", "exit_code": 1, "stdout": "FAIL: test_empty_input", "stderr": "AssertionError: expected []"}),
            ("timeout", "run_command", {}, {"status": "TIMEOUT", "command": "python app.py", "exit_code": None, "stdout": "server started", "stderr": "", "timeout": 60}),
            ("stale edit", "apply_patch", {"file": "parser.py", "symbol": "Parser.parse"}, {"status": "STALE_EDIT", "current_context": "latest parser source"}),
        ]
        print("=== Coarse classification keeps raw evidence ===")
        for label, tool, arguments, result in examples:
            event = classifier.classify_tool_result(state, tool, arguments, result)
            print(label, "->", event.type)
            print("  exit:", event.evidence.get("exit_code"))
            print("  stdout:", repr(event.evidence.get("stdout")))
            print("  stderr:", repr(event.evidence.get("stderr")))

        print("\n=== Repeated fingerprint triggers PLANNING ===")
        recovery = FailureRecovery(DemoTools(root), max_repeat_failures=3)
        failed_test = examples[1][3]
        for _ in range(3):
            state.set_phase(EXECUTING)
            record = recovery.handle_tool_result(state, "run_command", {}, failed_test)
            print(record["fingerprint"], "repeat", record["repeat_count"], "phase", state.current_phase)

        state.failure_analysis = {
            "previous_hypothesis": "empty branch only",
            "observed_evidence": "same assertion and raw runner output repeated",
            "previous_attempts": ["changed return value"],
            "why_previous_attempt_was_insufficient": "state setup was not examined",
            "remaining_possibilities": ["parser state leaks"],
            "revised_hypothesis": "reset state before parsing",
            "revised_plan": "inspect setup and make a distinct edit",
        }
        context = ContextManager(root).build_messages(state, "replan")
        print("Failure Analysis in PLANNING context:", "Failure Analysis" in context[1]["content"])
        print("Raw stderr in PLANNING context:", "AssertionError" in context[1]["content"])

        print("\n=== Exact failed edit is blocked ===")
        (root / "parser.py").write_text("def parse():\n    return []\n", encoding="utf-8")
        edit = {
            "file": "parser.py", "operation": "replace", "intent": "return token",
            "symbol": "parse", "old_block": "return []", "new_block": "return ['x']",
        }
        state.set_phase(EXECUTING)
        state.failure_history.append({
            "id": "failure_0099", "type": "TEST_FAILED",
            "action_signature": classifier.edit_action_signature(edit),
            "evidence": {"stdout": "FAIL: test_empty_input", "stderr": "AssertionError", "exit_code": 1},
        })
        print(ToolExecutor(root).call(state, "apply_patch", edit))

        print("\n=== Verification boundaries ===")
        regression = recovery.handle_verification_result(state, {
            "overall_status": "REGRESSED", "new_failures": ["test_normal_parse"],
            "failed_critical": [], "criterion_results": [], "evidence_summary": "new regression",
            "recovery_result": {"status": "UNDONE", "change_set_id": "change_0001"},
        })
        print("Regression recorded existing recovery:", regression["recovery_result"])
        unverified = recovery.handle_verification_result(state, {
            "overall_status": "UNVERIFIED", "failed_critical": [], "unverified_critical": ["AC1"],
            "criterion_results": [],
        })
        print("Evidence-only UNVERIFIED creates failure:", unverified is not None)
    finally:
        shutil.rmtree(root.parent)


if __name__ == "__main__":
    main()
