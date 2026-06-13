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

    def test_config_exposes_effective_readonly_model_names(self):
        cfg = self.get_json("/api/config")
        self.assertEqual(cfg["leader_model"], "leader-real-model")
        self.assertEqual(cfg["worker_model"], "worker-real-model")
        self.assertEqual(cfg["effective_leader_model"], "leader-real-model")
        self.assertEqual(cfg["effective_worker_model"], "worker-real-model")
        self.assertTrue(cfg["model_overrides_readonly"])

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
        self.assertTrue(cfg["model_overrides_readonly"])


if __name__ == "__main__":
    unittest.main()
