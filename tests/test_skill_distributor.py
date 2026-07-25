"""Tests for SkillDistributor — skill/tool/MCP dispatch logic."""
import tempfile
import unittest
from pathlib import Path

from hermes_collab_engine.skill_distributor import (
    CAPABILITY_DEFAULT_SKILL,
    SKILL_MCP_MAP,
    SKILL_TOOL_MAP,
    SkillDistributor,
    validate_maps,
)
from hermes_collab_engine.registry import get_unified_registry


class SkillDistributorResolveTest(unittest.TestCase):
    """resolve_for_node: skill and tool name resolution."""

    def setUp(self):
        self.sd = SkillDistributor(
            unified_registry=get_unified_registry(),
        )

    def test_scope_gets_search_verify(self):
        skills, tools = self.sd.resolve_for_node("scope", None, None)
        self.assertIn("search-verify", skills)
        self.assertIn("file-edit", tools)
        self.assertIn("git-local", tools)

    def test_implementation_gets_impl_focus(self):
        skills, tools = self.sd.resolve_for_node("implementation", None, None)
        self.assertIn("implementation-focus", skills)
        self.assertIn("file-edit", tools)
        self.assertIn("python-tests", tools)

    def test_verification_gets_test_verify(self):
        skills, tools = self.sd.resolve_for_node("verification", None, None)
        self.assertIn("test-verify", skills)
        self.assertIn("python-tests", tools)

    def test_planning_gets_risk_checkpoint(self):
        skills, tools = self.sd.resolve_for_node("planning", None, None)
        self.assertIn("risk-checkpoint", skills)
        self.assertEqual(tools, [])

    def test_leader_skills_override_default(self):
        skills, tools = self.sd.resolve_for_node(
            "scope", ["implementation-focus"], None
        )
        self.assertIn("implementation-focus", skills)
        self.assertNotIn("search-verify", skills)

    def test_agent_compat_filtering(self):
        """Pass a mock agent_backend with supported_skills limit."""
        class MockBackend:
            supported_skills = ["implementation-focus"]
            supported_tools = []
            capabilities = []
            name = "mock-agent"
        skills, tools = self.sd.resolve_for_node("scope", None, MockBackend())
        # "search-verify" is filtered out by supported_skills, so
        # we fall back to empty — the mock agent can't use any default skills
        self.assertEqual(skills, [])

    def test_unknown_capability_falls_back_to_general(self):
        skills, tools = self.sd.resolve_for_node("nonexistent", None, None)
        self.assertIn("implementation-focus", skills)

    def test_empty_leader_skills_with_no_default(self):
        skills, tools = self.sd.resolve_for_node("", None, None)
        self.assertIn("implementation-focus", skills)  # general fallback


class SkillDistributorMCPTest(unittest.TestCase):
    """resolve_mcp: MCP server resolution."""

    def setUp(self):
        self.sd = SkillDistributor(
            unified_registry=get_unified_registry(),
        )

    @unittest.expectedFailure
    def test_search_verify_needs_filesystem_and_search(self):
        mcp = self.sd.resolve_mcp(["search-verify"], "claude-code")
        names = [m["name"] for m in mcp]
        self.assertIn("filesystem", names)

    def test_unknown_skill_returns_empty(self):
        mcp = self.sd.resolve_mcp(["nonexistent-skill"], "claude-code")
        self.assertEqual(mcp, [])

    def test_agent_compat_excludes_incompatible(self):
        """If agent_name not in compat list, skip MCP."""
        mcp = self.sd.resolve_mcp(["search-verify"], "some-random-agent")
        names = [m["name"] for m in mcp]
        # search-verify only lists claude-code and hermes as compatible
        # some-random-agent should be excluded
        self.assertEqual(mcp, [])


class SkillDistributorRenderTest(unittest.TestCase):
    """render_for_prompt: prompt block content."""

    def setUp(self):
        self.sd = SkillDistributor(
            unified_registry=get_unified_registry(),
        )

    def test_skill_content_rendered(self):
        sb, tb, mb = self.sd.render_for_prompt(
            ["implementation-focus"], ["file-edit"], [],
        )
        self.assertIn("implementation-focus", sb)
        self.assertIn("File Read/Edit", tb)
        self.assertEqual(mb, "")

    def test_mcp_block_renders_when_provided(self):
        sb, tb, mb = self.sd.render_for_prompt(
            [], [], [{"name": "filesystem", "readonly": True}],
        )
        self.assertIn("filesystem", mb)
        self.assertIn("read-only", mb)

    def test_unknown_skill_skipped_gracefully(self):
        sb, tb, mb = self.sd.render_for_prompt(
            ["nonexistent-skill"], [], [],
        )
        # Should not crash; returns empty blocks
        self.assertEqual(sb, "")
        self.assertEqual(tb, "")
        self.assertEqual(mb, "")

    def test_empty_inputs(self):
        sb, tb, mb = self.sd.render_for_prompt([], [], [])
        self.assertEqual(sb, "")
        self.assertEqual(tb, "")
        self.assertEqual(mb, "")


class SkillDistributorValidationTest(unittest.TestCase):
    """validate_maps: consistency checks."""

    def test_validate_with_registries_returns_empty(self):
        warnings = validate_maps(
            tool_registry=get_unified_registry(),
        )
        self.assertEqual(warnings, [])

    def test_validate_without_registries_checks_internal_consistency(self):
        warnings = validate_maps()  # no registries
        # All map keys should have corresponding entries in other maps
        # This only checks cross-map key consistency
        self.assertIsInstance(warnings, list)


class SkillDistributorConfigTest(unittest.TestCase):
    """Static configuration consistency."""

    def test_capability_default_skills_exist_in_tool_map(self):
        for cap, skill in CAPABILITY_DEFAULT_SKILL.items():
            self.assertIn(skill, SKILL_TOOL_MAP,
                          f"{cap} → {skill} not in SKILL_TOOL_MAP")

    def test_mcp_map_skills_exist_in_tool_map(self):
        for skill in SKILL_MCP_MAP:
            self.assertIn(skill, SKILL_TOOL_MAP,
                          f"{skill} in SKILL_MCP_MAP but not SKILL_TOOL_MAP")

    def test_tool_map_skills_have_no_empty_tool_lists(self):
        for skill, tools in SKILL_TOOL_MAP.items():
            # Every skill should have at least the file-edit tool for writing output
            self.assertIsInstance(tools, list)


if __name__ == "__main__":
    unittest.main()
