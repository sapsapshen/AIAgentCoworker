from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from handoff_relay import __version__, runtime as runtime_module
from handoff_relay.config import load_config
from handoff_relay.contracts import PLAN_SCHEMA, RESULT_SCHEMA, read_contract, validate_instance
from handoff_relay.runtime import RelayError, doctor, run_task


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
MOCK_SCRIPT = FIXTURES / "mock_agent.py"


def _minimal_config() -> dict:
    return {
        "version": 1,
        "workspace": ".",
        "run_root": ".agent-relay/runs",
        "planner_role": "p",
        "worker_role": "w",
        "roles": {
            "p": {"executable": "python", "argv": [], "input": "prompt", "output": "json-file"},
            "w": {"executable": "python", "argv": [], "input": "json-file", "output": "json-file"},
        },
    }


class RelayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp())
        self.workspace = self.temp / "workspace"
        self.workspace.mkdir()
        self.config_path = self.temp / "relay.json"
        raw = json.loads((FIXTURES / "mock-relay.json").read_text(encoding="utf-8"))
        raw["workspace"] = str(self.workspace)
        raw["run_root"] = str(self.temp / "runs")
        for role in raw["roles"].values():
            role["executable"] = sys.executable
            role["argv"][0] = str(MOCK_SCRIPT)
        self.config_path.write_text(json.dumps(raw), encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.temp)

    def _write_config(self, raw: dict) -> None:
        self.config_path.write_text(json.dumps(raw), encoding="utf-8")

    def test_end_to_end_with_configurable_roles(self) -> None:
        result = run_task(load_config(self.config_path), FIXTURES / "task.md", dry_run=False)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["result"]["status"], "passed")
        run_dir = Path(result["run_dir"])
        self.assertTrue((run_dir / "plan.json").is_file())
        self.assertTrue((run_dir / "result.json").is_file())
        self.assertTrue((run_dir / "worker-prompt.txt").is_file())
        events = (run_dir / "events.jsonl").read_text(encoding="utf-8")
        self.assertIn('"state": "planned"', events)
        self.assertIn('"state": "completed"', events)
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["relay_version"], __version__)
        self.assertTrue(Path(manifest["outputs_dir"]).is_dir())
        self.assertEqual(Path(result["outputs_dir"]), Path(manifest["outputs_dir"]))

    def test_dry_run_does_not_create_a_real_run(self) -> None:
        config = load_config(self.config_path)
        result = run_task(config, FIXTURES / "task.md", dry_run=True)
        self.assertEqual(result["state"], "dry-run")
        self.assertFalse(config.run_root.exists())
        commands = [event["command"] for event in result["events"] if event["state"] == "invoking"]
        self.assertEqual(len(commands), 2)
        self.assertEqual(commands[0][0], sys.executable)
        self.assertIn(str(MOCK_SCRIPT), commands[0])

    def test_doctor_reports_missing_profile_without_fallback(self) -> None:
        raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        raw["roles"]["any-worker"]["executable"] = "reasonix-not-installed"
        self._write_config(raw)
        config = load_config(self.config_path)
        results = {item["role"]: item for item in doctor(config)}
        self.assertEqual(set(results), {"any-planner", "any-worker"})
        self.assertEqual(results["any-planner"]["status"], "ready")
        self.assertEqual(results["any-worker"]["status"], "missing")
        self.assertEqual(results["any-worker"]["executable"], "reasonix-not-installed")
        self.assertNotIn("resolved", results["any-worker"])

    def test_example_config_does_not_invent_a_fallback_role(self) -> None:
        config = load_config(ROOT / "relay.example.json")
        self.assertEqual(config.roles["codex-planner"].executable, "codex")
        self.assertEqual(config.roles["reasonix-worker"].executable, "reasonix")
        self.assertNotEqual(config.planner_role, config.worker_role)
        results = {item["role"]: item["status"] for item in doctor(config)}
        self.assertEqual(results["reasonix-worker"], "missing")
        self.assertIn(results["codex-planner"], {"ready", "missing", "broken"})

    def test_invalid_result_is_recorded_as_failed(self) -> None:
        raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        raw["roles"]["any-worker"]["argv"][1] = "bad-worker"
        self._write_config(raw)
        config = load_config(self.config_path)
        with self.assertRaises(ValueError):
            run_task(config, FIXTURES / "task.md", dry_run=False)
        run_dirs = list(config.run_root.iterdir())
        self.assertEqual(len(run_dirs), 1)
        manifest = (run_dirs[0] / "manifest.json").read_text(encoding="utf-8")
        self.assertIn('"state": "failed"', manifest)

    def test_reported_failure_completes_without_raising(self) -> None:
        raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        raw["roles"]["any-worker"]["argv"][1] = "failed-worker"
        self._write_config(raw)
        result = run_task(load_config(self.config_path), FIXTURES / "task.md", dry_run=False)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["result"]["status"], "failed")

    def test_missing_executable_is_audited(self) -> None:
        raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        raw["roles"]["any-planner"]["executable"] = "no-such-relay-bin"
        self._write_config(raw)
        config = load_config(self.config_path)
        with self.assertRaises(RelayError):
            run_task(config, FIXTURES / "task.md", dry_run=False)
        run_dirs = list(config.run_root.iterdir())
        self.assertEqual(len(run_dirs), 1)
        manifest = json.loads((run_dirs[0] / "manifest.json").read_text(encoding="utf-8"))
        self.assertIn("failed", [event["state"] for event in manifest["events"]])

    def test_invalid_planner_json_is_audited(self) -> None:
        script = self.temp / "garbage.py"
        script.write_text(
            "import pathlib, sys\npathlib.Path(sys.argv[1]).write_text('not-json', encoding='utf-8')\n",
            encoding="utf-8",
        )
        raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        raw["roles"]["any-planner"]["argv"] = [str(script), "{output_file}"]
        self._write_config(raw)
        config = load_config(self.config_path)
        with self.assertRaises(ValueError):
            run_task(config, FIXTURES / "task.md", dry_run=False)
        manifest = json.loads((next(config.run_root.iterdir()) / "manifest.json").read_text(encoding="utf-8"))
        self.assertIn("failed", [event["state"] for event in manifest["events"]])

    def test_unknown_placeholder_stays_a_dry_run(self) -> None:
        raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        raw["roles"]["any-planner"]["argv"] = ["{not_a_placeholder}"]
        self._write_config(raw)
        config = load_config(self.config_path)
        with self.assertRaises(RelayError):
            run_task(config, FIXTURES / "task.md", dry_run=True)
        self.assertFalse(config.run_root.exists())

    def test_empty_task_is_rejected(self) -> None:
        task = self.temp / "empty.md"
        task.write_text(" \n\t", encoding="utf-8")
        with self.assertRaises(RelayError):
            run_task(load_config(self.config_path), task, dry_run=True)

    def test_missing_task_file_is_rejected(self) -> None:
        with self.assertRaises(RelayError):
            run_task(load_config(self.config_path), self.temp / "missing.md", dry_run=True)

    def test_python_alias_uses_the_current_interpreter(self) -> None:
        real = runtime_module._command_exists

        def fake(executable: str) -> bool:
            if executable == "python":
                return False
            return real(executable)

        runtime_module._command_exists = fake
        try:
            self.assertEqual(runtime_module._resolve_executable("python"), sys.executable)
            self.assertEqual(runtime_module._resolve_executable("codex"), "codex")
            raw = _minimal_config()
            raw["workspace"] = str(self.workspace)
            raw["run_root"] = str(self.temp / "doctor-runs")
            raw["roles"]["p"]["executable"] = "python"
            raw["roles"]["w"]["executable"] = "python"
            self._write_config(raw)
            results = {item["role"]: item for item in doctor(load_config(self.config_path))}
        finally:
            runtime_module._command_exists = real
        self.assertEqual(results["p"]["status"], "ready")
        self.assertEqual(results["p"]["executable"], "python")
        self.assertEqual(results["p"]["resolved"], sys.executable)
        self.assertIn("version", results["p"])

    def test_doctor_survives_empty_or_failing_version_output(self) -> None:
        real_run = runtime_module.subprocess.run
        silent = subprocess.CompletedProcess(args=["tool"], returncode=0, stdout="", stderr="")
        broken = subprocess.CompletedProcess(args=["tool"], returncode=3, stdout="", stderr="boom\nmore")

        runtime_module.subprocess.run = lambda *args, **kwargs: silent
        try:
            silent_results = doctor(load_config(self.config_path))
        finally:
            runtime_module.subprocess.run = real_run
        self.assertTrue(silent_results)
        self.assertTrue(all(item["status"] == "ready" for item in silent_results))
        self.assertTrue(all("version" not in item for item in silent_results))

        runtime_module.subprocess.run = lambda *args, **kwargs: broken
        try:
            broken_results = doctor(load_config(self.config_path))
        finally:
            runtime_module.subprocess.run = real_run
        self.assertTrue(all(item["status"] == "broken" for item in broken_results))
        self.assertTrue(all(item["version"] == "boom" for item in broken_results))

        def explode(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired(cmd="tool", timeout=10)

        runtime_module.subprocess.run = explode
        try:
            timed_out = doctor(load_config(self.config_path))
        finally:
            runtime_module.subprocess.run = real_run
        self.assertTrue(all(item["status"] == "broken" and "detail" in item for item in timed_out))


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.temp)

    def _write(self, raw: object) -> Path:
        path = self.temp / "relay.json"
        text = raw if isinstance(raw, str) else json.dumps(raw)
        path.write_text(text, encoding="utf-8")
        return path

    def test_bom_config_loads(self) -> None:
        path = self.temp / "bom.json"
        path.write_bytes(b"\xef\xbb\xbf" + json.dumps(_minimal_config()).encode("utf-8"))
        config = load_config(path)
        self.assertEqual(config.planner_role, "p")

    def test_rejects_malformed_configs(self) -> None:
        cases: list[tuple[object, str]] = [
            ("[]", "JSON 对象"),
            ({**_minimal_config(), "version": 2}, "version"),
            ({**_minimal_config(), "roles": []}, "roles"),
            ({**_minimal_config(), "workspace": ""}, "workspace"),
        ]
        for raw, snippet in cases:
            with self.subTest(snippet=snippet):
                with self.assertRaises(ValueError) as caught:
                    load_config(self._write(raw))
                self.assertIn(snippet, str(caught.exception))

        timed = _minimal_config()
        timed["roles"]["p"]["timeout_seconds"] = True
        with self.assertRaises(ValueError) as caught:
            load_config(self._write(timed))
        self.assertIn("timeout_seconds", str(caught.exception))

        bad_input = _minimal_config()
        bad_input["roles"]["p"]["input"] = "shell"
        with self.assertRaises(ValueError):
            load_config(self._write(bad_input))


class ContractTests(unittest.TestCase):
    def test_schema_rejects_incomplete_or_unknown_fields(self) -> None:
        plan = {"contract": "plan-v1", "goal": "ship", "steps": ["edit"], "acceptance": ["tests pass"]}
        self.assertEqual(validate_instance(plan, PLAN_SCHEMA), [])
        self.assertTrue(validate_instance({**plan, "notes": "extra"}, PLAN_SCHEMA))
        self.assertTrue(validate_instance({**plan, "goal": "   "}, PLAN_SCHEMA))

        result = {"contract": "result-v1", "status": "passed", "summary": "done", "evidence": ["a.txt"]}
        self.assertEqual(validate_instance(result, RESULT_SCHEMA), [])
        self.assertTrue(validate_instance({**result, "evidence": []}, RESULT_SCHEMA))
        self.assertTrue(validate_instance({**result, "status": "maybe"}, RESULT_SCHEMA))
        self.assertGreaterEqual(RESULT_SCHEMA["properties"]["evidence"]["minItems"], 1)

    def test_read_contract_reports_invalid_json_and_extra_fields(self) -> None:
        root = Path(tempfile.mkdtemp())
        try:
            invalid = root / "plan.json"
            invalid.write_text("{", encoding="utf-8")
            with self.assertRaises(ValueError) as caught:
                read_contract(invalid, "plan-v1")
            self.assertIn("无法读取", str(caught.exception))

            extra = root / "extra.json"
            extra.write_text(
                json.dumps(
                    {
                        "contract": "plan-v1",
                        "goal": "ship",
                        "steps": ["edit"],
                        "acceptance": ["tests pass"],
                        "extra": True,
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError) as caught:
                read_contract(extra, "plan-v1")
            self.assertIn("未声明字段", str(caught.exception))
        finally:
            shutil.rmtree(root)


class CliTests(unittest.TestCase):
    def test_version_and_error_exit_codes(self) -> None:
        version = subprocess.run(
            [sys.executable, "-m", "handoff_relay", "--version"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(version.returncode, 0)
        self.assertIn(__version__, version.stdout)

        missing = subprocess.run(
            [sys.executable, "-m", "handoff_relay", "doctor", "--config", str(ROOT / "no-such.json")],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(missing.returncode, 1)
        self.assertIn("错误", missing.stderr)

        doctor_proc = subprocess.run(
            [sys.executable, "-m", "handoff_relay", "doctor", "--config", str(ROOT / "relay.example.json")],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertIn(doctor_proc.returncode, {0, 2})
        self.assertNotIn("Traceback", doctor_proc.stderr)


if __name__ == "__main__":
    unittest.main()
