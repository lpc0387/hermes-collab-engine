import tempfile
import threading
import time
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
            self.assertIn("auto", cmd)
            self.assertIn("--allowedTools", cmd)
            allowed = cmd[cmd.index("--allowedTools") + 1]
            self.assertIn("Read", allowed)
            self.assertIn("Edit", allowed)
            self.assertIn("Write", allowed)
            self.assertIn("MultiEdit", allowed)
            self.assertIn("Bash(", allowed)  # Some Bash tool profile is present

    def test_worker_prompt_includes_reserved_write_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = CollabEngine(db_path=Path(tmp) / "collab.sqlite3", cwd=tmp)
            node = WBSNode(
                id="wbs-targeted",
                title="Targeted edit",
                description="Modify only the scheduler.",
                capability="implementation",
                complexity=1,
                dependencies=[],
                parallelizable=True,
                deliverable="Scheduler patch",
                write_targets=["src/hermes_collab_engine/engine.py"],
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
            prompt = captured["cmd"][captured["cmd"].index("-p") + 1]
            self.assertIn("Write targets reserved for this worker: src/hermes_collab_engine/engine.py", prompt)
            self.assertIn("Only modify files under these repository-relative targets.", prompt)

    def test_write_targets_overlap_blocks_parallel_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = CollabEngine(db_path=Path(tmp) / "collab.sqlite3", cwd=tmp)
            first = WBSNode(
                id="wbs-first",
                title="First edit",
                description="Modify engine scheduler.",
                capability="implementation",
                complexity=1,
                dependencies=[],
                parallelizable=True,
                deliverable="First patch",
                write_targets=["src/hermes_collab_engine"],
            )
            second = WBSNode(
                id="wbs-second",
                title="Second edit",
                description="Modify engine tests.",
                capability="implementation",
                complexity=1,
                dependencies=[],
                parallelizable=True,
                deliverable="Second patch",
                write_targets=["src/hermes_collab_engine/engine.py"],
            )

            claimed = engine._claim_write_targets(first)

            self.assertEqual(claimed, {"src/hermes_collab_engine"})
            self.assertEqual(engine._blocked_by_active_write(second), "wbs-first")
            engine._release_write_targets("wbs-first")
            self.assertIsNone(engine._blocked_by_active_write(second))

    def test_store_round_trips_write_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = CollabEngine(db_path=Path(tmp) / "collab.sqlite3", cwd=tmp)
            node = WBSNode(
                id="wbs-roundtrip",
                title="Round trip",
                description="Modify engine and tests.",
                capability="implementation",
                complexity=1,
                dependencies=[],
                parallelizable=True,
                deliverable="Patch",
                write_targets=["src/hermes_collab_engine/engine.py", "tests/test_worker_permissions.py"],
            )

            data = node.to_dict()
            engine.store.insert_wbs_node("run_test", data)
            loaded = engine.store.get_node("run_test", "wbs-roundtrip")

            self.assertEqual(
                loaded["write_targets_json"],
                '["src/hermes_collab_engine/engine.py", "tests/test_worker_permissions.py"]',
            )
            self.assertEqual(loaded["fingerprint"], "")

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

    def test_worker_git_credentials_are_env_backed(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = CollabEngine(db_path=Path(tmp) / "collab.sqlite3", cwd=tmp)
            node = WBSNode(
                id="wbs-4",
                title="Push changes",
                description="Commit and push the implementation.",
                capability="implementation",
                complexity=1,
                dependencies=[],
                parallelizable=True,
                deliverable="Pushed branch",
            )

            captured = {}

            def fake_run(cmd, **kwargs):
                captured["env"] = kwargs["env"]
                class Proc:
                    returncode = 0
                    stdout = '{"result":"ok","session_id":"s1","is_error":false}'
                    stderr = ""
                return Proc()

            env = {
                "HERMES_COLLAB_WORKER_GIT_TOKEN": "secret-token",
                "HERMES_COLLAB_WORKER_GIT_USERNAME": "git-user",
                "HERMES_COLLAB_WORKER_GIT_ALLOWED_HOSTS": "github.com,git.example.com",
            }
            with patch.dict("os.environ", env, clear=False), patch("subprocess.run", side_effect=fake_run):
                result = engine._run_worker("run_test", node, timeout=30)

            self.assertTrue(result.ok)
            worker_env = captured["env"]
            self.assertEqual(worker_env["GIT_TERMINAL_PROMPT"], "0")
            self.assertEqual(worker_env["HERMES_COLLAB_GIT_TOKEN"], "secret-token")
            self.assertEqual(worker_env["HERMES_COLLAB_GIT_USERNAME"], "git-user")
            self.assertEqual(worker_env["HERMES_COLLAB_GIT_ALLOWED_HOSTS"], "github.com,git.example.com")
            self.assertEqual(worker_env["GIT_CONFIG_KEY_0"], "credential.helper")
            self.assertIn("HERMES_COLLAB_GIT_TOKEN", worker_env["GIT_CONFIG_VALUE_0"])
            self.assertNotIn("secret-token", worker_env["GIT_CONFIG_VALUE_0"])

    def test_minimal_worker_write_and_git_prompt_runs_without_overlap(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = CollabEngine(db_path=Path(tmp) / "collab.sqlite3", cwd=tmp)
            nodes = [
                WBSNode(
                    id="wbs-write-git",
                    title="Write and git",
                    description="Modify src/app.py, inspect git status, then commit the implementation.",
                    capability="implementation",
                    complexity=1,
                    dependencies=[],
                    parallelizable=True,
                    deliverable="Working write plus git operation",
                    write_targets=["src/app.py"],
                ),
                WBSNode(
                    id="wbs-overlap",
                    title="Overlapping write",
                    description="Modify the same app file.",
                    capability="implementation",
                    complexity=1,
                    dependencies=[],
                    parallelizable=True,
                    deliverable="Overlapping implementation",
                    write_targets=["src"],
                ),
            ]
            engine.planner.assess = lambda request: type("Score", (), {"routing": "wbs", "overall": 1, "to_dict": lambda self: {"routing": "wbs", "overall": 1}})()
            engine.planner.decompose = lambda request, **kw: type("PlanObj", (), {"nodes": nodes, "shared_brief": ""})()
            first_running = threading.Event()
            release_first = threading.Event()
            started: list[str] = []
            allowed_by_node: dict[str, str] = {}
            prompts_by_node: dict[str, str] = {}
            lock = threading.Lock()

            def fake_run(cmd, **kwargs):
                prompt = cmd[cmd.index("-p") + 1]
                node_id = "wbs-write-git" if "Write and git" in prompt else "wbs-overlap"
                allowed = cmd[cmd.index("--allowedTools") + 1]
                with lock:
                    started.append(node_id)
                    allowed_by_node[node_id] = allowed
                    prompts_by_node[node_id] = prompt
                if node_id == "wbs-write-git":
                    first_running.set()
                    release_first.wait(timeout=2)
                class Proc:
                    returncode = 0
                    stdout = '{"result":"ok\\nHERMES-COLLAB-RESULT: {\\"status\\":\\"ok\\",\\"summary\\":\\"done\\",\\"files_modified\\":[\\"src/app.py\\"],\\"verification\\":[\\"simulated worker command returned 0\\"],\\"notes\\":[]}","session_id":"s1","is_error":false}'
                    stderr = ""
                return Proc()

            with patch("subprocess.run", side_effect=fake_run):
                runner = threading.Thread(target=lambda: engine.run("minimal write git", concurrency=2, aggregate=False))
                runner.start()
                self.assertTrue(first_running.wait(timeout=2))
                time.sleep(0.05)
                with lock:
                    self.assertEqual(started, ["wbs-write-git"])
                release_first.set()
                runner.join(timeout=2)

            self.assertFalse(runner.is_alive())
            self.assertEqual(started, ["wbs-write-git", "wbs-overlap"])
            first_allowed = allowed_by_node["wbs-write-git"]
            self.assertIn("Read", first_allowed)
            self.assertIn("Write", first_allowed)
            self.assertIn("Edit", first_allowed)
            self.assertIn("Bash(git status*)", first_allowed)
            self.assertIn("Bash(git add*)", first_allowed)
            self.assertIn("Bash(git commit*)", first_allowed)
            self.assertIn("Write targets reserved for this worker: src/app.py", prompts_by_node["wbs-write-git"])


if __name__ == "__main__":
    unittest.main()
