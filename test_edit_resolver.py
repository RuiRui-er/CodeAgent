import unittest
from pathlib import Path
from unittest.mock import patch

from agent_state import EXECUTING, AgentState
from edit_models import AMBIGUOUS_TARGET, APPLIED, ResolvedEdit, STALE_EDIT, StructuredEditRequest
from edit_resolver import EditResolver
from tool_executor import ToolExecutor
from coding_agent import _record_tool_event
from tool_registry import tool_schemas_for_phase


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


class EditResolverTests(unittest.TestCase):
    def setUp(self):
        self.resolver = EditResolver()
        self.path = Path("parser.py")

    def test_model_edit_surface_exposes_structured_patch_not_write_file(self):
        names = {item["function"]["name"] for item in tool_schemas_for_phase(EXECUTING)}
        self.assertIn("apply_patch", names)
        self.assertNotIn("write_file", names)

    def test_symbol_anchor_and_unique_content_resolution(self):
        symbol = self.resolver.resolve(
            StructuredEditRequest("parser.py", "replace", "handle empty", "Parser.parse", None, "return None", "return ''"),
            SOURCE,
            self.path,
        )
        self.assertIsInstance(symbol, ResolvedEdit)
        self.assertEqual(symbol.resolution, "SYMBOL_SCOPE")

        anchor = self.resolver.resolve(
            StructuredEditRequest("parser.py", "replace", "trim value", None, "def parse(self, value):", "return value.strip()", "return value"),
            SOURCE,
            self.path,
        )
        self.assertIsInstance(anchor, ResolvedEdit)
        self.assertEqual(anchor.resolution, "ANCHOR_LOCAL_CONTEXT")

        unique = self.resolver.resolve(
            StructuredEditRequest("parser.py", "replace", "rename class", None, None, "class Parser:", "class ValueParser:"),
            SOURCE,
            self.path,
        )
        self.assertIsInstance(unique, ResolvedEdit)
        self.assertEqual(unique.resolution, "UNIQUE_CONTENT")

    def test_ambiguous_candidates_can_be_selected_without_resending_edit(self):
        result = self.resolver.resolve(
            StructuredEditRequest("parser.py", "replace", "change one return", None, None, "return None", "return 0"),
            SOURCE,
            self.path,
        )
        self.assertEqual(result["status"], AMBIGUOUS_TARGET)
        self.assertEqual(result["candidate_count"], 3)
        selected = self.resolver.resolve_candidate("B", SOURCE)
        self.assertIsInstance(selected, ResolvedEdit)
        self.assertEqual(selected.resolution, "CANDIDATE_SELECTION")

    def test_symbol_scope_detects_stale_edit(self):
        result = self.resolver.resolve(
            StructuredEditRequest("parser.py", "replace", "stale", "Parser.parse", None, "return missing", "return 1"),
            SOURCE,
            self.path,
        )
        self.assertEqual(result["status"], STALE_EDIT)
        self.assertIn("current_context", result)

    def test_insert_and_delete_are_supported(self):
        inserted = self.resolver.resolve(
            StructuredEditRequest("parser.py", "insert", "add guard", "Parser.parse", "def parse(self, value):", None, "\n        value = value or ''"),
            SOURCE,
            self.path,
        )
        self.assertIsInstance(inserted, ResolvedEdit)
        self.assertEqual(inserted.resolution, "SYMBOL_ANCHOR")

        deleted = self.resolver.resolve(
            StructuredEditRequest("parser.py", "delete", "remove trim", "Parser.parse", None, "        return value.strip()\n", None),
            SOURCE,
            self.path,
        )
        self.assertIsInstance(deleted, ResolvedEdit)
        self.assertEqual(deleted.after, "")

    def test_ambiguous_candidates_are_preserved_in_recent_actions(self):
        state = AgentState("edit", current_phase=EXECUTING)
        result = self.resolver.resolve(
            StructuredEditRequest("parser.py", "replace", "one return", None, None, "return None", "return 0"),
            SOURCE,
            self.path,
        )
        _record_tool_event(state, "apply_patch", {}, result, [], allow_phase_changes=True)
        candidates = state.recent_actions[-1]["observation"]["candidates"]
        self.assertEqual([item["id"] for item in candidates], ["A", "B", "C"])

    def test_executor_records_changeset_and_blocks_workspace_escape(self):
        tools = ToolExecutor(Path(__file__).parent / "demo_project")
        state = AgentState("edit", current_phase=EXECUTING, current_step="STEP-1")
        with patch.object(Path, "read_text", side_effect=[SOURCE, SOURCE]), patch.object(Path, "write_text") as write:
            result = tools.call(state, "apply_patch", {
                "file": "calculator.py",
                "operation": "replace",
                "intent": "handle empty",
                "symbol": "Parser.parse",
                "old_block": "return None",
                "new_block": "return ''",
            })
        self.assertEqual(result["status"], APPLIED)
        self.assertEqual(state.change_sets[0]["step_id"], "STEP-1")
        write.assert_called_once()

        blocked = tools.call(state, "apply_patch", {
            "file": "../outside.py",
            "operation": "replace",
            "intent": "escape",
            "old_block": "x",
            "new_block": "y",
        })
        self.assertEqual(blocked["status"], "BLOCKED")


if __name__ == "__main__":
    unittest.main()
