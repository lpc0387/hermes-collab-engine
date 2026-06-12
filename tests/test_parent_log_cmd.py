from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

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


class ParentLogCommandTests(unittest.TestCase):
    def test_parent_log_writes_scope_parent_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "db.sqlite3"

            proc = run_cli(
                "parent-log",
                "--db", str(db_path),
                "--run-id", "run_1",
                "--node-id", "wbs-1",
                "--level", "warning",
                "--message", "operator note",
                "--data-json", '{"key":"val","scope":"parent"}',
                "--json",
            )

            self.assertEqual(proc.returncode, 0, msg=f"stdout={proc.stdout!r} stderr={proc.stderr!r}")
            result = json.loads(proc.stdout)
            self.assertTrue(result["ok"])
            self.assertEqual(result["run_id"], "run_1")
            self.assertEqual(result["node_id"], "wbs-1")
            store = CollabStore(db_path)
            rows = store._query("SELECT * FROM logs WHERE run_id=?", ("run_1",))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["node_id"], "wbs-1")
            self.assertEqual(rows[0]["level"], "warning")
            self.assertEqual(rows[0]["message"], "operator note")
            data = json.loads(rows[0]["data_json"])
            self.assertEqual(data["source"], "parent-log")
            self.assertEqual(data["scope"], "parent")
            self.assertEqual(data["key"], "val")

    def test_parent_log_defaults_to_info_level(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "db.sqlite3"

            proc = run_cli("parent-log", "--db", str(db_path), "--message", "note")

            self.assertEqual(proc.returncode, 0, msg=f"stdout={proc.stdout!r} stderr={proc.stderr!r}")
            row = CollabStore(db_path)._one("SELECT level, message FROM logs")
            self.assertEqual(row["level"], "info")
            self.assertEqual(row["message"], "note")

    def test_parent_log_invalid_data_json_exits_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proc = run_cli(
                "parent-log",
                "--db", str(Path(tmp) / "db.sqlite3"),
                "--message", "note",
                "--data-json", "not json",
            )

            self.assertEqual(proc.returncode, 2, msg=f"stdout={proc.stdout!r} stderr={proc.stderr!r}")
            self.assertIn("invalid --data-json", proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
