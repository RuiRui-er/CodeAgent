import sys
import json
import unittest
from pathlib import Path

from agent_state import AgentState
from coding_agent import PLANNING_PROMPT, WorkspaceTools, _repair_plan, _validate_plan, run_planning
from planning_schema import project_repair_fields
from context_manager import ContextManager
from planning_schema import PlanningSchemaError


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
            "step_kind": "IMPLEMENT",
            "suggested_tools": ["read_file", "apply_patch", "run_command"],
            "related_acceptance_criteria": ["AC-1"],
            "expected_change_files": ["calculator.py"],
            "related_verification_ids": [],
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
                                "step_kind": "IMPLEMENT",
                                "suggested_tools": ["read_file", "apply_patch", "run_command"],
                                "related_acceptance_criteria": ["AC-1"],
                                "expected_change_files": ["calculator.py"],
                                "related_verification_ids": [],
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
    def test_demo_profile_requires_baseline_and_compact_plan(self):
        payload = valid_plan()
        payload["verification_contract"][0]["baseline_required"] = False
        payload["execution_plan"] = payload["execution_plan"] * 5
        for index, step in enumerate(payload["execution_plan"]):
            step = dict(step)
            step["step_id"] = f"STEP-{index}"
            payload["execution_plan"][index] = step
        state = AgentState("strict demo profile")
        state.require_all_baselines = True
        state.max_execution_plan_steps = 4

        with self.assertRaises(PlanningSchemaError) as raised:
            _validate_plan(payload, state)

        self.assertIn("baseline_required", str(raised.exception))
        self.assertIn("at most 4 concise steps", str(raised.exception))

    def test_projection_deletes_unexpected_field_when_repair_omits_it(self):
        original = valid_plan()
        original["verification_contract"][0]["additionalProperties"] = False
        repaired = valid_plan()

        projected = project_repair_fields(
            original,
            repaired,
            "$.verification_contract[0].additionalProperties: unexpected field",
        )

        self.assertNotIn("additionalProperties", projected["verification_contract"][0])

    def test_run_can_require_sanity_criterion_and_check(self):
        state = AgentState("demo requiring sanity")
        state.required_evidence_types = {"SANITY"}

        with self.assertRaisesRegex(PlanningSchemaError, "requires a SANITY criterion"):
            _validate_plan(valid_plan(), state)

    def test_demo_can_require_compile_based_sanity(self):
        payload = valid_plan()
        payload["acceptance_criteria"].append({
            "id": "AC-S", "description": "Python module compiles",
            "criticality": "NON_CRITICAL", "verification_mode": "AUTO",
            "evidence_type": "SANITY", "verification_method": "Run py_compile and require exit code 0.",
        })
        payload["verification_contract"].append({
            "id": "V-S", "description": "compile", "verification_mode": "AUTO",
            "evidence_type": "SANITY", "verification_method": "Import module and require success.",
            "command": [sys.executable, "-c", "import subprocess; subprocess.run(['echo'], check=True)"],
            "baseline_required": True, "related_acceptance_criteria": ["AC-S"],
        })
        payload["execution_plan"][0]["related_acceptance_criteria"].append("AC-S")
        state = AgentState("compile sanity required")
        state.required_sanity_command_fragment = "py_compile"

        with self.assertRaisesRegex(PlanningSchemaError, "must contain 'py_compile'"):
            _validate_plan(payload, state)

    def test_planning_prompt_matches_user_language_for_visible_text(self):
        self.assertIn("same language as the user's task", PLANNING_PROMPT)
        self.assertIn("For a Chinese task", PLANNING_PROMPT)
        self.assertIn("TARGET, REGRESSION, SANITY", PLANNING_PROMPT)

    def test_hollow_python_verification_probe_is_rejected(self):
        payload = valid_plan()
        payload["verification_contract"][0]["command"] = [
            sys.executable, "-c", "import subprocess, sys, tempfile; import calculator",
        ]

        with self.assertRaisesRegex(PlanningSchemaError, "must execute the product behavior"):
            _validate_plan(payload, AgentState("reject hollow evidence"))

    def test_unparseable_submit_plan_arguments_get_a_full_schema_retry(self):
        malformed = {
            "role": "assistant", "content": None,
            "tool_calls": [{
                "id": "broken", "type": "function",
                "function": {"name": "submit_plan", "arguments": '{"task_understanding":'},
            }],
        }
        client = SequenceClient([malformed, submit_response(valid_plan(), "json-repair")])

        state = run_planning(
            "Fix divide", WorkspaceTools(Path(__file__).parent / "demo_project"), client, 1,
        )

        self.assertTrue(state.planning_repair_success)
        self.assertEqual(state.planning_repair_attempts, 1)
        self.assertEqual(state.acceptance_criteria[0].id, "AC-1")

    def test_human_verification_is_explicitly_disabled_without_resume_channel(self):
        payload = valid_plan()
        payload["acceptance_criteria"][0]["verification_mode"] = "HUMAN"
        payload["verification_contract"][0]["verification_mode"] = "HUMAN"
        payload["verification_contract"][0]["command"] = None
        with self.assertRaisesRegex(PlanningSchemaError, "HUMAN verification is disabled"):
            _validate_plan(payload, AgentState("human-only plan"))
        client = SequenceClient([submit_response(payload)])
        with self.assertRaisesRegex(RuntimeError, "HUMAN verification is disabled"):
            run_planning(
                "human-only plan", WorkspaceTools(Path(__file__).parent / "demo_project"), client, 1,
            )
        self.assertEqual(len(client.messages), 1)

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

    def test_invalid_criterion_verification_method_can_be_repaired_without_semantic_drift(self):
        invalid = valid_plan()
        invalid["acceptance_criteria"][0]["verification_method"] = "verify feature works"
        repaired = valid_plan()
        original_core = {
            field: invalid["acceptance_criteria"][0][field]
            for field in ("id", "description", "criticality", "verification_mode", "evidence_type")
        }
        client = SequenceClient([submit_response(invalid), submit_response(repaired, "repair-method")])

        state = run_planning("Fix divide", WorkspaceTools(Path(__file__).parent / "demo_project"), client, 1)

        criterion = state.acceptance_criteria[0]
        self.assertEqual(criterion.verification_method, repaired["acceptance_criteria"][0]["verification_method"])
        self.assertEqual(
            {field: getattr(criterion, field) for field in original_core},
            original_core,
        )
        self.assertTrue(state.planning_frozen)

    def test_two_invalid_fields_can_be_repaired_in_one_response(self):
        invalid = valid_plan()
        invalid["acceptance_criteria"][0]["verification_method"] = "verify feature works"
        del invalid["verification_contract"][0]["verification_method"]
        repaired = valid_plan()
        client = SequenceClient([submit_response(invalid), submit_response(repaired, "repair-two")])

        state = run_planning("Fix divide", WorkspaceTools(Path(__file__).parent / "demo_project"), client, 1)

        repair_prompt = client.messages[1][-1]["content"]
        self.assertIn("$.acceptance_criteria[0].verification_method", repair_prompt)
        self.assertIn("$.verification_contract[0].verification_method", repair_prompt)
        self.assertTrue(state.planning_repair_success)
        self.assertTrue(state.planning_frozen)

    def test_multi_field_repair_discards_unrequested_field_change(self):
        invalid = valid_plan()
        invalid["acceptance_criteria"][0]["verification_method"] = "verify feature works"
        del invalid["verification_contract"][0]["verification_method"]
        drifted = valid_plan()
        drifted["execution_plan"][0]["description"] = "Unrelated rewritten step"
        client = SequenceClient([submit_response(invalid), submit_response(drifted, "repair-extra")])

        state = run_planning("Fix divide", WorkspaceTools(Path(__file__).parent / "demo_project"), client, 1)

        self.assertEqual(state.execution_plan[0].description, invalid["execution_plan"][0]["description"])
        self.assertTrue(state.planning_repair_success)

    def test_repair_discards_unrelated_core_semantic_change(self):
        invalid = valid_plan()
        invalid["acceptance_criteria"][0]["verification_method"] = "verify feature works"
        drifted = valid_plan()
        drifted["acceptance_criteria"][0]["description"] = "Different behavior"
        client = SequenceClient([submit_response(invalid), submit_response(drifted, "repair-drift")])

        state = run_planning("Fix divide", WorkspaceTools(Path(__file__).parent / "demo_project"), client, 1)

        self.assertEqual(state.acceptance_criteria[0].description, invalid["acceptance_criteria"][0]["description"])
        self.assertTrue(state.planning_repair_success)

    def test_frozen_plan_cannot_enter_repair_again(self):
        client = SequenceClient([submit_response(valid_plan())])
        root = Path(__file__).parent / "demo_project"
        state = run_planning("Fix divide", WorkspaceTools(root), client, 1)

        with self.assertRaisesRegex(PlanningSchemaError, "already frozen"):
            _repair_plan(
                valid_plan(),
                "$.acceptance_criteria[0].verification_method: invalid",
                state,
                SequenceClient([]),
                ContextManager(root),
            )

    def test_two_invalid_repairs_end_planning(self):
        invalid = valid_plan()
        del invalid["verification_contract"][0]["verification_method"]
        client = SequenceClient([
            submit_response(invalid),
            submit_response(invalid, "repair-1"),
            submit_response(invalid, "repair-2"),
            submit_response(invalid, "repair-3"),
        ])
        with self.assertRaisesRegex(RuntimeError, "repair failed after 3 attempts"):
            run_planning("Fix divide", WorkspaceTools(Path(__file__).parent / "demo_project"), client, 1)
        self.assertEqual(len(client.messages), 4)

    def test_malformed_field_repair_keeps_original_repair_paths(self):
        invalid = valid_plan()
        del invalid["verification_contract"][0]["verification_method"]
        malformed = {
            "role": "assistant", "content": None,
            "tool_calls": [{
                "id": "bad-repair", "type": "function",
                "function": {"name": "submit_plan", "arguments": '{"verification_contract":'},
            }],
        }
        client = SequenceClient([
            submit_response(invalid), malformed, submit_response(valid_plan(), "good-repair"),
        ])

        state = run_planning(
            "Fix divide", WorkspaceTools(Path(__file__).parent / "demo_project"), client, 1,
        )

        self.assertTrue(state.planning_repair_success)
        self.assertIn("invalid tool arguments", client.messages[2][-1]["content"])

    def test_last_planning_turn_requires_plan_submission(self):
        explore = {
            "role": "assistant", "content": None,
            "tool_calls": [{
                "id": "inspect", "type": "function",
                "function": {"name": "read_file", "arguments": json.dumps({"path": "calculator.py"})},
            }],
        }
        client = SequenceClient([explore, submit_response(valid_plan())])
        state = run_planning(
            "Fix divide", WorkspaceTools(Path(__file__).parent / "demo_project"), client, 2
        )
        self.assertEqual(state.current_step, "STEP-1")
        self.assertIn("read_file", client.tool_names[0])
        self.assertEqual(client.tool_names[1], ["submit_plan"])
        self.assertIn("final PLANNING turn", client.messages[1][0]["content"])

    def test_final_turn_wrong_tool_is_replaced_by_full_plan_retry(self):
        wrong_tool = {
            "role": "assistant", "content": None,
            "tool_calls": [{
                "id": "wrong", "type": "function",
                "function": {"name": "read_file", "arguments": json.dumps({"path": "calculator.py"})},
            }],
        }
        client = SequenceClient([wrong_tool, submit_response(valid_plan(), "retry-plan")])

        state = run_planning(
            "Fix divide", WorkspaceTools(Path(__file__).parent / "demo_project"), client, 1,
        )

        self.assertTrue(state.planning_repair_success)
        self.assertEqual(state.current_step, "STEP-1")

    def test_repeated_reads_get_nudge_without_losing_read_tools(self):
        read = {
            "role": "assistant", "content": None,
            "tool_calls": [{
                "id": "read", "type": "function",
                "function": {"name": "read_file", "arguments": json.dumps({"path": "calculator.py"})},
            }],
        }
        client = SequenceClient([read, read, read, submit_response(valid_plan())])
        state = run_planning(
            "Fix divide", WorkspaceTools(Path(__file__).parent / "demo_project"), client, 5
        )
        self.assertEqual(state.current_step, "STEP-1")
        self.assertIn("did not add a new relevant file", client.messages[3][0]["content"])
        self.assertIn("read_file", client.tool_names[3])
        self.assertIn("submit_plan", client.tool_names[3])


class SequenceClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.messages = []

    def complete(self, messages, tool_schemas=None):
        self.messages.append(messages)
        self.tool_names = getattr(self, "tool_names", [])
        self.tool_names.append([schema["function"]["name"] for schema in tool_schemas])
        return next(self.responses)


if __name__ == "__main__":
    unittest.main()
