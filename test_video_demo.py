import io
import os
import unittest
from pathlib import Path

from rich.console import Console

from demo_video_cli import load_project_env, run_csv_demo, run_recovery_demo


class VideoDemoTests(unittest.TestCase):
    @staticmethod
    def console():
        stream = io.StringIO()
        return Console(file=stream, force_terminal=False, color_system=None, width=100), stream

    def test_csv_demo_reaches_real_done_and_hidden_pass(self):
        console, stream = self.console()
        result = run_csv_demo(console)

        self.assertEqual(result["agent_final_status"], "DONE")
        self.assertEqual(result["verification_status"], "VERIFIED")
        self.assertEqual(result["hidden_evaluator"], "PASS")
        self.assertFalse(result["false_success"])
        self.assertEqual(result["checkpoint"], "checkpoint_001")
        self.assertNotIn("hidden_tests", os.listdir(result["workspace"]))
        rendered = stream.getvalue()
        self.assertIn("Verification Contract 已冻结", rendered)
        self.assertIn("FAIL → PASS", rendered)
        self.assertIn("Stable checkpoint: checkpoint_001", rendered)

    def test_recovery_demo_uses_real_regression_undo_and_debug_context(self):
        console, stream = self.console()
        result = run_recovery_demo(console)

        self.assertEqual(result["first_verification"], "REGRESSED")
        self.assertEqual(result["recovery_status"], "UNDONE")
        self.assertTrue(result["debug_context_has_regression_evidence"])
        self.assertEqual(result["agent_final_status"], "DONE")
        self.assertEqual(result["verification_status"], "VERIFIED")
        self.assertEqual(result["hidden_evaluator"], "PASS")
        self.assertFalse(result["false_success"])
        rendered = stream.getvalue()
        self.assertIn("PASS → FAIL", rendered)
        self.assertIn("ChangeSet change_0001 UNDONE", rendered)
        self.assertIn("VERIFYING -- VERIFICATION_REGRESSED --> DEBUGGING", rendered)

    def test_dotenv_loader_does_not_override_process_environment(self):
        env_file = Path(".test_workspaces") / "video_demo.env"
        env_file.parent.mkdir(exist_ok=True)
        env_file.write_text(
            "# demo\nOPENAI_API_KEY=file-key\nOPENAI_MODEL='demo-model'\n",
            encoding="utf-8",
        )
        old_key = os.environ.get("OPENAI_API_KEY")
        old_model = os.environ.pop("OPENAI_MODEL", None)
        os.environ["OPENAI_API_KEY"] = "shell-key"
        try:
            self.assertEqual(load_project_env(env_file), env_file)
            self.assertEqual(os.environ["OPENAI_API_KEY"], "shell-key")
            self.assertEqual(os.environ["OPENAI_MODEL"], "demo-model")
        finally:
            env_file.unlink(missing_ok=True)
            if old_key is None:
                os.environ.pop("OPENAI_API_KEY", None)
            else:
                os.environ["OPENAI_API_KEY"] = old_key
            if old_model is None:
                os.environ.pop("OPENAI_MODEL", None)
            else:
                os.environ["OPENAI_MODEL"] = old_model


if __name__ == "__main__":
    unittest.main()
