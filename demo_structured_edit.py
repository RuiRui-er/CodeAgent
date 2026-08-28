"""Offline resolver demo; it does not modify workspace files."""

from pathlib import Path

from edit_models import ResolvedEdit, StructuredEditRequest
from edit_resolver import EditResolver
from tool_executor import ToolExecutor
from agent_state import EXECUTING, AgentState


SOURCE = '''class Parser:
    def parse(self, value):
        if not value:
            return None
        return value.strip()

def first():
    return None

def second():
    return None
'''


def show(label: str, result) -> None:
    if isinstance(result, ResolvedEdit):
        result = {
            "status": "RESOLVED",
            "resolution": result.resolution,
            "symbol": result.symbol,
            "before": result.before,
            "after": result.after,
        }
    print(f"\n=== {label} ===")
    print(result)


def main() -> None:
    resolver = EditResolver()
    path = Path("parser.py")
    show("symbol unique", resolver.resolve(
        StructuredEditRequest("parser.py", "replace", "empty input", "Parser.parse", None, "return None", "return ''"), SOURCE, path
    ))
    show("anchor unique", resolver.resolve(
        StructuredEditRequest("parser.py", "replace", "trim", None, "def parse(self, value):", "return value.strip()", "return value"), SOURCE, path
    ))
    show("whole-file unique", resolver.resolve(
        StructuredEditRequest("parser.py", "replace", "rename", None, None, "class Parser:", "class ValueParser:"), SOURCE, path
    ))
    show("ambiguous candidates", resolver.resolve(
        StructuredEditRequest("parser.py", "replace", "one return", None, None, "return None", "return 0"), SOURCE, path
    ))
    show("candidate B", resolver.resolve_candidate("B", SOURCE))
    show("stale edit", resolver.resolve(
        StructuredEditRequest("parser.py", "replace", "stale", "Parser.parse", None, "return missing", "return 1"), SOURCE, path
    ))

    tools = ToolExecutor(Path(__file__).parent / "demo_project")
    state = AgentState("blocked demo", current_phase=EXECUTING)
    show("workspace escape blocked", tools.call(state, "apply_patch", {
        "file": "../outside.py", "operation": "replace", "intent": "escape", "old_block": "x", "new_block": "y"
    }))


if __name__ == "__main__":
    main()
