from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import RelayConfig, Role
from .contracts import PLAN_SCHEMA, RESULT_SCHEMA, read_contract, write_schema


class RelayError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _command_exists(executable: str) -> bool:
    return bool(shutil.which(executable) or Path(executable).is_file())


def _executable_argv(executable: str) -> list[str]:
    """Make npm's PowerShell-only command shims usable from shell=False on Windows."""
    resolved = shutil.which(executable)
    if os.name == "nt" and resolved:
        candidate = Path(resolved)
        if candidate.suffix.lower() == ".cmd":
            powershell_shim = candidate.with_suffix(".ps1")
            if powershell_shim.is_file():
                candidate = powershell_shim
        if candidate.suffix.lower() == ".ps1":
            return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(candidate)]
    return [executable]


def doctor(config: RelayConfig) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    for role in config.roles.values():
        item = {"role": role.name, "executable": role.executable, "status": "missing"}
        if _command_exists(role.executable):
            try:
                completed = subprocess.run(
                    [*_executable_argv(role.executable), "--version"],
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    capture_output=True,
                    timeout=10,
                    shell=False,
                )
                item["status"] = "ready" if completed.returncode == 0 else "broken"
                item["version"] = (completed.stdout or completed.stderr).strip().splitlines()[0]
            except (OSError, subprocess.TimeoutExpired) as error:
                item["status"] = "broken"
                item["detail"] = str(error)
        results.append(item)
    return results


class Run:
    def __init__(self, config: RelayConfig, task: str, dry_run: bool) -> None:
        self.config = config
        self.task = task.strip()
        if not self.task:
            raise RelayError("任务文件不能为空")
        self.dry_run = dry_run
        self.id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        self.dir = config.run_root / self.id
        self.outputs_dir: Path | None = None
        self.events: list[dict[str, Any]] = []

    def event(self, state: str, **details: Any) -> None:
        item = {"at": _now(), "state": state, **details}
        self.events.append(item)
        if not self.dry_run:
            with (self.dir / "events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(item, ensure_ascii=False) + "\n")

    def _write(self, name: str, contents: str) -> Path:
        path = self.dir / name
        path.write_text(contents, encoding="utf-8")
        return path

    def _render(self, role: Role, *, prompt: str, input_file: Path, output_file: Path, schema_file: Path) -> list[str]:
        values = {
            "workspace": str(self.config.workspace), "prompt": prompt, "prompt_file": str(self.dir / "prompt.txt"),
            "input_file": str(input_file), "output_file": str(output_file), "schema_file": str(schema_file),
            "run_dir": str(self.dir), "outputs_dir": str(self.outputs_dir) if self.outputs_dir else "",
        }
        try:
            return [*_executable_argv(role.executable), *[arg.format_map(values) for arg in role.argv]]
        except KeyError as error:
            raise RelayError(f"角色 {role.name} 使用了未知占位符 {error}") from error

    def _invoke(self, role: Role, *, prompt: str, input_file: Path, output_file: Path, schema: dict[str, Any]) -> list[str]:
        schema_file = self.dir / f"{role.name}.schema.json"
        if not self.dry_run:
            write_schema(schema_file, schema)
        command = self._render(role, prompt=prompt, input_file=input_file, output_file=output_file, schema_file=schema_file)
        self.event("invoking", role=role.name, command=command)
        if self.dry_run:
            return command
        if not _command_exists(role.executable):
            raise RelayError(f"角色 {role.name} 的可执行文件不可用：{role.executable}")
        try:
            completed = subprocess.run(
                command,
                cwd=self.config.workspace,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=role.timeout_seconds,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RelayError(f"角色 {role.name} 无法完成：{error}") from error
        self._write(f"{role.name}.stdout.log", completed.stdout or "")
        self._write(f"{role.name}.stderr.log", completed.stderr or "")
        if completed.returncode != 0:
            raise RelayError(f"角色 {role.name} 退出码为 {completed.returncode}，详见 {role.name}.stderr.log")
        return command

    def execute(self) -> dict[str, Any]:
        planner, worker = self.config.roles[self.config.planner_role], self.config.roles[self.config.worker_role]
        if self.dry_run:
            self.dir = self.config.run_root / "DRY-RUN"
            self.outputs_dir = None
        else:
            self.dir.mkdir(parents=True, exist_ok=False)
            self.outputs_dir = self.config.workspace / "outputs" / self.id
            self.outputs_dir.mkdir(parents=True, exist_ok=True)
        self.event("created", planner=planner.name, worker=worker.name)
        task_file = self._write("task.md", self.task + "\n") if not self.dry_run else self.dir / "task.md"
        outputs_hint = ""
        if self.outputs_dir is not None:
            outputs_hint = (
                f"如果任务需要生成文件（代码、文档等），请在 plan 中说明这些交付文件"
                f"应创建在目录：{self.outputs_dir}\n（由执行者负责把文件写入该目录）。\n\n"
            )
        planner_prompt = (
            "你是规划者。阅读任务后只输出符合所给 JSON Schema 的 plan-v1 JSON。"
            "不要修改代码，不要添加 Markdown。\n\n"
            + outputs_hint +
            "任务：\n" + self.task
        )
        if not self.dry_run:
            self._write("prompt.txt", planner_prompt)
        plan_file = self.dir / "plan.json"
        self._invoke(planner, prompt=planner_prompt, input_file=task_file, output_file=plan_file, schema=PLAN_SCHEMA)
        if self.dry_run:
            worker_prompt = "执行已规划的任务，并只输出 result-v1 JSON。"
            self._invoke(worker, prompt=worker_prompt, input_file=plan_file, output_file=self.dir / "result.json", schema=RESULT_SCHEMA)
            return {"id": self.id, "state": "dry-run", "run_dir": str(self.dir), "events": self.events}
        try:
            plan = read_contract(plan_file, "plan-v1")
            self.event("planned", plan_file=str(plan_file))
            worker_input = self.dir / "worker-input.json"
            worker_input.write_text(json.dumps({"task": self.task, "plan": plan}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            worker_prompt = "执行 input_file 中的任务计划；只输出符合 schema 的 result-v1 JSON。"
            if self.outputs_dir is not None:
                worker_prompt += f"\n把生成的交付文件（代码、文档等）写入目录：{self.outputs_dir}\n"
            result_file = self.dir / "result.json"
            self._invoke(worker, prompt=worker_prompt, input_file=worker_input, output_file=result_file, schema=RESULT_SCHEMA)
            result = read_contract(result_file, "result-v1")
            final_state = "completed" if result["status"] == "passed" else "failed"
            self.event(final_state, result_file=str(result_file), worker_status=result["status"])
            return {"id": self.id, "state": final_state, "run_dir": str(self.dir), "result": result}
        except (RelayError, ValueError) as error:
            self.event("failed", error=str(error))
            raise
        finally:
            manifest = {"id": self.id, "config": str(self.config.path), "workspace": str(self.config.workspace), "events": self.events}
            self._write("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")


def run_task(config: RelayConfig, task_file: str | Path, dry_run: bool) -> dict[str, Any]:
    return Run(config, Path(task_file).read_text(encoding="utf-8"), dry_run).execute()
