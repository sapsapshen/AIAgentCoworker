from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from handoff_relay.config import load_config
from handoff_relay.runtime import RelayError, Run, doctor, run_task


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


class RelayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp())
        self.config_path = self.temp / "relay.json"
        raw = json.loads((FIXTURES / "mock-relay.json").read_text(encoding="utf-8"))
        raw["workspace"] = str(ROOT)
        raw["run_root"] = str(self.temp / "runs")
        self.config_path.write_text(json.dumps(raw), encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.temp)

    def test_end_to_end_with_configurable_roles(self) -> None:
        result = run_task(load_config(self.config_path), FIXTURES / "task.md", dry_run=False)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["result"]["status"], "passed")
        run_dir = Path(result["run_dir"])
        self.assertTrue((run_dir / "plan.json").is_file())
        self.assertTrue((run_dir / "result.json").is_file())
        events = (run_dir / "events.jsonl").read_text(encoding="utf-8")
        self.assertIn('"state": "planned"', events)
        self.assertIn('"state": "completed"', events)

    def test_dry_run_does_not_create_a_real_run(self) -> None:
        config = load_config(self.config_path)
        result = run_task(config, FIXTURES / "task.md", dry_run=True)
        self.assertEqual(result["state"], "dry-run")
        self.assertFalse(config.run_root.exists())
        commands = [event["command"] for event in result["events"] if event["state"] == "invoking"]
        self.assertEqual(len(commands), 2)
        self.assertEqual(commands[0][0], "python")

    def test_doctor_reports_missing_profile_without_fallback(self) -> None:
        config = load_config(ROOT / "relay.example.json")
        results = {item["role"]: item["status"] for item in doctor(config)}
        self.assertEqual(results["codex-planner"], "ready")
        self.assertEqual(results["reasonix-worker"], "missing")

    def test_invalid_result_is_recorded_as_failed(self) -> None:
        raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        raw["roles"]["any-worker"]["argv"][1] = "bad-worker"
        self.config_path.write_text(json.dumps(raw), encoding="utf-8")
        config = load_config(self.config_path)
        with self.assertRaises(ValueError):
            run_task(config, FIXTURES / "task.md", dry_run=False)
        run_dirs = list(config.run_root.iterdir())
        self.assertEqual(len(run_dirs), 1)
        manifest = (run_dirs[0] / "manifest.json").read_text(encoding="utf-8")
        self.assertIn('"state": "failed"', manifest)


if __name__ == "__main__":
    unittest.main()
