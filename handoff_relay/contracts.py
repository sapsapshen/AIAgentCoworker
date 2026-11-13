from __future__ import annotations

import json
from pathlib import Path
from typing import Any


PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["contract", "goal", "steps", "acceptance"],
    "properties": {
        "contract": {"type": "string", "const": "plan-v1"},
        "goal": {"type": "string", "minLength": 1},
        "steps": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "string", "minLength": 1},
        },
        "acceptance": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "string", "minLength": 1},
        },
    },
}

RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["contract", "status", "summary", "evidence"],
    "properties": {
        "contract": {"type": "string", "const": "result-v1"},
        "status": {"enum": ["passed", "failed", "blocked"]},
        "summary": {"type": "string", "minLength": 1},
        "evidence": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "string", "minLength": 1},
        },
    },
}

_SCHEMAS = {"plan-v1": PLAN_SCHEMA, "result-v1": RESULT_SCHEMA}


def write_schema(path: Path, schema: dict[str, Any]) -> None:
    path.write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def validate_instance(data: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Validate data against the small JSON Schema subset used by the contracts."""
    if "enum" in schema and "type" not in schema:
        if data not in schema["enum"]:
            allowed = "、".join(str(item) for item in schema["enum"])
            return [f"{path} 必须是 {allowed} 之一"]
        return []

    expected = schema.get("type")
    if expected == "object":
        if not isinstance(data, dict):
            return [f"{path} 必须是对象"]
        errors: list[str] = []
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            properties = {}
        for key in schema.get("required", []):
            if key not in data:
                errors.append(f"{path} 缺少字段 {key}")
        if schema.get("additionalProperties") is False:
            for key in sorted(set(data) - set(properties)):
                errors.append(f"{path} 含有未声明字段 {key}")
        for key, subschema in properties.items():
            if key in data and isinstance(subschema, dict):
                errors.extend(validate_instance(data[key], subschema, f"{path}.{key}"))
        return errors

    if expected == "string":
        if not isinstance(data, str):
            return [f"{path} 必须是字符串"]
        if "const" in schema and data != schema["const"]:
            return [f"{path} 必须为 {schema['const']}"]
        min_length = schema.get("minLength")
        if isinstance(min_length, int) and (len(data) < min_length or (min_length > 0 and not data.strip())):
            return [f"{path} 必须是非空字符串"]
        return []

    if expected == "array":
        if not isinstance(data, list):
            return [f"{path} 必须是数组"]
        min_items = schema.get("minItems", 0)
        if isinstance(min_items, int) and len(data) < min_items:
            return [f"{path} 至少需要 {min_items} 项"]
        item_schema = schema.get("items")
        if not isinstance(item_schema, dict):
            return []
        errors = []
        for index, item in enumerate(data):
            errors.extend(validate_instance(item, item_schema, f"{path}[{index}]"))
        return errors

    return [f"{path} 使用了不支持的 schema"]


def read_contract(path: Path, expected: str) -> dict[str, Any]:
    schema = _SCHEMAS.get(expected)
    if schema is None:
        raise ValueError(f"未知合同类型：{expected}")
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"无法读取 {expected} JSON：{error}") from error
    errors = validate_instance(data, schema)
    if errors:
        preview = "；".join(errors[:8])
        raise ValueError(f"{path} 不是有效的 {expected} 合同：{preview}")
    if not isinstance(data, dict):
        raise ValueError(f"{path} 不是有效的 {expected} 合同")
    return data
