"""Reproducible Rich CLI for the CodeAgent two-minute demonstration."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import uuid
import os
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.prompt import Confirm, Prompt

from agent_events import EDIT_APPLIED, FINISH_REQUESTED, PLAN_READY, AgentEvent
from agent_orchestrator import AgentOrchestrator
from benchmarks.benchmark_evaluator import evaluate_hidden
from benchmarks.benchmark_runner import TASKS_ROOT, load_task, reset_task_repo
from coding_agent import (
    OpenAICompatibleClient,
    _record_tool_event,
    _transition_tool_result,
    _transition_verification,
    run_execution,
    run_planning,
)
from context_manager import ContextManager
from failure_recovery import FailureRecovery
from rich_console_renderer import RichConsoleRenderer
from tool_executor import ToolExecutor
from verification_engine import VerificationEngine
from video_demo_fixtures import (
    CSV_TASK,
    RECOVERY_MAIN_BEFORE,
    RECOVERY_MAIN_FIXED,
    RECOVERY_MAIN_REGRESSED,
    ScriptedClient,
    csv_execution_client,
    csv_planning_client,
    recovery_plan,
    tool_response,
)


PROJECT_ROOT = Path(__file__).resolve().parent
DEMO_RUNS = PROJECT_ROOT / ".demo_video_runs"


class RenderingOrchestrator(AgentOrchestrator):
    def __init__(self, renderer: RichConsoleRenderer):
        super().__init__()
        self.renderer = renderer

    def transition(self, state, event):
        result = super().transition(state, event)
        self.renderer.transition(result)
        self.renderer.header(state)
        if result.next_phase == "VERIFYING" and result.previous_phase != "VERIFYING":
            self.renderer.verification_start()
        return result


class RenderingToolExecutor(ToolExecutor):
    def __init__(self, root: Path, renderer: RichConsoleRenderer):
        self.renderer = renderer
        self._rendered_verification_ids: set[str] = set()
        super().__init__(root, confirm_callback=self._confirm_visible)

    def _confirm_visible(self, command: str, reason: str) -> bool:
        self.renderer.console.print("\n[bold yellow]命令需要确认[/bold yellow]")
        self.renderer.console.print(f"[dim]{command}[/dim]")
        self.renderer.console.print(f"原因: {reason}")
        return Confirm.ask("允许执行", default=False, console=self.renderer.console)

    def call(self, state, name, arguments):
        result = super().call(state, name, arguments)
        if name == "apply_patch" and result.get("status") == "APPLIED":
            self.renderer.structured_edit(result)
        elif name == "run_command" and state.current_phase == "VERIFYING":
            check = self._check_for_command(state, arguments.get("command"))
            if check:
                self._rendered_verification_ids.add(check.id)
                baseline = next(
                    (item["observation"] for item in state.baseline if item["verification_id"] == check.id),
                    None,
                )
                self.renderer.verification_item(check, result, baseline)
        return result

    def _check_for_command(self, state, command):
        requested = command if isinstance(command, list) else [str(command)]
        return next((
            check for check in state.verification_contract
            if check.command == requested and check.id not in self._rendered_verification_ids
        ), None)


def load_project_env(path: Path | None = None) -> Path | None:
    """Load a small .env file without adding a runtime dependency.

    Existing process environment variables win, so CI and shell configuration keep
    their usual precedence. Only simple KEY=VALUE entries are supported intentionally.
    """
    env_path = path or (PROJECT_ROOT / ".env")
    if not env_path.is_file():
        return None
    for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value[:1] == value[-1:] and value.startswith(("'", '"')):
            value = value[1:-1]
        if key and key.replace("_", "").isalnum():
            os.environ.setdefault(key, value)
    return env_path


def run_csv_demo(
    console: Console | None = None,
    live_model: bool = False,
    user_task: str | None = None,
) -> dict[str, Any]:
    renderer = RichConsoleRenderer(console)
    task_dir, benchmark_task, workspace = _prepare_workspace("csv_tool_video", "csv_main")
    raw_log = io.StringIO()
    try:
        with contextlib.redirect_stdout(raw_log), contextlib.redirect_stderr(raw_log):
            tools = RenderingToolExecutor(workspace, renderer)
            model_client = OpenAICompatibleClient() if live_model else None
            # The benchmark contract is deliberately frozen and deterministic. In
            # live mode the real model owns implementation decisions, while baseline
            # and final evidence remain identical across recordings.
            planning_client = csv_planning_client()
            execution_client = model_client or csv_execution_client()
            state = run_planning(
                user_task or CSV_TASK, tools, planning_client, max_planning_steps=1,
                required_evidence_types={"SANITY"},
                require_all_baselines=True,
                max_execution_plan_steps=4,
                required_sanity_command_fragment="py_compile",
            )
            renderer.header(state)
            renderer.planning(state)
            _render_baseline(renderer, state)

            orchestrator = RenderingOrchestrator(renderer)
            orchestrator.transition(state, AgentEvent(PLAN_READY, "video plan ready"))
            run_execution(
                state, tools, execution_client, max_steps=20 if live_model else 6,
                orchestrator=orchestrator,
            )

            renderer.verification_summary(state.verification_result or {})
            renderer.checkpoint((state.verification_result or {}).get("checkpoint_result"))
            hidden = evaluate_hidden(task_dir, workspace, benchmark_task)
            renderer.final(state, hidden)
    finally:
        _write_core_log(workspace.parent, raw_log.getvalue())

    result = _result(state, hidden, workspace)
    result["decision_source"] = (
        "FROZEN_DEMO_PLAN+LIVE_MODEL_EXECUTION"
        if live_model else "DETERMINISTIC_MODEL_FIXTURE"
    )
    _write_result(workspace.parent, result)
    return result


def run_recovery_demo(console: Console | None = None) -> dict[str, Any]:
    """Exercise the real recovery pipeline with a deterministic regressive edit.

    This fixture is used because an unconstrained model cannot be guaranteed to choose
    a regression on camera. The first edit is explicitly labelled as fixture input; all
    resulting tool output, evidence, undo, phases, second edit, and verification are real.
    """
    renderer = RichConsoleRenderer(console)
    task_dir, task, workspace = _prepare_workspace("recovery_video", "regression_recovery")
    raw_log = io.StringIO()
    with contextlib.redirect_stdout(raw_log), contextlib.redirect_stderr(raw_log):
        tools = RenderingToolExecutor(workspace, renderer)
        planning_client = ScriptedClient([tool_response("submit_plan", recovery_plan(), "recovery-plan")])
        state = run_planning(task.description, tools, planning_client, max_planning_steps=1)
        renderer.header(state)
        renderer.planning(state)
        _render_baseline(renderer, state)

        orchestrator = RenderingOrchestrator(renderer)
        orchestrator.transition(state, AgentEvent(PLAN_READY, "deterministic recovery fixture ready"))
        failure_recovery = FailureRecovery(tools)
        verification = VerificationEngine(tools, failure_recovery=failure_recovery)

        renderer.decision_summary(
            "Add optional prefix",
            "deterministic fixture injects a plausible unconditional-prefix edit",
            "edit label_tool.py::main",
        )
        first = tools.call(state, "apply_patch", {
            "file": "label_tool.py", "operation": "replace",
            "intent": "add prefix formatting", "symbol": "main",
            "old_block": RECOVERY_MAIN_BEFORE, "new_block": RECOVERY_MAIN_REGRESSED,
        })
        _record_tool_event(state, "apply_patch", {}, first, [], True, failure_recovery)
        _transition_tool_result(state, "apply_patch", first, None, orchestrator)

        orchestrator.transition(state, AgentEvent(FINISH_REQUESTED, "fixture edit ready for frozen verification"))
        regressed = verification.run_final_verification(state)
        renderer.verification_summary(regressed)
        renderer.recovery(regressed.get("recovery_result"))
        _transition_verification(state, regressed, orchestrator)

        debug_context = ContextManager(workspace).build_messages(state, "DEBUGGING context preview")[1]["content"]
        if "Current Failure" not in debug_context or "V_DEFAULT" not in debug_context:
            raise RuntimeError("DEBUGGING context did not contain the real regression evidence")
        renderer.decision_summary(
            "Repair default behavior after recovery",
            "baseline V_DEFAULT PASS, current V_DEFAULT FAIL; latest ChangeSet was UNDONE",
            "apply conditional prefix in label_tool.py::main",
        )

        second = tools.call(state, "apply_patch", {
            "file": "label_tool.py", "operation": "replace",
            "intent": "apply prefix only when the option is present", "symbol": "main",
            "old_block": RECOVERY_MAIN_BEFORE, "new_block": RECOVERY_MAIN_FIXED,
        })
        _record_tool_event(state, "apply_patch", {}, second, [], True, failure_recovery)
        _transition_tool_result(state, "apply_patch", second, None, orchestrator)

        orchestrator.transition(state, AgentEvent(FINISH_REQUESTED, "repaired edit ready for final verification"))
        final = verification.run_final_verification(state)
        renderer.verification_summary(final)
        _transition_verification(state, final, orchestrator)
        renderer.checkpoint(final.get("checkpoint_result"))

        hidden = evaluate_hidden(task_dir, workspace, task)
        renderer.final(state, hidden)

    _write_core_log(workspace.parent, raw_log.getvalue())
    result = _result(state, hidden, workspace)
    result["decision_source"] = "DETERMINISTIC_RECOVERY_FIXTURE"
    result["first_verification"] = regressed["overall_status"]
    result["recovery_status"] = (regressed.get("recovery_result") or {}).get("status")
    result["debug_context_has_regression_evidence"] = True
    _write_result(workspace.parent, result)
    return result


def _render_baseline(renderer: RichConsoleRenderer, state) -> None:
    renderer.baseline_start()
    checks = {item.id: item for item in state.verification_contract}
    for item in state.baseline:
        renderer.baseline_item(checks[item["verification_id"]], item["observation"])


def _prepare_workspace(task_id: str, directory: str):
    task_dir = TASKS_ROOT / task_id
    task = load_task(task_dir)
    run_dir = DEMO_RUNS / f"{directory}_{uuid.uuid4().hex[:8]}"
    workspace = run_dir / "workspace"
    run_dir.mkdir(parents=True, exist_ok=True)
    reset_task_repo(task_dir, workspace, task.initial_commit)
    return task_dir, task, workspace


def _write_core_log(run_dir: Path, content: str) -> None:
    (run_dir / "core_agent.log").write_text(content, encoding="utf-8")


def _write_result(run_dir: Path, result: dict[str, Any]) -> None:
    (run_dir / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8",
    )


def _result(state, hidden, workspace: Path) -> dict[str, Any]:
    return {
        "agent_final_status": state.current_phase,
        "verification_status": (state.verification_result or {}).get("overall_status"),
        "hidden_evaluator": "PASS" if hidden.success else "FAIL",
        "false_success": state.current_phase == "DONE" and not hidden.success,
        "checkpoint": (state.current_checkpoint or {}).get("id"),
        "hidden_evaluation": hidden.to_dict(),
        "workspace": str(workspace),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="CodeAgent two-minute Rich demonstration")
    parser.add_argument("scenario", choices=["csv", "recovery", "all"])
    parser.add_argument(
        "--live-model", action="store_true",
        help="Use the real model for CSV implementation with the benchmark's frozen plan/checks",
    )
    parser.add_argument("--task", help="Task for --live-model; if omitted, show an interactive prompt")
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI colors for redirected output")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    console = Console(
        file=sys.__stdout__, force_terminal=not args.no_color,
        color_system=None if args.no_color else "auto", highlight=False,
    )
    try:
        task = args.task
        if args.live_model:
            env_path = load_project_env()
            if env_path:
                console.print(f"[dim]模型配置: {env_path.name}[/dim]")
            if not task:
                console.rule("[bold blue]向 Agent 提交任务[/bold blue]")
                task = Prompt.ask("[bold]请输入任务[/bold]", default=CSV_TASK, console=console).strip()
            if not task:
                raise ValueError("task cannot be empty")
            console.print(f"[blue]用户任务[/blue]  {task}")
        if args.scenario in {"csv", "all"}:
            run_csv_demo(console, live_model=args.live_model, user_task=task)
        if args.scenario in {"recovery", "all"}:
            run_recovery_demo(console)
        return 0
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, StopIteration) as exc:
        console.print(f"[red]Demo failed:[/red] {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
