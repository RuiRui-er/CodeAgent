"""Tool definitions, side-effect classes, and phase permissions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_state import DEBUGGING, EXECUTING, PLANNING, VERIFYING


READ_ONLY = "READ_ONLY"
MUTATING = "MUTATING"
EXECUTION = "EXECUTION"
CONTROL = "CONTROL"


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    category: str
    description: str
    parameters: dict[str, Any]

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def _object(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


TOOLS = {
    "read_file": ToolDefinition(
        "read_file",
        READ_ONLY,
        "Read a UTF-8 workspace file, optionally selecting an inclusive line range.",
        _object({
            "path": {"type": "string"},
            "start_line": {"type": "integer", "minimum": 1},
            "end_line": {"type": "integer", "minimum": 1},
        }, ["path"]),
    ),
    "list_dir": ToolDefinition(
        "list_dir",
        READ_ONLY,
        "List a workspace directory up to a bounded depth.",
        _object({
            "path": {"type": "string", "description": "Relative directory; default is workspace root."},
            "max_depth": {"type": "integer", "minimum": 1, "maximum": 8},
        }),
    ),
    "search_code": ToolDefinition(
        "search_code",
        READ_ONLY,
        "Search for a literal string in workspace text files.",
        _object({
            "query": {"type": "string"},
            "path": {"type": "string", "description": "Relative file or directory; default is root."},
        }, ["query"]),
    ),
    "apply_patch": ToolDefinition(
        "apply_patch",
        MUTATING,
        "Apply one local structured edit, or select a candidate from the most recent ambiguous edit.",
        {
            "type": "object",
            "properties": {
                "file": {"type": "string"},
                "operation": {"type": "string", "enum": ["replace", "insert", "delete"]},
                "intent": {"type": "string"},
                "symbol": {"type": "string"},
                "anchor": {"type": "string"},
                "old_block": {"type": "string"},
                "new_block": {"type": "string"},
                "candidate_id": {"type": "string"},
            },
            "required": ["file", "operation", "intent"],
            "additionalProperties": False,
        },
    ),
    "run_command": ToolDefinition(
        "run_command",
        EXECUTION,
        "Run one development command under the workspace command policy.",
        _object({
            "command": {
                "oneOf": [
                    {"type": "string"},
                    {"type": "array", "items": {"type": "string"}, "minItems": 1},
                ]
            },
            "timeout": {"type": "integer", "minimum": 1, "maximum": 120},
        }, ["command"]),
    ),
    "finish": ToolDefinition(
        "finish",
        CONTROL,
        "Request the end of execution and transition to VERIFYING. This does not mark the task done.",
        _object({"summary": {"type": "string"}}),
    ),
    # Compatibility only. New edits should prefer apply_patch.
    "write_file": ToolDefinition(
        "write_file",
        MUTATING,
        "Compatibility tool: create or replace a UTF-8 workspace file.",
        _object({"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
    ),
}


PHASE_PERMISSIONS = {
    PLANNING: {"read_file", "list_dir", "search_code", "run_command"},
    EXECUTING: {"read_file", "list_dir", "search_code", "apply_patch", "run_command", "finish"},
    VERIFYING: {"read_file", "search_code", "run_command"},
    DEBUGGING: {"read_file", "list_dir", "search_code", "apply_patch", "run_command", "finish"},
}


def tool_schemas_for_phase(phase: str) -> list[dict[str, Any]]:
    return [TOOLS[name].schema() for name in TOOLS if name in PHASE_PERMISSIONS[phase]]


def permission_result(phase: str, name: str) -> dict[str, Any] | None:
    if name not in TOOLS:
        return {
            "status": "BLOCKED",
            "tool": name,
            "reason": "unknown tool",
            "available_tools": sorted(PHASE_PERMISSIONS[phase]),
        }
    if name in PHASE_PERMISSIONS[phase]:
        return None
    return {
        "status": "BLOCKED",
        "tool": name,
        "category": TOOLS[name].category,
        "reason": f"{TOOLS[name].category.lower()} tools are not allowed during {phase}",
        "available_tools": sorted(PHASE_PERMISSIONS[phase]),
    }
