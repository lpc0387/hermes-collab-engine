import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from src.hermes_collab_engine.server import DashboardServer
from src.hermes_collab_engine.store import CollabStore


class DashboardApiBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "collab.sqlite3")
        self.cwd = self.tmp.name
        self.port = self._free_port()
        self.server = DashboardServer(
            "127.0.0.1",
            self.port,
            self.db_path,
            self.cwd,
            model="general-model",
            leader_model="leader-real-model",
            worker_model="worker-real-model",
        )
        self.thread = threading.Thread(target=self.server.serve, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.port}"
        self._wait_ready()

    def tearDown(self):
        self.tmp.cleanup()

    def _free_port(self):
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def _wait_ready(self):
        last = None
        for _ in range(50):
            try:
                urllib.request.urlopen(self.base + "/api/overview", timeout=1).read()
                return
            except Exception as exc:
                last = exc
                time.sleep(0.05)
        raise AssertionError(f"server did not become ready: {last}")

    def get_json(self, path):
        return json.loads(urllib.request.urlopen(self.base + path, timeout=3).read().decode())

    def test_runs_list_omits_full_request_but_detail_preserves_long_request(self):
        long_request = "长文本" * 6000
        store = CollabStore(self.db_path)
        store.create_run("run_long", "长文本任务", long_request, {"overall": 1})

        runs = self.get_json("/api/runs")
        self.assertEqual(runs[0]["id"], "run_long")
        self.assertNotIn("request", runs[0])

        compact = self.get_json("/api/runs/run_long")
        self.assertNotIn("request", compact["run"])

        detail = self.get_json("/api/runs/run_long?full=1")
        self.assertEqual(detail["run"]["request"], long_request)
        self.assertEqual(len(detail["run"]["request"]), len(long_request))

    def test_config_exposes_effective_model_names(self):
        cfg = self.get_json("/api/config")
        self.assertEqual(cfg["leader_model"], "leader-real-model")
        self.assertEqual(cfg["worker_model"], "worker-real-model")
        self.assertEqual(cfg["effective_leader_model"], "leader-real-model")
        self.assertEqual(cfg["effective_worker_model"], "worker-real-model")
        self.assertFalse(cfg["model_overrides_readonly"])

    def test_config_uses_environment_fallbacks_when_server_started_without_cli_models(self):
        env = {
            "HERMES_COLLAB_MODEL": "shared-env-model",
            "HERMES_COLLAB_LEADER_MODEL": "leader-env-model",
            "HERMES_COLLAB_WORKER_MODEL": "worker-env-model",
        }
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, env, clear=True):
            server = DashboardServer("127.0.0.1", 0, str(Path(tmp) / "collab.sqlite3"), tmp)
            cfg = server.config_payload()

        self.assertEqual(cfg["model"], "shared-env-model")
        self.assertEqual(cfg["effective_leader_model"], "leader-env-model")
        self.assertEqual(cfg["effective_worker_model"], "worker-env-model")
        self.assertFalse(cfg["model_overrides_readonly"])

    def test_interrupt_endpoint_marks_running_run_as_failed(self):
        store = CollabStore(self.db_path)
        store.create_run("run_intr", "中断测试", "test request", {"overall": 1})
        store.update_run("run_intr", "running")

        req = urllib.request.Request(
            f"{self.base}/api/runs/run_intr/interrupt",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=3).read().decode())
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["run_id"], "run_intr")

        # verify run is now failed
        runs = self.get_json("/api/runs")
        match = [r for r in runs if r["id"] == "run_intr"]
        self.assertEqual(match[0]["status"], "failed")

    def test_interrupt_endpoint_returns_404_for_missing_run(self):
        req = urllib.request.Request(
            f"{self.base}/api/runs/nonexistent/interrupt",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req, timeout=3)
            self.fail("expected HTTPError")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)

    def test_interrupt_endpoint_returns_409_for_completed_run(self):
        store = CollabStore(self.db_path)
        store.create_run("run_done", "已完成", "test", {"overall": 1})
        store.update_run("run_done", "completed")

        req = urllib.request.Request(
            f"{self.base}/api/runs/run_done/interrupt",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req, timeout=3)
            self.fail("expected HTTPError")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 409)

    def test_resume_context_returns_404_when_no_runs(self):
        import urllib.error
        try:
            urllib.request.urlopen(f"{self.base}/api/resume-context", timeout=3).read()
            self.fail("expected HTTPError 404")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)

    def test_resume_context_returns_run_data_when_runs_exist(self):
        store = CollabStore(self.db_path)
        store.create_run("run_resume", "恢复测试", "original task request", {"overall": 1})

        ctx = self.get_json("/api/resume-context")
        self.assertEqual(ctx["run"]["id"], "run_resume")
        self.assertIn("summary", ctx)
        self.assertIn("estimated_tokens", ctx)


if __name__ == "__main__":
    unittest.main()
