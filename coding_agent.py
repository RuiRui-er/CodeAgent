"""A minimal coding agent built without an Agent framework or SDK."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import asdict
from pathlib import Path
from typing import Any

from agent_state import (
    DEBUGGING,
    EXECUTING,
    PLANNING,
    VERIFYING,
    AcceptanceCriterion,
    AgentState,
    ExecutionStep,
    VerificationCheck,
)
from context_manager import ContextManager
from tool_executor import ToolExecutor
from tool_registry import tool_schemas_for_phase

# Import compatibility for callers of the earlier single-file implementation.
WorkspaceTools = ToolExecutor


SYSTEM_PROMPT = """You are a small coding agent working inside one workspace.
Solve the user's task autonomously. Inspect relevant files before editing, make the
smallest useful change, and run an appropriate command or test to verify it.
Use only the provided tools. Never try to access anything outside the workspace.
When implementation is ready for verification, call finish. finish only requests a
transition to VERIFYING and never means the task is already done.
If a tool fails, inspect its error and decide how to recover.
"""

PLANNING_PROMPT = """You are in the PLANNING phase of a small coding agent.
Understand the user's task before any product-code modification. Use the read-only
exploration tools to inspect the project structure, README, tests, configuration,
relevant source, and current failures. Prefer resolving ambiguity from the workspace.
Only report that clarification is needed when the real intent cannot be inferred and
different interpretations would produce materially different implementations.

When sufficiently informed, call submit_plan exactly once. Acceptance criteria must
be observable and task-specific. Every AUTO criterion needs a concrete verification
method (an exact test, command, input/output case, build, or runtime check), never a
generic phrase such as 'check correctness'. Define verification checks before coding;
mark checks that should be run now as baseline_required. Prefer existing tests, then a
minimal reproduction or input/output check, then build/compile and runtime sanity.
If a missing target check needs a new file, put creation and its pre-fix run before
product-code changes in the execution plan. Tie every plan step to criterion IDs.
Do not propose structured patches, snapshots, Git checkpoints, advanced verification
statuses, multiple agents, RAG, or UI.
"""


SUBMIT_PLAN_SCHEMA = {
    "type": "function",
    "function": {
        "name": "submit_plan",
        "description": "Finish PLANNING by submitting the structured, pre-change plan.",
        "parameters": {
            "type": "object",
            "properties": {
                "task_understanding": {"type": "string"},
                "acceptance_criteria": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "description": {"type": "string"},
                            "criticality": {"type": "string", "enum": ["CRITICAL", "NON_CRITICAL"]},
                            "verification_mode": {"type": "string", "enum": ["AUTO", "HUMAN"]},
                            "evidence_type": {"type": "string", "enum": ["TARGET", "REGRESSION", "SANITY"]},
                            "verification_method": {"type": "string"},
                        },
                        "required": ["id", "description", "criticality", "verification_mode", "evidence_type", "verification_method"],
                        "additionalProperties": False,
                    },
                },
                "verification_contract": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "description": {"type": "string"},
                            "verification_mode": {"type": "string", "enum": ["AUTO", "HUMAN"]},
                            "evidence_type": {"type": "string", "enum": ["TARGET", "REGRESSION", "SANITY"]},
                            "verification_method": {"type": "string"},
                            "command": {"type": ["array", "null"], "items": {"type": "string"}},
                            "baseline_required": {"type": "boolean"},
                            "related_acceptance_criteria": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                        },
                        "required": ["id", "description", "verification_mode", "evidence_type", "verification_method", "command", "baseline_required", "related_acceptance_criteria"],
                        "additionalProperties": False,
                    },
                },
                "execution_plan": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "step_id": {"type": "string"},
                            "description": {"type": "string"},
                            "suggested_tools": {"type": "array", "items": {"type": "string"}},
                            "related_acceptance_criteria": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                        },
                        "required": ["step_id", "description", "suggested_tools", "related_acceptance_criteria"],
                        "additionalProperties": False,
                    },
                },
                "clarification_needed": {"type": ["string", "null"]},
            },
            "required": ["task_understanding", "acceptance_criteria", "verification_contract", "execution_plan", "clarification_needed"],
            "additionalProperties": False,
        },
    },
}


class OpenAICompatibleClient:
    """Tiny Chat Completions client implemented with the Python standard library."""

    def __init__(self) -> None:
        self.api_key = os.environ.get("OPENAI_API_KEY")
        self.base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        self.model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY environment variable is not set")

    def complete(
        self,
        messages: list[dict[str, Any]],
        tool_schemas: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        body_data: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
        }
        if tool_schemas:
            body_data["tools"] = tool_schemas
            body_data["tool_choice"] = "auto"
        body = json.dumps(body_data).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"model API returned HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RuntimeError(f"model API request failed: {exc}") from exc
        try:
            return payload["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"unexpected model response: {payload}") from exc


def log(label: str, value: Any) -> None:
    print(f"\n=== {label} ===", flush=True)
    if isinstance(value, str):
        print(value, flush=True)
    else:
        print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)


def _parse_tool_arguments(tool_call: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    raw = tool_call.get("function", {}).get("arguments", "{}")
    try:
        arguments = json.loads(raw)
        if not isinstance(arguments, dict):
            raise ValueError("tool arguments must decode to an object")
        return arguments, None
    except (json.JSONDecodeError, ValueError) as exc:
        return {}, f"invalid tool arguments: {exc}"


def _validate_plan(payload: dict[str, Any], state: AgentState) -> AgentState:
    criteria = [AcceptanceCriterion(**item) for item in payload["acceptance_criteria"]]
    checks = [VerificationCheck(**item) for item in payload["verification_contract"]]
    steps = [ExecutionStep(**item) for item in payload["execution_plan"]]
    criterion_ids = [item.id for item in criteria]
    if len(criterion_ids) != len(set(criterion_ids)):
        raise ValueError("acceptance criterion IDs must be unique")
    check_ids = [item.id for item in checks]
    if len(check_ids) != len(set(check_ids)):
        raise ValueError("verification check IDs must be unique")
    step_ids = [item.step_id for item in steps]
    if len(step_ids) != len(set(step_ids)):
        raise ValueError("execution step IDs must be unique")
    if any(not item.verification_method.strip() for item in criteria):
        raise ValueError("every acceptance criterion needs a verification_method")
    known = set(criterion_ids)
    for check in checks:
        if not set(check.related_acceptance_criteria) <= known:
            raise ValueError(f"verification check {check.id} refers to an unknown criterion")
        if check.verification_mode == "AUTO" and not check.command:
            raise ValueError(f"AUTO verification check {check.id} needs a command")
    for step in steps:
        if not set(step.related_acceptance_criteria) <= known:
            raise ValueError(f"execution step {step.step_id} refers to an unknown criterion")
    verified_criteria = {
        criterion_id
        for check in checks
        for criterion_id in check.related_acceptance_criteria
    }
    planned_criteria = {
        criterion_id
        for step in steps
        for criterion_id in step.related_acceptance_criteria
    }
    if verified_criteria != known:
        raise ValueError("every acceptance criterion must be covered by the verification contract")
    if planned_criteria != known:
        raise ValueError("every acceptance criterion must be covered by the execution plan")
    state.task_understanding = payload["task_understanding"]
    state.acceptance_criteria = criteria
    state.verification_contract = checks
    state.execution_plan = steps
    state.current_step = steps[0].step_id
    state.clarification_needed = payload.get("clarification_needed")
    return state


def _shorten(text: str, limit: int = 1200) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "... [truncated in action record]"


def _compact_observation(name: str, result: dict[str, Any]) -> dict[str, Any]:
    status = result.get("status", "UNKNOWN")
    if status == "AMBIGUOUS_TARGET":
        return {
            "status": status,
            "file": result.get("file"),
            "candidate_count": result.get("candidate_count"),
            "candidates": result.get("candidates", []),
            "reason": result.get("reason"),
        }
    if status == "STALE_EDIT":
        return {
            "status": status,
            "file": result.get("file"),
            "reason": result.get("reason"),
            "current_context": _shorten(result.get("current_context", "")),
        }
    if status not in {"SUCCESS", "APPLIED"}:
        return {
            "status": status,
            "reason": _shorten(str(result.get("reason", result.get("stderr", "unknown error")))),
        }
    if name == "apply_patch":
        change = result.get("change_set", {})
        return {
            "status": status,
            "file": result.get("file"),
            "symbol": result.get("symbol"),
            "operation": result.get("operation"),
            "resolution": result.get("resolution"),
            "change_set_id": change.get("id"),
        }
    if name == "read_file":
        content = result.get("content", "")
        return {"status": status, "path": result.get("path"), "chars": len(content), "preview": _shorten(content)}
    if name == "list_dir":
        items = result.get("items", [])
        return {"status": status, "item_count": len(items), "items": items[:40]}
    if name == "search_code":
        matches = result.get("matches", [])
        return {"status": status, "match_count": len(matches), "matches": matches[:30]}
    if name == "run_command":
        return {
            "status": status,
            "policy": result.get("policy"),
            "exit_code": result.get("exit_code"),
            "stdout": _shorten(result.get("stdout", "")),
            "stderr": _shorten(result.get("stderr", "")),
        }
    return result


def _record_tool_event(
    state: AgentState,
    name: str,
    arguments: dict[str, Any],
    result: dict[str, Any],
    trajectory: list[dict[str, Any]],
    allow_phase_changes: bool,
) -> None:
    trajectory.append({"tool": name, "arguments": arguments, "result": result})
    state.add_action({
        "tool": name,
        "arguments": arguments,
        "observation": _compact_observation(name, result),
    })

    succeeded = result.get("status") in {"SUCCESS", "APPLIED"}
    if name == "read_file" and succeeded:
        state.add_relevant_file(str(arguments.get("path", "")))
    elif name == "search_code" and succeeded:
        state.add_relevant_symbol(str(arguments.get("query", "")))
        for match in result.get("matches", []):
            state.add_relevant_file(str(match.get("path", "")))
    elif name in {"apply_patch", "write_file"} and succeeded:
        path = str(result.get("file", result.get("path", arguments.get("file", arguments.get("path", "")))))
        state.add_relevant_file(path)
        if result.get("symbol"):
            state.add_relevant_symbol(str(result["symbol"]))
        state.add_fact(f"A {name} mutation succeeded for {path}.")
    elif name == "finish" and succeeded:
        state.complete_current_step()

    failure_statuses = {"FAILED", "TIMEOUT", "TARGET_NOT_FOUND", "STALE_EDIT", "INVALID_EDIT"}
    if allow_phase_changes and result.get("status") in failure_statuses:
        reason = _shorten(str(result.get("reason", result.get("stderr", "unknown tool error"))))
        state.failed_attempts.append({"attempt": f"{name} {arguments}", "reason": reason})
        state.failure_evidence.append({"tool": name, "arguments": arguments, "error": reason})
        state.set_phase(DEBUGGING)

def _capture_baseline(state: AgentState, tools: WorkspaceTools) -> None:
    for check in state.verification_contract:
        if not check.baseline_required or check.verification_mode != "AUTO" or not check.command:
            continue
        log("BASELINE CHECK", {"id": check.id, "command": check.command})
        observation = tools.call(state, "run_command", {"command": check.command})
        state.baseline.append({"verification_id": check.id, "observation": observation})
        log("BASELINE RESULT", state.baseline[-1])


def run_planning(
    task: str,
    tools: WorkspaceTools,
    client: OpenAICompatibleClient,
    max_planning_steps: int,
) -> AgentState:
    state = AgentState(original_task=task)
    context_manager = ContextManager(tools.root)
    trajectory: list[dict[str, Any]] = []
    planning_tools = tool_schemas_for_phase(PLANNING) + [SUBMIT_PLAN_SCHEMA]
    for step in range(1, max_planning_steps + 1):
        messages = context_manager.build_messages(state, PLANNING_PROMPT)
        log(f"PLANNING STEP {step}/{max_planning_steps}", {"trajectory_events": len(trajectory)})
        message = client.complete(messages, planning_tools)
        trajectory.append({"assistant": message})
        if message.get("content"):
            log("PLANNING MESSAGE", message["content"])
        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            state.add_action({"model_message": message.get("content") or "No tool call returned."})
            continue
        for tool_call in tool_calls:
            name = tool_call.get("function", {}).get("name", "")
            arguments, parse_error = _parse_tool_arguments(tool_call)
            log("PLANNING TOOL CALL", {"name": name, "arguments": arguments})
            if parse_error:
                result = {"status": "FAILED", "tool": name, "reason": parse_error}
            elif name == "submit_plan":
                try:
                    state = _validate_plan(arguments, state)
                    if not state.clarification_needed:
                        _capture_baseline(state, tools)
                    log("Task Understanding", state.task_understanding)
                    log("Acceptance Criteria", [asdict(item) for item in state.acceptance_criteria])
                    log("Verification Contract", [asdict(item) for item in state.verification_contract])
                    log("Baseline", state.baseline)
                    log("Execution Plan", [asdict(item) for item in state.execution_plan])
                    return state
                except (KeyError, TypeError, ValueError) as exc:
                    result = {"status": "FAILED", "tool": name, "reason": f"invalid plan: {exc}"}
            else:
                result = tools.call(state, name, arguments)
            log("PLANNING TOOL RESULT", result)
            _record_tool_event(state, name, arguments, result, trajectory, allow_phase_changes=False)
    raise RuntimeError(f"PLANNING did not produce a valid plan in {max_planning_steps} steps")


def run_execution(
    state: AgentState,
    tools: WorkspaceTools,
    client: OpenAICompatibleClient,
    max_steps: int,
) -> str:
    execution_prompt = SYSTEM_PROMPT + """

PLANNING is complete. Follow the structured plan below. The acceptance criteria and
verification contract were fixed before implementation; do not rewrite or weaken
them. Execute planned pre-change verification setup before product-code changes when
present. This stage may use all provided tools.
The context is rebuilt from structured state before every request. Treat workspace
files as the only source of truth for current code.
"""
    state.set_phase(EXECUTING)
    context_manager = ContextManager(tools.root)
    trajectory: list[dict[str, Any]] = []

    for step in range(1, max_steps + 1):
        messages = context_manager.build_messages(state, execution_prompt)
        log(f"STEP {step}/{max_steps} - MODEL REQUEST", {"trajectory_events": len(trajectory)})
        message = client.complete(messages, tool_schemas_for_phase(state.current_phase))
        trajectory.append({"assistant": message})
        if message.get("content"):
            log("MODEL MESSAGE", message["content"])

        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            content = message.get("content") or "Model returned no tool call."
            if state.current_phase == VERIFYING:
                final = f"Verification phase reached (final verification status is not implemented yet): {content}"
                log("AGENT STOPPED", final)
                return final
            state.add_action({
                "model_message": content,
                "observation": "Call finish to request transition into VERIFYING.",
            })
            continue

        for tool_call in tool_calls:
            name = tool_call.get("function", {}).get("name", "")
            arguments, parse_error = _parse_tool_arguments(tool_call)
            if parse_error:
                result = {"status": "FAILED", "tool": name, "reason": parse_error}
            else:
                log("TOOL CALL", {"name": name, "arguments": arguments})
                result = tools.call(state, name, arguments)
            log("TOOL RESULT", result)
            _record_tool_event(state, name, arguments, result, trajectory, allow_phase_changes=True)

    final = f"Stopped after reaching the maximum of {max_steps} steps."
    log("AGENT STOPPED", final)
    return final


def run_agent(task: str, workspace: Path, max_steps: int, max_planning_steps: int = 8) -> str:
    tools = WorkspaceTools(workspace)
    client = OpenAICompatibleClient()
    log("PHASE", "PLANNING")
    state = run_planning(task, tools, client, max_planning_steps)
    if state.clarification_needed:
        final = f"Clarification required before execution: {state.clarification_needed}"
        log("AGENT STOPPED", final)
        return final
    log("PHASE", "EXECUTION")
    return run_execution(state, tools, client, max_steps)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Minimal coding agent baseline")
    parser.add_argument("task", nargs="?", help="Programming task. If omitted, read interactively.")
    parser.add_argument("--workspace", default=".", help="Workspace directory (default: current directory)")
    parser.add_argument("--max-steps", type=int, default=12, help="Maximum model turns (default: 12)")
    parser.add_argument("--max-planning-steps", type=int, default=8, help="Maximum planning turns (default: 8)")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    task = args.task or input("Programming task: ").strip()
    if not task:
        print("Task cannot be empty.", file=sys.stderr)
        return 2
    if args.max_steps < 1:
        print("--max-steps must be at least 1.", file=sys.stderr)
        return 2
    if args.max_planning_steps < 1:
        print("--max-planning-steps must be at least 1.", file=sys.stderr)
        return 2
    try:
        workspace = Path(args.workspace).resolve(strict=True)
        if not workspace.is_dir():
            raise ValueError("workspace is not a directory")
        run_agent(task, workspace, args.max_steps, args.max_planning_steps)
        return 0
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
