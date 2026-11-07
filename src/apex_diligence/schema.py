"""A small JSON-schema validator and serialiser for tool inputs and outputs.

The model sees the same schema we validate against. Validating on our side
means a malformed tool call becomes a clear error message the model can fix,
never a crash or a silently ignored argument.
"""

from __future__ import annotations

import dataclasses
import json
import re
from datetime import date
from decimal import Decimal

from .money import Money, money_str


class ToolInputError(ValueError):
    pass


def validate(value, schema: dict, path: str = "input") -> None:
    if "anyOf" in schema:
        errors = []
        for option in schema["anyOf"]:
            try:
                validate(value, option, path)
                return
            except ToolInputError as e:
                errors.append(str(e))
        raise ToolInputError(f"{path}: does not match any allowed form ({'; '.join(errors)})")

    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict):
            raise ToolInputError(f"{path} must be an object")
        if "properties" not in schema:
            return  # a free-form object; its contents are validated by the tool that uses it
        props = schema["properties"]
        unknown = sorted(set(value) - set(props))
        if unknown:
            raise ToolInputError(f"{path}: unknown field(s) {', '.join(unknown)}; allowed: {', '.join(props) or 'none'}")
        for name in schema.get("required", []):
            if name not in value:
                raise ToolInputError(f"{path}: missing required field '{name}'")
        for name, item in value.items():
            validate(item, props[name], f"{path}.{name}")
    elif kind == "array":
        if not isinstance(value, list):
            raise ToolInputError(f"{path} must be a list")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise ToolInputError(f"{path} has {len(value)} items; at most {schema['maxItems']} allowed")
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise ToolInputError(f"{path} needs at least {schema['minItems']} items")
        for i, item in enumerate(value):
            validate(item, schema.get("items", {}), f"{path}[{i}]")
    elif kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ToolInputError(f"{path} must be a number")
        if value != value or value in (float("inf"), float("-inf")):
            raise ToolInputError(f"{path} must be a finite number")
    elif kind == "integer":
        if isinstance(value, bool) or not (isinstance(value, int) or (isinstance(value, float) and value.is_integer())):
            raise ToolInputError(f"{path} must be a whole number")
    elif kind == "string":
        if not isinstance(value, str):
            raise ToolInputError(f"{path} must be a string")
        if "enum" in schema and value not in schema["enum"]:
            raise ToolInputError(f"{path} must be one of: {', '.join(schema['enum'])}")
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
            raise ToolInputError(f"{path} {value!r} is not in the expected format ({schema.get('description', schema['pattern'])})")
        if len(value) > schema.get("maxLength", 500):
            raise ToolInputError(f"{path} is too long")
    elif kind == "boolean":
        if not isinstance(value, bool):
            raise ToolInputError(f"{path} must be true or false")


def to_json(obj):
    """Convert results into JSON-ready values. Money -> "1234.50", Decimal -> "0.1234", date -> ISO."""
    if isinstance(obj, Money):
        return money_str(obj)
    if isinstance(obj, bool) or obj is None or isinstance(obj, (int, str)):
        return obj
    if isinstance(obj, Decimal):
        return format(obj, "f")
    if isinstance(obj, float):
        raise TypeError("floats are not allowed in tool output")
    if isinstance(obj, date):
        return obj.isoformat()
    if dataclasses.is_dataclass(obj):
        return {f.name: to_json(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, dict):
        return {str(k): to_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_json(v) for v in obj]
    raise TypeError(f"cannot serialise {type(obj).__name__}")


def canonical(data) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
