import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "hermes_collab_engine.cli", *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(REPO_ROOT),
    )


class CliLessonTest(unittest.TestCase):
    def test_lesson_add_and_list_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "db.sqlite3")

            add_proc = run_cli(
                "lesson", "add",
                "--db", db_path,
                "--category", "preflight",
                "--lesson", "test lesson",
                "--source", "hermes-delegate-task",
                "--evidence-json", '{"k":"v"}',
            )
            self.assertEqual(
                add_proc.returncode, 0,
                msg=f"stdout={add_proc.stdout!r} stderr={add_proc.stderr!r}",
            )
            self.assertIn('"ok": true', add_proc.stdout.replace('"ok":true', '"ok": true'))

            list_proc = run_cli(
                "lesson", "list",
                "--db", db_path,
                "--json",
            )
            self.assertEqual(
                list_proc.returncode, 0,
                msg=f"stdout={list_proc.stdout!r} stderr={list_proc.stderr!r}",
            )
            rows = json.loads(list_proc.stdout)
            self.assertIsInstance(rows, list)
            self.assertEqual(len(rows), 1)
            entry = rows[0]
            self.assertEqual(entry["category"], "preflight")
            self.assertEqual(entry["lesson"], "test lesson")
            evidence = json.loads(entry["evidence_json"])
            self.assertIsInstance(evidence, dict)
            self.assertEqual(evidence.get("source"), "hermes-delegate-task")
            self.assertEqual(evidence.get("k"), "v")

    def test_lesson_add_invalid_evidence_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "db.sqlite3")

            proc = run_cli(
                "lesson", "add",
                "--db", db_path,
                "--category", "preflight",
                "--lesson", "irrelevant",
                "--evidence-json", "not json",
            )
            self.assertEqual(
                proc.returncode, 2,
                msg=f"stdout={proc.stdout!r} stderr={proc.stderr!r}",
            )
            combined = proc.stdout + proc.stderr
            self.assertIn("invalid --evidence-json", combined)

    def test_lesson_list_filter_nonexistent_category(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "db.sqlite3")

            add_proc = run_cli(
                "lesson", "add",
                "--db", db_path,
                "--category", "preflight",
                "--lesson", "test lesson",
                "--evidence-json", "{}",
            )
            self.assertEqual(add_proc.returncode, 0)

            list_proc = run_cli(
                "lesson", "list",
                "--db", db_path,
                "--category", "nonexistent",
                "--json",
            )
            self.assertEqual(
                list_proc.returncode, 0,
                msg=f"stdout={list_proc.stdout!r} stderr={list_proc.stderr!r}",
            )
            self.assertEqual(list_proc.stdout.strip(), "[]")
            self.assertEqual(json.loads(list_proc.stdout), [])


if __name__ == "__main__":
    unittest.main()
