"""Offline recovery demo using an isolated nested Git repository."""

import os
import shutil
import stat
import subprocess
import uuid
from pathlib import Path

from agent_state import EXECUTING, AgentState
from edit_models import REGRESSED, VERIFIED
from tool_executor import ToolExecutor


def git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        capture_output=True,
        text=True,
        check=True,
        shell=False,
    )
    return result.stdout.strip()


def remove_readonly(function, path, excinfo) -> None:
    os.chmod(path, stat.S_IWRITE)
    function(path)


def apply(tools: ToolExecutor, state: AgentState, old: str, new: str, intent: str) -> dict:
    return tools.call(state, "apply_patch", {
        "file": "app.py",
        "operation": "replace",
        "intent": intent,
        "symbol": "value",
        "old_block": old,
        "new_block": new,
    })


def main() -> None:
    runtime = Path(__file__).parent / ".demo_checkpoint_runtime" / uuid.uuid4().hex
    repo = runtime / "target_repo"
    plain = runtime / "plain_workspace"
    repo.mkdir(parents=True)
    plain.mkdir()
    try:
        git(repo, "init")
        git(repo, "config", "user.name", "Checkpoint Demo")
        git(repo, "config", "user.email", "checkpoint@example.invalid")
        (repo / "app.py").write_text("def value():\n    return 1\n", encoding="utf-8")
        git(repo, "add", "--", "app.py")
        git(repo, "commit", "-m", "initial")

        tools = ToolExecutor(repo)
        manager = tools.checkpoint_manager
        state = AgentState("checkpoint demo", current_phase=EXECUTING)
        print("\n=== clean repo initial checkpoint ===")
        print(manager.get_current_checkpoint())

        initial_head = git(repo, "rev-parse", "HEAD")
        first = apply(tools, state, "return 1", "return 2", "verified change")
        print("\n=== patch creates ChangeSet, not commit ===")
        print({"changeset": first["change_set"]["id"], "head_unchanged": git(repo, "rev-parse", "HEAD") == initial_head})

        manager.update_change_verification(state, first["change_set"]["id"], VERIFIED)
        print("\n=== mark_stable creates checkpoint commit ===")
        print(manager.mark_stable(state, "verified demo behavior", "demo_verification_001"))

        regressed = apply(tools, state, "return 2", "return 3", "regressed change")
        manager.update_change_verification(state, regressed["change_set"]["id"], REGRESSED)
        print("\n=== latest ChangeSet safe undo ===")
        print(manager.undo_changeset(state, regressed["change_set"]["id"], "regression detected"))

        covered = apply(tools, state, "return 2", "return 3", "covered change")
        apply(tools, state, "return 3", "return 4", "later covering change")
        print("\n=== covered ChangeSet refuses undo ===")
        print(manager.undo_changeset(state, covered["change_set"]["id"], "unsafe old undo"))

        (repo / "manual.txt").write_text("external edit", encoding="utf-8")
        print("\n=== unknown change blocks rollback ===")
        print(manager.rollback_last_stable(state, "confused state"))
        (repo / "manual.txt").unlink()
        print("\n=== rollback to latest stable checkpoint ===")
        print(manager.rollback_last_stable(state, "confused state"))

        (plain / "app.py").write_text("value = 1\n", encoding="utf-8")
        plain_tools = ToolExecutor(plain)
        plain_state = AgentState("plain demo", current_phase=EXECUTING)
        plain_change = plain_tools.call(plain_state, "apply_patch", {
            "file": "app.py", "operation": "replace", "intent": "plain change",
            "old_block": "value = 1", "new_block": "value = 2",
        })
        print("\n=== non-Git workspace ChangeSet undo ===")
        print(plain_tools.checkpoint_manager.undo_changeset(
            plain_state, plain_change["change_set"]["id"], "local recovery"
        ))
    finally:
        shutil.rmtree(runtime, onexc=remove_readonly)


if __name__ == "__main__":
    main()
