import sys
import json
import unittest
from pathlib import Path

from coding_agent import WorkspaceTools, run_planning


def valid_plan():
    return {
        "task_understanding": "Fix divide so total is divided by count.",
        "acceptance_criteria": [{
            "id": "AC-1", "description": "divide(10, 2) returns 5",
            "criticality": "CRITICAL", "verification_mode": "AUTO",
            "evidence_type": "TARGET",
            "verification_method": "Run test_calculator.py and require the divide assertion to pass.",
        }],
        "verification_contract": [{
            "id": "V-1", "description": "Target divide test",
            "verification_mode": "AUTO", "evidence_type": "TARGET",
            "verification_method": "Run test_calculator.py and require exit code 0.",
            "command": [sys.executable, "test_calculator.py"],
            "baseline_required": True, "related_acceptance_criteria": ["AC-1"],
        }],
        "execution_plan": [{
            "step_id": "STEP-1", "description": "Correct the operand order, then run V-1.",
            "suggested_tools": ["read_file", "apply_patch", "run_command"],
            "related_acceptance_criteria": ["AC-1"],
        }],
        "clarification_needed": None,
    }


def submit_response(payload, call_id="call-plan"):
    return {
        "role": "assistant", "content": None,
        "tool_calls": [{
            "id": call_id, "type": "function",
            "function": {"name": "submit_plan", "arguments": json.dumps(payload)},
        }],
    }


class FakePlanningClient:
    def __init__(self):
        self.seen_tool_names = []
        self.message_counts = []
        self.responses = iter([
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "list_dir", "arguments": "{\"max_depth\": 3}"},
                }],
            },
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call-2",
                    "type": "function",
                    "function": {
                        "name": "submit_plan",
                        "arguments": __import__("json").dumps({
                            "task_understanding": "Fix divide so total is divided by count.",
                            "acceptance_criteria": [{
                                "id": "AC-1",
                                "description": "divide(10, 2) returns 5",
                                "criticality": "CRITICAL",
                                "verification_mode": "AUTO",
                                "evidence_type": "TARGET",
                                "verification_method": "Run the unittest that asserts divide(10, 2) == 5.",
                            }],
                            "verification_contract": [{
                                "id": "V-1",
                                "description": "Target divide test",
                                "verification_mode": "AUTO",
                                "evidence_type": "TARGET",
                                "verification_method": "Run test_calculator.py and require exit code 0.",
                                "command": [sys.executable, "test_calculator.py"],
                                "baseline_required": True,
                                "related_acceptance_criteria": ["AC-1"],
                            }],
                            "execution_plan": [{
                                "step_id": "STEP-1",
                                "description": "Correct the operand order, then run V-1.",
                                "suggested_tools": ["read_file", "apply_patch", "run_command"],
                                "related_acceptance_criteria": ["AC-1"],
                            }],
                            "clarification_needed": None,
                        }),
                    },
                }],
            },
        ])

    def complete(self, messages, tool_schemas=None):
        self.message_counts.append(len(messages))
        self.seen_tool_names = [schema["function"]["name"] for schema in tool_schemas]
        return next(self.responses)


class PlanningTests(unittest.TestCase):
    def test_planning_creates_structured_state_and_failed_baseline(self):
        root = Path(__file__).parent / "demo_project"
        client = FakePlanningClient()
        state = run_planning(
            "Fix the divide bug", WorkspaceTools(root), client, 3
        )

        self.assertEqual(state.original_task, "Fix the divide bug")
        self.assertEqual(state.acceptance_criteria[0].id, "AC-1")
        self.assertEqual(state.current_step, "STEP-1")
        self.assertNotIn("write_file", client.seen_tool_names)
        self.assertIn("submit_plan", client.seen_tool_names)
        self.assertEqual(client.message_counts, [2, 2])
        result = state.baseline[0]["observation"]
        self.assertNotEqual(result["exit_code"], 0)
        self.assertIn("AssertionError", result["stderr"])

    def test_valid_plan_does_not_trigger_repair(self):
        client = SequenceClient([submit_response(valid_plan())])
        state = run_planning("Fix divide", WorkspaceTools(Path(__file__).parent / "demo_project"), client, 1)
        self.assertEqual(state.planning_validation_failures, 0)
        self.assertEqual(state.planning_repair_attempts, 0)
        self.assertFalse(state.planning_repair_success)

    def test_missing_verification_method_repairs_successfully(self):
        invalid = valid_plan()
        del invalid["verification_contract"][0]["verification_method"]
        client = SequenceClient([submit_response(invalid), submit_response(valid_plan(), "repair-1")])
        state = run_planning("Fix divide", WorkspaceTools(Path(__file__).parent / "demo_project"), client, 1)
        self.assertEqual(state.planning_validation_failures, 1)
        self.assertEqual(state.planning_repair_attempts, 1)
        self.assertTrue(state.planning_repair_success)
        self.assertIn("$.verification_contract[0].verification_method: missing required field", client.messages[1][-1]["content"])
        self.assertIn("do not change the meaning or IDs", client.messages[1][-1]["content"])

    def test_two_invalid_repairs_end_planning(self):
        invalid = valid_plan()
        del invalid["verification_contract"][0]["verification_method"]
        client = SequenceClient([
            submit_response(invalid),
            submit_response(invalid, "repair-1"),
            submit_response(invalid, "repair-2"),
        ])
        with self.assertRaisesRegex(RuntimeError, "repair failed after 2 attempts"):
            run_planning("Fix divide", WorkspaceTools(Path(__file__).parent / "demo_project"), client, 1)
        self.assertEqual(len(client.messages), 3)


class SequenceClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.messages = []

    def complete(self, messages, tool_schemas=None):
        self.messages.append(messages)
        return next(self.responses)


if __name__ == "__main__":
    unittest.main()
