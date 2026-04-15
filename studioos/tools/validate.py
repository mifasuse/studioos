"""Minimal JSON-schema validator — supports the shapes StudioOS tools use.

We intentionally avoid pulling in `jsonschema` for this. Tools declare very
simple shapes: type=object, properties, required, type constraints, enums.
If we ever need $ref / oneOf / allOf we can swap in jsonschema.
"""
from __future__ import annotations

from typing import Any


class SchemaError(ValueError):
    """Raised when args don't match the tool's input_schema."""


_PY_TYPES: dict[str, type | tuple[type, ...]] = {
    "object": dict,
    "array": list,
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "null": type(None),
}


def _check(value: Any, schema: dict[str, Any], path: str) -> None:
    expected = schema.get("type")
    if expected is not None:
        py = _PY_TYPES.get(expected)
        if py is None:
            raise SchemaError(f"unknown type {expected!r} at {path}")
        if expected == "integer" and isinstance(value, bool):
            raise SchemaError(f"{path}: expected integer, got bool")
        if not isinstance(value, py):
            raise SchemaError(
                f"{path}: expected {expected}, got {type(value).__name__}"
            )

    enum = schema.get("enum")
    if enum is not None and value not in enum:
        raise SchemaError(f"{path}: value {value!r} not in enum {enum}")

    if expected == "object":
        props: dict[str, Any] = schema.get("properties") or {}
        required: list[str] = schema.get("required") or []
        for key in required:
            if key not in value:
                raise SchemaError(f"{path}.{key}: required property missing")
        additional = schema.get("additionalProperties", True)
        for key, val in value.items():
            sub = props.get(key)
            if sub is None:
                if additional is False:
                    raise SchemaError(
                        f"{path}.{key}: additional properties not allowed"
                    )
                continue
            _check(val, sub, f"{path}.{key}")

    if expected == "array":
        items = schema.get("items")
        if items is not None:
            for i, val in enumerate(value):
                _check(val, items, f"{path}[{i}]")


def validate(args: Any, schema: dict[str, Any]) -> None:
    """Raise SchemaError if args don't match schema. No return value."""
    _check(args, schema, path="$")
