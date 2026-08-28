import shutil
import json
import unittest
import uuid
from pathlib import Path

from agent_state import DEBUGGING, DONE, VERIFYING, AcceptanceCriterion, AgentState, VerificationCheck
from edit_models import PARTIALLY_VERIFIED, REGRESSED, UNVERIFIED, VERIFIED
from context_manager import ContextManager
from coding_agent import run_execution
from verification_engine import VerificationEngine


def observation(passed: bool, failures: str = "") -> dict:
    return {
        "status": "SUCCESS" if passed else "FAILED",
        "exit_code": 0 if passed else 1,
        "stdout": failures,
        "stderr": "",
    }


class FakeCheckpointManager:
    def __init__(self):
        self.pending_changesets = ["change_0001"]
        self.updated = []
        self.stable_calls = 0
        self.undo_calls = 0
        self.rollback_calls = 0

    def update_change_verification(self, state, change_id, status):
        self.updated.append((change_id, status))
        state.change_sets[0]["verification_status"] = status
        return {"status": "UPDATED"}

    def mark_stable(self, state, reason, verification_ref):
        self.stable_calls += 1
        self.pending_changesets.clear()
        return {"status": "CREATED", "checkpoint": {"id": "checkpoint_001"}}

    def undo_changeset(self, state, change_id, reason):
        self.undo_calls += 1
        self.pending_changesets.clear()
        return {"status": "UNDONE", "change_set_id": change_id}

    def rollback_last_stable(self, state, reason):
        self.rollback_calls += 1
        return {"status": "ROLLED_BACK"}

    def get_current_checkpoint(self):
        return {"id": "checkpoint_001"} if self.stable_calls else {"id": "checkpoint_000"}


class FakeTools:
    def __init__(self, results, root=None):
        self.results = {key: list(value) for key, value in results.items()}
        self.checkpoint_manager = FakeCheckpointManager()
        self.calls = []
        self.root = root or Path(__file__).parent

    def call(self, state, name, arguments):
        if name == "finish":
            state.set_phase(VERIFYING)
            return {"status": "SUCCESS", "tool": "finish", "next_phase": VERIFYING}
        self.calls.append(arguments["command"][0])
        return self.results[arguments["command"][0]].pop(0)


class FinishClient:
    def complete(self, messages, tool_schemas=None):
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "finish-1",
                "type": "function",
                "function": {"name": "finish", "arguments": json.dumps({"summary": "ready"})},
            }],
        }


def criterion(identifier, criticality, mode, evidence):
    return AcceptanceCriterion(identifier, identifier, criticality, mode, evidence, f"verify {identifier}")


def check(identifier, evidence, criterion_id, baseline=False, mode="AUTO"):
    return VerificationCheck(
        identifier, identifier, mode, evidence, f"run {identifier}",
        [identifier] if mode == "AUTO" else None, baseline, [criterion_id],
    )


def make_state(criteria, checks, baseline=None):
    state = AgentState("verify task", current_phase=VERIFYING)
    state.acceptance_criteria = criteria
    state.verification_contract = checks
    state.baseline = baseline or []
    state.change_sets = [{"id": "change_0001", "verification_status": UNVERIFIED}]
    return state


class VerificationEngineTests(unittest.TestCase):
    def test_finish_request_cannot_bypass_failed_critical_evidence(self):
        directory = Path(__file__).parent / ".test_workspaces" / f"finish_{uuid.uuid4().hex}"
        directory.mkdir(parents=True)
        try:
            criteria = [criterion("AC_TARGET", "CRITICAL", "AUTO", "TARGET")]
            checks = [check("target", "TARGET", "AC_TARGET")]
            tools = FakeTools({"target": [observation(False)]}, directory)
            state = make_state(criteria, checks)

            report = run_execution(state, tools, FinishClient(), max_steps=1)

            self.assertNotEqual(state.current_phase, DONE)
            self.assertEqual(state.verification_result["overall_status"], UNVERIFIED)
            self.assertIn("maximum", report)
        finally:
            shutil.rmtree(directory)

    def test_baseline_failure_without_new_failure_can_verify_and_checkpoint(self):
        criteria = [
            criterion("AC_TARGET", "CRITICAL", "AUTO", "TARGET"),
            criterion("AC_REG", "NON_CRITICAL", "AUTO", "REGRESSION"),
        ]
        checks = [check("target", "TARGET", "AC_TARGET"), check("regression", "REGRESSION", "AC_REG", True)]
        baseline = [{"verification_id": "regression", "observation": observation(False, "FAILED tests/test_old.py::test_old") }]
        tools = FakeTools({"target": [observation(True)], "regression": [observation(False, "FAILED tests/test_old.py::test_old")]})
        state = make_state(criteria, checks, baseline)

        result = VerificationEngine(tools).run_final_verification(state)

        self.assertEqual(result["overall_status"], VERIFIED)
        self.assertEqual(result["new_failures"], [])
        self.assertEqual(result["baseline_failures"], ["tests/test_old.py::test_old"])
        self.assertEqual(state.current_phase, DONE)
        self.assertEqual(tools.checkpoint_manager.stable_calls, 1)
        self.assertTrue(any("AC_TARGET verified" in fact for fact in state.confirmed_facts))

    def test_noncritical_human_item_is_partially_verified(self):
        criteria = [
            criterion("AC_CORE", "CRITICAL", "AUTO", "TARGET"),
            criterion("AC_DOC", "NON_CRITICAL", "HUMAN", "TARGET"),
        ]
        checks = [check("core", "TARGET", "AC_CORE"), check("docs", "TARGET", "AC_DOC", mode="HUMAN")]
        tools = FakeTools({"core": [observation(True)]})
        state = make_state(criteria, checks)

        result = VerificationEngine(tools).run_final_verification(state)

        self.assertEqual(result["overall_status"], PARTIALLY_VERIFIED)
        self.assertEqual(result["manual_items"], ["AC_DOC"])
        self.assertEqual(state.current_phase, DONE)
        self.assertEqual(tools.checkpoint_manager.stable_calls, 1)

    def test_target_pass_with_new_regression_recovers_and_debugs(self):
        criteria = [
            criterion("AC_TARGET", "CRITICAL", "AUTO", "TARGET"),
            criterion("AC_REG", "NON_CRITICAL", "AUTO", "REGRESSION"),
        ]
        checks = [check("target", "TARGET", "AC_TARGET"), check("regression", "REGRESSION", "AC_REG", True)]
        baseline = [{"verification_id": "regression", "observation": observation(False, "FAILED tests/test_old.py::test_old")}]
        current = "FAILED tests/test_old.py::test_old\nFAILED tests/test_new.py::test_new"
        tools = FakeTools({"target": [observation(True)], "regression": [observation(False, current)]})
        state = make_state(criteria, checks, baseline)

        result = VerificationEngine(tools).run_final_verification(state)

        self.assertEqual(result["overall_status"], REGRESSED)
        self.assertEqual(result["new_failures"], ["tests/test_new.py::test_new"])
        self.assertEqual(tools.checkpoint_manager.undo_calls, 1)
        self.assertEqual(tools.checkpoint_manager.rollback_calls, 0)
        self.assertEqual(state.current_phase, DEBUGGING)

    def test_critical_without_auto_evidence_stays_unverified(self):
        criteria = [criterion("AC_HUMAN", "CRITICAL", "HUMAN", "TARGET")]
        checks = [check("human", "TARGET", "AC_HUMAN", mode="HUMAN")]
        tools = FakeTools({})
        state = make_state(criteria, checks)

        result = VerificationEngine(tools).run_final_verification(state)

        self.assertEqual(result["overall_status"], UNVERIFIED)
        self.assertEqual(result["unverified_critical"], ["AC_HUMAN"])
        self.assertEqual(state.current_phase, VERIFYING)
        self.assertEqual(tools.checkpoint_manager.stable_calls, 0)

    def test_target_failure_without_regression_keeps_changes_and_debugs(self):
        criteria = [
            criterion("AC_TARGET", "CRITICAL", "AUTO", "TARGET"),
            criterion("AC_REG", "NON_CRITICAL", "AUTO", "REGRESSION"),
        ]
        checks = [check("target", "TARGET", "AC_TARGET"), check("regression", "REGRESSION", "AC_REG", True)]
        baseline = [{"verification_id": "regression", "observation": observation(True)}]
        tools = FakeTools({"target": [observation(False)], "regression": [observation(True)]})
        state = make_state(criteria, checks, baseline)

        result = VerificationEngine(tools, failed_finish_limit=2).run_final_verification(state)

        self.assertEqual(result["overall_status"], UNVERIFIED)
        self.assertEqual(result["failed_critical"], ["AC_TARGET"])
        self.assertEqual(state.current_phase, DEBUGGING)
        self.assertEqual(tools.checkpoint_manager.undo_calls, 0)
        self.assertEqual(tools.checkpoint_manager.rollback_calls, 0)
        self.assertNotEqual(state.current_phase, DONE)

        tools.results["target"] = [observation(False)]
        tools.results["regression"] = [observation(True)]
        VerificationEngine(tools, failed_finish_limit=2).run_final_verification(state)
        self.assertEqual(state.failed_finish_attempts, 2)
        self.assertTrue(state.finish_guardrail_active)
        directory = Path(__file__).parent / ".test_workspaces" / f"context_{uuid.uuid4().hex}"
        directory.mkdir(parents=True)
        try:
            messages = ContextManager(directory).build_messages(state, "debug")
            self.assertIn("Finish Guardrail", messages[1]["content"])
            self.assertIn("AC_TARGET", messages[1]["content"])
        finally:
            shutil.rmtree(directory)


if __name__ == "__main__":
    unittest.main()
