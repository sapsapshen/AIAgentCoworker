#!/usr/bin/env bash
# Agent Coworker — local web UI for macOS / Linux.
# Windows: double-click start-agent-coworker.bat instead.
set -euo pipefail
cd "$(dirname "$0")"

if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo "未找到 Python 3.11+。" >&2
  exit 1
fi

exec "$PY" "$PWD/web_server.py"
