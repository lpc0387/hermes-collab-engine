import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from hermes_collab_engine.store import CollabStore


REPO_ROOT = Path(__file__).resolve().parents[1]


def run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "hermes_collab_engine.cli", *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(REPO_ROOT),
    )


class StatusCliTests(unittest.TestCase):
    def test_status_json_returns_expected_structure(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "db.sqlite3"
            # Initialize an empty store to avoid using real runtime databases
            CollabStore(db_path)

            proc = run_cli("status", "--db", str(db_path), "--json")

            self.assertEqual(proc.returncode, 0, msg=f"stdout={proc.stdout!r} stderr={proc.stderr!r}")
            
            try:
                result = json.loads(proc.stdout)
            except json.JSONDecodeError:
                self.fail(f"CLI output is not valid JSON: {proc.stdout}")

            self.assertIn("overview", result)
            self.assertIn("runs", result)
            self.assertIn("lessons", result)

            self.assertIsInstance(result["overview"], dict)
            self.assertIsInstance(result["runs"], list)
            self.assertIsInstance(result["lessons"], list)


if __name__ == "__main__":
    unittest.main()
