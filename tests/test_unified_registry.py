"""Tests for the unified registry module.

Covers:
- RegistryEntry base class
- SkillEntry / ToolEntry / MCPEntry specialized entries
- UnifiedRegistry registration, capability indexing, selection
- Legacy import bridge
- MCP discovery from config files
- Rendering for worker prompts
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from src.hermes_collab_engine.registry import (
    MCPEntry,
    SkillEntry,
    ToolEntry,
    UnifiedRegistry,
    discover_mcp_entries,
    get_unified_registry,
)


class TestRegistryEntry(unittest.TestCase):
    """Test the base RegistryEntry dataclass."""

    def test_skill_entry_to_dict(self):
        entry = SkillEntry(
            name="test-skill",
            display_name="Test Skill",
            category="coding",
            description="A test skill",
            capabilities=["implementation", "coding"],
            source="hermes",
            priority=1,
            content="Do the thing.",
        )
        d = entry.to_dict()
        self.assertEqual(d["name"], "test-skill")
        self.assertEqual(d["capabilities"], ["implementation", "coding"])
        self.assertEqual(d["content"], "Do the thing.")

    def test_skill_entry_file_path_in_to_dict(self):
        entry = SkillEntry(
            name="fp-skill",
            display_name="FP Skill",
            category="coding",
            description="Has file path",
            capabilities=["*"],
            source="web-ui",
            content="content",
            file_path="/tmp/my-skill.md",
        )
        d = entry.to_dict()
        self.assertIn("file_path", d)
        self.assertEqual(d["file_path"], "/tmp/my-skill.md")

    def test_skill_entry_file_path_defaults_empty(self):
        entry = SkillEntry(
            name="no-fp",
            display_name="No FP",
            category="coding",
            description="No file path",
            capabilities=["*"],
            source="hermes",
            content="content",
        )
        self.assertEqual(entry.file_path, "")
        d = entry.to_dict()
        self.assertEqual(d["file_path"], "")

    def test_tool_entry_to_dict(self):
        entry = ToolEntry(
            name="test-tool",
            display_name="Test Tool",
            category="filesystem",
            description="A test tool",
            capabilities=["implementation"],
            source="hermes",
            allowed_tools=["Read", "Edit"],
        )
        d = entry.to_dict()
        self.assertEqual(d["allowed_tools"], ["Read", "Edit"])

    def test_mcp_entry_qualified_name(self):
        entry = MCPEntry(
            name="mcp__fs__read",
            display_name="MCP Read",
            category="mcp",
            description="Read a file via MCP",
            capabilities=["*"],
            source="mcp",
            server_name="fs",
            tool_name="read",
        )
        self.assertEqual(entry.qualified_name, "mcp__fs__read")

    def test_mcp_entry_to_dict_includes_qualified_name(self):
        entry = MCPEntry(
            name="mcp__db__query",
            display_name="MCP DB Query",
            category="mcp",
            description="Query via MCP",
            capabilities=["analysis"],
            source="mcp",
            server_name="db",
            tool_name="query",
        )
        d = entry.to_dict()
        self.assertIn("qualified_name", d)
        self.assertEqual(d["qualified_name"], "mcp__db__query")

    def test_mcp_entry_config_path_in_to_dict(self):
        entry = MCPEntry(
            name="mcp__cfg__test",
            display_name="MCP Config Test",
            category="mcp",
            description="Has config path",
            capabilities=["*"],
            source="mcp",
            server_name="cfg",
            tool_name="test",
            config_path="/home/user/.mcp.json",
        )
        d = entry.to_dict()
        self.assertIn("config_path", d)
        self.assertEqual(d["config_path"], "/home/user/.mcp.json")

    def test_mcp_entry_config_path_defaults_empty(self):
        entry = MCPEntry(
            name="mcp__no__cfg",
            display_name="No Config",
            category="mcp",
            description="No config path",
            capabilities=["*"],
            source="mcp",
            server_name="no",
            tool_name="cfg",
        )
        self.assertEqual(entry.config_path, "")


class TestUnifiedRegistry(unittest.TestCase):
    """Test the UnifiedRegistry."""

    def setUp(self):
        self.reg = UnifiedRegistry()
        self.skill = SkillEntry(
            name="impl-focus",
            display_name="Implementation Focus",
            category="coding",
            description="Keep implementation minimal",
            capabilities=["implementation", "coding"],
            source="hermes",
            priority=1,
            content="Make the smallest useful change.",
        )
        self.tool = ToolEntry(
            name="file-edit",
            display_name="File Read/Edit",
            category="filesystem",
            description="Read and edit files",
            capabilities=["implementation", "coding", "debugging"],
            source="hermes",
            priority=1,
            allowed_tools=["Read", "Edit", "Write"],
        )
        self.mcp = MCPEntry(
            name="mcp__filesystem__read_file",
            display_name="MCP Filesystem Read",
            category="mcp",
            description="Read file via MCP",
            capabilities=["analysis", "research"],
            source="mcp",
            priority=2,
            server_name="filesystem",
            tool_name="read_file",
            allowed_tools=["mcp__filesystem__read_file"],
        )
        self.reg.register(self.skill)
        self.reg.register(self.tool)
        self.reg.register(self.mcp)

    def test_register_and_get(self):
        self.assertIs(self.reg.get("impl-focus"), self.skill)
        self.assertIs(self.reg.get("file-edit"), self.tool)
        self.assertIsNone(self.reg.get("nonexistent"))

    def test_list_all_sorted(self):
        entries = self.reg.list_all()
        names = [e.name for e in entries]
        self.assertEqual(len(entries), 3)
        # sorted by (priority, name)
        self.assertEqual(names, ["file-edit", "impl-focus", "mcp__filesystem__read_file"])

    def test_list_by_type(self):
        skills = self.reg.list_by_type(SkillEntry)
        self.assertEqual(len(skills), 1)
        self.assertEqual(skills[0].name, "impl-focus")

        tools = self.reg.list_by_type(ToolEntry)
        self.assertEqual(len(tools), 1)

        mcp = self.reg.list_by_type(MCPEntry)
        self.assertEqual(len(mcp), 1)

    def test_select_for_capability_exact_match(self):
        results = self.reg.select_for_capability("implementation")
        names = {e.name for e in results}
        self.assertIn("impl-focus", names)
        self.assertIn("file-edit", names)
        self.assertNotIn("mcp__filesystem__read_file", names)

    def test_select_for_capability_wildcard(self):
        # Add a wildcard entry
        wc = SkillEntry(
            name="always-on",
            display_name="Always On",
            category="general",
            description="Applies everywhere",
            capabilities=["*"],
            source="hermes",
            priority=1,
            content="Always.",
        )
        self.reg.register(wc)
        results = self.reg.select_for_capability("implementation")
        names = {e.name for e in results}
        self.assertIn("always-on", names)

    def test_select_for_capability_with_type_filter(self):
        results = self.reg.select_for_capability("implementation", entry_type=SkillEntry)
        names = [e.name for e in results]
        self.assertEqual(names, ["impl-focus"])

    def test_select_for_capability_empty(self):
        results = self.reg.select_for_capability("nonexistent")
        self.assertEqual(results, [])

    def test_select_for_capability_none(self):
        results = self.reg.select_for_capability(None)
        self.assertEqual(results, [])

    def test_select_skills_with_task_text(self):
        skills = self.reg.select_skills("implementation", "implement the feature")
        self.assertEqual(len(skills), 1)
        self.assertEqual(skills[0].name, "impl-focus")

    def test_select_tools(self):
        tools = self.reg.select_tools("implementation")
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0].name, "file-edit")

    def test_select_mcp(self):
        mcp = self.reg.select_mcp("analysis")
        self.assertEqual(len(mcp), 1)
        self.assertEqual(mcp[0].name, "mcp__filesystem__read_file")

    def test_allowed_tools_for_capability(self):
        tools = self.reg.allowed_tools_for_capability("implementation")
        self.assertIn("Read", tools)
        self.assertIn("Edit", tools)
        self.assertIn("Write", tools)

    def test_render_skills_for_prompt(self):
        skills = [self.skill]
        rendered = self.reg.render_skills_for_prompt(skills)
        self.assertIn("Relevant skills injected by Hermes:", rendered)
        self.assertIn("Implementation Focus", rendered)
        self.assertIn("Make the smallest useful change.", rendered)

    def test_render_skills_for_prompt_includes_file_path(self):
        skill = SkillEntry(
            name="fp-test",
            display_name="FP Test",
            category="coding",
            description="Test",
            capabilities=["*"],
            source="web-ui",
            content="do stuff",
            file_path="/tmp/skill.md",
        )
        rendered = self.reg.render_skills_for_prompt([skill])
        self.assertIn("File: /tmp/skill.md", rendered)

    def test_render_skills_for_prompt_omits_empty_file_path(self):
        rendered = self.reg.render_skills_for_prompt([self.skill])
        self.assertNotIn("File:", rendered)

    def test_render_skills_empty(self):
        self.assertEqual(self.reg.render_skills_for_prompt([]), "")

    def test_render_tools_for_prompt(self):
        tools = [self.tool]
        rendered = self.reg.render_tools_for_prompt(tools)
        self.assertIn("Tool profiles selected by Hermes:", rendered)
        self.assertIn("File Read/Edit", rendered)
        self.assertIn("Read, Edit, Write", rendered)

    def test_render_tools_for_prompt_includes_config_path(self):
        mcp = MCPEntry(
            name="mcp__render__test",
            display_name="MCP Render Test",
            category="mcp",
            description="Test MCP",
            capabilities=["*"],
            source="mcp",
            server_name="render",
            tool_name="test",
            allowed_tools=["mcp__render__test"],
            config_path="/home/user/.mcp.json",
        )
        rendered = self.reg.render_tools_for_prompt([mcp])
        self.assertIn("Config: /home/user/.mcp.json", rendered)

    def test_render_tools_for_prompt_omits_empty_config_path(self):
        rendered = self.reg.render_tools_for_prompt([self.tool])
        self.assertNotIn("Config:", rendered)

    def test_render_tools_empty(self):
        self.assertEqual(self.reg.render_tools_for_prompt([]), "")

    def test_register_empty_name_raises(self):
        bad = SkillEntry(
            name="", display_name="Bad", category="x", description="x",
            capabilities=["*"], source="hermes", content="x",
        )
        with self.assertRaises(ValueError):
            self.reg.register(bad)

    def test_max_entries_limit(self):
        for i in range(20):
            self.reg.register(SkillEntry(
                name=f"skill-{i}", display_name=f"Skill {i}",
                category="coding", description=f"Skill {i}",
                capabilities=["implementation"], source="hermes",
                priority=1, content=f"Content {i}",
            ))
        results = self.reg.select_for_capability("implementation", max_entries=5)
        self.assertEqual(len(results), 5)

    def test_delete_removes_entry(self):
        self.reg.register(SkillEntry(
            name="temp-skill", display_name="Temp", category="x",
            description="Temporary", capabilities=["impl"], source="test",
            priority=1, content="temp",
        ))
        self.assertIsNotNone(self.reg.get("temp-skill"))
        result = self.reg.delete("temp-skill")
        self.assertTrue(result)
        self.assertIsNone(self.reg.get("temp-skill"))

    def test_delete_nonexistent_returns_false(self):
        result = self.reg.delete("no-such-entry")
        self.assertFalse(result)

    def test_register_overwrites_existing(self):
        self.reg.register(SkillEntry(
            name="overwrite-me", display_name="V1", category="x",
            description="Version 1", capabilities=["impl"], source="test",
            priority=1, content="v1",
        ))
        self.reg.register(SkillEntry(
            name="overwrite-me", display_name="V2", category="x",
            description="Version 2", capabilities=["impl"], source="test",
            priority=1, content="v2",
        ))
        entry = self.reg.get("overwrite-me")
        self.assertEqual(entry.display_name, "V2")
        self.assertEqual(entry.content, "v2")


class TestLegacyImport(unittest.TestCase):
    """Test from_legacy bridge."""

    def test_import_from_legacy_registries(self):
        from src.hermes_collab_engine.skills import get_default_registry
        from src.hermes_collab_engine.tools import get_default_tool_registry

        reg = UnifiedRegistry.from_legacy(
            skill_registry=get_default_registry(),
            tool_registry=get_default_tool_registry(),
        )
        all_entries = reg.list_all()
        # Should have all 5 builtin skills + 5 builtin tool profiles
        self.assertEqual(len(all_entries), 10)

        skills = reg.list_by_type(SkillEntry)
        self.assertEqual(len(skills), 5)

        tools = reg.list_by_type(ToolEntry)
        self.assertEqual(len(tools), 5)

    def test_legacy_skills_have_capabilities(self):
        from src.hermes_collab_engine.skills import get_default_registry

        reg = UnifiedRegistry.from_legacy(skill_registry=get_default_registry())
        impl_skills = reg.select_for_capability("implementation", entry_type=SkillEntry)
        self.assertGreater(len(impl_skills), 0)

    def test_legacy_tools_have_capabilities(self):
        from src.hermes_collab_engine.tools import get_default_tool_registry

        reg = UnifiedRegistry.from_legacy(tool_registry=get_default_tool_registry())
        impl_tools = reg.select_for_capability("implementation", entry_type=ToolEntry)
        self.assertGreater(len(impl_tools), 0)


class TestMCPDiscovery(unittest.TestCase):
    """Test MCP discovery from config files."""

    def test_discover_from_file(self):
        config = {
            "mcpServers": {
                "filesystem": {
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
                    "tools": ["read_file", "list_directory"],
                }
            }
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(config, f)
            f.flush()
            path = f.name
        try:
            entries = discover_mcp_entries(path)
            self.assertEqual(len(entries), 2)
            names = {e.name for e in entries}
            self.assertIn("mcp__filesystem__read_file", names)
            self.assertIn("mcp__filesystem__list_directory", names)
            for e in entries:
                self.assertEqual(e.source, "mcp")
                self.assertEqual(e.server_name, "filesystem")
                self.assertIn("*", e.capabilities)
                self.assertEqual(e.config_path, path)
        finally:
            os.unlink(path)

    def test_discover_from_env_var(self):
        config = {
            "mcpServers": {
                "db": {
                    "command": "mcp-server-db",
                    "tools": ["query"],
                }
            }
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(config, f)
            f.flush()
            path = f.name
        try:
            entries = discover_mcp_entries(path)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].name, "mcp__db__query")
        finally:
            os.unlink(path)

    def test_discover_missing_file(self):
        entries = discover_mcp_entries("/nonexistent/path.json")
        self.assertEqual(entries, [])

    def test_discover_empty_config(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({}, f)
            f.flush()
            path = f.name
        try:
            entries = discover_mcp_entries(path)
            self.assertEqual(entries, [])
        finally:
            os.unlink(path)

    def test_discover_no_path_returns_empty(self):
        with patch.dict(os.environ, {}, clear=True):
            entries = discover_mcp_entries(None)
            self.assertEqual(entries, [])


class TestGetUnifiedRegistry(unittest.TestCase):
    """Test the default singleton."""

    def test_get_unified_registry_returns_instance(self):
        reg = get_unified_registry()
        self.assertIsInstance(reg, UnifiedRegistry)
        # Should have at least the builtin skills and tools
        self.assertGreater(len(reg.list_all()), 0)

    def test_get_unified_registry_is_cached(self):
        reg1 = get_unified_registry()
        reg2 = get_unified_registry()
        self.assertIs(reg1, reg2)


# Patch helper for env tests
from unittest.mock import patch


if __name__ == "__main__":
    unittest.main()
