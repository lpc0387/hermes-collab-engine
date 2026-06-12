import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.hermes_collab_engine.engine import CollabEngine
from src.hermes_collab_engine.models import WBSNode


class WorkerPermissionCommandTest(unittest.TestCase):
    def test_worker_claude_command_allows_file_edits(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = CollabEngine(db_path=Path(tmp) / "collab.sqlite3", cwd=tmp)
            node = WBSNode(
                id="wbs-1",
                title="Edit docs",
                description="Modify docs/example.md only.",
                capability="docs",
                complexity=1,
                dependencies=[],
                parallelizable=True,
                deliverable="Updated docs/example.md",
            )

            captured = {}

            def fake_run(cmd, **kwargs):
                captured["cmd"] = cmd
                class Proc:
                    returncode = 0
                    stdout = '{"result":"ok","session_id":"s1","is_error":false}'
                    stderr = ""
                return Proc()

            with patch("subprocess.run", side_effect=fake_run):
                result = engine._run_worker("run_test", node, timeout=30)

            self.assertTrue(result.ok)
            cmd = captured["cmd"]
            self.assertIn("--permission-mode", cmd)
            self.assertIn("acceptEdits", cmd)
            self.assertIn("--allowedTools", cmd)
            allowed = cmd[cmd.index("--allowedTools") + 1]
            # Tool manager selects file-edit + git-local for docs capability
            self.assertIn("Read", allowed)
            self.assertIn("Edit", allowed)
            self.assertIn("Write", allowed)
            self.assertIn("MultiEdit", allowed)
            self.assertIn("Bash(git diff*)", allowed)
            self.assertIn("Bash(git status*)", allowed)
            self.assertNotIn("--dangerously-skip-permissions", cmd)

    def test_worker_claude_command_allows_git_write_for_implementation(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = CollabEngine(db_path=Path(tmp) / "collab.sqlite3", cwd=tmp)
            node = WBSNode(
                id="wbs-2",
                title="Implement feature",
                description="Implement and commit the new feature.",
                capability="implementation",
                complexity=2,
                dependencies=[],
                parallelizable=True,
                deliverable="Working implementation",
            )

            captured = {}

            def fake_run(cmd, **kwargs):
                captured["cmd"] = cmd
                class Proc:
                    returncode = 0
                    stdout = '{"result":"ok","session_id":"s1","is_error":false}'
                    stderr = ""
                return Proc()

            with patch("subprocess.run", side_effect=fake_run):
                result = engine._run_worker("run_test", node, timeout=30)

            self.assertTrue(result.ok)
            cmd = captured["cmd"]
            allowed = cmd[cmd.index("--allowedTools") + 1]
            # implementation includes git-write (commit, push) when task mentions it
            self.assertIn("Read", allowed)
            self.assertIn("Edit", allowed)
            self.assertIn("Bash(git diff*)", allowed)
            self.assertIn("Bash(python3 -m unittest*)", allowed)

    def test_worker_without_tool_profiles_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            from src.hermes_collab_engine.tools import ToolRegistry
            empty_registry = ToolRegistry()
            empty_registry._profiles = {}
            engine = CollabEngine(Path(tmp) / "db.sqlite3", tmp, tool_registry=empty_registry)
            node = WBSNode(
                id="wbs-3",
                title="Generic task",
                description="Do something unspecified.",
                capability="unknown",
                complexity=1,
                dependencies=[],
                parallelizable=True,
                deliverable="Result",
            )

            captured = {}

            def fake_run(cmd, **kwargs):
                captured["cmd"] = cmd
                class Proc:
                    returncode = 0
                    stdout = '{"result":"ok","session_id":"s1","is_error":false}'
                    stderr = ""
                return Proc()

            with patch("subprocess.run", side_effect=fake_run):
                result = engine._run_worker("run_test", node, timeout=30)

            self.assertTrue(result.ok)
            cmd = captured["cmd"]
            allowed = cmd[cmd.index("--allowedTools") + 1]
            # No tool profiles matched → falls back to backend defaults
            self.assertIn("Read", allowed)
            self.assertIn("Edit", allowed)


if __name__ == "__main__":
    unittest.main()
