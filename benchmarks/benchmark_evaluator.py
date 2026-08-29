"""Hidden ground-truth execution and fact-only failure summaries."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

from benchmarks.benchmark_models import HiddenEvaluationResult, TaskSpec


EVALUATOR_OUTPUT_CHARS = 8_000


def evaluate_hidden(task_dir: Path, workspace: Path, task: TaskSpec) -> HiddenEvaluationResult:
    task_dir = task_dir.resolve()
    workspace = workspace.resolve()
    replacements = {
        "{python}": sys.executable,
        "{workspace}": str(workspace),
        "{task_dir}": str(task_dir),
    }
    command = [replacements.get(part, part) for part in task.hidden_test_command]
    try:
        completed = subprocess.run(
            command,
            cwd=task_dir,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=120,
            shell=False,
        )
        return HiddenEvaluationResult(
            completed.returncode == 0,
            command,
            completed.returncode,
            _truncate(completed.stdout),
            _truncate(completed.stderr),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return HiddenEvaluationResult(False, command, None, "", f"{type(exc).__name__}: {exc}")


def build_failure_summary(
    final_state: dict[str, Any],
    trajectory: list[dict[str, Any]],
    hidden: HiddenEvaluationResult,
) -> dict[str, Any]:
    failures = final_state.get("failure_history", [])
    repeated = sorted({
        item.get("fingerprint") for item in failures if item.get("repeat_count", 1) > 1
    } - {None})
    phase_history = final_state.get("phase_history", [])
    return {
        "first_failure_step": _first_failure_step(trajectory),
        "first_failure_type": failures[0].get("type") if failures else None,
        "failed_criteria": (final_state.get("verification_result") or {}).get("failed_critical", []),
        "repeated_fingerprints": repeated,
        "rollback_history": [item for item in trajectory if item.get("event") in {"CHANGESET_UNDO", "CHECKPOINT_ROLLBACK"}],
        "replanning_history": [item for item in phase_history if item.get("event") == "REPLAN_REQUIRED"],
        "final_unmet_criteria": sorted(set(
            (final_state.get("verification_result") or {}).get("failed_critical", [])
            + (final_state.get("verification_result") or {}).get("unverified_critical", [])
        )),
        "hidden_evaluator_failure": None if hidden.success else {
            "exit_code": hidden.exit_code,
            "stdout": hidden.stdout,
            "stderr": hidden.stderr,
        },
    }


def _first_failure_step(trajectory: list[dict[str, Any]]) -> int | None:
    for item in trajectory:
        if item.get("failure_id") or item.get("result_status") in {
            "FAILED", "TIMEOUT", "STALE_EDIT", "AMBIGUOUS_TARGET", "TARGET_NOT_FOUND"
        }:
            return item.get("step")
    return None


def _truncate(text: str) -> str:
    if len(text) <= EVALUATOR_OUTPUT_CHARS:
        return text
    half = (EVALUATOR_OUTPUT_CHARS - 50) // 2
    return text[:half] + "\n[... evaluator output truncated ...]\n" + text[-half:]
