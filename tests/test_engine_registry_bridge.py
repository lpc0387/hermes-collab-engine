"""Tests for the UnifiedRegistry -> Engine bridge via SkillDistributor.

Verifies that skills/tools from the UnifiedRegistry are resolved correctly
through the SkillDistributor for worker nodes.
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
from src.hermes_collab_engine.skill_distributor import SkillDistributor


class TestEngineRegistryBridge(unittest.TestCase):
    """Test that the engine bridges UnifiedRegistry entries."""

    def _make_engine(self):
        tmp = tempfile.mkdtemp()
        return CollabEngine(Path(tmp) / "db.sqlite3", tmp)

    def _distributor(self):
        return SkillDistributor(unified_registry=get_unified_registry())

    def test_skills_for_worker_includes_builtin_skills(self):
        engine = self._make_engine()
        skill_names, tool_names = self._distributor().resolve_for_node(
            node_capability="implementation",
            leader_skills=None,
            agent_backend=engine.agent_backend,
        )
        skills_block, _, _ = self._distributor().render_for_prompt(
            skill_names, [], [],
        )
        self.assertIn("implementation-focus", skill_names)
        self.assertIn("Relevant skills injected by Hermes:", skills_block)

    def test_skills_for_worker_includes_web_added_skill(self):
        engine = self._make_engine()
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
            skill_names, _ = self._distributor().resolve_for_node(
                node_capability="implementation",
                leader_skills=["implementation-focus", "web-custom-skill"],
                agent_backend=engine.agent_backend,
            )
            skills_block, _, _ = self._distributor().render_for_prompt(
                skill_names, [], [],
            )
            self.assertIn("web-custom-skill", skill_names)
            self.assertIn("Custom instructions from web.", skills_block)
        finally:
            unified.delete("web-custom-skill")

    def test_skills_for_worker_no_duplicates(self):
        engine = self._make_engine()
        skill_names, tool_names = self._distributor().resolve_for_node(
            node_capability="implementation",
            leader_skills=None,
            agent_backend=engine.agent_backend,
        )
        self.assertEqual(len(skill_names), len(set(skill_names)))

    def test_tools_for_worker_includes_builtin_tools(self):
        engine = self._make_engine()
        skill_names, tool_names = self._distributor().resolve_for_node(
            node_capability="implementation",
            leader_skills=None,
            agent_backend=engine.agent_backend,
        )
        self.assertIn("file-edit", tool_names)

    def test_tools_for_worker_includes_web_added_tool(self):
        engine = self._make_engine()
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
            _, tool_block, _ = self._distributor().render_for_prompt(
                [], ["web-custom-tool"], [],
            )
            self.assertIn("Web Custom Tool", tool_block)
            self.assertIn("CustomTool1", tool_block)
            self.assertIn("CustomTool2", tool_block)
        finally:
            unified.delete("web-custom-tool")

    def test_tools_for_worker_no_duplicates(self):
        skill_names, tool_names = self._distributor().resolve_for_node(
            node_capability="implementation",
            leader_skills=None,
            agent_backend=None,
        )
        self.assertEqual(len(tool_names), len(set(tool_names)))

    def test_backward_compat_empty_unified_registry(self):
        engine = self._make_engine()
        skill_names, tool_names = self._distributor().resolve_for_node(
            node_capability="implementation",
            leader_skills=None,
            agent_backend=engine.agent_backend,
        )
        self.assertTrue(len(skill_names) > 0)

    def test_tools_for_worker_includes_web_added_mcp(self):
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
            _, tool_block, _ = self._distributor().render_for_prompt(
                [], ["web-mcp-fs"], [],
            )
            self.assertIn("MCP Filesystem", tool_block)
            self.assertIn("mcp__filesystem__read_file", tool_block)
            self.assertIn("mcp__filesystem__write_file", tool_block)
        finally:
            unified.delete("web-mcp-fs")


if __name__ == "__main__":
    unittest.main()
