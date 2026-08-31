import unittest
from pathlib import Path
from unittest.mock import patch

from agent_state import AcceptanceCriterion, AgentState, ExecutionStep, VerificationCheck, EXECUTING
from agent_events import AgentEvent, PLAN_READY
from agent_orchestrator import AgentOrchestrator
from coding_agent import _capture_baseline, _record_tool_event
from context_manager import ContextManager
from tool_executor import ToolExecutor


def criterion(identifier):
    return AcceptanceCriterion(
        identifier, identifier, "CRITICAL", "AUTO", "TARGET",
        f"Run verification for {identifier} and require pass",
    )


def check(identifier, criteria, baseline=False):
    return VerificationCheck(
        identifier, identifier, "AUTO", "TARGET",
        f"Run {identifier} and require exit code 0", ["verify"], baseline, criteria,
    )


class SuccessfulTools:
    def call(self, state, name, arguments):
        return {"status": "SUCCESS", "exit_code": 0, "stdout": "ok", "stderr": ""}


class ExecutionProgressTests(unittest.TestCase):
    def test_pre_edit_verify_step_is_consumed_when_baseline_already_exists(self):
        from coding_agent import _complete_baseline_owned_verify_steps

        state = AgentState("baseline-owned verification")
        state.execution_plan = [
            ExecutionStep("V", "capture baseline", "VERIFY", ["run_command"], ["AC"], [], ["VC"]),
            ExecutionStep("I", "implement", "IMPLEMENT", ["apply_patch"], ["AC"], ["x.py"], []),
        ]
        state.current_step = "V"
        state.baseline = [{"verification_id": "VC", "observation": {"status": "FAILED"}}]
        AgentOrchestrator().transition(state, AgentEvent(PLAN_READY, "test plan ready"))

        _complete_baseline_owned_verify_steps(state)

        self.assertEqual(state.completed_steps, ["V"])
        self.assertEqual(state.current_step, "I")

    def test_post_edit_verify_step_is_delegated_to_final_engine(self):
        from coding_agent import _complete_baseline_owned_verify_steps

        state = AgentState("core-owned final verification")
        state.execution_plan = [
            ExecutionStep("V", "final checks", "VERIFY", ["finish"], ["AC"], [], ["VC"]),
        ]
        state.current_step = "V"
        state.verification_contract = [check("VC", ["AC"], baseline=True)]
        state.change_sets = [{"id": "change_0001"}]
        AgentOrchestrator().transition(state, AgentEvent(PLAN_READY, "test plan ready"))

        _complete_baseline_owned_verify_steps(state)

        self.assertEqual(state.completed_steps, ["V"])
        self.assertIsNone(state.current_step)

    def make_state(self):
        return AgentState(
            "fix parser",
            current_phase=EXECUTING,
            acceptance_criteria=[criterion("AC_IMPL"), criterion("AC_VERIFY")],
            verification_contract=[
                check("V_BASE", ["AC_IMPL"], True),
                check("V_TARGET", ["AC_VERIFY"]),
            ],
            execution_plan=[
                ExecutionStep("STEP_1", "run baseline", "VERIFY", ["run_command"], ["AC_IMPL"], [], ["V_BASE"]),
                ExecutionStep("STEP_2", "modify parser", "IMPLEMENT", ["apply_patch"], ["AC_IMPL"], ["calculator.py"], []),
                ExecutionStep("STEP_3", "run target checks", "VERIFY", ["run_command"], ["AC_VERIFY"], [], ["V_TARGET"]),
            ],
            current_step="STEP_1",
        )

    def test_successful_baseline_advances_to_implementation_step(self):
        state = self.make_state()
        _capture_baseline(state, SuccessfulTools())
        self.assertEqual(state.completed_steps, ["STEP_1"])
        self.assertEqual(state.current_step, "STEP_2")

    def test_applied_patch_binds_then_completes_current_step(self):
        state = self.make_state()
        state.complete_current_step()
        root = Path(__file__).parent / "demo_project"
        tools = ToolExecutor(root)
        source = (root / "calculator.py").read_text(encoding="utf-8")
        with patch.object(Path, "read_text", side_effect=[source, source]), patch.object(Path, "write_text"):
            result = tools.call(state, "apply_patch", {
                "file": "calculator.py", "operation": "replace", "intent": "fix divide",
                "old_block": "return count / total", "new_block": "return total / count",
            })
        _record_tool_event(state, "apply_patch", {}, result, [], allow_phase_changes=False)
        self.assertEqual(result["change_set"]["step_id"], "STEP_2")
        self.assertEqual(state.completed_steps, ["STEP_1", "STEP_2"])
        self.assertEqual(state.current_step, "STEP_3")

    def test_execution_context_shows_progress_changes_and_completion_nudge(self):
        state = self.make_state()
        state.completed_steps = ["STEP_1", "STEP_2"]
        state.current_step = "STEP_3"
        state.change_sets = [{
            "id": "change_0001", "file": "parser.py", "symbol": "parse_line",
            "intent": "handle blanks", "apply_status": "APPLIED",
            "verification_status": "UNVERIFIED", "rollback_status": "NONE", "step_id": "STEP_2",
        }]
        state.relevant_files = ["parser.py"]
        with patch.object(Path, "read_text", return_value="def parse_line(line): return None\n"):
            content = ContextManager(Path(__file__).parent).build_messages(state, "prompt")[1]["content"]
        self.assertIn("## Completed Steps", content)
        self.assertIn("## Current Step", content)
        self.assertIn("## Applied ChangeSets", content)
        self.assertIn('"related_step": "STEP_2"', content)
        self.assertIn("## Pending Verification Items", content)
        self.assertIn("## Completion Nudge", content)
        self.assertIn("def parse_line", content)

    def test_completion_nudge_does_not_fire_with_remaining_edit_step(self):
        state = self.make_state()
        state.completed_steps = ["STEP_1"]
        state.current_step = "STEP_2"
        state.change_sets = [{
            "id": "change_0001", "apply_status": "APPLIED", "rollback_status": "NONE",
            "verification_status": "UNVERIFIED", "step_id": "STEP_1",
        }]
        content = ContextManager(Path(__file__).parent).build_messages(state, "prompt")[1]["content"]
        self.assertNotIn("## Completion Nudge", content)

    def test_implementation_kind_is_not_completed_by_suggested_or_used_command(self):
        state = self.make_state()
        state.completed_steps = ["STEP_1"]
        state.current_step = "STEP_2"
        result = {"status": "SUCCESS", "exit_code": 0, "stdout": "ok", "stderr": ""}
        _record_tool_event(state, "run_command", {"command": ["pytest"]}, result, [], False)
        self.assertEqual(state.current_step, "STEP_2")
        self.assertNotIn("STEP_2", state.completed_steps)

    def test_verify_step_requires_its_bound_check_command(self):
        state = self.make_state()
        state.completed_steps = ["STEP_1", "STEP_2"]
        state.current_step = "STEP_3"
        result = {"status": "SUCCESS", "exit_code": 0, "stdout": "ok", "stderr": ""}
        _record_tool_event(state, "run_command", {"command": ["unrelated"]}, result, [], False)
        self.assertEqual(state.current_step, "STEP_3")
        _record_tool_event(state, "run_command", {"command": ["verify"]}, result, [], False)
        self.assertIsNone(state.current_step)

    def test_inspect_kind_completes_after_successful_read(self):
        state = self.make_state()
        state.execution_plan = [ExecutionStep("STEP_I", "inspect parser", "INSPECT", ["read_file"], ["AC_IMPL"])]
        state.current_step = "STEP_I"
        result = {"status": "SUCCESS", "path": "parser.py", "content": "source"}
        _record_tool_event(state, "read_file", {"path": "parser.py"}, result, [], False)
        self.assertEqual(state.completed_steps, ["STEP_I"])
        self.assertIsNone(state.current_step)


if __name__ == "__main__":
    unittest.main()
