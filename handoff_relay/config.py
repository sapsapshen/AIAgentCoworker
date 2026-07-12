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


def load_config(path: str | Path) -> RelayConfig:
    config_path = Path(path).resolve()
    try:
        raw: dict[str, Any] = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"无法读取配置：{error}") from error
    base = config_path.parent
    if raw.get("version") != 1:
        raise ValueError("只支持 version: 1")
    roles: dict[str, Role] = {}
    for name, item in raw.get("roles", {}).items():
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
        if not isinstance(timeout, int) or timeout < 1:
            raise ValueError(f"角色 {name} 的 timeout_seconds 必须为正整数")
        roles[name] = Role(name, item["executable"], tuple(argv), input_mode, output_mode, timeout)
    planner, worker = raw.get("planner_role"), raw.get("worker_role")
    if planner not in roles or worker not in roles:
        raise ValueError("planner_role 和 worker_role 必须引用已定义角色")
    return RelayConfig(
        path=config_path,
        workspace=_resolve(base, raw.get("workspace", ".")),
        run_root=_resolve(base, raw.get("run_root", ".agent-relay/runs")),
        planner_role=planner,
        worker_role=worker,
        roles=roles,
    )
