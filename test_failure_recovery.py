import json
import shutil
import unittest
import uuid
from pathlib import Path

from agent_state import DEBUGGING, EXECUTING, PLANNING, AcceptanceCriterion, AgentState, ExecutionStep
from agent_events import REPLAN_REQUIRED, TOOL_FAILED, AgentEvent
from agent_orchestrator import AgentOrchestrator
from coding_agent import run_replanning
from context_manager import ContextManager
from failure_classifier import FailureClassifier
from failure_memory import FailureMemory
from failure_models import BUILD_FAILED, REGRESSION_DETECTED, STALE_EDIT_FAILURE, TEST_FAILED, TIMEOUT
from failure_recovery import FailureRecovery
from tool_executor import ToolExecutor


class NoRecoveryManager:
    def __init__(self):
        self.pending_changesets = []
        self.rollback_calls = 0

    def can_rollback(self):
        return False

    def rollback_last_stable(self, state, reason):
        self.rollback_calls += 1
        raise AssertionError("FailureRecovery must not implement regression rollback")


class FakeTools:
    def __init__(self, root: Path):
        self.root = root
        self.checkpoint_manager = NoRecoveryManager()

    def call(self, state, name, arguments):
        return {"status": "SUCCESS", "tool": name}


def state_for_failure() -> AgentState:
    state = AgentState("fix parser", current_phase=EXECUTING)
    state.acceptance_criteria = [
        AcceptanceCriterion("AC1", "parser works", "CRITICAL", "AUTO", "TARGET", "run parser test")
    ]
    state.execution_plan = [ExecutionStep("STEP1", "fix parser", ["apply_patch"], ["AC1"])]
    state.current_step = "STEP1"
    return state


class ReplanClient:
    def __init__(self):
        self.messages = []
        self.tool_names = []
        analysis = {
            "previous_hypothesis": "empty input branch is incomplete",
            "observed_evidence": "the same frozen test failed three times",
            "previous_attempts": ["changed the return value"],
            "why_previous_attempt_was_insufficient": "the parser state was not reset",
            "remaining_possibilities": ["state leaks between calls"],
            "revised_hypothesis": "reset parser state before the empty-input branch",
            "revised_plan": "inspect state initialization, then make a distinct edit",
        }
        self.responses = iter([
            {"tool_calls": [{"function": {"name": "submit_failure_analysis", "arguments": json.dumps({"failure_analysis": analysis})}}]},
            {"tool_calls": [{"function": {"name": "submit_replan", "arguments": json.dumps({
                "execution_plan": [{
                    "step_id": "REVISED1",
                    "description": "inspect and reset parser state",
                    "suggested_tools": ["read_file", "apply_patch"],
                    "related_acceptance_criteria": ["AC1"],
                }]
            })}}]},
        ])

    def complete(self, messages, tool_schemas=None):
        self.messages.append(messages)
        self.tool_names.append([item["function"]["name"] for item in tool_schemas])
        return next(self.responses)


class FailureRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(__file__).parent / ".test_workspaces" / f"failure_{uuid.uuid4().hex}"
        self.base.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.base)

    def test_classifier_keeps_raw_streams_and_coarse_types(self):
        classifier = FailureClassifier()
        state = state_for_failure()
        result = {
            "status": "FAILED", "command": "python -m unittest test_parser.py", "exit_code": 2,
            "stdout": "stdout-start\n" + "A" * 4000 + "\nstdout-end",
            "stderr": "stderr-start\n" + "B" * 4000 + "\nstderr-end",
            "truncated": False,
        }
        event = classifier.classify_tool_result(state, "run_command", {}, result)
        self.assertEqual(event.type, TEST_FAILED)
        self.assertEqual(event.evidence["exit_code"], 2)
        self.assertIn("stdout-start", event.evidence["stdout"])
        self.assertIn("stdout-end", event.evidence["stdout"])
        self.assertIn("stderr-start", event.evidence["stderr"])
        self.assertIn("stderr-end", event.evidence["stderr"])
        self.assertTrue(event.evidence["stdout_truncated_for_failure_context"])
        self.assertTrue(event.evidence["stderr_truncated_for_failure_context"])

        verification = classifier.classify_verification_result(state, {
            "overall_status": "UNVERIFIED", "failed_critical": ["AC1"], "new_failures": [],
            "evidence_summary": "target failed", "criterion_results": [{
                "criterion_id": "AC1", "status": "FAIL", "evidence_type": "TARGET", "summary": "failed",
                "evidence": [{
                    "verification_id": "V1", "command": ["python", "target.py"], "status": "FAILED",
                    "exit_code": 3, "stdout": "S" * 4000, "stderr": "E" * 4000, "truncated": False,
                }],
            }],
        })
        command_evidence = verification.evidence["criterion_results"][0]["evidence"][0]
        self.assertEqual(command_evidence["exit_code"], 3)
        self.assertTrue(command_evidence["stdout"])
        self.assertTrue(command_evidence["stderr"])
        self.assertTrue(command_evidence["stdout_truncated_for_failure_context"])
        self.assertTrue(command_evidence["stderr_truncated_for_failure_context"])

        build = classifier.classify_tool_result(state, "run_command", {}, {"status": "FAILED", "command": "cmake --build .", "exit_code": 1})
        timeout = classifier.classify_tool_result(state, "run_command", {}, {"status": "TIMEOUT", "command": "python app.py", "timeout": 60})
        stale = classifier.classify_tool_result(state, "apply_patch", {"file": "parser.py", "symbol": "parse"}, {"status": "STALE_EDIT"})
        self.assertEqual(build.type, BUILD_FAILED)
        self.assertEqual(timeout.type, TIMEOUT)
        self.assertEqual(stale.type, STALE_EDIT_FAILURE)

    def test_repeat_guardrail_replans_and_context_contains_analysis_and_evidence(self):
        state = state_for_failure()
        tools = FakeTools(self.base)
        recovery = FailureRecovery(tools, max_repeat_failures=3)
        orchestrator = AgentOrchestrator()
        failure_result = {
            "status": "FAILED", "command": "python -m unittest test_parser.py", "exit_code": 1,
            "stdout": "FAIL: test_empty_input", "stderr": "AssertionError: expected []", "truncated": False,
        }
        for expected in (1, 2, 3):
            record = recovery.handle_tool_result(state, "run_command", {}, failure_result)
            self.assertEqual(record["repeat_count"], expected)
            orchestrator.transition(state, AgentEvent(TOOL_FAILED, "test command failed"))
            if record["decision"] == "REPLAN_REQUIRED":
                orchestrator.transition(state, AgentEvent(REPLAN_REQUIRED, "repeated failure fingerprint"))
        self.assertEqual(state.current_phase, PLANNING)
        self.assertEqual(state.replan_reason, "repeated failure fingerprint")
        self.assertEqual(len(state.failure_history), 3)
        self.assertNotIn(state.current_failure, state.confirmed_facts)

        client = ReplanClient()
        run_replanning(state, tools, client, ContextManager(self.base), orchestrator)
        self.assertIn("submit_failure_analysis", client.tool_names[0])
        self.assertNotIn("submit_replan", client.tool_names[0])
        self.assertIn("submit_replan", client.tool_names[1])
        second_context = client.messages[1][1]["content"]
        self.assertIn("Failure Analysis", second_context)
        self.assertIn("AssertionError", second_context)
        self.assertEqual(state.current_phase, EXECUTING)
        self.assertEqual(state.acceptance_criteria[0].id, "AC1")

    def test_duplicate_failed_edit_is_blocked_with_previous_evidence(self):
        (self.base / "parser.py").write_text("def parse():\n    return []\n", encoding="utf-8")
        state = state_for_failure()
        arguments = {
            "file": "parser.py", "operation": "replace", "intent": "return token",
            "symbol": "parse", "old_block": "return []", "new_block": "return ['x']",
        }
        signature = FailureClassifier.edit_action_signature(arguments)
        state.failure_history = [{
            "id": "failure_0007", "type": TEST_FAILED, "action_signature": signature,
            "evidence": {"stdout": "FAIL: test_empty_input", "stderr": "AssertionError", "exit_code": 1},
        }]
        result = ToolExecutor(self.base).call(state, "apply_patch", arguments)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["reason"], "DUPLICATE_FAILED_ACTION")
        self.assertEqual(result["related_failure"], "failure_0007")
        self.assertIn("AssertionError", result["evidence"]["stderr"])
        self.assertIn("return []", (self.base / "parser.py").read_text(encoding="utf-8"))

    def test_regression_is_recorded_without_second_recovery_and_unverified_is_ignored(self):
        state = state_for_failure()
        tools = FakeTools(self.base)
        recovery = FailureRecovery(tools)
        regression = {
            "overall_status": "REGRESSED", "new_failures": ["test_normal_parse"],
            "failed_critical": [], "criterion_results": [], "evidence_summary": "one new failure",
            "recovery_result": {"status": "UNDONE", "change_set_id": "change_0001"},
        }
        record = recovery.handle_verification_result(state, regression)
        self.assertEqual(record["type"], REGRESSION_DETECTED)
        self.assertEqual(record["recovery_result"]["status"], "UNDONE")
        self.assertEqual(tools.checkpoint_manager.rollback_calls, 0)

        before = len(state.failure_history)
        missing_evidence = {
            "overall_status": "UNVERIFIED", "failed_critical": [],
            "unverified_critical": ["AC1"], "criterion_results": [],
        }
        self.assertIsNone(recovery.handle_verification_result(state, missing_evidence))
        self.assertEqual(len(state.failure_history), before)


if __name__ == "__main__":
    unittest.main()
