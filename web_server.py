#!/usr/bin/env python3
"""Agent Coworker — local web UI server (stdlib only, no third-party deps).

Endpoints
  GET  /                       serve the single-page UI (web/index.html)
  GET  /api/health            {"status":"ok","python":...}
  GET  /api/configs           {"configs":[...]}  relay.*.json in project root
  POST /api/doctor            {"config":...}      run doctor, return {output,exit}
  POST /api/run               {"config","task","mode"}   start background run
  POST /api/stop              terminate the active run process
  GET  /api/runs              list runs (newest first)
  GET  /api/runs/<run_id>     full run record {status,log,...}

Run records live in the RUNS dict; each run executes in a daemon thread that
streams merged stdout/stderr into run["log"] so the page can poll progress.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
TASKS_DIR = ROOT / ".agent-relay" / "web-tasks"

RUNS: dict[str, dict] = {}
RUNS_LOCK = threading.Lock()
CURRENT: dict[str, subprocess.Popen | None] = {"proc": None, "run_id": None}

PORT_ENV = os.environ.get("AGENT_COWORKER_PORT")
PORT = int(PORT_ENV) if PORT_ENV and PORT_ENV.isdigit() else 8000


def ensure_codewhale_on_path(env: dict[str, str]) -> None:
    cw_bin = os.path.join(
        os.environ.get("LOCALAPPDATA", ""),
        "Programs", "CodeWhale", "bin",
    )
    if cw_bin and os.path.isdir(cw_bin):
        existing = env.get("PATH", "")
        if cw_bin.lower() not in existing.lower():
            env["PATH"] = cw_bin + os.pathsep + existing


import re

# Cache the resolved OpenAI Codex bin dir (the bundled CLI is often newer than
# the npm `codex` shim on PATH and supports the model the ChatGPT account uses).
_CODEX_BIN_CACHE: str | None = None


def _resolve_codex_bin() -> str | None:
    global _CODEX_BIN_CACHE
    if _CODEX_BIN_CACHE is not None:
        return _CODEX_BIN_CACHE
    base = os.path.join(os.environ.get("LOCALAPPDATA", ""), "OpenAI", "Codex", "bin")
    if not os.path.isdir(base):
        _CODEX_BIN_CACHE = ""
        return ""
    best_dir = ""
    best_key: tuple = ()
    for exe in Path(base).rglob("codex.exe"):
        try:
            out = subprocess.run([str(exe), "--version"], capture_output=True,
                                 text=True, timeout=15).stdout
        except Exception:
            continue
        m = re.search(r"codex-cli\s+([\d.]+)", out)
        if not m:
            continue
        key = tuple(int(x) for x in m.group(1).split("."))
        if key > best_key:
            best_key, best_dir = key, os.path.dirname(str(exe))
    _CODEX_BIN_CACHE = best_dir
    return best_dir


def prepend_newest_codex_on_path(env: dict[str, str]) -> None:
    d = _resolve_codex_bin()
    if d:
        existing = env.get("PATH", "")
        if d.lower() not in existing.lower():
            env["PATH"] = d + os.pathsep + existing


def write_task(text: str) -> Path:
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%dT%H%M%SZ")
    p = TASKS_DIR / f"task-{ts}.md"
    p.write_text(text.strip() + "\n", encoding="utf-8")
    return p


def _append(run_id: str, msg: str) -> None:
    with RUNS_LOCK:
        RUNS[run_id]["log"] += msg


def run_worker(run_id: str, config: str, task: str, mode: str) -> None:
    with RUNS_LOCK:
        RUNS[run_id] = {
            "id": run_id, "config": config, "task": task, "mode": mode,
            "status": "running", "log": "", "start": time.time(), "end": None,
        }
    env = dict(os.environ)
    ensure_codewhale_on_path(env)
    prepend_newest_codex_on_path(env)
    _append(run_id, f"[run started] mode={mode} config={config} @ {time.strftime('%H:%M:%S')}\n")
    try:
        if mode == "check":
            steps = [
                ("doctor (relay.example.json)",
                 [sys.executable, "-m", "handoff_relay", "doctor", "--config", "relay.example.json"]),
                ("unit tests",
                 [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"]),
                ("mock end-to-end run",
                 [sys.executable, "-m", "handoff_relay", "run",
                  "--config", "tests/fixtures/mock-relay.json",
                  "--task-file", "tests/fixtures/task.md"]),
            ]
            for idx, (title, cmd) in enumerate(steps):
                _append(run_id, f"\n>> {title}\n")
                proc = subprocess.Popen(
                    cmd, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace", env=env, bufsize=1,
                )
                CURRENT["proc"] = proc
                CURRENT["run_id"] = run_id
                for line in proc.stdout:
                    _append(run_id, line)
                proc.wait()
                # doctor (step 0) reports missing roles with exit 2 — non-fatal, informational only.
                if idx > 0 and proc.returncode != 0:
                    _append(run_id, f"\n[step failed exit={proc.returncode}]\n")
                    with RUNS_LOCK:
                        RUNS[run_id]["status"] = "failed"
                        RUNS[run_id]["end"] = time.time()
                    return
            with RUNS_LOCK:
                RUNS[run_id]["status"] = "completed"
                RUNS[run_id]["end"] = time.time()
            return

        # dry / real
        task_file = write_task(task)
        if mode == "dry":
            cmd = [sys.executable, "-m", "handoff_relay", "run",
                   "--config", config, "--task-file", str(task_file), "--dry-run"]
            label = "DRY-RUN"
        else:
            cmd = [sys.executable, "-m", "handoff_relay", "run",
                   "--config", config, "--task-file", str(task_file)]
            label = "REAL RUN"

        _append(run_id, f">> {label} ({config})\nrun_id={run_id}\n已提交任务，正在启动子进程执行...\n\n$ {' '.join(cmd)}\n\n")
        proc = subprocess.Popen(
            cmd, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", env=env, bufsize=1,
        )
        CURRENT["proc"] = proc
        CURRENT["run_id"] = run_id
        for line in proc.stdout:
            _append(run_id, line)
        proc.wait()
        with RUNS_LOCK:
            RUNS[run_id]["status"] = "completed" if proc.returncode == 0 else "failed"
            RUNS[run_id]["end"] = time.time()
            if proc.returncode != 0:
                RUNS[run_id]["log"] += f"\n[run exited with code {proc.returncode}]\n"
    except Exception as exc:  # pragma: no cover - defensive
        with RUNS_LOCK:
            RUNS[run_id]["log"] += f"\n[ERROR] {exc}\n"
            RUNS[run_id]["status"] = "failed"
            RUNS[run_id]["end"] = time.time()
    finally:
        CURRENT["proc"] = None
        CURRENT["run_id"] = None


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, obj: object, status: int = 200) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, path: Path, content_type: str) -> None:
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            self._send_file(WEB_DIR / "index.html", "text/html; charset=utf-8")
        elif path == "/api/health":
            self._send_json({"status": "ok", "python": sys.version.split()[0]})
        elif path == "/api/configs":
            configs = sorted(p.name for p in ROOT.glob("relay.*.json"))
            self._send_json({"configs": configs})
        elif path == "/api/runs":
            with RUNS_LOCK:
                runs = [dict(r) for r in RUNS.values()]
            runs.sort(key=lambda r: r.get("start", 0), reverse=True)
            self._send_json({"runs": runs})
        elif path.startswith("/api/runs/"):
            run_id = path[len("/api/runs/"):]
            with RUNS_LOCK:
                run = RUNS.get(run_id)
            self._send_json(run if run else {"error": "not found"}, 200 if run else 404)
        else:
            self._send_json({"error": "not found"}, status=404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {}

        if path == "/api/run":
            mode = payload.get("mode", "check")
            config = payload.get("config", "relay.codewhale.json")
            task = payload.get("task", "")
            if mode in ("dry", "real") and not task.strip():
                self._send_json({"error": "task is required for dry/real mode"}, status=400)
                return
            run_id = time.strftime("%Y%m%dT%H%M%SZ") + "-" + os.urandom(4).hex()
            threading.Thread(target=run_worker, args=(run_id, config, task, mode), daemon=True).start()
            self._send_json({"run_id": run_id, "status": "running"})

        elif path == "/api/doctor":
            config = payload.get("config", "relay.codewhale.json")
            try:
                env = dict(os.environ)
                ensure_codewhale_on_path(env)
                prepend_newest_codex_on_path(env)
                proc = subprocess.run(
                    [sys.executable, "-m", "handoff_relay", "doctor", "--config", config],
                    cwd=str(ROOT), capture_output=True, text=True,
                    encoding="utf-8", errors="replace", env=env, timeout=30,
                )
                out = (proc.stdout or "") + (proc.stderr or "")
                self._send_json({"output": out, "exit": proc.returncode})
            except Exception as exc:
                self._send_json({"output": str(exc), "exit": -1})

        elif path == "/api/stop":
            proc = CURRENT.get("proc")
            if proc and proc.poll() is None:
                proc.terminate()
                self._send_json({"stopped": True})
            else:
                self._send_json({"stopped": False, "message": "no running process"})

        elif path == "/api/shutdown":
            self._send_json({"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()

        else:
            self._send_json({"error": "not found"}, status=404)

    def log_message(self, fmt: str, *args) -> None:  # silence default logging
        pass


def find_free_port(start: int, limit: int = 8100) -> int:
    port = start
    while port < limit:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
        port += 1
    return start


def main() -> None:
    global PORT
    PORT = find_free_port(PORT)
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}"
    print(f"Agent Coworker web UI: {url}")
    print("Press Ctrl+C to stop the server.")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
