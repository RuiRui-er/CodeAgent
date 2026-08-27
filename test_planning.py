import sys
import unittest
from pathlib import Path

from coding_agent import WorkspaceTools, run_planning


class FakePlanningClient:
    def __init__(self):
        self.seen_tool_names = []
        self.responses = iter([
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "list_files", "arguments": "{\"recursive\": true}"},
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
                                "suggested_tools": ["read_file", "write_file", "run_command"],
                                "related_acceptance_criteria": ["AC-1"],
                            }],
                            "clarification_needed": None,
                        }),
                    },
                }],
            },
        ])

    def complete(self, messages, tool_schemas=None):
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
        result = state.baseline[0]["observation"]["result"]
        self.assertNotEqual(result["exit_code"], 0)
        self.assertIn("AssertionError", result["stderr"])


if __name__ == "__main__":
    unittest.main()
