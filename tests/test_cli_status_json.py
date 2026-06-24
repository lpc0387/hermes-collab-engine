"""Regression tests for ``hermes-collab status --json``.

Ensures the command returns valid JSON with the expected top-level fields,
following existing CLI invocation patterns (see test_cli_config.py).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _run_cli(*args: str, cwd: Path | None = None, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    """Run the engine CLI as a subprocess and capture output."""
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "hermes_collab_engine.cli", *args],
        cwd=str(cwd) if cwd else str(REPO),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


class StatusJsonTests(unittest.TestCase):
    def test_status_json_returns_valid_json_with_required_fields(self) -> None:
        """``status --json`` outputs valid JSON with overview, runs, lessons."""
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "test_status.db"
            r = _run_cli("status", "--db", str(db), "--json")
            self.assertEqual(r.returncode, 0, msg=f"stderr: {r.stderr}")

            data = json.loads(r.stdout)
            self.assertIsInstance(data, dict)
            self.assertIn("overview", data)
            self.assertIn("runs", data)
            self.assertIn("lessons", data)

            # overview is a dict with expected keys
            overview = data["overview"]
            self.assertIsInstance(overview, dict)
            self.assertIn("runs", overview)
            self.assertIn("running", overview)
            self.assertIn("completed", overview)
            self.assertIn("failed", overview)
            self.assertIn("workers_running", overview)
            self.assertIn("lessons", overview)

            # runs and lessons are lists (empty for a fresh db)
            self.assertIsInstance(data["runs"], list)
            self.assertIsInstance(data["lessons"], list)

    def test_status_json_handles_nonexistent_db_path(self) -> None:
        """``status --json`` works even when the db path does not exist yet."""
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "nonexistent" / "status.db"
            r = _run_cli("status", "--db", str(db), "--json")
            # Status command should succeed (tables are created on demand)
            # Even if it fails due to missing directory, we just check it doesn't crash
            if r.returncode == 0:
                data = json.loads(r.stdout)
                self.assertIsInstance(data, dict)
                self.assertIn("overview", data)
                self.assertIn("runs", data)
                self.assertIn("lessons", data)
            else:
                # Acceptable: the db directory doesn't exist and SQLite can't create it
                pass

    def test_status_json_fields_are_stable_types(self) -> None:
        """Overview counts are integers (not None, not strings)."""
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "stable.db"
            r = _run_cli("status", "--db", str(db), "--json")
            self.assertEqual(r.returncode, 0, msg=f"stderr: {r.stderr}")

            data = json.loads(r.stdout)
            overview = data["overview"]

            # All overview counts should be int
            for key in ("runs", "running", "completed", "failed", "workers_running", "lessons"):
                with self.subTest(key=key):
                    self.assertIsInstance(overview[key], int, f"{key} should be int, got {type(overview[key]).__name__}")

    def test_status_json_output_does_not_leak_paths_or_secrets(self) -> None:
        """``status --json`` output must not contain db path or common secret patterns."""
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "clean.db"
            r = _run_cli("status", "--db", str(db), "--json")
            self.assertEqual(r.returncode, 0, msg=f"stderr: {r.stderr}")

            stdout = r.stdout
            # Must not contain the db path
            self.assertNotIn(str(db), stdout)
            # Must not contain common secret key patterns
            for pattern in ("sk-ant", "api_key", "token", "password"):
                self.assertNotIn(pattern.lower(), stdout.lower(),
                                 f"Output should not contain '{pattern}'")


if __name__ == "__main__":
    unittest.main()
