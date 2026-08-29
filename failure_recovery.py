"""State transitions driven by repeated structured failure evidence."""

from __future__ import annotations

from typing import Any

from failure_classifier import FailureClassifier
from failure_memory import FailureMemory
from failure_models import REGRESSION_DETECTED, FailureEvent


MAX_REPEAT_FAILURES = 3
CONTINUE_DEBUGGING = "CONTINUE_DEBUGGING"
REPLAN_DECISION = "REPLAN_REQUIRED"


class FailureRecovery:
    def __init__(self, tools: Any, max_repeat_failures: int = MAX_REPEAT_FAILURES):
        self.tools = tools
        self.max_repeat_failures = max(2, int(max_repeat_failures))
        self.classifier = FailureClassifier()
        self.memory = FailureMemory()

    def handle_tool_result(
        self,
        state: Any,
        tool: str,
        arguments: dict[str, Any],
        result: dict[str, Any],
    ) -> dict[str, Any] | None:
        event = self.classifier.classify_tool_result(state, tool, arguments, result)
        return self.handle_failure(state, event) if event else None

    def handle_verification_result(self, state: Any, result: dict[str, Any]) -> dict[str, Any] | None:
        event = self.classifier.classify_verification_result(state, result)
        return self.handle_failure(state, event) if event else None

    def handle_failure(self, state: Any, event: FailureEvent) -> dict[str, Any]:
        record = self.memory.register_failure(state, event)
        state.failure_evidence.append(record)
        state.failed_attempts.append({
            "failure_id": record["id"],
            "attempt": record.get("attempt"),
            "hypothesis": record.get("hypothesis"),
            "reason": record["type"],
            "evidence": record["evidence"],
        })

        decision = CONTINUE_DEBUGGING
        if record["type"] == REGRESSION_DETECTED:
            # VerificationEngine already performed the only recovery operation.
            decision = CONTINUE_DEBUGGING
        elif record["repeat_count"] >= self.max_repeat_failures:
            rollback = None
            manager = self.tools.checkpoint_manager
            if len(manager.pending_changesets) > 1 and manager.can_rollback():
                rollback = manager.rollback_last_stable(state, "repeated ordinary failure before replanning")
                record["recovery_result"] = rollback
            decision = REPLAN_DECISION

        record["decision"] = decision
        self._log(record, decision)
        return record

    @staticmethod
    def _log(record: dict[str, Any], decision: str) -> None:
        print("\n[Failure]", flush=True)
        print(f"Type: {record['type']}", flush=True)
        if record.get("location"):
            print(f"Location: {record['location']}", flush=True)
        print(f"Fingerprint: {record['fingerprint']}", flush=True)
        print(f"Repeat: {record['repeat_count']}", flush=True)
        print(f"Decision: {decision}", flush=True)
