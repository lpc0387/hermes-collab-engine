from __future__ import annotations

import argparse
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.hermes_collab_engine.cli import _model_options
from src.hermes_collab_engine.engine import CollabEngine


class EnvModelFallbackTests(unittest.TestCase):
    def args(self, model=None, leader_model=None, worker_model=None):
        return argparse.Namespace(model=model, leader_model=leader_model, worker_model=worker_model)

    def test_cli_model_argument_overrides_environment_fallback(self) -> None:
        with patch.dict(os.environ, {"HERMES_COLLAB_MODEL": "env-model", "ANTHROPIC_MODEL": "anthropic-model"}, clear=True):
            model, leader, worker = _model_options(self.args(model="cli-model"))

        self.assertEqual(model, "cli-model")
        self.assertIsNone(leader)
        self.assertIsNone(worker)

    def test_cli_model_argument_overrides_role_environment_fallbacks(self) -> None:
        env = {
            "HERMES_COLLAB_LEADER_MODEL": "leader-env",
            "HERMES_COLLAB_WORKER_MODEL": "worker-env",
        }
        with patch.dict(os.environ, env, clear=True):
            model, leader, worker = _model_options(self.args(model="cli-model"))

        self.assertEqual(model, "cli-model")
        self.assertIsNone(leader)
        self.assertIsNone(worker)

    def test_hermes_collab_model_env_falls_back_before_anthropic_model(self) -> None:
        with patch.dict(os.environ, {"HERMES_COLLAB_MODEL": "hermes-model", "ANTHROPIC_MODEL": "anthropic-model"}, clear=False):
            model, _leader, _worker = _model_options(self.args())

        self.assertEqual(model, "hermes-model")

    def test_anthropic_model_env_used_when_hermes_model_absent(self) -> None:
        with patch.dict(os.environ, {"ANTHROPIC_MODEL": "anthropic-model"}, clear=True):
            model, _leader, _worker = _model_options(self.args())

        self.assertEqual(model, "anthropic-model")

    def test_leader_and_worker_specific_env_fallbacks(self) -> None:
        env = {"HERMES_COLLAB_LEADER_MODEL": "leader-env", "HERMES_COLLAB_WORKER_MODEL": "worker-env"}
        with patch.dict(os.environ, env, clear=True):
            model, leader, worker = _model_options(self.args())

        self.assertIsNone(model)
        self.assertEqual(leader, "leader-env")
        self.assertEqual(worker, "worker-env")

    def test_cli_specific_model_args_override_specific_env_fallbacks(self) -> None:
        env = {"HERMES_COLLAB_LEADER_MODEL": "leader-env", "HERMES_COLLAB_WORKER_MODEL": "worker-env"}
        with patch.dict(os.environ, env, clear=True):
            _model, leader, worker = _model_options(self.args(leader_model="leader-cli", worker_model="worker-cli"))

        self.assertEqual(leader, "leader-cli")
        self.assertEqual(worker, "worker-cli")

    def test_engine_uses_general_model_for_leader_and_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = CollabEngine(Path(tmp) / "db.sqlite3", tmp, model="shared-model")

        self.assertEqual(engine.leader_model, "shared-model")
        self.assertEqual(engine.worker_model, "shared-model")

    def test_engine_uses_model_environment_fallbacks_when_constructed_directly(self) -> None:
        env = {
            "HERMES_COLLAB_MODEL": "shared-env",
            "HERMES_COLLAB_LEADER_MODEL": "leader-env",
            "HERMES_COLLAB_WORKER_MODEL": "worker-env",
        }
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, env, clear=True):
            engine = CollabEngine(Path(tmp) / "db.sqlite3", tmp)

        self.assertEqual(engine.leader_model, "leader-env")
        self.assertEqual(engine.worker_model, "worker-env")

    def test_engine_specific_models_override_general_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = CollabEngine(
                Path(tmp) / "db.sqlite3",
                tmp,
                model="shared-model",
                leader_model="leader-model",
                worker_model="worker-model",
            )

        self.assertEqual(engine.leader_model, "leader-model")
        self.assertEqual(engine.worker_model, "worker-model")

    def test_engine_all_models_none_remains_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            engine = CollabEngine(Path(tmp) / "db.sqlite3", tmp)

        self.assertIsNone(engine.leader_model)
        self.assertIsNone(engine.worker_model)

    def test_engine_worker_role_env_overrides_leader_env(self) -> None:
        env = {
            "ANTHROPIC_API_KEY": "leader-key",
            "ANTHROPIC_AUTH_TOKEN": "leader-key",
            "ANTHROPIC_BASE_URL": "https://leader.example",
            "ANTHROPIC_MODEL": "leader-model",
            "HERMES_COLLAB_WORKER_API_KEY": "worker-key",
            "HERMES_COLLAB_WORKER_BASE_URL": "https://worker.example",
            "HERMES_COLLAB_WORKER_MODEL": "worker-model",
        }
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, env, clear=True):
            engine = CollabEngine(Path(tmp) / "db.sqlite3", tmp, leader_model="leader-model", worker_model="worker-model")
            worker_env = engine._env_for_role("worker")

        self.assertEqual(worker_env["ANTHROPIC_API_KEY"], "worker-key")
        self.assertEqual(worker_env["ANTHROPIC_AUTH_TOKEN"], "worker-key")
        self.assertEqual(worker_env["ANTHROPIC_BASE_URL"], "https://worker.example")
        self.assertEqual(worker_env["ANTHROPIC_MODEL"], "worker-model")

    def test_engine_leader_role_env_preserves_leader_env(self) -> None:
        env = {
            "ANTHROPIC_API_KEY": "leader-key",
            "ANTHROPIC_AUTH_TOKEN": "leader-key",
            "ANTHROPIC_BASE_URL": "https://leader.example",
            "ANTHROPIC_MODEL": "leader-model",
            "HERMES_COLLAB_LEADER_API_KEY": "leader-role-key",
            "HERMES_COLLAB_LEADER_BASE_URL": "https://leader-role.example",
            "HERMES_COLLAB_LEADER_MODEL": "leader-role-model",
            "HERMES_COLLAB_WORKER_API_KEY": "worker-key",
            "HERMES_COLLAB_WORKER_BASE_URL": "https://worker.example",
            "HERMES_COLLAB_WORKER_MODEL": "worker-model",
        }
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, env, clear=True):
            engine = CollabEngine(Path(tmp) / "db.sqlite3", tmp, leader_model="leader-role-model", worker_model="worker-model")
            leader_env = engine._env_for_role("leader")

        self.assertEqual(leader_env["ANTHROPIC_API_KEY"], "leader-role-key")
        self.assertEqual(leader_env["ANTHROPIC_AUTH_TOKEN"], "leader-role-key")
        self.assertEqual(leader_env["ANTHROPIC_BASE_URL"], "https://leader-role.example")
        self.assertEqual(leader_env["ANTHROPIC_MODEL"], "leader-role-model")

    def test_engine_run_worker_uses_worker_role_env(self) -> None:
        env = {
            "ANTHROPIC_API_KEY": "leader-key",
            "ANTHROPIC_AUTH_TOKEN": "leader-key",
            "ANTHROPIC_BASE_URL": "https://leader.example",
            "ANTHROPIC_MODEL": "leader-model",
            "HERMES_COLLAB_WORKER_API_KEY": "worker-key",
            "HERMES_COLLAB_WORKER_BASE_URL": "https://worker.example",
            "HERMES_COLLAB_WORKER_MODEL": "worker-model",
        }
        from src.hermes_collab_engine.models import WBSNode

        captured = {}
        completed = {"args": None}

        def fake_run(cmd, **kwargs):
            captured.update(kwargs["env"])
            completed["args"] = cmd
            return type("Proc", (), {"returncode": 0, "stdout": '{"result":"ok"}', "stderr": ""})()

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, env, clear=True), patch("subprocess.run", fake_run):
            engine = CollabEngine(Path(tmp) / "db.sqlite3", tmp, leader_model="leader-model", worker_model="worker-model")
            node = WBSNode("wbs-1", "Test", "Do test", "verification", 2, [], True, "Result")
            result = engine._run_worker("run-1", node, 30)

        self.assertTrue(result.ok)
        self.assertIn("--model", completed["args"])
        self.assertIn("worker-model", completed["args"])
        self.assertEqual(captured["ANTHROPIC_API_KEY"], "worker-key")
        self.assertEqual(captured["ANTHROPIC_AUTH_TOKEN"], "worker-key")
        self.assertEqual(captured["ANTHROPIC_BASE_URL"], "https://worker.example")
        self.assertEqual(captured["ANTHROPIC_MODEL"], "worker-model")

    def test_engine_aggregate_uses_leader_role_env(self) -> None:
        env = {
            "ANTHROPIC_API_KEY": "leader-key",
            "ANTHROPIC_AUTH_TOKEN": "leader-key",
            "ANTHROPIC_BASE_URL": "https://leader.example",
            "ANTHROPIC_MODEL": "leader-model",
            "HERMES_COLLAB_LEADER_API_KEY": "leader-role-key",
            "HERMES_COLLAB_LEADER_BASE_URL": "https://leader-role.example",
            "HERMES_COLLAB_LEADER_MODEL": "leader-role-model",
            "HERMES_COLLAB_WORKER_API_KEY": "worker-key",
            "HERMES_COLLAB_WORKER_BASE_URL": "https://worker.example",
            "HERMES_COLLAB_WORKER_MODEL": "worker-model",
        }
        from src.hermes_collab_engine.models import WorkerResult

        captured = {}
        completed = {"args": None}

        def fake_run(cmd, **kwargs):
            captured.update(kwargs["env"])
            completed["args"] = cmd
            return type("Proc", (), {"returncode": 0, "stdout": '{"result":"ok"}', "stderr": ""})()

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, env, clear=True), patch("subprocess.run", fake_run):
            engine = CollabEngine(Path(tmp) / "db.sqlite3", tmp, leader_model="leader-role-model", worker_model="worker-model")
            engine.store.create_run("run-1", "request", "request", {})
            prior = WorkerResult("wbs-1", "Test", True, "ok", None, 0.01, 0, "", 1)
            result = engine._aggregate("run-1", "request", [prior], 30)

        self.assertTrue(result.ok)
        self.assertIn("--model", completed["args"])
        self.assertIn("leader-role-model", completed["args"])
        self.assertEqual(captured["ANTHROPIC_API_KEY"], "leader-role-key")
        self.assertEqual(captured["ANTHROPIC_AUTH_TOKEN"], "leader-role-key")
        self.assertEqual(captured["ANTHROPIC_BASE_URL"], "https://leader-role.example")
        self.assertEqual(captured["ANTHROPIC_MODEL"], "leader-role-model")

    def test_engine_persists_aggregate_node_result_for_dashboard_diary(self) -> None:
        env = {
            "ANTHROPIC_API_KEY": "leader-key",
            "ANTHROPIC_AUTH_TOKEN": "leader-key",
            "ANTHROPIC_MODEL": "leader-model",
        }
        outputs = iter(['{"result":"worker output","session_id":"worker-session"}', '{"result":"# Final diary","session_id":"leader-session"}'])

        def fake_run(cmd, **kwargs):
            return type("Proc", (), {"returncode": 0, "stdout": next(outputs), "stderr": ""})()

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, env, clear=True), patch("subprocess.run", fake_run):
            engine = CollabEngine(Path(tmp) / "db.sqlite3", tmp, leader_model="leader-model")
            engine.planner.assess = lambda _request: type("Score", (), {"routing": "direct", "overall": 1, "to_dict": lambda self: {"routing": "direct", "overall": 1}})()
            engine.planner.decompose = lambda _request: None

            result = engine.run("write final diary", concurrency=1, timeout=30, max_retries=0, aggregate=True)
            detail = engine.store.run_detail(result["run_id"])

        aggregate_nodes = [node for node in detail["nodes"] if node["id"].endswith("-aggregate")]
        self.assertEqual(len(aggregate_nodes), 1)
        self.assertEqual(aggregate_nodes[0]["status"], "completed")
        self.assertEqual(aggregate_nodes[0]["result"], "# Final diary")
        self.assertTrue(any(log["node_id"].endswith("-aggregate") and log["message"] == "worker finished" for log in detail["logs"]))


if __name__ == "__main__":
    unittest.main()
