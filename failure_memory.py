"""Deterministic failure fingerprints and duplicate failed-edit lookup."""

from __future__ import annotations

import json
import re
from typing import Any

from failure_classifier import FailureClassifier
from failure_models import BUILD_FAILED, TEST_FAILED, TIMEOUT, FailureEvent


class FailureMemory:
    def register_failure(self, state: Any, event: FailureEvent) -> dict[str, Any]:
        event.fingerprint = self.build_fingerprint(event)
        previous = [item for item in state.failure_history if item.get("fingerprint") == event.fingerprint]
        event.repeat_count = len(previous) + 1
        event.id = f"failure_{len(state.failure_history) + 1:04d}"
        record = event.to_dict()
        state.failure_history.append(record)
        state.current_failure = record
        state.repeated_failure_count = event.repeat_count
        return record

    def build_fingerprint(self, event: FailureEvent) -> str:
        location = event.location
        pieces = [event.type]
        if event.type == TEST_FAILED:
            pieces.extend([location.get("test"), location.get("file"), self._error_category(event.evidence)])
        elif event.type == BUILD_FAILED:
            pieces.extend([location.get("file"), self._error_category(event.evidence)])
        elif event.type == TIMEOUT:
            pieces.extend([self._normalized_command(event.evidence.get("command")), event.step_id])
        else:
            pieces.extend([
                location.get("file"), location.get("symbol"), location.get("test"),
                event.related_criterion,
            ])
        return ":".join(self._stable(piece) for piece in pieces if piece)

    def duplicate_failed_action(self, state: Any, arguments: dict[str, Any]) -> dict[str, Any] | None:
        signature = FailureClassifier.edit_action_signature(arguments)
        if not signature:
            return None
        for failure in reversed(state.failure_history):
            if failure.get("action_signature") == signature:
                return {
                    "status": "BLOCKED",
                    "reason": "DUPLICATE_FAILED_ACTION",
                    "related_failure": failure["id"],
                    "failure_type": failure["type"],
                    "evidence": failure["evidence"],
                }
        return None

    @staticmethod
    def related_failures(state: Any) -> list[dict[str, Any]]:
        current = state.current_failure or {}
        return [
            item for item in state.failure_history
            if item.get("fingerprint") == current.get("fingerprint")
            or item.get("related_criterion") == current.get("related_criterion")
        ][-6:]

    @staticmethod
    def _normalized_command(command: Any) -> str:
        text = " ".join(command) if isinstance(command, list) else str(command or "")
        text = re.sub(r"[A-Za-z]:\\[^\s]+", "<path>", text)
        return re.sub(r"\s+", " ", text).strip().lower()

    @staticmethod
    def _error_category(evidence: dict[str, Any]) -> str:
        text = f"{evidence.get('stdout', '')}\n{evidence.get('stderr', '')}"
        for category in ("AssertionError", "SyntaxError", "TypeError", "ValueError", "ImportError", "NameError"):
            if category in text:
                return category
        match = re.search(r"\b(?:error|exception)\s*[:\[]?\s*([A-Za-z_][\w.-]*)", text, re.IGNORECASE)
        return match.group(1).lower() if match else "unspecified"

    @staticmethod
    def _stable(value: Any) -> str:
        text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, ensure_ascii=False)
        text = re.sub(r"\b\d{2,}\b", "<n>", text)
        return text.replace("\\", "/").strip().lower()
