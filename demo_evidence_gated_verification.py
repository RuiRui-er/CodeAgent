"""Offline evidence-gate demo using isolated nested Git repositories."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import uuid
from pathlib import Path

from agent_state import EXECUTING, AcceptanceCriterion, AgentState, VerificationCheck
from tool_executor import ToolExecutor
from verification_engine import VerificationEngine
from agent_events import FINISH_REQUESTED, AgentEvent
from agent_orchestrator import AgentOrchestrator
from coding_agent import _transition_verification


ROOT = Path(__file__).parent / ".demo_verification_runtime"
RUNNER = """from pathlib import Path
import sys
state = Path('evidence_state.txt').read_text(encoding='utf-8')
mode = sys.argv[1]
if mode == 'target':
    ok = 'target_pass' in state
    print('target behavior:', 'PASS' if ok else 'FAIL')
    raise SystemExit(0 if ok else 1)
if mode == 'regression':
    if 'regression_old_new' in state:
        print('FAILED tests/test_old.py::test_old')
        print('FAILED tests/test_new.py::test_new')
        raise SystemExit(1)
    if 'regression_old' in state:
        print('FAILED tests/test_old.py::test_old')
        raise SystemExit(1)
    print('regression suite: PASS')
    raise SystemExit(0)
print('syntax sanity: PASS')
"""


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True,
        check=True, shell=False,
    )
    return result.stdout.strip()


def remove_readonly(function, path, excinfo):
    os.chmod(path, stat.S_IWRITE)
    function(path)


def make_repo(label: str) -> tuple[Path, ToolExecutor]:
    root = ROOT / f"{label}_{uuid.uuid4().hex}" / "target_repo"
    root.mkdir(parents=True)
    git(root, "init")
    git(root, "config", "user.name", "Verification Demo")
    git(root, "config", "user.email", "verification@example.invalid")
    (root / "evidence_runner.py").write_text(RUNNER, encoding="utf-8")
    (root / "evidence_state.txt").write_text("target_fail regression_old\n", encoding="utf-8")
    git(root, "add", "--", "evidence_runner.py", "evidence_state.txt")
    git(root, "commit", "-m", "initial frozen evidence fixture")
    return root, ToolExecutor(root)


def auto_criterion(identifier: str, evidence: str, criticality: str = "CRITICAL") -> AcceptanceCriterion:
    return AcceptanceCriterion(identifier, identifier, criticality, "AUTO", evidence, f"frozen {identifier} command")


def auto_check(identifier: str, evidence: str, criterion_id: str, baseline: bool = False) -> VerificationCheck:
    return VerificationCheck(
        identifier, identifier, "AUTO", evidence, f"run frozen {identifier}",
        [sys.executable, "evidence_runner.py", identifier], baseline, [criterion_id],
    )


def state_with_contract(tools: ToolExecutor, include_human: bool = False, human_critical: bool = False) -> AgentState:
    state = AgentState("evidence-gated demo", current_phase=EXECUTING)
    state.acceptance_criteria = [
        auto_criterion("target", "TARGET"),
        auto_criterion("regression", "REGRESSION", "NON_CRITICAL"),
    ]
    state.verification_contract = [
        auto_check("target", "TARGET", "target"),
        auto_check("regression", "REGRESSION", "regression", True),
    ]
    if include_human:
        criticality = "CRITICAL" if human_critical else "NON_CRITICAL"
        state.acceptance_criteria.append(
            AcceptanceCriterion("human", "human review", criticality, "HUMAN", "TARGET", "manual confirmation")
        )
        state.verification_contract.append(
            VerificationCheck("human", "human review", "HUMAN", "TARGET", "manual confirmation", None, False, ["human"])
        )
    baseline = tools.call(state, "run_command", {"command": [sys.executable, "evidence_runner.py", "regression"]})
    state.baseline = [{"verification_id": "regression", "observation": baseline}]
    return state


def apply_state(tools: ToolExecutor, state: AgentState, value: str) -> None:
    tools.call(state, "apply_patch", {
        "file": "evidence_state.txt", "operation": "replace", "intent": f"demo {value}",
        "old_block": "target_fail regression_old", "new_block": value,
    })


def run_scenario(label: str, value: str, include_human: bool = False, human_critical: bool = False) -> dict:
    root, tools = make_repo(label)
    state = state_with_contract(tools, include_human, human_critical)
    apply_state(tools, state, value)
    orchestrator = AgentOrchestrator()
    orchestrator.transition(state, AgentEvent(FINISH_REQUESTED, "demo final verification"))
    result = VerificationEngine(tools).run_final_verification(state)
    _transition_verification(state, result, orchestrator)
    print(f"\n=== {label} ===")
    print("overall:", result["overall_status"])
    print("baseline failures:", result["baseline_failures"])
    print("new failures:", result["new_failures"])
    print("phase:", state.current_phase)
    print("manual items:", result["manual_items"])
    print("recovery:", result.get("recovery_result"))
    print("checkpoint:", result.get("checkpoint_result"))
    return result


def main() -> None:
    ROOT.mkdir(exist_ok=True)
    try:
        run_scenario("baseline failure unchanged + target pass", "target_pass regression_old")
        run_scenario("critical pass + noncritical human", "target_pass regression_old", include_human=True)
        run_scenario("target pass + new regression", "target_pass regression_old_new")
        run_scenario("critical has no auto evidence", "target_pass regression_old", include_human=True, human_critical=True)
        failed = run_scenario("target fail + no regression", "target_fail regression_old")
        assert failed["overall_status"] == "UNVERIFIED"
        print("finish cannot reach DONE when target evidence fails: confirmed")
    finally:
        shutil.rmtree(ROOT, onexc=remove_readonly)


if __name__ == "__main__":
    main()
