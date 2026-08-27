import unittest
from pathlib import Path
from unittest.mock import patch

from agent_state import DEBUGGING, EXECUTING, AgentState
from context_manager import ContextManager


class ContextManagerTests(unittest.TestCase):
    def test_relevant_file_is_read_fresh_and_context_is_bounded(self):
        state = AgentState(
            "Check live code",
            current_phase=EXECUTING,
            relevant_files=["live.py"],
        )
        manager = ContextManager(Path(__file__).parent, max_context_chars=800)
        with patch.object(
            Path,
            "read_text",
            side_effect=["def live(): return 1", "def live(): return 2"],
        ):
            first = manager.build_messages(state, "prompt")
            self.assertIn("return 1", first[1]["content"])
            second = manager.build_messages(state, "prompt")
            self.assertIn("return 2", second[1]["content"])
            self.assertNotIn("return 1", second[1]["content"])
            self.assertLessEqual(sum(len(item["content"]) for item in second), 800)

    def test_debugging_selects_failure_state(self):
        state = AgentState("Fix parser", current_phase=DEBUGGING)
        state.failure_evidence.append({"stderr": "SyntaxError"})
        state.failed_attempts.append({"attempt": "run tests", "reason": "SyntaxError"})
        messages = ContextManager(Path(__file__).parent).build_messages(state, "prompt")
        content = messages[1]["content"]
        self.assertIn("## Failure Evidence", content)
        self.assertIn("## Failed Attempts", content)
        self.assertNotIn("## Verification Contract", content)


if __name__ == "__main__":
    unittest.main()
