"""A minimal coding agent built without an Agent framework or SDK."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


SYSTEM_PROMPT = """You are a small coding agent working inside one workspace.
Solve the user's task autonomously. Inspect relevant files before editing, make the
smallest useful change, and run an appropriate command or test to verify it.
Use only the provided tools. Never try to access anything outside the workspace.
When the task is complete, respond with a concise summary and verification result.
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
Do not propose state machines, context budgeting, summaries, structured patches,
snapshots, Git checkpoints, advanced verification statuses, multiple agents, RAG, or UI.
"""


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files and directories inside the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative directory; default is workspace root."},
                    "recursive": {"type": "boolean", "description": "List recursively."},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 text file in the workspace.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or replace a UTF-8 text file in the workspace. Use the full desired content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_text",
            "description": "Search for a literal string in workspace text files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "path": {"type": "string", "description": "Relative file or directory; default is root."},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a command in the workspace. Pass argv as a JSON array, without shell syntax.",
            "parameters": {
                "type": "object",
                "properties": {
                    "args": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                    "timeout": {"type": "integer", "minimum": 1, "maximum": 120},
                },
                "required": ["args"],
                "additionalProperties": False,
            },
        },
    },
]

READ_ONLY_TOOL_SCHEMAS = [
    schema for schema in TOOL_SCHEMAS
    if schema["function"]["name"] in {"list_files", "read_file", "search_text", "run_command"}
]

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


@dataclass(frozen=True)
class AcceptanceCriterion:
    id: str
    description: str
    criticality: str
    verification_mode: str
    evidence_type: str
    verification_method: str


@dataclass(frozen=True)
class VerificationCheck:
    id: str
    description: str
    verification_mode: str
    evidence_type: str
    verification_method: str
    command: list[str] | None
    baseline_required: bool
    related_acceptance_criteria: list[str]


@dataclass(frozen=True)
class ExecutionStep:
    step_id: str
    description: str
    suggested_tools: list[str]
    related_acceptance_criteria: list[str]


@dataclass
class AgentState:
    original_task: str
    task_understanding: str = ""
    acceptance_criteria: list[AcceptanceCriterion] = field(default_factory=list)
    verification_contract: list[VerificationCheck] = field(default_factory=list)
    baseline: list[dict[str, Any]] = field(default_factory=list)
    execution_plan: list[ExecutionStep] = field(default_factory=list)
    current_step: str | None = None
    clarification_needed: str | None = None

    def planning_snapshot(self) -> dict[str, Any]:
        return asdict(self)


class WorkspaceTools:
    """Local tools whose direct file operations are confined to one root."""

    def __init__(self, root: Path):
        self.root = root.resolve(strict=True)

    def _resolve(self, relative: str = ".") -> Path:
        candidate = (self.root / relative).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(f"path escapes workspace: {relative!r}") from exc
        return candidate

    def list_files(self, path: str = ".", recursive: bool = False) -> dict[str, Any]:
        base = self._resolve(path)
        if not base.is_dir():
            raise ValueError(f"not a directory: {path}")
        entries = base.rglob("*") if recursive else base.iterdir()
        items = []
        for item in sorted(entries):
            kind = "dir" if item.is_dir() else "file"
            items.append({"path": item.relative_to(self.root).as_posix(), "type": kind})
        return {"items": items}

    def read_file(self, path: str) -> dict[str, Any]:
        target = self._resolve(path)
        if not target.is_file():
            raise ValueError(f"not a file: {path}")
        return {"path": path, "content": target.read_text(encoding="utf-8")}

    def write_file(self, path: str, content: str) -> dict[str, Any]:
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {"path": target.relative_to(self.root).as_posix(), "bytes": len(content.encode("utf-8"))}

    def search_text(self, query: str, path: str = ".") -> dict[str, Any]:
        base = self._resolve(path)
        files = [base] if base.is_file() else (p for p in base.rglob("*") if p.is_file())
        matches: list[dict[str, Any]] = []
        for file in files:
            try:
                lines = file.read_text(encoding="utf-8").splitlines()
            except (UnicodeDecodeError, OSError):
                continue
            for number, line in enumerate(lines, 1):
                if query in line:
                    matches.append({
                        "path": file.relative_to(self.root).as_posix(),
                        "line": number,
                        "text": line,
                    })
                if len(matches) >= 200:
                    return {"matches": matches, "truncated": True}
        return {"matches": matches, "truncated": False}

    def run_command(self, args: list[str], timeout: int = 30) -> dict[str, Any]:
        if not args or not all(isinstance(arg, str) for arg in args):
            raise ValueError("args must be a non-empty list of strings")
        timeout = max(1, min(int(timeout), 120))
        # shell=False prevents pipes, redirections, command chaining, and shell built-ins.
        completed = subprocess.run(
            args,
            cwd=self.root,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            shell=False,
        )
        return {
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "exit_code": completed.returncode,
        }

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        methods = {
            "list_files": self.list_files,
            "read_file": self.read_file,
            "write_file": self.write_file,
            "search_text": self.search_text,
            "run_command": self.run_command,
        }
        try:
            if name not in methods:
                raise ValueError(f"unknown tool: {name}")
            return {"ok": True, "result": methods[name](**arguments)}
        except subprocess.TimeoutExpired as exc:
            return {"ok": False, "error": f"command timed out after {exc.timeout} seconds"}
        except Exception as exc:  # Tool failures become observations for the model.
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


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
        body = json.dumps({
            "model": self.model,
            "messages": messages,
            "tools": tool_schemas or TOOL_SCHEMAS,
            "tool_choice": "auto",
        }).encode("utf-8")
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


def _validate_plan(payload: dict[str, Any], task: str) -> AgentState:
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
    return AgentState(
        original_task=task,
        task_understanding=payload["task_understanding"],
        acceptance_criteria=criteria,
        verification_contract=checks,
        execution_plan=steps,
        current_step=steps[0].step_id,
        clarification_needed=payload.get("clarification_needed"),
    )


def _capture_baseline(state: AgentState, tools: WorkspaceTools) -> None:
    for check in state.verification_contract:
        if not check.baseline_required or check.verification_mode != "AUTO" or not check.command:
            continue
        log("BASELINE CHECK", {"id": check.id, "command": check.command})
        observation = tools.call("run_command", {"args": check.command})
        state.baseline.append({"verification_id": check.id, "observation": observation})
        log("BASELINE RESULT", state.baseline[-1])


def run_planning(
    task: str,
    tools: WorkspaceTools,
    client: OpenAICompatibleClient,
    max_planning_steps: int,
) -> AgentState:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": PLANNING_PROMPT},
        {"role": "user", "content": task},
    ]
    planning_tools = READ_ONLY_TOOL_SCHEMAS + [SUBMIT_PLAN_SCHEMA]
    for step in range(1, max_planning_steps + 1):
        log(f"PLANNING STEP {step}/{max_planning_steps}", {"message_count": len(messages)})
        message = client.complete(messages, planning_tools)
        messages.append(message)
        if message.get("content"):
            log("PLANNING MESSAGE", message["content"])
        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            messages.append({
                "role": "user",
                "content": "Continue environment discovery or call submit_plan with the required structured plan.",
            })
            continue
        for tool_call in tool_calls:
            name = tool_call.get("function", {}).get("name", "")
            arguments, parse_error = _parse_tool_arguments(tool_call)
            log("PLANNING TOOL CALL", {"name": name, "arguments": arguments})
            if parse_error:
                result = {"ok": False, "error": parse_error}
            elif name == "submit_plan":
                try:
                    state = _validate_plan(arguments, task)
                    if not state.clarification_needed:
                        _capture_baseline(state, tools)
                    log("Task Understanding", state.task_understanding)
                    log("Acceptance Criteria", [asdict(item) for item in state.acceptance_criteria])
                    log("Verification Contract", [asdict(item) for item in state.verification_contract])
                    log("Baseline", state.baseline)
                    log("Execution Plan", [asdict(item) for item in state.execution_plan])
                    return state
                except (KeyError, TypeError, ValueError) as exc:
                    result = {"ok": False, "error": f"invalid plan: {exc}"}
            else:
                result = tools.call(name, arguments)
            log("PLANNING TOOL RESULT", result)
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.get("id", ""),
                "content": json.dumps(result, ensure_ascii=False),
            })
    raise RuntimeError(f"PLANNING did not produce a valid plan in {max_planning_steps} steps")


def run_execution(
    state: AgentState,
    tools: WorkspaceTools,
    client: OpenAICompatibleClient,
    max_steps: int,
) -> str:
    frozen_plan = json.dumps(state.planning_snapshot(), ensure_ascii=False, indent=2)
    execution_prompt = SYSTEM_PROMPT + """

PLANNING is complete. Follow the structured plan below. The acceptance criteria and
verification contract were fixed before implementation; do not rewrite or weaken
them. Execute planned pre-change verification setup before product-code changes when
present. This stage may use all provided tools.

STRUCTURED PLAN:
""" + frozen_plan
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": execution_prompt},
        {"role": "user", "content": state.original_task},
    ]

    for step in range(1, max_steps + 1):
        log(f"STEP {step}/{max_steps} - MODEL REQUEST", {"message_count": len(messages)})
        message = client.complete(messages)
        messages.append(message)
        if message.get("content"):
            log("MODEL MESSAGE", message["content"])

        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            final = message.get("content") or "Model stopped without a final message."
            log("AGENT FINISHED", final)
            return final

        for tool_call in tool_calls:
            name = tool_call.get("function", {}).get("name", "")
            arguments, parse_error = _parse_tool_arguments(tool_call)
            if parse_error:
                result = {"ok": False, "error": parse_error}
            else:
                log("TOOL CALL", {"name": name, "arguments": arguments})
                result = tools.call(name, arguments)
            log("TOOL RESULT", result)
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.get("id", ""),
                "content": json.dumps(result, ensure_ascii=False),
            })

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
