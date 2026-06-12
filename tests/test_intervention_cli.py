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
from src.hermes_collab_engine.models import WBSNode
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


def seed_node(db_path: Path, node_id: str = "wbs-1") -> CollabStore:
    store = CollabStore(db_path)
    store.create_run("run_1", "title", "request", {})
    store.insert_wbs_node("run_1", WBSNode(
        node_id,
        "Node title",
        "Do node work",
        "verification",
        5,
        [],
        True,
        "Node deliverable",
    ).to_dict())
    return store


class InterventionCliTests(unittest.TestCase):
    def test_split_node_creates_shards_and_marks_original_split(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "db.sqlite3"
            seed_node(db_path)

            proc = run_cli("split-node", "--db", str(db_path), "--node-id", "wbs-1", "--split-count", "3", "--json")

            self.assertEqual(proc.returncode, 0, msg=f"stdout={proc.stdout!r} stderr={proc.stderr!r}")
            result = json.loads(proc.stdout)
            self.assertTrue(result["ok"])
            self.assertEqual(len(result["shards"]), 3)
            store = CollabStore(db_path)
            original = store._one("SELECT status, result FROM wbs_nodes WHERE id='wbs-1'")
            self.assertEqual(original["status"], "split")
            self.assertIn("wbs-1-scope-1", original["result"])
            shards = store._query("SELECT id, parent_id, status FROM wbs_nodes WHERE parent_id='wbs-1' ORDER BY id")
            self.assertEqual(len(shards), 3)
            self.assertTrue(all(row["status"] == "pending" for row in shards))

    def test_split_node_invalid_split_count_is_error_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "db.sqlite3"
            seed_node(db_path)

            proc = run_cli("split-node", "--db", str(db_path), "--node-id", "wbs-1", "--split-count", "0")

            self.assertEqual(proc.returncode, 2, msg=f"stdout={proc.stdout!r} stderr={proc.stderr!r}")
            self.assertIn("--split-count must be >= 1", proc.stdout)

    def test_missing_node_is_error_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "db.sqlite3"
            CollabStore(db_path)

            proc = run_cli("skip-node", "--db", str(db_path), "--node-id", "missing", "--reason", "not found")

            self.assertEqual(proc.returncode, 1, msg=f"stdout={proc.stdout!r} stderr={proc.stderr!r}")
            self.assertIn("node not found: missing", proc.stdout)

    def test_skip_node_marks_failed_and_logs_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "db.sqlite3"
            seed_node(db_path)

            proc = run_cli("skip-node", "--db", str(db_path), "--node-id", "wbs-1", "--reason", "operator skip", "--json")

            self.assertEqual(proc.returncode, 0, msg=f"stdout={proc.stdout!r} stderr={proc.stderr!r}")
            result = json.loads(proc.stdout)
            self.assertTrue(result["ok"])
            store = CollabStore(db_path)
            row = store._one("SELECT status, error FROM wbs_nodes WHERE id='wbs-1'")
            self.assertEqual(row["status"], "failed")
            self.assertEqual(row["error"], "operator skip")
            log = store._one("SELECT message, data_json FROM logs WHERE message='node skipped by intervention'")
            self.assertIsNotNone(log)
            self.assertEqual(json.loads(log["data_json"])["reason"], "operator skip")

    def test_kill_node_without_matching_processes_marks_failed_and_returns_1(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "db.sqlite3"
            seed_node(db_path)
            output = io.StringIO()
            with patch.object(sys, "argv", ["hermes-collab", "kill-node", "--db", str(db_path), "--node-id", "wbs-1", "--json"]), \
                 patch("src.hermes_collab_engine.cli.subprocess.run", return_value=subprocess.CompletedProcess(["pgrep"], 1, stdout="", stderr="")), \
                 redirect_stdout(output):
                code = cli.main()

            self.assertEqual(code, 1)
            self.assertEqual(json.loads(output.getvalue())["pids"], [])
            store = CollabStore(db_path)
            row = store._one("SELECT status, error FROM wbs_nodes WHERE id='wbs-1'")
            self.assertEqual(row["status"], "failed")
            self.assertEqual(row["error"], "killed by parent/operator intervention")

    def test_kill_node_with_matching_process_kills_pid_and_fails_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "db.sqlite3"
            store = seed_node(db_path)
            store.worker_start("worker_1", "run_1", "wbs-1")
            pgrep_out = "4242 claude -p 'WBS node: Node title' --output-format json\n"
            output = io.StringIO()
            with patch.object(sys, "argv", ["hermes-collab", "kill-node", "--db", str(db_path), "--node-id", "wbs-1", "--json"]), \
                 patch("src.hermes_collab_engine.cli.subprocess.run", return_value=subprocess.CompletedProcess(["pgrep"], 0, stdout=pgrep_out, stderr="")), \
                 patch("src.hermes_collab_engine.cli.os.getpid", return_value=999), \
                 patch("src.hermes_collab_engine.cli.os.kill") as mock_kill, \
                 redirect_stdout(output):
                code = cli.main()

            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue())["pids"], [4242])
            mock_kill.assert_called_once()
            killed_pid = mock_kill.call_args.args[0]
            self.assertEqual(killed_pid, 4242)
            worker = CollabStore(db_path)._one("SELECT status, error FROM workers WHERE id='worker_1'")
            self.assertEqual(worker["status"], "failed")
            self.assertEqual(worker["error"], "killed by parent/operator intervention")


if __name__ == "__main__":
    unittest.main()
