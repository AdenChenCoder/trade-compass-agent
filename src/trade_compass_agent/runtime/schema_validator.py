from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SchemaError:
    path: str
    message: str


class SchemaValidationError(ValueError):
    def __init__(self, errors: list[SchemaError]) -> None:
        self.errors = errors
        detail = "; ".join(f"{e.path}: {e.message}" for e in errors)
        super().__init__(detail)


def validate_schema(instance: Any, schema: dict[str, Any]) -> None:
    """Validate the JSON-schema subset used by Trade Compass assets.

    This intentionally covers the production schemas in this repo without adding
    a runtime dependency: type, required, properties, items, enum, const, oneOf,
    anyOf, additionalProperties, min/max string length, and numeric bounds.
    """
    errors: list[SchemaError] = []
    _validate(instance, schema, "$", errors)
    if errors:
        raise SchemaValidationError(errors)


def check_schema(instance: Any, schema: dict[str, Any]) -> list[SchemaError]:
    errors: list[SchemaError] = []
    _validate(instance, schema, "$", errors)
    return errors


def _validate(value: Any, schema: dict[str, Any], path: str, errors: list[SchemaError]) -> None:
    if not isinstance(schema, dict):
        errors.append(SchemaError(path, "schema must be an object"))
        return

    if "oneOf" in schema:
        matches = [_branch_valid(value, branch) for branch in schema.get("oneOf") or []]
        if sum(1 for ok in matches if ok) != 1:
            errors.append(SchemaError(path, "must match exactly one oneOf schema"))
        return
    if "anyOf" in schema:
        if not any(_branch_valid(value, branch) for branch in schema.get("anyOf") or []):
            errors.append(SchemaError(path, "must match at least one anyOf schema"))
        return

    expected = schema.get("type")
    if expected and not _type_matches(value, expected):
        errors.append(SchemaError(path, f"expected {expected}, got {type(value).__name__}"))
        return

    if "enum" in schema and value not in schema["enum"]:
        errors.append(SchemaError(path, f"must be one of {schema['enum']}"))
        return

    if "const" in schema and value != schema["const"]:
        errors.append(SchemaError(path, f"must equal {schema['const']!r}"))
        return

    if isinstance(value, dict):
        required = schema.get("required") or []
        for key in required:
            if key not in value:
                errors.append(SchemaError(f"{path}.{key}", "required property missing"))

        props = schema.get("properties") or {}
        for key, item in value.items():
            if key in props:
                _validate(item, props[key], f"{path}.{key}", errors)
            elif schema.get("additionalProperties") is False:
                errors.append(SchemaError(f"{path}.{key}", "additional property not allowed"))
        return

    if isinstance(value, list):
        if "maxItems" in schema and len(value) > int(schema["maxItems"]):
            errors.append(SchemaError(path, f"too many items: {len(value)}"))
        if "minItems" in schema and len(value) < int(schema["minItems"]):
            errors.append(SchemaError(path, f"too few items: {len(value)}"))
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _validate(item, item_schema, f"{path}[{index}]", errors)
        return

    if isinstance(value, str):
        if "maxLength" in schema and len(value) > int(schema["maxLength"]):
            errors.append(SchemaError(path, f"string longer than {schema['maxLength']}"))
        if "minLength" in schema and len(value) < int(schema["minLength"]):
            errors.append(SchemaError(path, f"string shorter than {schema['minLength']}"))
        return

    if isinstance(value, int | float) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(SchemaError(path, f"number less than {schema['minimum']}"))
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(SchemaError(path, f"number greater than {schema['maximum']}"))


def _branch_valid(value: Any, schema: dict[str, Any]) -> bool:
    errors: list[SchemaError] = []
    _validate(value, schema, "$", errors)
    return not errors


def _type_matches(value: Any, expected: Any) -> bool:
    if isinstance(expected, list):
        return any(_type_matches(value, item) for item in expected)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return True
