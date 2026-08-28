import os
import shutil
import stat
import subprocess
import unittest
import uuid
from pathlib import Path

from agent_state import DEBUGGING, EXECUTING, AgentState
from checkpoint_manager import UNSAFE_TO_UNDO, UNEXPECTED_WORKSPACE_CHANGE
from edit_models import CHECKPOINT_ROLLED_BACK, REGRESSED, UNDONE, UNVERIFIED, VERIFIED
from tool_executor import ToolExecutor


class CheckpointManagerTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).parent / ".test_workspaces"
        base.mkdir(exist_ok=True)
        self.root = base / f"repo_{uuid.uuid4().hex}"
        self.root.mkdir()
        self._git("init")
        self._git("config", "user.name", "Checkpoint Test")
        self._git("config", "user.email", "checkpoint@example.invalid")
        (self.root / "app.py").write_text("def value():\n    return 1\n", encoding="utf-8")
        self._git("add", "--", "app.py")
        self._git("commit", "-m", "initial")

    def tearDown(self):
        shutil.rmtree(self.root, onexc=self._remove_readonly)

    @staticmethod
    def _remove_readonly(function, path, excinfo):
        os.chmod(path, stat.S_IWRITE)
        function(path)

    def _git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.root), *args],
            capture_output=True,
            text=True,
            check=True,
            shell=False,
        )
        return result.stdout.strip()

    def _apply(self, tools: ToolExecutor, state: AgentState, old: str, new: str, intent: str) -> dict:
        return tools.call(state, "apply_patch", {
            "file": "app.py",
            "operation": "replace",
            "intent": intent,
            "symbol": "value",
            "old_block": old,
            "new_block": new,
        })

    def test_checkpoint_lifecycle_undo_and_safe_rollback(self):
        tools = ToolExecutor(self.root)
        manager = tools.checkpoint_manager
        state = AgentState("recover", current_phase=EXECUTING)
        initial = manager.get_current_checkpoint()
        initial_head = self._git("rev-parse", "HEAD")
        self.assertEqual(initial["commit_hash"], initial_head)

        first = self._apply(tools, state, "return 1", "return 2", "first stable change")
        first_change = first["change_set"]
        self.assertEqual(first_change["verification_status"], UNVERIFIED)
        self.assertEqual(self._git("rev-parse", "HEAD"), initial_head)
        self.assertEqual(manager.mark_stable(state, "too early")["status"], "VERIFICATION_REQUIRED")

        manager.update_change_verification(state, first_change["id"], VERIFIED)
        created = manager.mark_stable(state, "verified behavior", "verification_001")
        self.assertEqual(created["status"], "CREATED")
        stable_head = self._git("rev-parse", "HEAD")
        self.assertNotEqual(stable_head, initial_head)
        stable_undo = manager.undo_changeset(state, first_change["id"], "must not undo checkpointed change")
        self.assertEqual(stable_undo["status"], UNSAFE_TO_UNDO)

        regressed = self._apply(tools, state, "return 2", "return 3", "regression")
        manager.update_change_verification(state, regressed["change_set"]["id"], REGRESSED)
        undone = manager.undo_changeset(state, regressed["change_set"]["id"], "regression detected")
        self.assertEqual(undone["status"], UNDONE)
        self.assertIn("return 2", (self.root / "app.py").read_text(encoding="utf-8"))
        self.assertEqual(regressed["change_set"]["verification_status"], REGRESSED)

        covered = self._apply(tools, state, "return 2", "return 3", "covered change")
        latest = self._apply(tools, state, "return 3", "return 4", "later same-file change")
        unsafe = manager.undo_changeset(state, covered["change_set"]["id"], "try old undo")
        self.assertEqual(unsafe["status"], UNSAFE_TO_UNDO)

        (self.root / "manual.txt").write_text("user edit", encoding="utf-8")
        blocked = manager.rollback_last_stable(state, "state confused")
        self.assertEqual(blocked["status"], UNEXPECTED_WORKSPACE_CHANGE)
        (self.root / "manual.txt").unlink()

        rolled_back = manager.rollback_last_stable(state, "state confused")
        self.assertEqual(rolled_back["status"], "ROLLED_BACK")
        self.assertEqual(state.current_phase, DEBUGGING)
        self.assertIn("return 2", (self.root / "app.py").read_text(encoding="utf-8"))
        self.assertEqual(covered["change_set"]["rollback_status"], CHECKPOINT_ROLLED_BACK)
        self.assertEqual(latest["change_set"]["rollback_status"], CHECKPOINT_ROLLED_BACK)

    def test_non_git_workspace_keeps_changeset_undo(self):
        plain = self.root.parent / f"plain_{uuid.uuid4().hex}"
        plain.mkdir()
        try:
            (plain / "app.py").write_text("value = 1\n", encoding="utf-8")
            tools = ToolExecutor(plain)
            state = AgentState("plain", current_phase=EXECUTING)
            applied = tools.call(state, "apply_patch", {
                "file": "app.py",
                "operation": "replace",
                "intent": "plain edit",
                "old_block": "value = 1",
                "new_block": "value = 2",
            })
            self.assertFalse(tools.checkpoint_manager.checkpoint_available)
            undone = tools.checkpoint_manager.undo_changeset(state, applied["change_set"]["id"], "local recovery")
            self.assertEqual(undone["status"], UNDONE)
            self.assertEqual((plain / "app.py").read_text(encoding="utf-8"), "value = 1\n")
        finally:
            shutil.rmtree(plain, onexc=self._remove_readonly)

    def test_dirty_git_workspace_refuses_initial_checkpoint(self):
        (self.root / "manual.txt").write_text("uncommitted", encoding="utf-8")
        tools = ToolExecutor(self.root)
        self.assertFalse(tools.checkpoint_manager.checkpoint_available)
        self.assertIn("dirty", tools.checkpoint_manager.unavailable_reason)


if __name__ == "__main__":
    unittest.main()
