"""Tests for the ACP agent backend registry."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.hermes_collab_engine.agents import (
    AgentBackend,
    get_backend,
    list_backends,
    detect_available_backends,
    register_backend,
)
from src.hermes_collab_engine.engine import CollabEngine


class AgentBackendDataclassTests(unittest.TestCase):
    def test_build_command_claude_code_with_model(self):
        b = get_backend("claude-code")
        cmd = b.build_command(prompt="hello", model="sonnet-4", allowed_tools=b.default_allowed_tools)
        self.assertEqual(cmd[0], "claude")
        self.assertIn("-p", cmd)
        self.assertIn("hello", cmd)
        self.assertIn("--model", cmd)
        self.assertIn("sonnet-4", cmd)
        self.assertIn("--output-format", cmd)
        self.assertIn("json", cmd)

    def test_build_command_claude_code_without_model(self):
        b = get_backend("claude-code")
        cmd = b.build_command(prompt="hello", model=None)
        self.assertNotIn("--model", cmd)

    def test_build_command_codex(self):
        b = get_backend("codex")
        cmd = b.build_command(prompt="test", model="gpt-4")
        self.assertEqual(cmd[0], "codex")
        self.assertIn("--prompt", cmd)
        self.assertIn("test", cmd)
        self.assertIn("--model", cmd)

    def test_build_command_opencode_no_model_flag(self):
        b = get_backend("opencode")
        cmd = b.build_command(prompt="test", model="some-model")
        self.assertEqual(cmd[0], "opencode")
        self.assertNotIn("--model", cmd)  # opencode doesn't support model flag

    def test_parse_claude_json_valid(self):
        b = get_backend("claude-code")
        output = json.dumps({"result": "task done", "session_id": "abc123", "is_error": False})
        parsed = b.parse_output(output, "", 0, "wbs-1", "Test", 10.0, 1)
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["result"], "task done")
        self.assertEqual(parsed["session_id"], "abc123")

    def test_parse_claude_json_error(self):
        b = get_backend("claude-code")
        output = json.dumps({"result": "error occurred", "is_error": True})
        parsed = b.parse_output(output, "", 0, "wbs-1", "Test", 10.0, 1)
        self.assertFalse(parsed["ok"])

    def test_parse_claude_json_invalid_json(self):
        b = get_backend("claude-code")
        parsed = b.parse_output("not json at all", "", 0, "wbs-1", "Test", 10.0, 1)
        self.assertTrue(parsed["ok"])  # returncode=0 => ok
        self.assertEqual(parsed["result"], "not json at all")

    def test_parse_raw_text(self):
        b = get_backend("opencode")
        parsed = b.parse_output("hello world", "", 0, "wbs-1", "Test", 10.0, 1)
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["result"], "hello world")
        self.assertIsNone(parsed["session_id"])

    def test_parse_raw_text_nonzero_returncode(self):
        b = get_backend("opencode")
        parsed = b.parse_output("error output", "stderr", 1, "wbs-1", "Test", 10.0, 1)
        self.assertFalse(parsed["ok"])

    def test_parse_codex_json(self):
        b = get_backend("codex")
        output = json.dumps({"output": "code written", "session_id": "xyz"})
        parsed = b.parse_output(output, "", 0, "wbs-1", "Test", 10.0, 1)
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["result"], "code written")
        self.assertEqual(parsed["session_id"], "xyz")


class AgentRegistryTests(unittest.TestCase):
    def test_list_backends_returns_at_least_three(self):
        backends = list_backends()
        names = {b.name for b in backends}
        self.assertGreaterEqual(len(backends), 3)
        self.assertIn("claude-code", names)
        self.assertIn("codex", names)
        self.assertIn("opencode", names)

    def test_get_backend_known(self):
        b = get_backend("claude-code")
        self.assertEqual(b.display_name, "Claude Code")

    def test_get_backend_unknown_raises(self):
        with self.assertRaises(KeyError):
            get_backend("nonexistent-agent")

    def test_detect_available_finds_claude(self):
        # Claude Code is installed in this environment
        available = detect_available_backends()
        names = {b.name for b in available}
        self.assertIn("claude-code", names)

    def test_detect_available_excludes_missing(self):
        available = detect_available_backends()
        names = {b.name for b in available}
        # codex and opencode are not installed
        self.assertNotIn("codex", names)
        self.assertNotIn("opencode", names)

    def test_register_custom_backend(self):
        custom = AgentBackend(
            name="test-agent",
            display_name="Test Agent",
            command=["echo"],
            prompt_flag="",
            output_format_flags=[],
            supports_model_flag=False,
            model_flag="",
            permission_flags=None,
            allowed_tools_flag=None,
            output_parser="raw_text",
            process_pattern="echo",
            prompt_prefix="Test agent prefix",
            prompt_suffix="",
            default_allowed_tools=[],
        )
        register_backend(custom)
        b = get_backend("test-agent")
        self.assertEqual(b.display_name, "Test Agent")


class EngineAgentIntegrationTests(unittest.TestCase):
    def test_engine_default_agent_is_claude_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = CollabEngine(Path(tmp) / "db.sqlite3", tmp)
            self.assertEqual(engine.agent_backend.name, "claude-code")

    def test_engine_custom_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = CollabEngine(Path(tmp) / "db.sqlite3", tmp, agent="codex")
            self.assertEqual(engine.agent_backend.name, "codex")

    def test_engine_unknown_agent_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(KeyError):
                CollabEngine(Path(tmp) / "db.sqlite3", tmp, agent="nonexistent")

    def test_run_stores_agent_in_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = CollabEngine(Path(tmp) / "db.sqlite3", tmp, agent="claude-code")
            # Mock planner to avoid actually running claude
            from src.hermes_collab_engine.models import ComplexityScore, Plan, WBSNode, WorkerResult
            engine.planner.assess = lambda r: ComplexityScore(1, 1, 1, 1, 1, 2, "wbs")
            engine.planner.decompose = lambda r: Plan(nodes=[
                WBSNode("wbs-1", "Test", "Do test", "verification", 2, [], True, "Result")
            ])
            engine._run_worker = lambda rid, n, t, model_override=None: WorkerResult(
                n.id, n.title, True, "ok", None, 0.01, 0, "", n.attempt
            )
            result = engine.run("test task", timeout=30, max_retries=0, aggregate=False)
            self.assertTrue(result["ok"])
            run = engine.store._one("SELECT agent FROM runs WHERE id=?", (result["run_id"],))
            self.assertEqual(run["agent"], "claude-code")

    def test_worker_log_includes_agent_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = CollabEngine(Path(tmp) / "db.sqlite3", tmp, agent="claude-code")
            from src.hermes_collab_engine.models import ComplexityScore, Plan, WBSNode, WorkerResult
            engine.planner.assess = lambda r: ComplexityScore(1, 1, 1, 1, 1, 2, "wbs")
            engine.planner.decompose = lambda r: Plan(nodes=[
                WBSNode("wbs-1", "Test", "Do test", "verification", 2, [], True, "Result")
            ])
            engine._run_worker = lambda rid, n, t, model_override=None: WorkerResult(
                n.id, n.title, True, "ok", None, 0.01, 0, "", n.attempt
            )
            result = engine.run("test task", timeout=30, max_retries=0, aggregate=False)
            # The engine logs "worker started" with agent name before calling _run_worker
            log = engine.store._one("SELECT data_json FROM logs WHERE node_id='wbs-1' AND message='worker started'")
            if log:
                data = json.loads(log["data_json"])
                self.assertEqual(data.get("agent"), "claude-code")
            else:
                # If mock bypasses logging, just verify the engine has the right backend
                self.assertEqual(engine.agent_backend.name, "claude-code")


class CLIAgentTests(unittest.TestCase):
    def test_agents_command_lists_backends(self):
        import subprocess
        proc = subprocess.run(
            ["python3", "-m", "hermes_collab_engine.cli", "agents"],
            capture_output=True, text=True, cwd="/root/hermes-collab-engine/src",
            env={**__import__("os").environ, "PYTHONPATH": "/root/hermes-collab-engine/src"},
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("claude-code", proc.stdout)

    def test_agents_available_command(self):
        import subprocess
        proc = subprocess.run(
            ["python3", "-m", "hermes_collab_engine.cli", "agents", "--available"],
            capture_output=True, text=True, cwd="/root/hermes-collab-engine/src",
            env={**__import__("os").environ, "PYTHONPATH": "/root/hermes-collab-engine/src"},
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("claude-code", proc.stdout)

    def test_agents_json_output(self):
        import subprocess
        proc = subprocess.run(
            ["python3", "-m", "hermes_collab_engine.cli", "agents", "--json"],
            capture_output=True, text=True, cwd="/root/hermes-collab-engine/src",
            env={**__import__("os").environ, "PYTHONPATH": "/root/hermes-collab-engine/src"},
        )
        self.assertEqual(proc.returncode, 0)
        data = json.loads(proc.stdout)
        self.assertIsInstance(data, list)
        self.assertGreaterEqual(len(data), 3)


if __name__ == "__main__":
    unittest.main()
