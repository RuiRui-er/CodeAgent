"""Evidence-gated verification using the frozen planning contract."""

from __future__ import annotations

import re
from typing import Any, Iterable

from agent_state import AgentState, VerificationCheck
from edit_models import PARTIALLY_VERIFIED, REGRESSED, UNVERIFIED, VERIFIED
from verification_models import CRITERION_UNVERIFIED, FAIL, PASS, CriterionResult, VerificationResult


FINAL = "FINAL"
INCREMENTAL = "INCREMENTAL"
EVIDENCE_ORDER = {"SANITY": 0, "TARGET": 1, "REGRESSION": 2}


class VerificationEngine:
    """Runs only pre-planned checks and applies discrete evidence rules."""

    def __init__(self, tools: Any, failed_finish_limit: int = 2, failure_recovery: Any | None = None):
        self.tools = tools
        self.failed_finish_limit = max(1, int(failed_finish_limit))
        self.failure_recovery = failure_recovery

    def run_final_verification(self, state: AgentState) -> dict[str, Any]:
        result = self._run(state, FINAL, {item.id for item in state.acceptance_criteria})
        return self._apply_gate(state, result, final=True)

    def run_incremental_verification(
        self,
        state: AgentState,
        criterion_ids: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        selected = set(criterion_ids or self._current_step_criteria(state))
        result = self._run(state, INCREMENTAL, selected)
        return self._apply_gate(state, result, final=False)

    def _run(self, state: AgentState, mode: str, selected: set[str]) -> VerificationResult:
        checks = [
            check for check in state.verification_contract
            if selected.intersection(check.related_acceptance_criteria)
        ]
        checks.sort(key=lambda item: EVIDENCE_ORDER.get(item.evidence_type, 99))
        observations: dict[str, dict[str, Any]] = {}
        stopped_after_sanity = False

        for check in checks:
            if check.verification_mode != "AUTO" or not check.command:
                continue
            if stopped_after_sanity:
                continue
            observation = self.tools.call(state, "run_command", {"command": check.command})
            observations[check.id] = observation
            if check.evidence_type == "SANITY" and not self._passed(observation):
                stopped_after_sanity = True

        criterion_results = [
            self._criterion_result(state, criterion, checks, observations)
            for criterion in state.acceptance_criteria
            if criterion.id in selected
        ]
        baseline_failures, current_failures, new_failures = self._regression_delta(state, checks, observations)
        by_type = lambda kind: [
            result.to_dict() for result in criterion_results if result.evidence_type == kind
        ]
        critical = {
            item.id: item for item in state.acceptance_criteria
            if item.id in selected and item.criticality == "CRITICAL"
        }
        verified_critical = [item.criterion_id for item in criterion_results if item.criterion_id in critical and item.status == PASS]
        failed_critical = [item.criterion_id for item in criterion_results if item.criterion_id in critical and item.status == FAIL]
        unverified_critical = [
            item.criterion_id for item in criterion_results
            if item.criterion_id in critical and item.status == CRITERION_UNVERIFIED
        ]
        manual_items = [
            item.criterion_id for item in criterion_results
            if item.status == CRITERION_UNVERIFIED
            and next(c for c in state.acceptance_criteria if c.id == item.criterion_id).verification_mode == "HUMAN"
        ]
        sanity_failed = any(item.status != PASS for item in criterion_results if item.evidence_type == "SANITY")
        all_critical_selected = mode != FINAL or set(critical) == {
            item.id for item in state.acceptance_criteria if item.criticality == "CRITICAL"
        }
        noncritical_auto_failed = any(
            item.status == FAIL
            and next(c for c in state.acceptance_criteria if c.id == item.criterion_id).criticality == "NON_CRITICAL"
            for item in criterion_results
        )

        if new_failures:
            overall = REGRESSED
        elif failed_critical or unverified_critical or sanity_failed or not all_critical_selected or noncritical_auto_failed:
            overall = UNVERIFIED
        elif manual_items:
            overall = PARTIALLY_VERIFIED
        else:
            overall = VERIFIED

        summary = (
            f"{len(verified_critical)} critical PASS; {len(failed_critical)} critical FAIL; "
            f"{len(unverified_critical)} critical UNVERIFIED; {len(new_failures)} new regression(s); "
            f"{len(manual_items)} manual item(s)."
        )
        result = VerificationResult(
            mode=mode,
            overall_status=overall,
            criterion_results=[item.to_dict() for item in criterion_results],
            target_results=by_type("TARGET"),
            regression_results=by_type("REGRESSION"),
            sanity_results=by_type("SANITY"),
            baseline_failures=baseline_failures,
            current_failures=current_failures,
            new_failures=new_failures,
            verified_critical=verified_critical,
            unverified_critical=unverified_critical,
            failed_critical=failed_critical,
            manual_items=manual_items,
            evidence_summary=summary,
        )
        self._log(result)
        return result

    def _apply_gate(self, state: AgentState, result: VerificationResult, final: bool) -> dict[str, Any]:
        status = result.overall_status
        manager = self.tools.checkpoint_manager
        for change_id in list(manager.pending_changesets):
            manager.update_change_verification(state, change_id, status)

        state.verification_result = result.to_dict()
        state.manual_confirmation_items = list(result.manual_items)
        if status in {VERIFIED, PARTIALLY_VERIFIED}:
            for item in result.criterion_results:
                if item["status"] == PASS:
                    state.add_fact(f"{item['criterion_id']} verified by environment evidence: {item['summary']}")
            checkpoint = manager.mark_stable(
                state,
                f"{result.mode.lower()} evidence gate passed",
                state.next_verification_ref(),
            )
            result.checkpoint_result = checkpoint
            state.current_checkpoint = manager.get_current_checkpoint()
        elif status == REGRESSED:
            if not self.failure_recovery:
                evidence = {"kind": "NEW_REGRESSION", "new_failures": result.new_failures, "summary": result.evidence_summary}
                state.failure_evidence.append(evidence)
                state.failed_attempts.append({"attempt": "verification", "reason": result.evidence_summary})
            result.recovery_result = self._recover_regression(state)
        else:
            if not self.failure_recovery:
                state.failure_evidence.extend(
                    {"kind": "CRITERION", "criterion_id": item["criterion_id"], "status": item["status"], "details": item["details"]}
                    for item in result.criterion_results if item["status"] != PASS
                )

        state.verification_result = result.to_dict()
        if self.failure_recovery:
            failure = self.failure_recovery.handle_verification_result(state, state.verification_result)
            if failure:
                state.verification_result["failure_event"] = failure
        return state.verification_result

    def _recover_regression(self, state: AgentState) -> dict[str, Any]:
        manager = self.tools.checkpoint_manager
        if manager.pending_changesets:
            latest = manager.pending_changesets[-1]
            undone = manager.undo_changeset(state, latest, "new regression detected by VerificationEngine")
            if undone.get("status") == "UNDONE":
                return undone
        return manager.rollback_last_stable(state, "new regression detected by VerificationEngine")

    def _criterion_result(
        self,
        state: AgentState,
        criterion: Any,
        checks: list[VerificationCheck],
        observations: dict[str, dict[str, Any]],
    ) -> CriterionResult:
        related = [check for check in checks if criterion.id in check.related_acceptance_criteria]
        if criterion.verification_mode == "HUMAN":
            return CriterionResult(
                criterion.id, CRITERION_UNVERIFIED, criterion.evidence_type, "HUMAN_CONFIRMATION",
                None, None, "Human confirmation required", criterion.verification_method,
                [item.id for item in related],
                [],
            )
        observed = [(check, observations[check.id]) for check in related if check.id in observations]
        if not observed:
            return CriterionResult(
                criterion.id, CRITERION_UNVERIFIED, criterion.evidence_type, "NONE", None, None,
                "No independent environment evidence", criterion.verification_method,
                [item.id for item in related],
                [],
            )
        baseline = {item["verification_id"]: item["observation"] for item in state.baseline}
        missing_regression_baseline = [
            check.id for check, _ in observed
            if check.evidence_type == "REGRESSION" and check.id not in baseline
        ]
        if missing_regression_baseline:
            return CriterionResult(
                criterion.id, CRITERION_UNVERIFIED, criterion.evidence_type, "COMMAND", None, None,
                "Regression baseline is unavailable", ", ".join(missing_regression_baseline),
                [item.id for item, _ in observed],
                [self._command_evidence(check, item) for check, item in observed],
            )
        failed = [
            (check, item) for check, item in observed
            if not (
                self._regression_check_passed(baseline[check.id], item)
                if check.evidence_type == "REGRESSION"
                else self._passed(item)
            )
        ]
        status = FAIL if failed else PASS
        command = (failed or observed)[0][0].command
        exit_code = (failed or observed)[0][1].get("exit_code")
        details = "\n".join(self._observation_detail(check, item) for check, item in observed)
        summary = f"{criterion.id} {'passed' if status == PASS else 'failed'} {len(observed)} planned command(s)"
        return CriterionResult(
            criterion.id, status, criterion.evidence_type, "COMMAND", command, exit_code,
            summary, details, [item.id for item, _ in observed],
            [self._command_evidence(check, item) for check, item in observed],
        )

    def _regression_check_passed(
        self,
        before: dict[str, Any],
        current: dict[str, Any],
    ) -> bool:
        if self._passed(before):
            return self._passed(current)
        if self._passed(current):
            return True
        before_names = self._failure_names(before)
        current_names = self._failure_names(current)
        return not (before_names and current_names and current_names - before_names)

    def _regression_delta(
        self,
        state: AgentState,
        checks: list[VerificationCheck],
        observations: dict[str, dict[str, Any]],
    ) -> tuple[list[str], list[str], list[str]]:
        baseline = {item["verification_id"]: item["observation"] for item in state.baseline}
        baseline_failures: list[str] = []
        current_failures: list[str] = []
        new_failures: list[str] = []
        for check in checks:
            if check.evidence_type != "REGRESSION" or check.id not in observations:
                continue
            before = baseline.get(check.id)
            current = observations[check.id]
            before_names = self._failure_names(before) if before else set()
            current_names = self._failure_names(current)
            if before and not self._passed(before):
                baseline_failures.extend(sorted(before_names or {check.id}))
            if not self._passed(current):
                current_failures.extend(sorted(current_names or {check.id}))
            if before and self._passed(before) and not self._passed(current):
                new_failures.extend(sorted(current_names or {check.id}))
            elif before and not self._passed(before) and not self._passed(current) and before_names and current_names:
                new_failures.extend(sorted(current_names - before_names))
        return sorted(set(baseline_failures)), sorted(set(current_failures)), sorted(set(new_failures))

    @staticmethod
    def _passed(observation: dict[str, Any]) -> bool:
        return observation.get("status") == "SUCCESS" and observation.get("exit_code") == 0

    @staticmethod
    def _failure_names(observation: dict[str, Any] | None) -> set[str]:
        if not observation:
            return set()
        text = f"{observation.get('stdout', '')}\n{observation.get('stderr', '')}"
        patterns = (
            r"^(?:FAIL|ERROR):\s+([^\s(]+(?:\.[^\s(]+)*)",
            r"^FAILED\s+([^\s]+)",
            r"^([^\s]+)\s+\.\.\.\s+(?:FAIL|ERROR)$",
        )
        names: set[str] = set()
        for line in text.splitlines():
            for pattern in patterns:
                match = re.search(pattern, line.strip())
                if match:
                    names.add(match.group(1))
                    break
        return names

    @staticmethod
    def _observation_detail(check: VerificationCheck, observation: dict[str, Any]) -> str:
        output = (observation.get("stdout") or observation.get("stderr") or "").strip()
        return f"{check.id}: exit={observation.get('exit_code')} {output[:1200]}"

    @staticmethod
    def _command_evidence(check: VerificationCheck, observation: dict[str, Any]) -> dict[str, Any]:
        return {
            "verification_id": check.id,
            "command": observation.get("command", check.command),
            "status": observation.get("status"),
            "exit_code": observation.get("exit_code"),
            "stdout": observation.get("stdout", ""),
            "stderr": observation.get("stderr", ""),
            "truncated": observation.get("truncated", False),
        }

    @staticmethod
    def _current_step_criteria(state: AgentState) -> list[str]:
        step = next((item for item in state.execution_plan if item.step_id == state.current_step), None)
        return list(step.related_acceptance_criteria) if step else []

    @staticmethod
    def _log(result: VerificationResult) -> None:
        print("\n[Verification]", flush=True)
        print(f"Mode: {result.mode}", flush=True)
        for kind, items in (("Sanity", result.sanity_results), ("Target", result.target_results), ("Regression", result.regression_results)):
            print(f"{kind}:", flush=True)
            for item in items:
                print(f"- {item['criterion_id']} {item['status']}", flush=True)
        print(f"New failures: {len(result.new_failures)}", flush=True)
        for failure in result.new_failures:
            print(f"- {failure}", flush=True)
        print(f"Overall: {result.overall_status}", flush=True)
