"""Local, repeatable benchmark runner that leaves production Agent defaults untouched."""

from __future__ import annotations

import argparse
import contextlib
import csv
import io
import json
import math
import os
import shutil
import subprocess
import sys
import uuid
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent_events import MAX_STEPS_REACHED, PLAN_BLOCKED_BY_USER_INTENT, PLAN_READY, UNRECOVERABLE_FAILURE, AgentEvent
from agent_orchestrator import AgentOrchestrator
from agent_state import FAILED, PLANNING, AgentState
from benchmarks.benchmark_evaluator import build_failure_summary, evaluate_hidden
from benchmarks.benchmark_models import RunResult, TaskSpec, TrajectoryRecorder, VARIANTS
from coding_agent import OpenAICompatibleClient, run_execution, run_planning
from tool_executor import ToolExecutor


BENCHMARK_ROOT = Path(__file__).resolve().parent
TASKS_ROOT = BENCHMARK_ROOT / "tasks"
RUNS_ROOT = BENCHMARK_ROOT / "runs"
FIXED_GIT_DATE = "2000-01-01T00:00:00+00:00"


class RecordingClient:
    def __init__(self, client: OpenAICompatibleClient, recorder: TrajectoryRecorder):
        self.client = client
        self.recorder = recorder

    def complete(self, messages, tool_schemas=None):
        chars = sum(len(item.get("content", "")) for item in messages)
        phase = _phase_from_messages(messages)
        response = self.client.complete(messages, tool_schemas)
        calls = response.get("tool_calls") or []
        action = ",".join(item.get("function", {}).get("name", "") for item in calls) or "MODEL_MESSAGE"
        self.recorder.record_context(chars, phase, action)
        return response


class RecordingToolExecutor(ToolExecutor):
    def __init__(self, root: Path, recorder: TrajectoryRecorder):
        self.recorder = recorder
        super().__init__(root)

    def call(self, state, name, arguments):
        phase = state.current_phase
        result = super().call(state, name, arguments)
        change = result.get("change_set") or {}
        self.recorder.record(
            phase=phase,
            event="TOOL_RESULT",
            tool=name,
            action=name,
            action_target=_action_target(arguments, result),
            result_status=result.get("status"),
            context_chars=None,
            related_criterion=_related_criterion(state),
            related_changeset=change.get("id"),
            verification_status=(state.verification_result or {}).get("overall_status"),
            failure_id=(state.current_failure or {}).get("id"),
            checkpoint_id=(state.current_checkpoint or {}).get("id"),
            transition_result=None,
        )
        return result


def run_benchmark(task_id: str, variant_name: str, runs: int, save_demo_trace: bool, smoke_agent: bool) -> list[RunResult]:
    variant = VARIANTS.get(variant_name)
    if not variant:
        raise ValueError(f"unknown variant: {variant_name}")
    if not variant.implemented:
        raise ValueError(f"variant {variant_name} is reserved for a later ablation and is not implemented")
    task_dir = TASKS_ROOT / task_id
    task = load_task(task_dir)
    results = []
    for index in range(1, runs + 1):
        result = _run_once(task_dir, task, variant, index, save_demo_trace, smoke_agent)
        results.append(result)
    write_summary(results, variant_name)
    return results


def load_task(task_dir: Path) -> TaskSpec:
    payload = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
    return TaskSpec(**payload)


def _run_once(task_dir: Path, task: TaskSpec, variant, run_number: int, save_demo_trace: bool, smoke_agent: bool) -> RunResult:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_id = f"{task.task_id}_{variant.name}_{timestamp}_{run_number:02d}_{uuid.uuid4().hex[:8]}"
    run_dir = RUNS_ROOT / run_id
    run_dir.mkdir(parents=True)
    workspace = run_dir / "workspace"
    initial_commit = reset_task_repo(task_dir, workspace, task.initial_commit)
    recorder = TrajectoryRecorder()
    log_buffer = io.StringIO()
    state = AgentState(task.description)
    termination_reason = ""

    with contextlib.redirect_stdout(log_buffer), contextlib.redirect_stderr(log_buffer):
        if smoke_agent:
            recorder.record_context(0, PLANNING, "SMOKE_NOOP")
            orchestrator = AgentOrchestrator()
            orchestrator.transition(state, AgentEvent(MAX_STEPS_REACHED, "benchmark infrastructure smoke agent"))
            termination_reason = "benchmark infrastructure smoke agent intentionally made no code changes"
        else:
            orchestrator = AgentOrchestrator()
            try:
                tools = RecordingToolExecutor(workspace, recorder)
                client = RecordingClient(OpenAICompatibleClient(), recorder)
                state = run_planning(task.description, tools, client, max_planning_steps=8)
                if state.clarification_needed:
                    orchestrator.transition(state, AgentEvent(
                        PLAN_BLOCKED_BY_USER_INTENT, state.clarification_needed
                    ))
                    termination_reason = f"clarification required: {state.clarification_needed}"
                else:
                    orchestrator.transition(state, AgentEvent(PLAN_READY, "benchmark plan ready"))
                    termination_reason = run_execution(state, tools, client, max_steps=20, orchestrator=orchestrator)
            except Exception as exc:
                termination_reason = f"{type(exc).__name__}: {exc}"
                if state.current_phase not in {"DONE", "FAILED"}:
                    orchestrator.transition(state, AgentEvent(UNRECOVERABLE_FAILURE, termination_reason))

    for transition in state.phase_history:
        recorder.record(
            phase=transition["from"],
            event=transition["event"],
            tool=None,
            action=None,
            action_target=None,
            result_status=None,
            context_chars=None,
            related_criterion=None,
            related_changeset=None,
            verification_status=(state.verification_result or {}).get("overall_status"),
            failure_id=None,
            checkpoint_id=(state.current_checkpoint or {}).get("id"),
            transition_result=transition,
        )
    for failure in state.failure_history:
        recorder.record(
            phase=failure.get("phase"),
            event="FAILURE_EVIDENCE",
            tool=None,
            action=failure.get("action"),
            action_target=failure.get("target"),
            result_status="FAILED",
            context_chars=None,
            related_criterion=failure.get("related_criterion"),
            related_changeset=failure.get("related_changeset"),
            verification_status=(state.verification_result or {}).get("overall_status"),
            failure_id=failure.get("id"),
            checkpoint_id=(state.current_checkpoint or {}).get("id"),
            transition_result=None,
        )
    for change in state.change_sets:
        rollback_status = change.get("rollback_status")
        if rollback_status in {"UNDONE", "CHECKPOINT_ROLLED_BACK"}:
            recorder.record(
                phase=state.current_phase,
                event="CHANGESET_UNDO" if rollback_status == "UNDONE" else "CHECKPOINT_ROLLBACK",
                tool=None,
                action="rollback",
                action_target=change.get("file"),
                result_status=rollback_status,
                context_chars=None,
                related_criterion=None,
                related_changeset=change.get("id"),
                verification_status=(state.verification_result or {}).get("overall_status"),
                failure_id=None,
                checkpoint_id=(state.current_checkpoint or {}).get("id"),
                transition_result=None,
            )

    hidden = evaluate_hidden(task_dir, workspace, task)
    final_state = state.planning_snapshot()
    result = _build_run_result(
        run_id, task, variant, state, recorder, hidden, termination_reason, initial_commit
    )
    _write_json(run_dir / "trajectory.json", recorder.entries)
    _write_json(run_dir / "result.json", result.to_dict())
    _write_json(run_dir / "final_state.json", final_state)
    (run_dir / "agent_log.txt").write_text(log_buffer.getvalue(), encoding="utf-8")
    if not hidden.success or result.false_success:
        _write_json(run_dir / "failure_summary.json", build_failure_summary(final_state, recorder.entries, hidden))
    if save_demo_trace:
        (run_dir / "trace_summary.txt").write_text(build_demo_trace(recorder.entries), encoding="utf-8")
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return result


def reset_task_repo(task_dir: Path, workspace: Path, expected_commit: str) -> str:
    source = task_dir / "repo"
    if workspace.exists():
        shutil.rmtree(workspace)
    shutil.copytree(source, workspace)
    _git(workspace, ["init", "-q"])
    _git(workspace, ["config", "user.name", "CodeAgent Benchmark"])
    _git(workspace, ["config", "user.email", "benchmark@example.invalid"])
    files = sorted(path.relative_to(workspace).as_posix() for path in workspace.rglob("*") if path.is_file() and ".git" not in path.parts)
    _git(workspace, ["add", "--", *files])
    env = os.environ.copy()
    env.update({"GIT_AUTHOR_DATE": FIXED_GIT_DATE, "GIT_COMMITTER_DATE": FIXED_GIT_DATE})
    _git(workspace, ["commit", "-q", "-m", f"benchmark initial state: {task_dir.name}"], env=env)
    commit = _git(workspace, ["rev-parse", "HEAD"]).stdout.strip()
    if expected_commit not in {"AUTO_DETERMINISTIC", commit}:
        raise RuntimeError(f"initial commit mismatch for {task_dir.name}: expected {expected_commit}, got {commit}")
    status = _git(workspace, ["status", "--porcelain"]).stdout
    if status:
        raise RuntimeError(f"task reset left a dirty workspace: {status}")
    return commit


def _build_run_result(run_id, task, variant, state, recorder, hidden, termination_reason, initial_commit):
    contexts = recorder.context_sizes
    changes = state.change_sets
    failures = state.failure_history
    undo_count = sum(item.get("rollback_status") == "UNDONE" for item in changes)
    checkpoint_rollbacks = sum(item.get("rollback_status") == "CHECKPOINT_ROLLED_BACK" for item in changes)
    ambiguous = sum(item.get("result_status") == "AMBIGUOUS_TARGET" for item in recorder.entries)
    stale = sum(item.get("result_status") == "STALE_EDIT" for item in recorder.entries)
    repeated_actions = sum(
        item.get("result_status") == "BLOCKED" and item.get("action_target") == "DUPLICATE_FAILED_ACTION"
        for item in recorder.entries
    )
    verification_status = (state.verification_result or {}).get("overall_status")
    return RunResult(
        run_id=run_id,
        task_id=task.task_id,
        variant=variant.name,
        agent_final_phase=state.current_phase,
        agent_verification_status=verification_status,
        hidden_success=hidden.success,
        false_success=state.current_phase == "DONE" and not hidden.success,
        steps=recorder.llm_calls,
        llm_calls=recorder.llm_calls,
        context_chars_total=sum(contexts),
        context_chars_avg=round(sum(contexts) / len(contexts), 2) if contexts else 0.0,
        context_chars_max=max(contexts, default=0),
        context_chars_p95=_p95(contexts),
        repeated_actions=repeated_actions,
        failure_events=len(failures),
        regressions=sum(item.get("type") == "REGRESSION_DETECTED" for item in failures),
        changeset_undos=undo_count,
        checkpoint_rollbacks=checkpoint_rollbacks,
        successful_recoveries=undo_count + checkpoint_rollbacks,
        replans=sum(item.get("event") == "REPLAN_REQUIRED" for item in state.phase_history),
        final_checkpoint=(state.current_checkpoint or {}).get("id"),
        termination_reason=termination_reason,
        wrong_location_edits=None,
        ambiguous_edit_rejections=ambiguous,
        stale_edit_rejections=stale,
        hidden_evaluation=hidden.to_dict(),
        initial_commit=initial_commit,
        variant_config=asdict(variant),
    )


def write_summary(results: list[RunResult], variant: str) -> None:
    RUNS_ROOT.mkdir(parents=True, exist_ok=True)
    count = len(results)
    summary = {
        "variant": variant,
        "runs": count,
        "task_success_rate": sum(item.hidden_success for item in results) / count,
        "false_success_rate": sum(item.false_success for item in results) / count,
        "avg_steps": sum(item.steps for item in results) / count,
        "avg_llm_calls": sum(item.llm_calls for item in results) / count,
        "avg_context_chars": sum(item.context_chars_avg for item in results) / count,
        "avg_repeated_actions": sum(item.repeated_actions for item in results) / count,
        "regression_runs": sum(item.regressions > 0 for item in results),
        "successful_recoveries": sum(item.successful_recoveries for item in results),
    }
    _write_json(RUNS_ROOT / f"benchmark_summary_{variant}.json", summary)
    with (RUNS_ROOT / f"benchmark_summary_{variant}.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(summary))
        writer.writeheader()
        writer.writerow(summary)


def build_demo_trace(entries: list[dict[str, Any]]) -> str:
    lines = []
    for item in entries:
        label = f"Step {item['step']} {item.get('phase') or ''}".rstrip()
        detail = item.get("event") or item.get("tool") or item.get("action") or ""
        target = item.get("action_target")
        status = item.get("result_status")
        suffix = " | ".join(str(value) for value in (detail, target, status) if value)
        lines.append(f"{label}: {suffix}" if suffix else label)
    return "\n".join(lines) + "\n"


def _git(workspace: Path, arguments: list[str], env=None):
    completed = subprocess.run(
        ["git", "-C", str(workspace), *arguments], capture_output=True, text=True,
        errors="replace", env=env, shell=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip())
    return completed


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _phase_from_messages(messages) -> str | None:
    for item in messages:
        text = item.get("content", "")
        marker = "Current phase: "
        if marker in text:
            return text.split(marker, 1)[1].splitlines()[0].strip()
    return None


def _action_target(arguments, result):
    if result.get("reason") == "DUPLICATE_FAILED_ACTION":
        return "DUPLICATE_FAILED_ACTION"
    return result.get("file") or result.get("path") or arguments.get("file") or arguments.get("path") or result.get("command")


def _related_criterion(state):
    step = next((item for item in state.execution_plan if item.step_id == state.current_step), None)
    return step.related_acceptance_criteria[0] if step and step.related_acceptance_criteria else None


def _p95(values: list[int]) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]


def parse_args():
    parser = argparse.ArgumentParser(description="Run isolated CodeAgent benchmarks")
    parser.add_argument("--task", required=True, help="Task id under benchmarks/tasks")
    parser.add_argument("--variant", default="full", choices=sorted(VARIANTS))
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--save-demo-trace", action="store_true")
    parser.add_argument("--smoke-agent", action="store_true", help="Exercise infrastructure without an API call or code changes")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.runs < 1:
        print("--runs must be at least 1", file=sys.stderr)
        return 2
    try:
        run_benchmark(args.task, args.variant, args.runs, args.save_demo_trace, args.smoke_agent)
        return 0
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
        print(f"Benchmark error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
