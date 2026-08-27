"""Offline demonstration of tool permissions and command policy."""

import sys
from pathlib import Path

from agent_state import EXECUTING, PLANNING, AgentState
from tool_executor import ToolExecutor


def show(title: str, result: dict) -> None:
    print(f"\n=== {title} ===")
    print(result)


def main() -> None:
    workspace = Path(__file__).parent / "demo_project"
    tools = ToolExecutor(workspace, confirm_callback=lambda command, reason: False)
    state = AgentState("Demonstrate tool safety", current_phase=PLANNING)

    show("PLANNING read_file succeeds", tools.call(state, "read_file", {"path": "calculator.py"}))
    show(
        "PLANNING mutation is blocked",
        tools.call(state, "apply_patch", {"path": "calculator.py", "old_text": "x", "new_text": "y"}),
    )

    state.set_phase(EXECUTING)
    show("EXECUTING safe command succeeds", tools.call(state, "run_command", {"command": [sys.executable, "--version"]}))
    show("CONFIRM command is rejected by user", tools.call(state, "run_command", {"command": ["git", "clean", "-fd"]}))
    show("DENY command is rejected", tools.call(state, "run_command", {"command": ["shutdown", "/s"]}))
    show("finish enters VERIFYING", tools.call(state, "finish", {"summary": "Implementation ready for verification."}))
    print(f"Current phase: {state.current_phase}")


if __name__ == "__main__":
    main()
