"""Tests for Hermes worker tool profile management."""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.hermes_collab_engine.engine import CollabEngine
from src.hermes_collab_engine.models import WBSNode
from src.hermes_collab_engine.tools import ToolProfile, ToolRegistry, get_default_tool_registry


def _extract_allowed_tools(cmd: list[str]) -> list[str]:
    if "--allowedTools" not in cmd:
        return []
    value = cmd[cmd.index("--allowedTools") + 1]
    return [part for part in value.split(",") if part]


def _extract_prompt(cmd: list[str]) -> str:
    for idx, token in enumerate(cmd):
        if token == "-p" and idx + 1 < len(cmd):
            return cmd[idx + 1]
    return max((arg for arg in cmd if isinstance(arg, str)), key=len)


class ToolRegistryTests(unittest.TestCase):
    def test_builtin_registry_contains_core_profiles(self):
        registry = get_default_tool_registry()
        names = {profile.name for profile in registry.list_all()}
        self.assertIn("file-edit", names)
        self.assertIn("git-local", names)
        self.assertIn("python-tests", names)
        self.assertIn("mcp-readonly", names)

    def test_select_for_implementation_includes_edit_and_tests(self):
        registry = get_default_tool_registry()
        profiles = registry.select_for_node("implementation", "Implement files and verify with unittest")
        names = [profile.name for profile in profiles]
        self.assertIn("file-edit", names)
        self.assertIn("python-tests", names)
        self.assertLessEqual(len(profiles), 4)

    def test_select_for_mcp_research_includes_readonly_mcp_profile(self):
        registry = get_default_tool_registry()
        profiles = registry.select_for_node("research", "Use MCP read-only tools to search external context")
        names = [profile.name for profile in profiles]
        self.assertIn("mcp-readonly", names)

    def test_select_for_node_normalizes_capability_and_empty_task(self):
        registry = get_default_tool_registry()
        profiles = registry.select_for_node(" Verification ", None)
        names = [profile.name for profile in profiles]
        self.assertIn("file-edit", names)
        self.assertIn("python-tests", names)

    def test_allowed_tools_are_deduplicated_in_profile_order(self):
        registry = ToolRegistry()
        one = ToolProfile("one", "One", "test", "", ["Read", "Edit"], ["implementation"], 1)
        two = ToolProfile("two", "Two", "test", "", ["Read", "Write"], ["implementation"], 1)
        self.assertEqual(registry.allowed_tools_for_profiles([one, two]), ["Read", "Edit", "Write"])

    def test_render_for_prompt_includes_profile_tools(self):
        registry = get_default_tool_registry()
        profile = registry.get("mcp-readonly")
        rendered = registry.render_for_prompt([profile])
        self.assertIn("Tool profiles selected by Hermes", rendered)
        self.assertIn("Read-only MCP Tools", rendered)
        self.assertIn("mcp__filesystem__read_file", rendered)


class EngineToolInjectionTests(unittest.TestCase):
    def test_run_worker_uses_selected_tool_profiles_for_allowed_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = CollabEngine(Path(tmp) / "db.sqlite3", tmp)
            node = WBSNode(
                id="wbs-impl",
                title="Implement feature",
                description="Modify files and run unittest verification.",
                capability="implementation",
                complexity=4,
                dependencies=[],
                parallelizable=True,
                deliverable="Working implementation",
            )
            completed = subprocess.CompletedProcess(
                args=["claude"],
                returncode=0,
                stdout=json.dumps({"result": "ok", "session_id": "s1", "is_error": False}),
                stderr="",
            )
            with patch("src.hermes_collab_engine.engine.subprocess.run", return_value=completed) as mock_run:
                result = engine._run_worker("run_test", node, timeout=30)

            self.assertTrue(result.ok)
            tools = _extract_allowed_tools(mock_run.call_args.args[0])
            self.assertIn("Read", tools)
            self.assertIn("Edit", tools)
            self.assertIn("Bash(python3 -m unittest*)", tools)
            self.assertNotIn("Bash(git push*)", tools)
            prompt = _extract_prompt(mock_run.call_args.args[0])
            self.assertIn("Tool profiles selected by Hermes", prompt)
            self.assertIn("File Read/Edit", prompt)
            self.assertIn("Python Test Runner", prompt)
            log = engine.store._one(
                "SELECT data_json FROM logs WHERE run_id=? AND node_id=? AND message='worker tool profiles selected'",
                ("run_test", "wbs-impl"),
            )
            self.assertIsNotNone(log)
            data = json.loads(log["data_json"])
            self.assertIn("file-edit", data["profiles"])
            self.assertIn("python-tests", data["profiles"])

    def test_empty_tool_registry_falls_back_to_backend_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty_registry = ToolRegistry()
            empty_registry._profiles = {}
            engine = CollabEngine(Path(tmp) / "db.sqlite3", tmp, tool_registry=empty_registry)
            node = WBSNode(
                id="wbs-impl",
                title="Implement feature",
                description="Modify files.",
                capability="implementation",
                complexity=2,
                dependencies=[],
                parallelizable=True,
                deliverable="Working implementation",
            )
            completed = subprocess.CompletedProcess(
                args=["claude"],
                returncode=0,
                stdout=json.dumps({"result": "ok", "session_id": "s1", "is_error": False}),
                stderr="",
            )
            with patch("src.hermes_collab_engine.engine.subprocess.run", return_value=completed) as mock_run:
                engine._run_worker("run_test", node, timeout=30)

            tools = _extract_allowed_tools(mock_run.call_args.args[0])
            self.assertIn("Read", tools)
            self.assertIn("Bash(git status*)", tools)


class CLIToolTests(unittest.TestCase):
    def test_tools_command_lists_profiles(self):
        proc = subprocess.run(
            ["python3", "-m", "hermes_collab_engine.cli", "tools", "--json"],
            capture_output=True,
            text=True,
            cwd="/root/hermes-collab-engine/src",
            env={**__import__("os").environ, "PYTHONPATH": "/root/hermes-collab-engine/src"},
        )
        self.assertEqual(proc.returncode, 0)
        data = json.loads(proc.stdout)
        names = {item["name"] for item in data}
        self.assertIn("mcp-readonly", names)

    def test_tools_command_can_preview_selection(self):
        proc = subprocess.run(
            [
                "python3", "-m", "hermes_collab_engine.cli", "tools",
                "--node-type", "verification",
                "--task", "verify an MCP read-only tool integration with tests",
            ],
            capture_output=True,
            text=True,
            cwd="/root/hermes-collab-engine/src",
            env={**__import__("os").environ, "PYTHONPATH": "/root/hermes-collab-engine/src"},
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("mcp-readonly", proc.stdout)


if __name__ == "__main__":
    unittest.main()
