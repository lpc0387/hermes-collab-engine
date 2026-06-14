"""Tests for the UnifiedRegistry → Engine bridge.

Verifies that web-added skills/tools in the UnifiedRegistry are picked up
by the engine's _skills_for_worker and _tools_for_worker methods, while
preserving backward compatibility with legacy registries.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.hermes_collab_engine.engine import CollabEngine
from src.hermes_collab_engine.models import WBSNode
from src.hermes_collab_engine.registry import (
    MCPEntry as UMCPEntry,
    SkillEntry as USkillEntry,
    ToolEntry as UToolEntry,
    get_unified_registry,
)


class TestEngineRegistryBridge(unittest.TestCase):
    """Test that the engine bridges UnifiedRegistry entries."""

    def _make_engine(self):
        tmp = tempfile.mkdtemp()
        return CollabEngine(Path(tmp) / "db.sqlite3", tmp)

    def _make_node(self, capability="implementation"):
        return WBSNode(
            id="wbs-test",
            title="Test task",
            description="Do something testable.",
            capability=capability,
            complexity=5,
            dependencies=[],
            parallelizable=True,
            deliverable="Result",
        )

    def test_skills_for_worker_includes_builtin_skills(self):
        engine = self._make_engine()
        node = self._make_node("implementation")
        names, prompt = engine._skills_for_worker(node)
        self.assertIn("implementation-focus", names)
        self.assertIn("Relevant skills injected by Hermes:", prompt)

    def test_skills_for_worker_includes_web_added_skill(self):
        engine = self._make_engine()
        node = self._make_node("implementation")
        # Register a web-added skill in the unified registry
        unified = get_unified_registry()
        web_skill = USkillEntry(
            name="web-custom-skill",
            display_name="Web Custom Skill",
            category="custom",
            description="A skill added via web UI",
            capabilities=["implementation"],
            source="web-ui",
            priority=2,
            content="Custom instructions from web.",
        )
        unified.register(web_skill)
        try:
            names, prompt = engine._skills_for_worker(node)
            self.assertIn("web-custom-skill", names)
            self.assertIn("Custom instructions from web.", prompt)
        finally:
            unified.delete("web-custom-skill")

    def test_skills_for_worker_excludes_hermes_source_from_bridge(self):
        """Built-in skills with source='hermes' should come from legacy registry only."""
        engine = self._make_engine()
        node = self._make_node("implementation")
        # The built-in "implementation-focus" has source="hermes" in the unified registry
        # It should still appear (from legacy), but not be duplicated
        names, _ = engine._skills_for_worker(node)
        self.assertEqual(names.count("implementation-focus"), 1)

    def test_tools_for_worker_includes_builtin_tools(self):
        engine = self._make_engine()
        node = self._make_node("implementation")
        names, allowed, prompt = engine._tools_for_worker(node)
        self.assertIn("file-edit", names)
        self.assertIn("Read", allowed)

    def test_tools_for_worker_includes_web_added_tool(self):
        engine = self._make_engine()
        node = self._make_node("implementation")
        unified = get_unified_registry()
        web_tool = UToolEntry(
            name="web-custom-tool",
            display_name="Web Custom Tool",
            category="custom",
            description="A tool added via web UI",
            capabilities=["implementation"],
            source="web-ui",
            priority=2,
            allowed_tools=["CustomTool1", "CustomTool2"],
        )
        unified.register(web_tool)
        try:
            names, allowed, prompt = engine._tools_for_worker(node)
            self.assertIn("web-custom-tool", names)
            self.assertIn("CustomTool1", allowed)
            self.assertIn("CustomTool2", allowed)
        finally:
            unified.delete("web-custom-tool")

    def test_tools_for_worker_no_duplicates(self):
        """Legacy and unified entries with the same name should not duplicate."""
        engine = self._make_engine()
        node = self._make_node("implementation")
        names, _, _ = engine._tools_for_worker(node)
        # file-edit comes from both legacy and unified, should appear once
        self.assertEqual(names.count("file-edit"), 1)

    def test_backward_compat_empty_unified_registry(self):
        """Engine should still work when unified registry has no web entries."""
        engine = self._make_engine()
        node = self._make_node("implementation")
        names, prompt = engine._skills_for_worker(node)
        self.assertTrue(len(names) > 0)
        self.assertTrue(len(prompt) > 0)

    def test_tools_for_worker_includes_web_added_mcp(self):
        """MCP entries from UnifiedRegistry should flow into allowed tools."""
        engine = self._make_engine()
        node = self._make_node("implementation")
        unified = get_unified_registry()
        mcp = UMCPEntry(
            name="web-mcp-fs",
            display_name="MCP Filesystem",
            category="mcp",
            description="Filesystem MCP server",
            capabilities=["implementation"],
            source="web-ui",
            priority=2,
            server_name="filesystem",
            tool_name="read_file",
            allowed_tools=["mcp__filesystem__read_file", "mcp__filesystem__write_file"],
        )
        unified.register(mcp)
        try:
            names, allowed, prompt = engine._tools_for_worker(node)
            self.assertIn("web-mcp-fs", names)
            self.assertIn("mcp__filesystem__read_file", allowed)
            self.assertIn("mcp__filesystem__write_file", allowed)
        finally:
            unified.delete("web-mcp-fs")


if __name__ == "__main__":
    unittest.main()
