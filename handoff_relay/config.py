from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Role:
    name: str
    executable: str
    argv: tuple[str, ...]
    input_mode: str
    output_mode: str
    timeout_seconds: int


@dataclass(frozen=True)
class RelayConfig:
    path: Path
    workspace: Path
    run_root: Path
    planner_role: str
    worker_role: str
    roles: dict[str, Role]


def _resolve(base: Path, raw: str) -> Path:
    path = Path(raw)
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def _require_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} 必须是非空字符串")
    return value


def load_config(path: str | Path) -> RelayConfig:
    config_path = Path(path).resolve()
    try:
        loaded: Any = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"无法读取配置：{error}") from error
    if not isinstance(loaded, dict):
        raise ValueError("配置必须是 JSON 对象")
    raw: dict[str, Any] = loaded
    base = config_path.parent
    if raw.get("version") != 1:
        raise ValueError("只支持 version: 1")
    roles_raw = raw.get("roles", {})
    if not isinstance(roles_raw, dict):
        raise ValueError("roles 必须是对象")
    roles: dict[str, Role] = {}
    for name, item in roles_raw.items():
        if not isinstance(name, str) or not name:
            raise ValueError("角色名必须是非空字符串")
        if not isinstance(item, dict):
            raise ValueError(f"角色 {name} 必须是对象")
        argv = item.get("argv")
        if not isinstance(item.get("executable"), str) or not item["executable"]:
            raise ValueError(f"角色 {name} 缺少 executable")
        if not isinstance(argv, list) or not all(isinstance(arg, str) for arg in argv):
            raise ValueError(f"角色 {name} 的 argv 必须是字符串数组")
        input_mode, output_mode = item.get("input"), item.get("output")
        if input_mode not in {"prompt", "json-file"} or output_mode != "json-file":
            raise ValueError(f"角色 {name} 的 input/output 协议不受支持")
        timeout = item.get("timeout_seconds", 900)
        if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 1:
            raise ValueError(f"角色 {name} 的 timeout_seconds 必须为正整数")
        roles[name] = Role(name, item["executable"], tuple(argv), input_mode, output_mode, timeout)
    planner, worker = raw.get("planner_role"), raw.get("worker_role")
    if not isinstance(planner, str) or not isinstance(worker, str) or planner not in roles or worker not in roles:
        raise ValueError("planner_role 和 worker_role 必须引用已定义角色")
    return RelayConfig(
        path=config_path,
        workspace=_resolve(base, _require_text(raw.get("workspace", "."), "workspace")),
        run_root=_resolve(base, _require_text(raw.get("run_root", ".agent-relay/runs"), "run_root")),
        planner_role=planner,
        worker_role=worker,
        roles=roles,
    )
