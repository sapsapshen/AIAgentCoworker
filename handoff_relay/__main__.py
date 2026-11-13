from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .config import load_config
from .runtime import RelayError, doctor, run_task


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="handoff-relay", description="本地可审计的指挥者/执行者编排器")
    parser.add_argument("--version", action="version", version=f"handoff-relay {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("doctor", "run"):
        command = sub.add_parser(name)
        command.add_argument("--config", required=True)
        if name == "run":
            command.add_argument("--task-file", required=True)
            command.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "doctor":
            result = doctor(config)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if all(item["status"] == "ready" for item in result) else 2
        print(json.dumps(run_task(config, args.task_file, args.dry_run), ensure_ascii=False, indent=2))
        return 0
    except (RelayError, ValueError, OSError) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
