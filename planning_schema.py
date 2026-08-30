"""Deterministic validation and repair prompts for PLANNING structured output."""

from __future__ import annotations

import json
import re
from typing import Any


MAX_PLANNING_REPAIR_ATTEMPTS = 2
ACCEPTANCE_CRITERION_CORE_FIELDS = {
    "id", "description", "criticality", "verification_mode", "evidence_type",
}


class PlanningSchemaError(ValueError):
    def __init__(self, errors: str | list[str]):
        self.errors = [errors] if isinstance(errors, str) else list(errors)
        super().__init__("\n".join(self.errors))


def validate_schema(value: Any, schema: dict[str, Any], path: str = "$") -> None:
    """Validate the schema subset and report every independently invalid path."""
    errors: list[str] = []
    _collect_schema_errors(value, schema, path, errors)
    if errors:
        raise PlanningSchemaError(errors)


def _collect_schema_errors(
    value: Any, schema: dict[str, Any], path: str, errors: list[str],
) -> None:
    expected = schema.get("type")
    if expected is not None and not _matches_type(value, expected):
        errors.append(f"{path}: expected {_type_label(expected)}, got {type(value).__name__}")
        return
    if isinstance(value, dict):
        required = schema.get("required", [])
        for name in required:
            if name not in value:
                errors.append(f"{path}.{name}: missing required field")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extras = sorted(set(value) - set(properties))
            for extra in extras:
                errors.append(f"{path}.{extra}: unexpected field")
        for name, child in value.items():
            if name in properties:
                _collect_schema_errors(child, properties[name], f"{path}.{name}", errors)
    elif isinstance(value, list):
        minimum = schema.get("minItems")
        if minimum is not None and len(value) < minimum:
            errors.append(f"{path}: expected at least {minimum} item(s), got {len(value)}")
        child_schema = schema.get("items")
        if child_schema:
            for index, child in enumerate(value):
                _collect_schema_errors(child, child_schema, f"{path}[{index}]", errors)
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: expected one of {schema['enum']}, got {value!r}")


def build_repair_instruction(payload: dict[str, Any], error: str, attempt: int) -> str:
    return """The submitted PLANNING structure failed local validation.
Repair only the JSON structure and call submit_plan again. Do not re-explain the task,
do not inspect files, and do not change the meaning or IDs of existing Acceptance
Criteria. The Acceptance Criterion core fields id, description, criticality,
verification_mode, and evidence_type are immutable. Modify only fields named by the
complete validation error list; preserve every other field. Do not invent default filler text.
verification_method must state a concrete command, test, input/output check, or manual
procedure and its required result. Return only a submit_plan tool call.

Repair attempt: {attempt}/{maximum}
Complete validation errors (one path per line):
{error}
Invalid submit_plan arguments:
{payload}
""".format(
        attempt=attempt,
        maximum=MAX_PLANNING_REPAIR_ATTEMPTS,
        error=error,
        payload=json.dumps(payload, ensure_ascii=False, indent=2),
    )


def validate_repair_scope(
    original: dict[str, Any], repaired: dict[str, Any], validation_error: str,
) -> None:
    """Allow a repair to change only this round's invalid fields, never AC semantics."""
    allowed_paths = _validation_error_paths(validation_error)
    if not allowed_paths:
        raise PlanningSchemaError("$: validation error does not identify a repairable field")
    before = original.get("acceptance_criteria")
    after = repaired.get("acceptance_criteria")
    if not isinstance(before, list) or not isinstance(after, list) or len(before) != len(after):
        raise PlanningSchemaError("$.acceptance_criteria: repair changed the criteria collection")
    for index, original_item in enumerate(before):
        if not isinstance(original_item, dict) or not isinstance(after[index], dict):
            continue
        for field in ACCEPTANCE_CRITERION_CORE_FIELDS:
            if original_item.get(field) != after[index].get(field):
                raise PlanningSchemaError(
                    f"$.acceptance_criteria[{index}].{field}: repair changed protected core semantics"
                )
    changed_paths: list[str] = []
    _collect_changed_paths(original, repaired, "$", changed_paths)
    disallowed = [path for path in changed_paths if path not in allowed_paths]
    if disallowed:
        allowed_label = ", ".join(sorted(allowed_paths))
        raise PlanningSchemaError(f"{disallowed[0]}: repair changed a field outside [{allowed_label}]")


def _validation_error_paths(error: str) -> set[str]:
    pattern = r"(?m)^(\$(?:\.[A-Za-z_][A-Za-z0-9_]*|\[\d+\])+):"
    return set(re.findall(pattern, error))


def _collect_changed_paths(before: Any, after: Any, path: str, changed: list[str]) -> None:
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(set(before) | set(after)):
            child_path = f"{path}.{key}"
            if key not in before or key not in after:
                changed.append(child_path)
            else:
                _collect_changed_paths(before[key], after[key], child_path, changed)
        return
    if isinstance(before, list) and isinstance(after, list):
        if len(before) != len(after):
            changed.append(path)
            return
        for index, (left, right) in enumerate(zip(before, after)):
            _collect_changed_paths(left, right, f"{path}[{index}]", changed)
        return
    if before != after:
        changed.append(path)


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
