from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=("planner", "worker", "bad-worker"))
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    input_path, output_path = Path(args.input), Path(args.output)
    if args.role == "planner":
        data = {
            "contract": "plan-v1",
            "goal": "完成输入任务",
            "steps": ["读取任务", "实施最小改动", "运行验证"],
            "acceptance": ["测试通过", "提供证据路径"],
        }
    elif args.role == "worker":
        source = json.loads(input_path.read_text(encoding="utf-8"))
        assert source["plan"]["contract"] == "plan-v1"
        evidence = output_path.parent / "worker-proof.txt"
        evidence.write_text("mock worker completed\n", encoding="utf-8")
        data = {"contract": "result-v1", "status": "passed", "summary": "mock worker 完成任务", "evidence": [str(evidence)]}
    else:
        data = {"contract": "result-v1", "status": "passed", "summary": "缺少证据", "evidence": []}
    output_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
