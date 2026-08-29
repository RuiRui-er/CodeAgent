import sys
import unittest
from pathlib import Path

from agent_state import EXECUTING, PLANNING, VERIFYING, AgentState
from agent_events import FINISH_REQUESTED, AgentEvent
from agent_orchestrator import AgentOrchestrator
from tool_executor import ToolExecutor, truncate_command_output
from tool_safety import CONFIRM, DENY, SAFE


class ToolSafetyTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).parent / "demo_project"
        self.tools = ToolExecutor(self.root, confirm_callback=lambda command, reason: False)

    def test_phase_permissions_and_finish_transition(self):
        state = AgentState("test", current_phase=PLANNING)
        read_result = self.tools.call(state, "read_file", {"path": "calculator.py"})
        self.assertEqual(read_result["status"], "SUCCESS")

        blocked = self.tools.call(state, "apply_patch", {"path": "calculator.py", "old_text": "x", "new_text": "y"})
        self.assertEqual(blocked["status"], "BLOCKED")

        state.set_phase(EXECUTING)
        finish = self.tools.call(state, "finish", {"summary": "ready"})
        self.assertEqual(finish["done"], False)
        self.assertEqual(state.current_phase, EXECUTING)
        AgentOrchestrator().transition(state, AgentEvent(FINISH_REQUESTED, "test finish"))
        self.assertEqual(state.current_phase, VERIFYING)
        repeated = self.tools.call(state, "finish", {})
        self.assertEqual(repeated["status"], "BLOCKED")

    def test_command_policy_safe_confirm_and_deny(self):
        safe = self.tools.command_policy.classify([sys.executable, "--version"])
        confirm = self.tools.command_policy.classify(["git", "clean", "-fd"])
        deny = self.tools.command_policy.classify(["shutdown", "/s"])
        self.assertEqual(safe.policy, SAFE)
        self.assertEqual(confirm.policy, CONFIRM)
        self.assertEqual(deny.policy, DENY)

        state = AgentState("test", current_phase=EXECUTING)
        rejected = self.tools.call(state, "run_command", {"command": ["git", "clean", "-fd"]})
        denied = self.tools.call(state, "run_command", {"command": ["shutdown", "/s"]})
        self.assertEqual(rejected["status"], "DENIED")
        self.assertEqual(rejected["reason"], "command rejected by user")
        self.assertEqual(denied["status"], "DENIED")

    def test_workspace_escape_and_output_truncation(self):
        with self.assertRaises(ValueError):
            self.tools.guard.resolve("../outside.txt")
        decision = self.tools.command_policy.classify([sys.executable, "../outside.py"])
        self.assertEqual(decision.policy, DENY)

        text = "head\n" + "x" * 200 + "\ntail"
        shortened, truncated = truncate_command_output(text, 100)
        self.assertTrue(truncated)
        self.assertIn("head", shortened)
        self.assertIn("tail", shortened)
        self.assertIn("omitted", shortened)
        self.assertLessEqual(len(shortened), 100)


if __name__ == "__main__":
    unittest.main()
