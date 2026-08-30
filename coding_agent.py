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
    DONE,
    EXECUTING,
    FAILED,
    PLANNING,
    VERIFYING,
    AcceptanceCriterion,
    AgentState,
    ExecutionStep,
    VerificationCheck,
)
from agent_events import (
    CONTINUE_EXECUTION,
    EDIT_APPLIED,
    EDIT_FAILED,
    FINAL_PARTIAL,
    FINAL_VERIFIED,
    FINISH_REQUESTED,
    INCREMENTAL_PARTIAL,
    INCREMENTAL_VERIFIED,
    MAX_STEPS_REACHED,
    PLAN_BLOCKED_BY_USER_INTENT,
    PLAN_READY,
    REPLAN_REQUIRED,
    TARGET_FAILED,
    TOOL_FAILED,
    UNRECOVERABLE_FAILURE,
    VERIFICATION_REGRESSED,
    VERIFICATION_REQUESTED,
    VERIFICATION_UNVERIFIED,
    AgentEvent,
)
from agent_orchestrator import AgentOrchestrator
from context_manager import ContextManager
from failure_models import FAILURE_ANALYSIS_FIELDS
from failure_recovery import FailureRecovery
from tool_executor import ToolExecutor
from tool_registry import tool_schemas_for_phase
from verification_engine import VerificationEngine
from planning_schema import (
    MAX_PLANNING_REPAIR_ATTEMPTS,
    PlanningSchemaError,
    build_repair_instruction,
    validate_repair_scope,
    validate_schema,
)

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
Classify every execution step explicitly as INSPECT, IMPLEMENT, or VERIFY. The
step_kind describes semantics; suggested_tools are recommendations only.
Freeze target cases, expected behavior, and commands now; they must not be regenerated
after implementation. Prefer cheap sanity evidence before target and regression checks.
Do not propose multiple agents, RAG, or UI.
"""

PLANNING_FINALIZATION_PROMPT = """
This is the final PLANNING turn. Stop exploring: use the evidence already present in
the structured planning context and call submit_plan now. Do not call read-only tools
again and do not return a prose-only answer. If the task is genuinely ambiguous, set
clarification_needed in submit_plan instead of continuing exploration.
"""

PLANNING_CONVERGENCE_PROMPT = """
Recent read/search turns did not add a new relevant file, symbol, or planning finding.
Before exploring again, state the specific critical information still missing. If no
critical gap remains, call submit_plan now. Read-only tools remain available when a
concrete unresolved gap justifies them.
"""

PLANNING_STAGNATION_NUDGE_AFTER = 2


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
                            "step_kind": {"type": "string", "enum": ["INSPECT", "IMPLEMENT", "VERIFY"]},
                            "suggested_tools": {"type": "array", "items": {"type": "string"}},
                            "related_acceptance_criteria": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                        },
                        "required": ["step_id", "description", "step_kind", "suggested_tools", "related_acceptance_criteria"],
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


FAILURE_ANALYSIS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "submit_failure_analysis",
        "description": "Submit structured analysis of the repeated evidence before revising the plan.",
        "parameters": {
            "type": "object",
            "properties": {
                "failure_analysis": {
                    "type": "object",
                    "properties": {
                        "previous_hypothesis": {"type": "string"},
                        "observed_evidence": {"type": "string"},
                        "previous_attempts": {"type": "array", "items": {"type": "string"}},
                        "why_previous_attempt_was_insufficient": {"type": "string"},
                        "remaining_possibilities": {"type": "array", "items": {"type": "string"}},
                        "revised_hypothesis": {"type": "string"},
                        "revised_plan": {"type": "string"},
                    },
                    "required": list(FAILURE_ANALYSIS_FIELDS),
                    "additionalProperties": False,
                },
            },
            "required": ["failure_analysis"],
            "additionalProperties": False,
        },
    },
}

SUBMIT_REPLAN_SCHEMA = {
    "type": "function",
    "function": {
        "name": "submit_replan",
        "description": "Submit a revised strategy after Failure Analysis without changing frozen criteria.",
        "parameters": {
            "type": "object",
            "properties": {
                "execution_plan": SUBMIT_PLAN_SCHEMA["function"]["parameters"]["properties"]["execution_plan"],
            },
            "required": ["execution_plan"],
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
    if state.planning_frozen:
        raise PlanningSchemaError("$: planning output is already frozen")
    validation_errors: list[str] = []
    try:
        validate_schema(payload, SUBMIT_PLAN_SCHEMA["function"]["parameters"])
    except PlanningSchemaError as exc:
        validation_errors.extend(exc.errors)
    raw_criteria = payload.get("acceptance_criteria", [])
    if isinstance(raw_criteria, list):
        for index, item in enumerate(raw_criteria):
            if not isinstance(item, dict):
                continue
            method = item.get("verification_method")
            if isinstance(method, str) and not _specific_verification_method(method):
                validation_errors.append(
                    f"$.acceptance_criteria[{index}].verification_method: must name a concrete command, test, "
                    "input/output check, or manual procedure and required result"
                )
    raw_checks = payload.get("verification_contract", [])
    if isinstance(raw_checks, list):
        for index, item in enumerate(raw_checks):
            if not isinstance(item, dict):
                continue
            method = item.get("verification_method")
            if isinstance(method, str) and not _specific_verification_method(method):
                validation_errors.append(
                    f"$.verification_contract[{index}].verification_method: must name a concrete command, test, "
                    "input/output check, or manual procedure and required result"
                )
            if item.get("verification_mode") == "AUTO" and item.get("command") in (None, []):
                validation_errors.append(
                    f"$.verification_contract[{index}].command: AUTO verification requires a non-empty command"
                )
    if validation_errors:
        raise PlanningSchemaError(list(dict.fromkeys(validation_errors)))

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
    known = set(criterion_ids)
    for check in checks:
        if not set(check.related_acceptance_criteria) <= known:
            raise ValueError(f"verification check {check.id} refers to an unknown criterion")
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
    state.planning_frozen = True
    return state


def _specific_verification_method(method: str) -> bool:
    normalized = " ".join(method.lower().split())
    if len(normalized) < 16:
        return False
    vague = {"verify feature works", "verify it works", "check correctness", "test feature"}
    return normalized not in vague


def _repair_plan(
    payload: dict[str, Any],
    validation_error: str,
    state: AgentState,
    client: OpenAICompatibleClient,
    context_manager: ContextManager,
) -> AgentState:
    if state.planning_frozen:
        raise PlanningSchemaError("$: planning output is already frozen; repair is not allowed")
    current_payload = payload
    current_error = validation_error
    repair_rejection: str | None = None
    for attempt in range(1, MAX_PLANNING_REPAIR_ATTEMPTS + 1):
        state.planning_repair_attempts += 1
        prompt_error = current_error
        if repair_rejection:
            prompt_error += f"\nPrevious repair rejection (not an allowed path): {repair_rejection}"
        prompt = build_repair_instruction(current_payload, prompt_error, attempt)
        messages = context_manager.build_messages(state, PLANNING_PROMPT)
        messages.append({"role": "user", "content": prompt})
        log(f"PLANNING SCHEMA REPAIR {attempt}/{MAX_PLANNING_REPAIR_ATTEMPTS}", current_error)
        message = client.complete(messages, [SUBMIT_PLAN_SCHEMA])
        calls = message.get("tool_calls") or []
        submit = next(
            (call for call in calls if call.get("function", {}).get("name") == "submit_plan"),
            None,
        )
        if submit is None:
            current_error = "$: repair response must contain exactly a submit_plan tool call"
            state.planning_validation_failures += 1
            continue
        repaired, parse_error = _parse_tool_arguments(submit)
        if parse_error:
            current_error = parse_error
            state.planning_validation_failures += 1
            current_payload = repaired
            continue
        try:
            validate_repair_scope(current_payload, repaired, current_error)
        except PlanningSchemaError as exc:
            repair_rejection = str(exc)
            state.planning_validation_failures += 1
            continue
        try:
            repaired_state = _validate_plan(repaired, state)
        except (KeyError, TypeError, ValueError, PlanningSchemaError) as exc:
            current_error = str(exc)
            current_payload = repaired
            repair_rejection = None
            state.planning_validation_failures += 1
            continue
        state.planning_repair_success = True
        return repaired_state
    raise RuntimeError(
        f"PLANNING schema repair failed after {MAX_PLANNING_REPAIR_ATTEMPTS} attempts; "
        f"last validation error: {repair_rejection or current_error}"
    )


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
    failure_recovery: FailureRecovery | None = None,
) -> dict[str, Any] | None:
    trajectory.append({"tool": name, "arguments": arguments, "result": result})
    state.add_action({
        "tool": name,
        "arguments": arguments,
        "observation": _compact_observation(name, result),
    })

    succeeded = result.get("status") in {"SUCCESS", "APPLIED"}
    if name == "read_file" and succeeded:
        state.add_relevant_file(str(arguments.get("path", "")))
        step = state.current_execution_step()
        if state.current_phase == EXECUTING and step and step.step_kind == "INSPECT":
            state.complete_current_step()
    elif name == "search_code" and succeeded:
        state.add_relevant_symbol(str(arguments.get("query", "")))
        for match in result.get("matches", []):
            state.add_relevant_file(str(match.get("path", "")))
        step = state.current_execution_step()
        if state.current_phase == EXECUTING and step and step.step_kind == "INSPECT":
            state.complete_current_step()
    elif name in {"apply_patch", "write_file"} and succeeded:
        path = str(result.get("file", result.get("path", arguments.get("file", arguments.get("path", "")))))
        state.add_relevant_file(path)
        if result.get("symbol"):
            state.add_relevant_symbol(str(result["symbol"]))
        step = state.current_execution_step()
        if step and step.step_kind == "IMPLEMENT":
            state.complete_current_step()
    elif name == "run_command" and succeeded and state.current_phase == EXECUTING:
        step = state.current_execution_step()
        if step and step.step_kind == "VERIFY":
            state.complete_current_step()
    elif name == "list_dir" and succeeded and state.current_phase == EXECUTING:
        step = state.current_execution_step()
        if step and step.step_kind == "INSPECT":
            state.complete_current_step()

    if allow_phase_changes and failure_recovery:
        return failure_recovery.handle_tool_result(state, name, arguments, result)
    return None


def _validate_replan(payload: dict[str, Any], state: AgentState) -> None:
    steps = [ExecutionStep(**item) for item in payload["execution_plan"]]
    if not steps:
        raise ValueError("revised execution plan cannot be empty")
    known = {item.id for item in state.acceptance_criteria}
    covered = {criterion for step in steps for criterion in step.related_acceptance_criteria}
    if any(not set(step.related_acceptance_criteria) <= known for step in steps):
        raise ValueError("revised plan refers to an unknown frozen criterion")
    if covered != known:
        raise ValueError("revised plan must still cover every frozen acceptance criterion")
    state.execution_plan = steps
    state.current_step = steps[0].step_id


def run_replanning(
    state: AgentState,
    tools: WorkspaceTools,
    client: OpenAICompatibleClient,
    context_manager: ContextManager,
    orchestrator: AgentOrchestrator,
    max_steps: int = 3,
) -> None:
    prompt = """You are replanning because deterministic failure evidence repeated.
Do not change the frozen Acceptance Criteria, Verification Contract, baseline, or user
task. Inspect current files when needed, then submit a structured Failure Analysis and
a revised execution plan. The previous hypothesis may still be valid; explain only why
the previous attempt was insufficient. Do not judge whether plans are semantically
similar and do not replay an identical failed edit.
"""
    for turn in range(1, max_steps + 1):
        submit_schema = FAILURE_ANALYSIS_SCHEMA if state.failure_analysis is None else SUBMIT_REPLAN_SCHEMA
        schemas = tool_schemas_for_phase(PLANNING) + [submit_schema]
        messages = context_manager.build_messages(state, prompt)
        log(f"FAILURE STRATEGY UPDATE {turn}/{max_steps}", {"reason": state.replan_reason})
        message = client.complete(messages, schemas)
        for tool_call in message.get("tool_calls") or []:
            name = tool_call.get("function", {}).get("name", "")
            arguments, parse_error = _parse_tool_arguments(tool_call)
            if parse_error:
                state.add_action({"replanning_error": parse_error})
                continue
            if name == "submit_failure_analysis":
                analysis = arguments["failure_analysis"]
                if any(field not in analysis for field in FAILURE_ANALYSIS_FIELDS):
                    raise ValueError("failure analysis is incomplete")
                state.failure_analysis = analysis
                log("Failure Analysis", state.failure_analysis)
                break
            if name == "submit_replan" and state.failure_analysis is not None:
                _validate_replan(arguments, state)
                orchestrator.transition(state, AgentEvent(PLAN_READY, "revised execution plan available"))
                log("Revised Execution Plan", [asdict(item) for item in state.execution_plan])
                return
            result = tools.call(state, name, arguments)
            state.add_action({"tool": name, "arguments": arguments, "observation": _compact_observation(name, result)})
    raise RuntimeError("replanning did not produce a valid Failure Analysis and revised plan")

def _capture_baseline(state: AgentState, tools: WorkspaceTools) -> None:
    for check in state.verification_contract:
        if not check.baseline_required or check.verification_mode != "AUTO" or not check.command:
            continue
        log("BASELINE CHECK", {"id": check.id, "command": check.command})
        observation = tools.call(state, "run_command", {"command": check.command})
        state.baseline.append({"verification_id": check.id, "observation": observation})
        log("BASELINE RESULT", state.baseline[-1])
    _complete_successful_baseline_steps(state)


def _complete_successful_baseline_steps(state: AgentState) -> None:
    successful_ids = {
        item["verification_id"]
        for item in state.baseline
        if item.get("observation", {}).get("status") == "SUCCESS"
    }
    covered_criteria = {
        criterion
        for check in state.verification_contract
        if check.id in successful_ids
        for criterion in check.related_acceptance_criteria
    }
    while True:
        step = state.current_execution_step()
        if not step or step.step_kind != "VERIFY":
            return
        if not set(step.related_acceptance_criteria) <= covered_criteria:
            return
        state.complete_current_step()


def run_planning(
    task: str,
    tools: WorkspaceTools,
    client: OpenAICompatibleClient,
    max_planning_steps: int,
) -> AgentState:
    state = AgentState(original_task=task)
    state.current_checkpoint = tools.checkpoint_manager.get_current_checkpoint()
    context_manager = ContextManager(tools.root)
    trajectory: list[dict[str, Any]] = []
    planning_tools = tool_schemas_for_phase(PLANNING) + [SUBMIT_PLAN_SCHEMA]
    stagnant_exploration_turns = 0
    for step in range(1, max_planning_steps + 1):
        finalizing = step == max_planning_steps
        converging = stagnant_exploration_turns >= PLANNING_STAGNATION_NUDGE_AFTER
        prompt = PLANNING_PROMPT
        if converging and not finalizing:
            prompt += PLANNING_CONVERGENCE_PROMPT
        if finalizing:
            prompt += PLANNING_FINALIZATION_PROMPT
        available_tools = [SUBMIT_PLAN_SCHEMA] if finalizing else planning_tools
        messages = context_manager.build_messages(state, prompt)
        log(f"PLANNING STEP {step}/{max_planning_steps}", {"trajectory_events": len(trajectory)})
        message = client.complete(messages, available_tools)
        trajectory.append({"assistant": message})
        if message.get("content"):
            log("PLANNING MESSAGE", message["content"])
        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            state.add_action({"model_message": message.get("content") or "No tool call returned."})
            stagnant_exploration_turns = 0
            continue
        knowledge_before = (
            frozenset(state.relevant_files),
            frozenset(state.relevant_symbols),
            len(state.confirmed_facts),
        )
        exploration_only = True
        for tool_call in tool_calls:
            name = tool_call.get("function", {}).get("name", "")
            if name not in {"read_file", "search_code"}:
                exploration_only = False
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
                except (KeyError, TypeError, ValueError, PlanningSchemaError) as exc:
                    state.planning_validation_failures += 1
                    state = _repair_plan(arguments, str(exc), state, client, context_manager)
                    if not state.clarification_needed:
                        _capture_baseline(state, tools)
                    log("Task Understanding", state.task_understanding)
                    log("Acceptance Criteria", [asdict(item) for item in state.acceptance_criteria])
                    log("Verification Contract", [asdict(item) for item in state.verification_contract])
                    log("Baseline", state.baseline)
                    log("Execution Plan", [asdict(item) for item in state.execution_plan])
                    return state
            else:
                result = tools.call(state, name, arguments)
            log("PLANNING TOOL RESULT", result)
            _record_tool_event(state, name, arguments, result, trajectory, allow_phase_changes=False)
        knowledge_after = (
            frozenset(state.relevant_files),
            frozenset(state.relevant_symbols),
            len(state.confirmed_facts),
        )
        if exploration_only and knowledge_after == knowledge_before:
            stagnant_exploration_turns += 1
        else:
            stagnant_exploration_turns = 0
    raise RuntimeError(f"PLANNING did not produce a valid plan in {max_planning_steps} steps")


def run_execution(
    state: AgentState,
    tools: WorkspaceTools,
    client: OpenAICompatibleClient,
    max_steps: int,
    orchestrator: AgentOrchestrator | None = None,
) -> str:
    execution_prompt = SYSTEM_PROMPT + """

PLANNING is complete. Follow the structured plan below. The acceptance criteria and
verification contract were fixed before implementation; do not rewrite or weaken
them. Execute planned pre-change verification setup before product-code changes when
present. This stage may use all provided tools.
The context is rebuilt from structured state before every request. Treat workspace
files as the only source of truth for current code.
"""
    orchestrator = orchestrator or AgentOrchestrator()
    if state.current_phase == PLANNING and state.execution_plan:
        orchestrator.transition(state, AgentEvent(PLAN_READY, "execution plan available"))
    context_manager = ContextManager(tools.root)
    failure_recovery = FailureRecovery(tools)
    verification_engine = VerificationEngine(tools, failure_recovery=failure_recovery)
    trajectory: list[dict[str, Any]] = []

    for step in range(1, max_steps + 1):
        if state.needs_user_confirmation:
            final = _pause_summary(state)
            log("AUTONOMOUS LOOP PAUSED", final)
            return final
        if state.current_phase == PLANNING:
            run_replanning(state, tools, client, context_manager, orchestrator)
        if state.current_phase == EXECUTING and state.execution_plan_complete():
            final = _request_final_verification(
                state, verification_engine, orchestrator,
                AgentEvent(VERIFICATION_REQUESTED, "execution plan completed", {"mode": "FINAL"}),
            )
            if final is not None:
                return final
            continue
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
            failure = _record_tool_event(
                state, name, arguments, result, trajectory,
                allow_phase_changes=True, failure_recovery=failure_recovery,
            )
            if name == "finish" and result.get("status") == "SUCCESS":
                final = _request_final_verification(
                    state, verification_engine, orchestrator,
                    AgentEvent(FINISH_REQUESTED, "model requested final verification"),
                )
                if final is not None:
                    return final
                # A failed finish starts a fresh DEBUGGING decision; ignore any
                # additional tool calls bundled with the finish request.
                break
            transition = _transition_tool_result(state, name, result, failure, orchestrator)
            if transition and transition.next_phase == PLANNING:
                break
            if state.current_phase == EXECUTING and state.execution_plan_complete():
                final = _request_final_verification(
                    state, verification_engine, orchestrator,
                    AgentEvent(VERIFICATION_REQUESTED, "execution plan completed", {"mode": "FINAL"}),
                )
                if final is not None:
                    return final
                break

    orchestrator.transition(state, AgentEvent(MAX_STEPS_REACHED, f"maximum of {max_steps} model steps reached"))
    final = _termination_summary(state, max_steps)
    log("AGENT STOPPED", final)
    return final


def _request_final_verification(
    state: AgentState,
    verification_engine: VerificationEngine,
    orchestrator: AgentOrchestrator,
    event: AgentEvent,
) -> str | None:
    orchestrator.transition(state, event)
    verification = verification_engine.run_final_verification(state)
    log("FINAL VERIFICATION", verification)
    transition = _transition_verification(state, verification, orchestrator)
    if transition.next_phase == DONE:
        final = _verification_report(verification)
        log("AGENT DONE", final)
        return final
    if transition.pause_autonomous_loop:
        final = _verification_report(verification)
        log("HUMAN CONFIRMATION REQUIRED", final)
        return final
    return None


def _transition_tool_result(
    state: AgentState,
    name: str,
    result: dict[str, Any],
    failure: dict[str, Any] | None,
    orchestrator: AgentOrchestrator,
):
    status = result.get("status")
    if status == "APPLIED" and name == "apply_patch":
        return orchestrator.transition(state, AgentEvent(EDIT_APPLIED, "structured edit applied"))
    if status == "SUCCESS" and name == "run_command" and state.current_phase == DEBUGGING:
        return orchestrator.transition(state, AgentEvent(CONTINUE_EXECUTION, "debugging command succeeded"))
    if status in {"SUCCESS", "APPLIED"}:
        return None
    event_type = EDIT_FAILED if name == "apply_patch" else TOOL_FAILED
    transition = orchestrator.transition(state, AgentEvent(event_type, str(result.get("reason") or status)))
    if failure and failure.get("decision") == "REPLAN_REQUIRED":
        transition = orchestrator.transition(
            state,
            AgentEvent(REPLAN_REQUIRED, "repeated failure fingerprint", {"failure": failure}),
        )
    elif failure and failure.get("decision") == "UNRECOVERABLE_FAILURE":
        transition = orchestrator.transition(
            state,
            AgentEvent(UNRECOVERABLE_FAILURE, "same failure persisted across replans", {"failure": failure}),
        )
    return transition


def _transition_verification(
    state: AgentState,
    result: dict[str, Any],
    orchestrator: AgentOrchestrator,
):
    overall = result["overall_status"]
    mode = result.get("mode", state.verification_mode)
    if overall == "VERIFIED":
        event_type = FINAL_VERIFIED if mode == "FINAL" else INCREMENTAL_VERIFIED
    elif overall == "PARTIALLY_VERIFIED":
        event_type = FINAL_PARTIAL if mode == "FINAL" else INCREMENTAL_PARTIAL
    elif overall == "REGRESSED":
        event_type = VERIFICATION_REGRESSED
    elif result.get("failed_critical"):
        event_type = TARGET_FAILED
    else:
        event_type = VERIFICATION_UNVERIFIED
    transition = orchestrator.transition(state, AgentEvent(event_type, result.get("evidence_summary", ""), result))
    failure = result.get("failure_event") or {}
    if transition.next_phase == DEBUGGING and failure.get("decision") == "REPLAN_REQUIRED":
        transition = orchestrator.transition(
            state,
            AgentEvent(REPLAN_REQUIRED, "repeated failure fingerprint", {"failure": failure}),
        )
    elif transition.next_phase == DEBUGGING and failure.get("decision") == "UNRECOVERABLE_FAILURE":
        transition = orchestrator.transition(
            state,
            AgentEvent(UNRECOVERABLE_FAILURE, "same verification failure persisted across replans", {"failure": failure}),
        )
    return transition


def _termination_summary(state: AgentState, max_steps: int) -> str:
    result = state.verification_result or {}
    pending = [
        item["id"] for item in state.change_sets
        if item.get("rollback_status") == "NONE" and item.get("verification_status") == "UNVERIFIED"
    ]
    summary = {
        "reason": f"maximum of {max_steps} model steps reached",
        "completed_criteria": result.get("verified_critical", []),
        "unmet_criteria": sorted(set(result.get("failed_critical", []) + result.get("unverified_critical", []))),
        "verification_status": result.get("overall_status"),
        "last_failure": state.current_failure,
        "pending_changesets": pending,
        "current_checkpoint": state.current_checkpoint,
        "phase_history": state.phase_history,
    }
    return "Agent reached MAX_AGENT_STEPS:\n" + json.dumps(summary, ensure_ascii=False, indent=2)


def _pause_summary(state: AgentState) -> str:
    result = state.verification_result or {}
    return "Autonomous loop paused for user confirmation:\n" + json.dumps({
        "phase": state.current_phase,
        "clarification": state.clarification_needed,
        "unverified_critical": result.get("unverified_critical", []),
        "manual_confirmation_items": state.manual_confirmation_items,
        "last_failure": state.current_failure,
    }, ensure_ascii=False, indent=2)


def _verification_report(result: dict[str, Any]) -> str:
    lines = [f"Verification: {result['overall_status']}", result["evidence_summary"]]
    if result.get("manual_items"):
        lines.append("Manual confirmation: " + ", ".join(result["manual_items"]))
    if result.get("failed_critical"):
        lines.append("Failed critical criteria: " + ", ".join(result["failed_critical"]))
    if result.get("unverified_critical"):
        lines.append("Critical criteria without environment evidence: " + ", ".join(result["unverified_critical"]))
    if result.get("new_failures"):
        lines.append("New regressions: " + ", ".join(result["new_failures"]))
    checkpoint = result.get("checkpoint_result") or {}
    if checkpoint.get("status"):
        lines.append(f"Checkpoint: {checkpoint['status']}")
    return "\n".join(lines)


def run_agent(task: str, workspace: Path, max_steps: int, max_planning_steps: int = 8) -> str:
    tools = WorkspaceTools(workspace)
    client = OpenAICompatibleClient()
    orchestrator = AgentOrchestrator()
    log("PHASE", "PLANNING")
    state = run_planning(task, tools, client, max_planning_steps)
    if state.clarification_needed:
        orchestrator.transition(state, AgentEvent(PLAN_BLOCKED_BY_USER_INTENT, state.clarification_needed))
        final = f"Clarification required before execution: {state.clarification_needed}"
        log("AGENT STOPPED", final)
        return final
    orchestrator.transition(state, AgentEvent(PLAN_READY, "initial execution plan available"))
    log("PHASE", "EXECUTION")
    return run_execution(state, tools, client, max_steps, orchestrator)


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
