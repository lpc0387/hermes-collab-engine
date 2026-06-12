from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from src.hermes_collab_engine import cli
from src.hermes_collab_engine.store import CollabStore


REPO_ROOT = Path(__file__).resolve().parents[1]


def run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "src.hermes_collab_engine.cli", *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(REPO_ROOT),
    )


class InterventionCliV3Tests(unittest.TestCase):
    def test_pause_run_cli_calls_engine_and_prints_json(self) -> None:
        calls: list[tuple[str, str]] = []

        class FakeEngine:
            def __init__(self, db, cwd, worker_model=None):
                calls.append((str(db), str(cwd)))

            def pause_run(self, run_id: str, reason: str | None = None):
                calls.append((run_id, reason or ""))
                return {"ok": True, "run_id": run_id, "paused": True}

        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(sys, "argv", ["hermes-collab", "pause-run", "--db", str(Path(tmp) / "db.sqlite3"), "--cwd", tmp, "--run-id", "run_1", "--reason", "operator pause", "--json"]), \
             patch("src.hermes_collab_engine.cli.CollabEngine", FakeEngine), \
             redirect_stdout(output):
            code = cli.main()

        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["run_id"], "run_1")
        self.assertEqual(calls[-1], ("run_1", "operator pause"))

    def test_resume_run_cli_calls_engine_and_prints_json(self) -> None:
        calls: list[tuple[str, str]] = []

        class FakeEngine:
            def __init__(self, db, cwd, worker_model=None):
                pass

            def resume_run(self, run_id: str, reason: str | None = None):
                calls.append((run_id, reason or ""))
                return {"ok": True, "run_id": run_id, "resumed": True}

        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(sys, "argv", ["hermes-collab", "resume-run", "--db", str(Path(tmp) / "db.sqlite3"), "--cwd", tmp, "--run-id", "run_1", "--reason", "operator resume", "--json"]), \
             patch("src.hermes_collab_engine.cli.CollabEngine", FakeEngine), \
             redirect_stdout(output):
            code = cli.main()

        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertTrue(result["resumed"])
        self.assertEqual(calls, [("run_1", "operator resume")])

    def test_redo_node_cli_passes_run_id_node_id_cascade_and_model(self) -> None:
        calls: list[tuple] = []

        class FakeEngine:
            def __init__(self, db, cwd, worker_model=None):
                calls.append(("init", str(db), str(cwd), worker_model))

            def redo_node(self, run_id: str, node_id: str, cascade: bool = False, worker_model: str | None = None, reason: str | None = None, description_delta: str | None = None):
                calls.append((run_id, node_id, cascade, worker_model, reason, description_delta))
                return {"ok": True, "node_id": node_id, "attempt": 2}

        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(sys, "argv", ["hermes-collab", "redo-node", "--db", str(Path(tmp) / "db.sqlite3"), "--cwd", tmp, "--run-id", "run_1", "--node-id", "wbs-1", "--cascade", "--worker-model", "worker-model", "--reason", "retry", "--description-delta", "narrow scope", "--json"]), \
             patch("src.hermes_collab_engine.cli.CollabEngine", FakeEngine), \
             redirect_stdout(output):
            code = cli.main()

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["node_id"], "wbs-1")
        self.assertEqual(calls[0], ("init", str(Path(tmp) / "db.sqlite3"), tmp, "worker-model"))
        self.assertEqual(calls[1], ("run_1", "wbs-1", True, "worker-model", "retry", "narrow scope"))

    def test_risk_policy_set_and_show_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "db.sqlite3"

            set_proc = run_cli(
                "risk-policy",
                "set",
                "--db",
                str(db_path),
                "--low",
                "auto",
                "--medium",
                "notify",
                "--high",
                "pause",
                "--checkpoint-timeout",
                "45",
            )
            show_proc = run_cli("risk-policy", "show", "--db", str(db_path))

            self.assertEqual(set_proc.returncode, 0, msg=f"stdout={set_proc.stdout!r} stderr={set_proc.stderr!r}")
            self.assertEqual(show_proc.returncode, 0, msg=f"stdout={show_proc.stdout!r} stderr={show_proc.stderr!r}")
            policy = json.loads(show_proc.stdout)["risk_policy"]
            self.assertEqual(policy, {"low": "auto", "medium": "notify", "high": "pause", "checkpoint_timeout": 45})

    def test_risk_policy_rejects_invalid_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "db.sqlite3"

            proc = run_cli("risk-policy", "set", "--db", str(db_path), "--checkpoint-timeout", "0")

            self.assertEqual(proc.returncode, 2, msg=f"stdout={proc.stdout!r} stderr={proc.stderr!r}")
            self.assertIn("--checkpoint-timeout must be >= 1", proc.stdout)
            self.assertIsNone(CollabStore(db_path).get_setting("risk_policy"))


if __name__ == "__main__":
    unittest.main()
