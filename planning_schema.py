"""Deterministic validation and repair prompts for PLANNING structured output."""

from __future__ import annotations

import json
from typing import Any


MAX_PLANNING_REPAIR_ATTEMPTS = 2


class PlanningSchemaError(ValueError):
    pass


def validate_schema(value: Any, schema: dict[str, Any], path: str = "$") -> None:
    """Validate the JSON-schema subset used by submit_plan with precise paths."""
    expected = schema.get("type")
    if expected is not None and not _matches_type(value, expected):
        raise PlanningSchemaError(
            f"{path}: expected {_type_label(expected)}, got {type(value).__name__}"
        )
    if isinstance(value, dict):
        required = schema.get("required", [])
        for name in required:
            if name not in value:
                raise PlanningSchemaError(f"{path}.{name}: missing required field")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extras = sorted(set(value) - set(properties))
            if extras:
                raise PlanningSchemaError(f"{path}.{extras[0]}: unexpected field")
        for name, child in value.items():
            if name in properties:
                validate_schema(child, properties[name], f"{path}.{name}")
    elif isinstance(value, list):
        minimum = schema.get("minItems")
        if minimum is not None and len(value) < minimum:
            raise PlanningSchemaError(f"{path}: expected at least {minimum} item(s), got {len(value)}")
        child_schema = schema.get("items")
        if child_schema:
            for index, child in enumerate(value):
                validate_schema(child, child_schema, f"{path}[{index}]")
    if "enum" in schema and value not in schema["enum"]:
        raise PlanningSchemaError(f"{path}: expected one of {schema['enum']}, got {value!r}")


def build_repair_instruction(payload: dict[str, Any], error: str, attempt: int) -> str:
    return """The submitted PLANNING structure failed local validation.
Repair only the JSON structure and call submit_plan again. Do not re-explain the task,
do not inspect files, and do not change the meaning or IDs of existing Acceptance
Criteria. Preserve every already-valid field. Do not invent default filler text.
verification_method must state a concrete command, test, input/output check, or manual
procedure and its required result. Return only a submit_plan tool call.

Repair attempt: {attempt}/{maximum}
Exact validation error: {error}
Invalid submit_plan arguments:
{payload}
""".format(
        attempt=attempt,
        maximum=MAX_PLANNING_REPAIR_ATTEMPTS,
        error=error,
        payload=json.dumps(payload, ensure_ascii=False, indent=2),
    )


def validate_acceptance_criteria_preserved(original: dict[str, Any], repaired: dict[str, Any]) -> None:
    """Reject repairs that rewrite any Acceptance Criteria field already supplied."""
    before = original.get("acceptance_criteria")
    after = repaired.get("acceptance_criteria")
    if not isinstance(before, list) or not isinstance(after, list) or len(before) != len(after):
        raise PlanningSchemaError("$.acceptance_criteria: repair changed the criteria collection")
    for index, original_item in enumerate(before):
        if not isinstance(original_item, dict) or not isinstance(after[index], dict):
            continue
        for field, value in original_item.items():
            if after[index].get(field) != value:
                raise PlanningSchemaError(
                    f"$.acceptance_criteria[{index}].{field}: repair changed an existing value"
                )


def _matches_type(value: Any, expected: str | list[str]) -> bool:
    options = [expected] if isinstance(expected, str) else expected
    return any(_matches_one(value, option) for option in options)


def _matches_one(value: Any, expected: str) -> bool:
    checks = {
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
        "string": lambda item: isinstance(item, str),
        "boolean": lambda item: isinstance(item, bool),
        "null": lambda item: item is None,
    }
    return checks.get(expected, lambda _item: True)(value)


def _type_label(expected: str | list[str]) -> str:
    return "/".join([expected] if isinstance(expected, str) else expected)
