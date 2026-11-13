from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import web_server
from handoff_relay import __version__


ROOT = Path(__file__).resolve().parents[1]


class WebApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), web_server.Handler)
        cls.httpd.daemon_threads = True
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        host, port = cls.httpd.server_address[:2]
        cls.base = f"http://{host}:{port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)

    def setUp(self) -> None:
        self._stop_and_wait()
        with web_server.STATE_LOCK:
            web_server.RUNS.clear()
            web_server.CANCELLED.clear()
            web_server.CURRENT["proc"] = None
            web_server.CURRENT["run_id"] = None

    def tearDown(self) -> None:
        self._stop_and_wait()

    def _stop_and_wait(self) -> None:
        web_server.request_stop()
        deadline = time.time() + 5
        while time.time() < deadline:
            with web_server.STATE_LOCK:
                running = any(item.get("status") == "running" for item in web_server.RUNS.values())
            if not running:
                return
            time.sleep(0.05)

    def _request(
        self,
        method: str,
        path: str,
        payload: object | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 30,
        raw: bytes | None = None,
    ) -> tuple[int, dict]:
        data = raw
        hdrs: dict[str, str] = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        if headers:
            hdrs.update(headers)
        request = urllib.request.Request(self.base + path, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8")
                return response.status, json.loads(body) if body else {}
        except urllib.error.HTTPError as error:
            raw_body = error.read().decode("utf-8")
            try:
                parsed = json.loads(raw_body) if raw_body else {}
            except json.JSONDecodeError:
                parsed = {"raw": raw_body}
            return error.code, parsed

    def _wait(self, run_id: str, timeout: float = 10) -> dict:
        deadline = time.time() + timeout
        last: dict = {}
        while time.time() < deadline:
            status, last = self._request("GET", f"/api/runs/{run_id}")
            self.assertEqual(status, 200, last)
            if last.get("status") != "running":
                return last
            time.sleep(0.05)
        self.fail(f"run did not finish: {last}")

    def test_bind_server_honors_an_explicit_high_port(self) -> None:
        server = web_server.bind_server(8765, attempts=5)
        try:
            port = server.server_address[1]
            self.assertGreaterEqual(port, 8765)
            self.assertLess(port, 8770)
        finally:
            server.server_close()

    def test_health_and_index(self) -> None:
        status, health = self._request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["version"], __version__)
        with urllib.request.urlopen(self.base + "/", timeout=10) as response:
            page = response.read().decode("utf-8")
            self.assertEqual(response.status, 200)
        self.assertIn("Agent Coworker", page)
        self.assertIn("start-web.sh", page)
        self.assertIn("createElement", page)

        status, configs = self._request("GET", "/api/configs")
        self.assertEqual(status, 200)
        self.assertIn("relay.example.json", configs["configs"])
        self.assertIn("relay.codewhale.json", configs["configs"])

    def test_dry_run_is_visible_immediately(self) -> None:
        status, body = self._request(
            "POST",
            "/api/run",
            {"mode": "dry", "config": "tests/fixtures/mock-relay.json", "task": "做一个最小改动"},
        )
        self.assertEqual(status, 200, body)
        immediate, run = self._request("GET", f"/api/runs/{body['run_id']}")
        self.assertEqual(immediate, 200, run)
        self.assertNotIn("error", run)
        finished = self._wait(body["run_id"])
        self.assertEqual(finished["status"], "completed")
        self.assertIn("dry-run", finished["log"])

        missing, payload = self._request("GET", "/api/runs/does-not-exist")
        self.assertEqual(missing, 404)
        self.assertEqual(payload["error"], "未找到")

    def test_example_config_dry_run_renders_both_roles(self) -> None:
        status, body = self._request(
            "POST",
            "/api/run",
            {"mode": "dry", "config": "relay.example.json", "task": "只渲染命令"},
        )
        self.assertEqual(status, 200, body)
        finished = self._wait(body["run_id"])
        self.assertEqual(finished["status"], "completed")
        self.assertIn("codex", finished["log"])
        self.assertIn("reasonix", finished["log"])

    def test_rejects_bad_requests(self) -> None:
        status, body = self._request("POST", "/api/run", {"mode": "real", "config": "relay.example.json", "task": "  "})
        self.assertEqual(status, 400)
        self.assertIn("任务", body["error"])

        status, body = self._request("POST", "/api/run", {"mode": "yolo", "task": "x"})
        self.assertEqual(status, 400)
        self.assertIn("mode", body["error"])

        status, body = self._request(
            "POST",
            "/api/run",
            {"mode": "dry", "config": "/etc/passwd", "task": "x"},
        )
        self.assertEqual(status, 400)
        self.assertIn("项目目录", body["error"])

        status, body = self._request(
            "POST",
            "/api/doctor",
            {"config": "tests/fixtures/../../../../../../etc/passwd"},
        )
        self.assertEqual(status, 400)
        self.assertIn("项目目录", body["error"])

        status, body = self._request("POST", "/api/doctor", {"config": "tests/fixtures/task.md"})
        self.assertEqual(status, 400)
        self.assertIn("JSON", body["error"])

        status, body = self._request("POST", "/api/doctor", {"config": "relay-missing.json"})
        self.assertEqual(status, 400)
        self.assertIn("不存在", body["error"])

        status, body = self._request(
            "POST",
            "/api/stop",
            {},
            headers={"Content-Type": "text/plain", "Sec-Fetch-Site": "cross-site"},
        )
        self.assertEqual(status, 403)

        status, body = self._request("POST", "/api/stop", headers={"Content-Type": "text/plain"}, raw=b"{}")
        self.assertEqual(status, 415)

        status, body = self._request("POST", "/api/stop", headers={"Content-Type": "application/json"}, raw=b"{")
        self.assertEqual(status, 400)
        self.assertIn("JSON", body["error"])

        status, body = self._request("POST", "/api/stop", headers={"Content-Type": "application/json"}, raw=b"[1]")
        self.assertEqual(status, 400)
        self.assertIn("对象", body["error"])

        original_limit = web_server.MAX_BODY_BYTES
        web_server.MAX_BODY_BYTES = 8
        try:
            status, body = self._request("POST", "/api/stop", {"hello": "world"})
        finally:
            web_server.MAX_BODY_BYTES = original_limit
        self.assertEqual(status, 413)

    def test_doctor_endpoint_reports_missing_role(self) -> None:
        status, body = self._request("POST", "/api/doctor", {"config": "relay.example.json"})
        self.assertEqual(status, 200, body)
        self.assertIn("reasonix-worker", body["output"])
        self.assertIn(body["exit"], {0, 2})

    def test_second_run_is_rejected_until_stop(self) -> None:
        directory = Path(tempfile.mkdtemp())
        config_path = ROOT / "tests" / "fixtures" / "slow.local.json"
        script = directory / "sleep.py"
        script.write_text("import time\ntime.sleep(20)\n", encoding="utf-8")
        workspace = directory / "workspace"
        workspace.mkdir()
        config = {
            "version": 1,
            "workspace": str(workspace),
            "run_root": str(directory / "runs"),
            "planner_role": "sleeper",
            "worker_role": "sleeper",
            "roles": {
                "sleeper": {
                    "executable": sys.executable,
                    "argv": [str(script)],
                    "input": "prompt",
                    "output": "json-file",
                    "timeout_seconds": 25,
                }
            },
        }
        try:
            config_path.write_text(json.dumps(config), encoding="utf-8")
            status, body = self._request(
                "POST",
                "/api/run",
                {"mode": "real", "config": "tests/fixtures/slow.local.json", "task": "sleep"},
            )
            self.assertEqual(status, 200, body)
            again, again_body = self._request(
                "POST",
                "/api/run",
                {"mode": "dry", "config": "relay.example.json", "task": "should not start"},
            )
            self.assertEqual(again, 409, again_body)
            stopped, stopped_body = self._request("POST", "/api/stop", {})
            self.assertEqual(stopped, 200, stopped_body)
            self.assertTrue(stopped_body["stopped"])
            finished = self._wait(body["run_id"], timeout=10)
            self.assertEqual(finished["status"], "stopped")
        finally:
            config_path.unlink(missing_ok=True)
            self._stop_and_wait()
            shutil.rmtree(directory, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
