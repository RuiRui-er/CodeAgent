"""Coarse failure labels layered on top of original environment evidence."""

from __future__ import annotations

from typing import Any

from failure_models import (
    BUILD_FAILED,
    EDIT_TARGET_AMBIGUOUS,
    EDIT_TARGET_NOT_FOUND,
    INVALID_EDIT_FAILURE,
    OUTPUT_MISMATCH,
    REGRESSION_DETECTED,
    RUNTIME_ERROR,
    STALE_EDIT_FAILURE,
    TASK_INCOMPLETE,
    TEST_FAILED,
    TIMEOUT,
    TOOL_BLOCKED,
    TOOL_EXECUTION_FAILED,
    UNKNOWN_FAILURE,
    FailureEvent,
)


EVIDENCE_STREAM_CHARS = 3_000
HINTS = {
    TIMEOUT: ["Check for blocking work, non-terminating control flow, or a command whose normal runtime exceeds the limit."],
    TEST_FAILED: ["Inspect the failing test, assertion, and the preserved runner output before changing code."],
    BUILD_FAILED: ["Inspect the earliest compiler/build error and its referenced source; later errors may be cascading."],
    STALE_EDIT_FAILURE: ["Re-read the current file because it changed after the edit was resolved."],
    EDIT_TARGET_AMBIGUOUS: ["Use the returned candidates and select an exact target."],
    EDIT_TARGET_NOT_FOUND: ["Re-read the current file and verify the symbol, anchor, or old block."],
    TASK_INCOMPLETE: ["Continue against the frozen failed Acceptance Criteria and their command evidence."],
    REGRESSION_DETECTED: ["Use the recovery already performed by VerificationEngine and inspect the new regression evidence."],
}


class FailureClassifier:
    """Adds a deterministic coarse type without replacing raw error fields."""

    EDIT_STATUS_TYPES = {
        "TARGET_NOT_FOUND": EDIT_TARGET_NOT_FOUND,
        "AMBIGUOUS_TARGET": EDIT_TARGET_AMBIGUOUS,
        "STALE_EDIT": STALE_EDIT_FAILURE,
        "INVALID_EDIT": INVALID_EDIT_FAILURE,
    }

    def classify_tool_result(
        self,
        state: Any,
        tool: str,
        arguments: dict[str, Any],
        result: dict[str, Any],
    ) -> FailureEvent | None:
        status = result.get("status", "UNKNOWN")
        if status in {"SUCCESS", "APPLIED"} or result.get("reason") == "DUPLICATE_FAILED_ACTION":
            return None
        failure_type = self.EDIT_STATUS_TYPES.get(status)
        if not failure_type:
            if status == "TIMEOUT":
                failure_type = TIMEOUT
            elif status in {"BLOCKED", "DENIED"}:
                failure_type = TOOL_BLOCKED
            elif tool == "run_command" and status == "FAILED":
                failure_type = self._command_failure_type(result.get("command", arguments.get("command", "")))
            elif status == "FAILED":
                failure_type = TOOL_EXECUTION_FAILED
            else:
                failure_type = UNKNOWN_FAILURE

        location = {
            "file": result.get("file") or arguments.get("file") or arguments.get("path"),
            "symbol": result.get("symbol") or arguments.get("symbol"),
            "test": self._first_test_name(result),
        }
        change = state.change_sets[-1] if state.change_sets else None
        return FailureEvent(
            id="",
            type=failure_type,
            location={key: value for key, value in location.items() if value},
            evidence=self._tool_evidence(tool, arguments, result),
            related_changeset=change.get("id") if change else None,
            related_criterion=self._current_criterion(state),
            hypothesis=self._current_hypothesis(state),
            attempt={"tool": tool, "arguments": arguments},
            diagnostic_hints=list(HINTS.get(failure_type, [])),
            step_id=state.current_step,
            phase=state.current_phase,
            action_signature=self.edit_action_signature(arguments) if tool == "apply_patch" else None,
        )

    def classify_verification_result(self, state: Any, result: dict[str, Any]) -> FailureEvent | None:
        overall = result.get("overall_status")
        if overall == "REGRESSED":
            failure_type = REGRESSION_DETECTED
            criterion = next(iter(result.get("failed_critical") or []), None)
        elif overall == "UNVERIFIED" and result.get("failed_critical"):
            failure_type = TASK_INCOMPLETE
            criterion = result["failed_critical"][0]
        else:
            # Evidence absence is not an execution failure.
            return None
        failed_results = [item for item in result.get("criterion_results", []) if item.get("status") == "FAIL"]
        compact_results = self._verification_evidence(failed_results)
        change = state.change_sets[-1] if state.change_sets else None
        return FailureEvent(
            id="",
            type=failure_type,
            location={"test": result["new_failures"][0]} if result.get("new_failures") else {},
            evidence={
                "overall_status": overall,
                "new_failures": result.get("new_failures", []),
                "criterion_results": compact_results,
                "evidence_summary": result.get("evidence_summary", ""),
            },
            related_changeset=change.get("id") if change else None,
            related_criterion=criterion,
            hypothesis=self._current_hypothesis(state),
            attempt=self._changeset_attempt(change),
            diagnostic_hints=list(HINTS.get(failure_type, [])),
            step_id=state.current_step,
            phase=state.current_phase,
            recovery_result=result.get("recovery_result"),
            action_signature=self.changeset_action_signature(change),
        )

    @staticmethod
    def edit_action_signature(arguments: dict[str, Any]) -> dict[str, Any] | None:
        if not arguments.get("file") or not arguments.get("operation"):
            return None
        import hashlib
        replacement = arguments.get("new_block")
        return {
            "file": arguments.get("file"),
            "symbol_or_target": arguments.get("symbol") or arguments.get("anchor"),
            "operation": arguments.get("operation"),
            "intent": arguments.get("intent"),
            "replacement_hash": hashlib.sha256((replacement or "").encode("utf-8")).hexdigest(),
        }

    @staticmethod
    def changeset_action_signature(change: dict[str, Any] | None) -> dict[str, Any] | None:
        if not change or not change.get("file") or not change.get("operation"):
            return None
        import hashlib
        return {
            "file": change.get("file"),
            "symbol_or_target": change.get("symbol"),
            "operation": change.get("operation"),
            "intent": change.get("intent"),
            "replacement_hash": hashlib.sha256((change.get("after") or "").encode("utf-8")).hexdigest(),
        }

    def _tool_evidence(self, tool: str, arguments: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        stdout, stdout_cut = self._truncate_stream(str(result.get("stdout", "")))
        stderr, stderr_cut = self._truncate_stream(str(result.get("stderr", "")))
        return {
            "tool": tool,
            "status": result.get("status"),
            "command": result.get("command", arguments.get("command")),
            "exit_code": result.get("exit_code"),
            "stdout": stdout,
            "stderr": stderr,
            "stdout_truncated_for_failure_context": stdout_cut,
            "stderr_truncated_for_failure_context": stderr_cut,
            "tool_output_truncated": result.get("truncated", False),
            "timeout_seconds": result.get("timeout"),
            "reason": result.get("reason"),
            "current_context": result.get("current_context"),
            "candidates": result.get("candidates"),
        }

    @staticmethod
    def _truncate_stream(text: str, limit: int = EVIDENCE_STREAM_CHARS) -> tuple[str, bool]:
        if len(text) <= limit:
            return text, False
        half = max(1, (limit - 80) // 2)
        return text[:half] + "\n[... stream truncated for failure context ...]\n" + text[-half:], True

    def _verification_evidence(self, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        command_count = sum(len(item.get("evidence", [])) for item in results) or 1
        stream_limit = max(300, min(1_500, 5_000 // (command_count * 2)))
        compact: list[dict[str, Any]] = []
        for item in results:
            evidence = []
            for command in item.get("evidence", []):
                stdout, stdout_cut = self._truncate_stream(str(command.get("stdout", "")), stream_limit)
                stderr, stderr_cut = self._truncate_stream(str(command.get("stderr", "")), stream_limit)
                evidence.append({
                    "verification_id": command.get("verification_id"),
                    "command": command.get("command"),
                    "status": command.get("status"),
                    "exit_code": command.get("exit_code"),
                    "stdout": stdout,
                    "stderr": stderr,
                    "stdout_truncated_for_failure_context": stdout_cut,
                    "stderr_truncated_for_failure_context": stderr_cut,
                    "tool_output_truncated": command.get("truncated", False),
                })
            compact.append({
                "criterion_id": item.get("criterion_id"),
                "status": item.get("status"),
                "evidence_type": item.get("evidence_type"),
                "summary": item.get("summary"),
                "evidence": evidence,
            })
        return compact

    @staticmethod
    def _command_failure_type(command: Any) -> str:
        text = " ".join(command) if isinstance(command, list) else str(command)
        lowered = text.lower()
        if any(token in lowered for token in ("pytest", "unittest", " test", "test_")):
            return TEST_FAILED
        if any(token in lowered for token in ("build", "compile", "cmake", "make", "gcc", "g++", "cargo check", "tsc")):
            return BUILD_FAILED
        return RUNTIME_ERROR

    @staticmethod
    def _first_test_name(result: dict[str, Any]) -> str | None:
        import re
        text = f"{result.get('stdout', '')}\n{result.get('stderr', '')}"
        match = re.search(r"(?:FAILED\s+|FAIL:\s+)([^\s(]+)", text)
        return match.group(1) if match else None

    @staticmethod
    def _current_criterion(state: Any) -> str | None:
        step = next((item for item in state.execution_plan if item.step_id == state.current_step), None)
        return step.related_acceptance_criteria[0] if step and step.related_acceptance_criteria else None

    @staticmethod
    def _current_hypothesis(state: Any) -> str | None:
        analysis = state.failure_analysis or {}
        return analysis.get("revised_hypothesis") or analysis.get("previous_hypothesis")

    @staticmethod
    def _changeset_attempt(change: dict[str, Any] | None) -> dict[str, Any] | None:
        if not change:
            return None
        return {key: change.get(key) for key in ("file", "symbol", "operation", "intent")}
