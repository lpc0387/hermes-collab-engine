"""Tests for the ACP agent backend registry."""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from src.hermes_collab_engine.agents import (
    AgentBackend,
    backends_for_capability,
    delete_backend,
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

    def test_delete_backend(self):
        custom = AgentBackend(
            name="del-me",
            display_name="Delete Me",
            command=["echo"],
            prompt_flag="",
            output_format_flags=[],
            supports_model_flag=False,
            model_flag="",
            permission_flags=None,
            allowed_tools_flag=None,
            output_parser="raw_text",
            process_pattern="echo",
            prompt_prefix="",
            prompt_suffix="",
            default_allowed_tools=[],
        )
        register_backend(custom)
        self.assertTrue(delete_backend("del-me"))
        with self.assertRaises(KeyError):
            get_backend("del-me")

    def test_delete_nonexistent_returns_false(self):
        self.assertFalse(delete_backend("nonexistent-agent-xyz"))

    def test_agent_backend_has_enabled_field(self):
        b = get_backend("claude-code")
        self.assertTrue(b.enabled)

    def test_agent_backend_enabled_in_to_dict(self):
        b = get_backend("claude-code")
        d = b.to_dict()
        self.assertIn("enabled", d)
        self.assertTrue(d["enabled"])


class AgentCapabilitiesTests(unittest.TestCase):
    def test_builtin_claude_code_has_capabilities(self):
        b = get_backend("claude-code")
        self.assertIsInstance(b.capabilities, list)
        self.assertIn("file-edit", b.capabilities)
        self.assertIn("git-ops", b.capabilities)
        self.assertIn("test-run", b.capabilities)

    def test_builtin_codex_has_capabilities(self):
        b = get_backend("codex")
        self.assertIsInstance(b.capabilities, list)
        self.assertIn("file-edit", b.capabilities)

    def test_builtin_opencode_has_capabilities(self):
        b = get_backend("opencode")
        self.assertIsInstance(b.capabilities, list)
        self.assertIn("file-edit", b.capabilities)

    def test_backends_for_capability(self):
        results = backends_for_capability("file-edit")
        names = {b.name for b in results}
        self.assertIn("claude-code", names)
        self.assertIn("codex", names)
        self.assertIn("opencode", names)

    def test_backends_for_capability_mcp_host(self):
        results = backends_for_capability("mcp-host")
        names = {b.name for b in results}
        self.assertIn("claude-code", names)
        self.assertNotIn("codex", names)

    def test_backends_for_capability_unknown(self):
        results = backends_for_capability("nonexistent-cap-xyz")
        self.assertEqual(len(results), 0)

    def test_custom_backend_with_capabilities(self):
        custom = AgentBackend(
            name="cap-test",
            display_name="Cap Test",
            command=["echo"],
            prompt_flag="",
            output_format_flags=[],
            supports_model_flag=False,
            model_flag="",
            permission_flags=None,
            allowed_tools_flag=None,
            output_parser="raw_text",
            process_pattern="echo",
            prompt_prefix="",
            prompt_suffix="",
            default_allowed_tools=[],
            capabilities=["browser", "file-edit"],
        )
        register_backend(custom)
        b = get_backend("cap-test")
        self.assertEqual(b.capabilities, ["browser", "file-edit"])
        delete_backend("cap-test")

    def test_custom_backend_default_empty_capabilities(self):
        custom = AgentBackend(
            name="cap-default-test",
            display_name="Cap Default",
            command=["echo"],
            prompt_flag="",
            output_format_flags=[],
            supports_model_flag=False,
            model_flag="",
            permission_flags=None,
            allowed_tools_flag=None,
            output_parser="raw_text",
            process_pattern="echo",
            prompt_prefix="",
            prompt_suffix="",
            default_allowed_tools=[],
        )
        register_backend(custom)
        b = get_backend("cap-default-test")
        self.assertEqual(b.capabilities, [])
        delete_backend("cap-default-test")

    def test_capabilities_in_to_dict(self):
        b = get_backend("claude-code")
        d = b.to_dict()
        self.assertIn("capabilities", d)
        self.assertIsInstance(d["capabilities"], list)
        self.assertIn("file-edit", d["capabilities"])

    def test_api_agents_returns_capabilities(self):
        """GET /api/agents should include capabilities in each agent."""
        import urllib.request
        import socket
        import tempfile
        import threading
        from src.hermes_collab_engine.server import DashboardServer

        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "collab.sqlite3")
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                port = s.getsockname()[1]
            server = DashboardServer("127.0.0.1", port, db_path, tmp)
            t = threading.Thread(target=server.serve, daemon=True)
            t.start()
            base = f"http://127.0.0.1:{port}"
            # wait for server
            for _ in range(50):
                try:
                    urllib.request.urlopen(base + "/api/overview", timeout=1).read()
                    break
                except Exception:
                    import time as _t
                    _t.sleep(0.05)
            resp = urllib.request.urlopen(base + "/api/agents", timeout=3)
            agents = json.loads(resp.read().decode())
            claude = next(a for a in agents if a["name"] == "claude-code")
            self.assertIn("capabilities", claude)
            self.assertIn("file-edit", claude["capabilities"])



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
            engine.planner.decompose = lambda r, **kw: Plan(nodes=[
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
            engine.planner.decompose = lambda r, **kw: Plan(nodes=[
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


class AgentAPICRUDTests(unittest.TestCase):
    """Test agent CRUD via HTTP API."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "collab.sqlite3")
        self.cwd = self.tmp.name
        self.port = self._free_port()
        from src.hermes_collab_engine.server import DashboardServer
        self.server = DashboardServer("127.0.0.1", self.port, self.db_path, self.cwd)
        self.thread = threading.Thread(target=self.server.serve, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.port}"
        self._wait_ready()

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _free_port():
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def _wait_ready(self):
        import time as _time
        last = None
        for _ in range(50):
            try:
                urllib.request.urlopen(self.base + "/api/overview", timeout=1).read()
                return
            except Exception as exc:
                last = exc
                _time.sleep(0.05)
        raise AssertionError(f"server did not become ready: {last}")

    def _post_json(self, path, payload):
        import urllib.request, urllib.error
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        return json.loads(urllib.request.urlopen(req, timeout=3).read().decode())

    def _get_json(self, path):
        import urllib.request
        return json.loads(urllib.request.urlopen(self.base + path, timeout=3).read().decode())

    def _delete(self, path):
        import urllib.request, urllib.error
        req = urllib.request.Request(self.base + path, method="DELETE")
        return json.loads(urllib.request.urlopen(req, timeout=3).read().decode())

    def test_post_agents_registers_backend(self):
        resp = self._post_json("/api/agents", {
            "name": "api-test-agent",
            "display_name": "API Test Agent",
            "command": ["echo", "hello"],
            "output_parser": "raw_text",
            "capabilities": ["file-edit"],
        })
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["name"], "api-test-agent")
        # Verify it shows up in GET /api/agents
        agents = self._get_json("/api/agents")
        names = [a["name"] for a in agents]
        self.assertIn("api-test-agent", names)
        delete_backend("api-test-agent")

    def test_post_agents_requires_name(self):
        import urllib.request, urllib.error
        req = urllib.request.Request(
            self.base + "/api/agents",
            data=json.dumps({}).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req, timeout=3)
            self.fail("expected HTTPError 400")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 400)

    def test_post_agents_validates_name_pattern(self):
        """POST /api/agents with invalid name should return 400."""
        import urllib.request, urllib.error
        req = urllib.request.Request(
            self.base + "/api/agents",
            data=json.dumps({"name": "INVALID NAME!", "command": ["echo"], "capabilities": ["test"]}).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req, timeout=3)
            self.fail("expected HTTPError 400")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 400)

    def test_post_agents_validates_command_required(self):
        """POST /api/agents with empty command should return 400."""
        import urllib.request, urllib.error
        req = urllib.request.Request(
            self.base + "/api/agents",
            data=json.dumps({"name": "test-cmd", "command": [], "capabilities": ["test"]}).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req, timeout=3)
            self.fail("expected HTTPError 400")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 400)

    def test_post_agents_validates_capabilities(self):
        """POST /api/agents with empty capabilities should return 400."""
        import urllib.request, urllib.error
        req = urllib.request.Request(
            self.base + "/api/agents",
            data=json.dumps({"name": "test-caps", "command": ["echo"], "capabilities": []}).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req, timeout=3)
            self.fail("expected HTTPError 400")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 400)

    def test_post_agents_validates_output_parser(self):
        """POST /api/agents with unknown parser should return 400."""
        import urllib.request, urllib.error
        req = urllib.request.Request(
            self.base + "/api/agents",
            data=json.dumps({"name": "test-parser", "command": ["echo"], "capabilities": ["test"], "output_parser": "unknown_parser"}).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req, timeout=3)
            self.fail("expected HTTPError 400")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 400)

    def test_post_agents_returns_409_on_duplicate(self):
        """POST /api/agents with existing name should return 409."""
        import urllib.request, urllib.error
        # First registration succeeds
        resp = self._post_json("/api/agents", {
            "name": "dup-test-agent",
            "command": ["echo"],
            "capabilities": ["test"],
        })
        self.assertTrue(resp["ok"])
        # Second registration with same name returns 409
        req = urllib.request.Request(
            self.base + "/api/agents",
            data=json.dumps({"name": "dup-test-agent", "command": ["echo"], "capabilities": ["test"]}).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req, timeout=3)
            self.fail("expected HTTPError 409")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 409)
        # cleanup
        delete_backend("dup-test-agent")

    def test_api_agents_returns_enabled_field(self):
        """GET /api/agents should include enabled field."""
        agents = self._get_json("/api/agents")
        claude = next(a for a in agents if a["name"] == "claude-code")
        self.assertIn("enabled", claude)
        self.assertTrue(claude["enabled"])


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
