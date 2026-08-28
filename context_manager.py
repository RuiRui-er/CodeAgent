"""Phase-aware, character-budgeted context construction."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from agent_state import DEBUGGING, EXECUTING, PLANNING, VERIFYING, AgentState


MAX_CONTEXT_CHARS = 18_000
RECENT_ACTION_LIMIT = 5

# Static limits are deliberately simple and easy to tune.
PHASE_SECTION_BUDGETS = {
    PLANNING: {
        "Task": 3000,
        "Acceptance Criteria": 2500,
        "Planning Findings": 4500,
        "Recent Actions": 4500,
    },
    EXECUTING: {
        "Task": 2200,
        "Current Step": 2200,
        "Step Acceptance Criteria": 3000,
        "Relevant Code": 6500,
        "Recent Actions": 2500,
        "Failed Attempts": 1600,
    },
    VERIFYING: {
        "Acceptance Criteria": 3000,
        "Verification Contract": 4000,
        "Baseline": 3000,
        "Modification Summary": 2200,
        "Change Sets": 3200,
        "Current Checkpoint": 1800,
        "Criterion Results": 4200,
        "New Regressions": 2200,
        "Verification Summary": 2200,
        "Manual Confirmation": 1800,
        "Completed Steps": 2200,
        "Latest Results": 4500,
    },
    DEBUGGING: {
        "Task": 1600,
        "Current Step": 2200,
        "Failure Evidence": 4000,
        "Failed Attempts": 3000,
        "Failed Criteria": 3200,
        "Recovery Result": 2200,
        "Finish Guardrail": 2600,
        "Relevant Code": 5500,
        "Recent Actions": 2200,
    },
}


class ContextManager:
    def __init__(self, workspace: Path, max_context_chars: int = MAX_CONTEXT_CHARS):
        self.workspace = workspace.resolve(strict=True)
        self.max_context_chars = max_context_chars

    def build_messages(self, state: AgentState, system_prompt: str) -> list[dict[str, str]]:
        sections = self._sections_for_phase(state)
        included: list[str] = []
        rendered: list[str] = []
        # Reserve prompt and small framing overhead so the complete request stays bounded.
        remaining = max(0, self.max_context_chars - len(system_prompt) - 220)
        budgets = PHASE_SECTION_BUDGETS[state.current_phase]

        for title, value in sections:
            if value in (None, "", [], {}):
                continue
            text = self._render(value)
            limit = min(budgets[title], remaining)
            if limit <= 0:
                break
            block = f"## {title}\n{self._truncate(text, limit)}"
            if len(block) > remaining:
                block = self._truncate(block, remaining)
            rendered.append(block)
            included.append(self._included_label(title, state))
            remaining -= len(block)

        body = (
            f"Current phase: {state.current_phase}\n"
            "Use this structured state and current workspace content for the next decision. "
            "Older trajectory entries are intentionally omitted.\n\n"
            + "\n\n".join(rendered)
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": body},
        ]
        self._log_context(state.current_phase, included, sum(len(m["content"]) for m in messages))
        return messages

    def _sections_for_phase(self, state: AgentState) -> list[tuple[str, Any]]:
        if state.current_phase == PLANNING:
            return [
                ("Task", state.original_task),
                ("Acceptance Criteria", [asdict(item) for item in state.acceptance_criteria]),
                ("Planning Findings", state.confirmed_facts),
                ("Recent Actions", state.recent_actions[-RECENT_ACTION_LIMIT:]),
            ]
        if state.current_phase == EXECUTING:
            return [
                ("Task", state.original_task),
                ("Current Step", self._current_step(state)),
                ("Step Acceptance Criteria", self._step_criteria(state)),
                ("Relevant Code", self._read_relevant_code(state)),
                ("Recent Actions", state.recent_actions[-RECENT_ACTION_LIMIT:]),
                ("Failed Attempts", state.failed_attempts),
            ]
        if state.current_phase == VERIFYING:
            return [
                ("Acceptance Criteria", [asdict(item) for item in state.acceptance_criteria]),
                ("Verification Contract", [asdict(item) for item in state.verification_contract]),
                ("Baseline", state.baseline),
                ("Modification Summary", state.confirmed_facts),
                ("Change Sets", state.change_sets),
                ("Current Checkpoint", state.current_checkpoint),
                ("Criterion Results", (state.verification_result or {}).get("criterion_results")),
                ("New Regressions", (state.verification_result or {}).get("new_failures")),
                ("Verification Summary", (state.verification_result or {}).get("evidence_summary")),
                ("Manual Confirmation", state.manual_confirmation_items),
                ("Completed Steps", state.completed_steps),
                ("Latest Results", state.recent_actions[-RECENT_ACTION_LIMIT:]),
            ]
        return [
            ("Task", state.original_task),
            ("Current Step", self._current_step(state)),
            ("Failure Evidence", state.failure_evidence),
            ("Failed Attempts", state.failed_attempts),
            ("Failed Criteria", self._failed_criteria(state)),
            ("Recovery Result", (state.verification_result or {}).get("recovery_result")),
            ("Finish Guardrail", self._finish_guardrail(state)),
            ("Relevant Code", self._read_relevant_code(state)),
            ("Recent Actions", state.recent_actions[-RECENT_ACTION_LIMIT:]),
        ]

    @staticmethod
    def _failed_criteria(state: AgentState) -> list[dict[str, Any]]:
        result = state.verification_result or {}
        return [item for item in result.get("criterion_results", []) if item.get("status") != "PASS"]

    @staticmethod
    def _finish_guardrail(state: AgentState) -> dict[str, Any] | None:
        if not state.finish_guardrail_active:
            return None
        return {
            "failed_finish_attempts": state.failed_finish_attempts,
            "acceptance_criteria": [asdict(item) for item in state.acceptance_criteria],
            "instruction": "Continue debugging against the frozen criteria and evidence; finish cannot bypass the gate.",
        }

    def _current_step(self, state: AgentState) -> dict[str, Any] | None:
        return next(
            (asdict(step) for step in state.execution_plan if step.step_id == state.current_step),
            None,
        )

    def _step_criteria(self, state: AgentState) -> list[dict[str, Any]]:
        step = next((item for item in state.execution_plan if item.step_id == state.current_step), None)
        if not step:
            return []
        related = set(step.related_acceptance_criteria)
        return [asdict(item) for item in state.acceptance_criteria if item.id in related]

    def _read_relevant_code(self, state: AgentState) -> list[dict[str, str]]:
        code: list[dict[str, str]] = []
        for relative in state.relevant_files:
            target = (self.workspace / relative).resolve()
            try:
                target.relative_to(self.workspace)
                content = target.read_text(encoding="utf-8")
            except (ValueError, OSError, UnicodeDecodeError):
                continue
            excerpts = self._symbol_excerpts(content, state.relevant_symbols)
            code.append({"path": relative, "content": excerpts or content})
        return code

    @staticmethod
    def _symbol_excerpts(content: str, symbols: list[str]) -> str:
        if not symbols:
            return ""
        lines = content.splitlines()
        selected: set[int] = set()
        for index, line in enumerate(lines):
            if any(symbol in line for symbol in symbols):
                selected.update(range(max(0, index - 3), min(len(lines), index + 8)))
        return "\n".join(f"{index + 1}: {lines[index]}" for index in sorted(selected))

    @staticmethod
    def _render(value: Any) -> str:
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)

    @staticmethod
    def _truncate(text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        marker = "\n... [truncated by character budget]"
        return text[:max(0, limit - len(marker))] + marker

    @staticmethod
    def _included_label(title: str, state: AgentState) -> str:
        if title == "Relevant Code":
            return ", ".join(state.relevant_files) or title
        if title == "Recent Actions":
            return f"Last {min(len(state.recent_actions), RECENT_ACTION_LIMIT)} Actions"
        return title

    @staticmethod
    def _log_context(phase: str, included: list[str], total_chars: int) -> None:
        print("\n[Context]", flush=True)
        print(f"Phase: {phase}", flush=True)
        print("Included:", flush=True)
        for item in included:
            print(f"- {item}", flush=True)
        print(f"Total chars: {total_chars}", flush=True)
