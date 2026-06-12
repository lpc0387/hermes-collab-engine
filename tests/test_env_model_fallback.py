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
        with tempfile.TemporaryDirectory() as tmp:
            engine = CollabEngine(Path(tmp) / "db.sqlite3", tmp)

        self.assertIsNone(engine.leader_model)
        self.assertIsNone(engine.worker_model)


if __name__ == "__main__":
    unittest.main()
