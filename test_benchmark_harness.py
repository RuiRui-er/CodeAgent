import json
import shutil
import stat
import unittest
from pathlib import Path

from benchmarks.benchmark_evaluator import evaluate_hidden
from benchmarks.benchmark_runner import RUNS_ROOT, TASKS_ROOT, load_task, reset_task_repo, run_benchmark


PYTHON = __import__("sys").executable


def remove_tree(path):
    def handle_readonly(function, target, _error):
        Path(target).chmod(stat.S_IWRITE)
        function(target)

    if path.exists():
        shutil.rmtree(path, onerror=handle_readonly)


class BenchmarkHarnessTests(unittest.TestCase):
    def setUp(self):
        self.sandbox = Path(".test_workspaces") / "benchmark_harness"
        remove_tree(self.sandbox)
        self.sandbox.mkdir(parents=True)

    def tearDown(self):
        remove_tree(self.sandbox)

    def test_all_task_resets_are_reproducible_and_hidden_checks_start_failing(self):
        task_dirs = sorted(path for path in TASKS_ROOT.iterdir() if path.is_dir())
        self.assertGreaterEqual(len(task_dirs), 4)
        for task_dir in task_dirs:
            task = load_task(task_dir)
            workspace = self.sandbox / task.task_id
            initial = reset_task_repo(task_dir, workspace, task.initial_commit)
            if task.initial_commit == "AUTO_DETERMINISTIC":
                repeated = reset_task_repo(task_dir, workspace, task.initial_commit)
                self.assertEqual(initial, repeated)
            else:
                self.assertEqual(initial, task.initial_commit)
            self.assertFalse(any(path.name == "__pycache__" for path in workspace.rglob("__pycache__")))
            self.assertFalse(any(workspace.rglob("*.pyc")))
            self.assertFalse(evaluate_hidden(task_dir, workspace, task).success)
            self.assertFalse((workspace / "hidden_tests").exists())

    def test_hidden_evaluator_accepts_a_correct_fix(self):
        task_dir = TASKS_ROOT / "bug_001"
        task = load_task(task_dir)
        workspace = self.sandbox / task.task_id
        reset_task_repo(task_dir, workspace, task.initial_commit)
        (workspace / "stats.py").write_text(
            "def average(values):\n    return sum(values) / len(values) if values else 0\n",
            encoding="utf-8",
        )
        self.assertTrue(evaluate_hidden(task_dir, workspace, task).success)

    def test_reserved_ablation_fails_fast(self):
        with self.assertRaisesRegex(ValueError, "reserved"):
            run_benchmark("bug_001", "baseline_full_history", 1, False, True)

    def test_full_smoke_writes_evidence_and_reports_no_false_success(self):
        before = set(RUNS_ROOT.glob("bug_001_full_*")) if RUNS_ROOT.exists() else set()
        results = run_benchmark("bug_001", "full", 1, True, True)
        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertFalse(result.hidden_success)
        self.assertFalse(result.false_success)
        created = set(RUNS_ROOT.glob("bug_001_full_*")) - before
        self.assertEqual(len(created), 1)
        run_dir = created.pop()
        for name in ("trajectory.json", "agent_log.txt", "final_state.json", "result.json", "failure_summary.json", "trace_summary.txt"):
            self.assertTrue((run_dir / name).exists(), name)
        payload = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["variant"], "full")
        remove_tree(run_dir)


if __name__ == "__main__":
    unittest.main()
