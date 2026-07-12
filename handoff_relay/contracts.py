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
        "steps": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}},
        "acceptance": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}},
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
        "evidence": {"type": "array", "items": {"type": "string", "minLength": 1}},
    },
}


def write_schema(path: Path, schema: dict[str, Any]) -> None:
    path.write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_contract(path: Path, expected: str) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"无法读取 {expected} JSON：{error}") from error
    if not isinstance(data, dict) or data.get("contract") != expected:
        raise ValueError(f"{path} 不是有效的 {expected} 合同")
    if expected == "plan-v1":
        required_lists = ("steps", "acceptance")
        required_strings = ("goal",)
    else:
        required_lists = ("evidence",)
        required_strings = ("summary", "status")
        if data.get("status") not in {"passed", "failed", "blocked"}:
            raise ValueError("result-v1 的 status 必须为 passed、failed 或 blocked")
    if any(not isinstance(data.get(key), str) or not data[key].strip() for key in required_strings):
        raise ValueError(f"{expected} 缺少非空文本字段")
    if any(not isinstance(data.get(key), list) or not data[key] or not all(isinstance(v, str) and v.strip() for v in data[key]) for key in required_lists):
        raise ValueError(f"{expected} 缺少非空字符串列表")
    return data
