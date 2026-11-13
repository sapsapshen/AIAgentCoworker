#!/usr/bin/env python3
"""Agent Coworker — local web UI server (stdlib only, no third-party deps).

Endpoints
  GET  /                       serve the single-page UI (web/index.html)
  GET  /api/health             {"status":"ok","python":...,"version":...}
  GET  /api/configs            {"configs":[...]}  relay.*.json in project root
  POST /api/doctor             {"config":...}      run doctor, return {output,exit}
  POST /api/run                {"config","task","mode"}   start background run
  POST /api/stop               terminate the active run process
  GET  /api/runs               list runs (newest first)
  GET  /api/runs/<run_id>      full run record {status,log,...}

The server binds to 127.0.0.1 only. POST requests must use
``Content-Type: application/json`` and are rejected when the browser marks
them cross-site. Run configs must be JSON files inside this repository.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from handoff_relay import __version__

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
TASKS_DIR = ROOT / ".agent-relay" / "web-tasks"

RUNS: dict[str, dict[str, Any]] = {}
CANCELLED: set[str] = set()
CURRENT: dict[str, Any] = {"proc": None, "run_id": None}
STATE_LOCK = threading.Lock()

MAX_BODY_BYTES = 1_000_000
MAX_LOG_CHARS = 2_000_000

PORT_ENV = os.environ.get("AGENT_COWORKER_PORT")
PORT = int(PORT_ENV) if PORT_ENV and PORT_ENV.isdigit() else 8000

# Cache the resolved OpenAI Codex bin dir (the bundled CLI is often newer than
# the npm `codex` shim on PATH and supports the model the ChatGPT account uses).
_CODEX_BIN_CACHE: str | None = None


def _local_appdata(*parts: str) -> str | None:
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return None
    return os.path.join(local, *parts)


def ensure_codewhale_on_path(env: dict[str, str]) -> None:
    cw_bin = _local_appdata("Programs", "CodeWhale", "bin")
    if not cw_bin or not os.path.isdir(cw_bin):
        return
    existing = env.get("PATH", "")
    if cw_bin.lower() not in existing.lower():
        env["PATH"] = cw_bin + os.pathsep + existing


def _resolve_codex_bin() -> str:
    global _CODEX_BIN_CACHE
    if _CODEX_BIN_CACHE is not None:
        return _CODEX_BIN_CACHE
    base = _local_appdata("OpenAI", "Codex", "bin")
    if not base or not os.path.isdir(base):
        _CODEX_BIN_CACHE = ""
        return ""
    best_dir = ""
    best_key: tuple[int, ...] = ()
    for exe in Path(base).rglob("codex.exe"):
        try:
            out = subprocess.run(
                [str(exe), "--version"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        match = re.search(r"codex-cli\s+([\d.]+)", out)
        if not match:
            continue
        key = tuple(int(part) for part in match.group(1).split(".") if part.isdigit())
        if key > best_key:
            best_key, best_dir = key, os.path.dirname(str(exe))
    _CODEX_BIN_CACHE = best_dir
    return best_dir


def prepend_newest_codex_on_path(env: dict[str, str]) -> None:
    directory = _resolve_codex_bin()
    if not directory:
        return
    existing = env.get("PATH", "")
    if directory.lower() not in existing.lower():
        env["PATH"] = directory + os.pathsep + existing


def resolve_config_path(config: str) -> str:
    """Return a repo-relative JSON config path, or raise ValueError."""
    if not isinstance(config, str) or not config.strip():
        raise ValueError("缺少配置文件")
    raw = Path(config)
    resolved = raw.resolve() if raw.is_absolute() else (ROOT / raw).resolve()
    if not resolved.is_relative_to(ROOT):
        raise ValueError("配置文件必须位于项目目录内")
    if resolved.suffix.lower() != ".json":
        raise ValueError("配置文件必须是 JSON")
    if not resolved.is_file():
        raise ValueError("配置文件不存在")
    return resolved.relative_to(ROOT).as_posix()


def _safe_label(value: str) -> str:
    cleaned = "".join(ch for ch in value if ch.isprintable())
    return cleaned[:200]


def write_task(text: str) -> Path:
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ")
    path = TASKS_DIR / f"task-{stamp}-{os.urandom(3).hex()}.md"
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return path


def _append(run_id: str, msg: str) -> None:
    with STATE_LOCK:
        run = RUNS.get(run_id)
        if run is None:
            return
        log = str(run.get("log", "")) + msg
        if len(log) > MAX_LOG_CHARS:
            log = "[log truncated]\n" + log[-MAX_LOG_CHARS:]
        run["log"] = log


def _cancelled(run_id: str) -> bool:
    with STATE_LOCK:
        return run_id in CANCELLED


def _finish(run_id: str, status: str) -> None:
    with STATE_LOCK:
        CANCELLED.discard(run_id)
        run = RUNS.get(run_id)
        if run is None:
            return
        run["status"] = status
        run["end"] = time.time()


def _terminate(proc: subprocess.Popen[str]) -> None:
    if os.name == "posix":
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    if proc.poll() is None:
        proc.terminate()


def request_stop() -> bool:
    with STATE_LOCK:
        proc = CURRENT.get("proc")
        current_id = CURRENT.get("run_id")
        running_ids = [str(run["id"]) for run in RUNS.values() if run.get("status") == "running"]
        target = current_id if isinstance(current_id, str) else None
        if target is None and running_ids:
            target = running_ids[0]
        if target is None:
            return False
        CANCELLED.add(target)
        to_kill = proc if isinstance(proc, subprocess.Popen) and proc.poll() is None else None
    if to_kill is not None:
        _terminate(to_kill)
    return True


def _run_command(run_id: str, cmd: list[str], env: dict[str, str]) -> int | None:
    if _cancelled(run_id):
        return None
    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        bufsize=1,
        start_new_session=True,
    )
    with STATE_LOCK:
        CURRENT["proc"] = proc
        CURRENT["run_id"] = run_id
        cancelled = run_id in CANCELLED
    if cancelled:
        _terminate(proc)
    try:
        stdout = proc.stdout
        if stdout is not None:
            for line in stdout:
                _append(run_id, line)
        return proc.wait()
    finally:
        if proc.stdout is not None and not proc.stdout.closed:
            proc.stdout.close()
        if proc.poll() is None:
            _terminate(proc)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        with STATE_LOCK:
            if CURRENT.get("run_id") == run_id:
                CURRENT["proc"] = None
                CURRENT["run_id"] = None


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    ensure_codewhale_on_path(env)
    prepend_newest_codex_on_path(env)
    return env


def _run_check(run_id: str, env: dict[str, str]) -> str:
    steps: tuple[tuple[str, list[str], bool], ...] = (
        (
            "doctor (relay.example.json)",
            [sys.executable, "-m", "handoff_relay", "doctor", "--config", "relay.example.json"],
            False,
        ),
        (
            "unit tests",
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
            True,
        ),
        (
            "mock end-to-end run",
            [
                sys.executable,
                "-m",
                "handoff_relay",
                "run",
                "--config",
                "tests/fixtures/mock-relay.json",
                "--task-file",
                "tests/fixtures/task.md",
            ],
            True,
        ),
    )
    for title, cmd, fatal in steps:
        if _cancelled(run_id):
            return "stopped"
        _append(run_id, f"\n>> {title}\n")
        code = _run_command(run_id, cmd, env)
        if code is None or _cancelled(run_id):
            return "stopped"
        if fatal and code != 0:
            _append(run_id, f"\n[step failed exit={code}]\n")
            return "failed"
    return "completed"


def _run_pipeline(run_id: str, config: str, task: str, env: dict[str, str], *, dry: bool) -> str:
    task_file = write_task(task)
    if dry:
        cmd = [sys.executable, "-m", "handoff_relay", "run", "--config", config, "--task-file", str(task_file), "--dry-run"]
        label = "DRY-RUN"
    else:
        cmd = [sys.executable, "-m", "handoff_relay", "run", "--config", config, "--task-file", str(task_file)]
        label = "REAL RUN"
    _append(
        run_id,
        f">> {label} ({config})\nrun_id={run_id}\n已提交任务，正在启动子进程执行...\n\n$ {' '.join(cmd)}\n\n",
    )
    code = _run_command(run_id, cmd, env)
    if code is None or _cancelled(run_id):
        return "stopped"
    if code != 0:
        _append(run_id, f"\n[run exited with code {code}]\n")
        return "failed"
    return "completed"


def run_worker(run_id: str, config: str, task: str, mode: str) -> None:
    status = "failed"
    try:
        env = _child_env()
        _append(run_id, f"[run started] mode={mode} config={config} @ {time.strftime('%H:%M:%S')}\n")
        if mode == "check":
            status = _run_check(run_id, env)
        elif mode == "dry":
            status = _run_pipeline(run_id, config, task, env, dry=True)
        elif mode == "real":
            status = _run_pipeline(run_id, config, task, env, dry=False)
        else:
            _append(run_id, f"\n[ERROR] 未知模式：{mode}\n")
            status = "failed"
    except Exception as exc:  # pragma: no cover - defensive
        _append(run_id, f"\n[ERROR] {exc}\n")
        status = "failed"
    finally:
        if _cancelled(run_id):
            status = "stopped"
            _append(run_id, "\n[stopped]\n")
        _finish(run_id, status)
        with STATE_LOCK:
            if CURRENT.get("run_id") == run_id:
                CURRENT["proc"] = None
                CURRENT["run_id"] = None


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, obj: object, status: int = 200) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, path: Path, content_type: str) -> None:
        if not path.is_file():
            self._send_json({"error": "未找到"}, 404)
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def _reject_cross_site(self) -> bool:
        site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if site in {"", "none", "same-origin"}:
            return False
        self._send_json({"error": "拒绝跨站请求"}, 403)
        return True

    def _read_payload(self) -> dict[str, Any] | None:
        media = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if media != "application/json":
            self._send_json({"error": "Content-Type 必须为 application/json"}, 415)
            return None
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError:
            self._send_json({"error": "无效的 Content-Length"}, 400)
            return None
        if length < 0 or length > MAX_BODY_BYTES:
            self._send_json({"error": "请求体过大"}, 413)
            return None
        if length == 0:
            return {}
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json({"error": "无效的 JSON"}, 400)
            return None
        if not isinstance(payload, dict):
            self._send_json({"error": "JSON 必须是对象"}, 400)
            return None
        return payload

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler name
        try:
            self._get()
        except Exception as exc:  # pragma: no cover - defensive
            self._send_json({"error": str(exc)}, 500)

    def _get(self) -> None:
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send_file(WEB_DIR / "index.html", "text/html; charset=utf-8")
            return
        if path == "/api/health":
            self._send_json({"status": "ok", "python": sys.version.split()[0], "version": __version__})
            return
        if path == "/api/configs":
            configs = sorted(item.name for item in ROOT.glob("relay.*.json"))
            self._send_json({"configs": configs})
            return
        if path == "/api/runs":
            with STATE_LOCK:
                runs = [dict(item) for item in RUNS.values()]
            runs.sort(key=lambda item: item.get("start", 0), reverse=True)
            self._send_json({"runs": runs})
            return
        if path.startswith("/api/runs/"):
            run_id = path[len("/api/runs/") :]
            with STATE_LOCK:
                run = RUNS.get(run_id)
                payload = dict(run) if run else None
            if payload is None:
                self._send_json({"error": "未找到"}, 404)
            else:
                self._send_json(payload)
            return
        self._send_json({"error": "未找到"}, 404)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler name
        try:
            self._post()
        except Exception as exc:  # pragma: no cover - defensive
            self._send_json({"error": str(exc)}, 500)

    def _post(self) -> None:
        if self._reject_cross_site():
            return
        path = urlparse(self.path).path
        payload = self._read_payload()
        if payload is None:
            return
        if path == "/api/run":
            self._post_run(payload)
        elif path == "/api/doctor":
            self._post_doctor(payload)
        elif path == "/api/stop":
            stopped = request_stop()
            if stopped:
                self._send_json({"stopped": True})
            else:
                self._send_json({"stopped": False, "message": "no running process"})
        elif path == "/api/shutdown":
            self._send_json({"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        else:
            self._send_json({"error": "未找到"}, 404)

    def _post_run(self, payload: dict[str, Any]) -> None:
        mode = payload.get("mode", "check")
        config_value = payload.get("config", "")
        task = payload.get("task", "")
        if mode not in {"check", "dry", "real"}:
            self._send_json({"error": "mode 必须是 check、dry 或 real"}, 400)
            return
        if not isinstance(task, str):
            self._send_json({"error": "task 必须是字符串"}, 400)
            return
        config = ""
        if mode in {"dry", "real"}:
            if not task.strip():
                self._send_json({"error": "dry / real 模式必须填写任务"}, 400)
                return
            if not isinstance(config_value, str):
                self._send_json({"error": "config 必须是字符串"}, 400)
                return
            try:
                config = resolve_config_path(config_value)
            except ValueError as error:
                self._send_json({"error": str(error)}, 400)
                return
        elif isinstance(config_value, str):
            config = _safe_label(Path(config_value).name)
        run_id = time.strftime("%Y%m%dT%H%M%SZ") + "-" + os.urandom(4).hex()
        record: dict[str, Any] = {
            "id": run_id,
            "config": config,
            "task": task,
            "mode": mode,
            "status": "running",
            "log": "",
            "start": time.time(),
            "end": None,
        }
        with STATE_LOCK:
            busy = any(item.get("status") == "running" for item in RUNS.values())
            if not busy:
                RUNS[run_id] = record
        if busy:
            self._send_json({"error": "已有运行正在进行"}, 409)
            return
        threading.Thread(target=run_worker, args=(run_id, config, task, mode), daemon=True).start()
        self._send_json({"run_id": run_id, "status": "running"})

    def _post_doctor(self, payload: dict[str, Any]) -> None:
        config_value = payload.get("config", "relay.codewhale.json")
        if not isinstance(config_value, str):
            self._send_json({"error": "config 必须是字符串"}, 400)
            return
        try:
            config = resolve_config_path(config_value)
        except ValueError as error:
            self._send_json({"error": str(error)}, 400)
            return
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "handoff_relay", "doctor", "--config", config],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=_child_env(),
                timeout=60,
                check=False,
            )
            out = (proc.stdout or "") + (proc.stderr or "")
            self._send_json({"output": out, "exit": proc.returncode})
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._send_json({"output": str(exc), "exit": -1})

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def bind_server(start: int, attempts: int = 100) -> ThreadingHTTPServer:
    """Bind 127.0.0.1, trying ``start`` and the following ports.

    ``AGENT_COWORKER_PORT`` may be any port, including values above the old
    8100 scan ceiling. A busy port falls through to the next candidate.
    """
    if attempts < 1:
        raise ValueError("attempts 必须为正整数")
    last_error: OSError | None = None
    for port in range(start, start + attempts):
        try:
            return ThreadingHTTPServer(("127.0.0.1", port), Handler)
        except OSError as error:
            last_error = error
    raise OSError(f"无法绑定端口 {start}-{start + attempts - 1}：{last_error}")


def main() -> None:
    try:
        server = bind_server(PORT)
    except OSError as error:
        raise SystemExit(str(error)) from error
    url = f"http://127.0.0.1:{server.server_address[1]}"
    print(f"Agent Coworker web UI: {url}")
    print("Press Ctrl+C to stop the server.")
    if os.environ.get("AGENT_COWORKER_NO_BROWSER") != "1":
        try:
            webbrowser.open(url)
        except (OSError, webbrowser.Error):
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
