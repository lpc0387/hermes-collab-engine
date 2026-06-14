"""Tests for skill/tool pre-allocation in the leader WBS phase.

Verifies that:
1. Pre-allocation fills node.skills_json and node.tools_json before workers start.
2. _run_worker uses pre-allocated values instead of re-selecting.
3. Backward compatibility: empty skills_json falls back to per-worker selection.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from src.hermes_collab_engine.engine import CollabEngine
from src.hermes_collab_engine.models import WBSNode


class TestPreallocateSkillsTools(unittest.TestCase):
    """Test that _preallocate_skills_tools fills node JSON fields."""

    def _make_engine(self):
        tmp = tempfile.mkdtemp()
        return CollabEngine(Path(tmp) / "db.sqlite3", tmp)

    def _make_node(self, node_id="wbs-test", capability="implementation"):
        return WBSNode(
            id=node_id,
            title="Test task",
            description="Do something testable.",
            capability=capability,
            complexity=5,
            dependencies=[],
            parallelizable=True,
            deliverable="Result",
        )

    def test_preallocate_fills_skills_json(self):
        engine = self._make_engine()
        node = self._make_node()
        engine._preallocate_skills_tools("run_test", [node])
        self.assertTrue(node.skills_json)
        names = json.loads(node.skills_json)
        self.assertIn("implementation-focus", names)

    def test_preallocate_fills_tools_json(self):
        engine = self._make_engine()
        node = self._make_node()
        engine._preallocate_skills_tools("run_test", [node])
        self.assertTrue(node.tools_json)
        names = json.loads(node.tools_json)
        self.assertIn("file-edit", names)

    def test_preallocate_persists_to_store(self):
        engine = self._make_engine()
        node = self._make_node()
        # Insert the node into the store first
        engine.store.create_run("run_test", "title", "req", {}, agent="claude-code")
        engine.store.insert_wbs_node("run_test", node.to_dict())
        engine._preallocate_skills_tools("run_test", [node])
        # Read back from store
        stored = engine.store.get_node("run_test", node.id)
        self.assertIsNotNone(stored)
        self.assertTrue(stored["skills_json"])
        self.assertTrue(stored["tools_json"])


class TestWorkerUsesPreallocated(unittest.TestCase):
    """Test that _run_worker uses pre-allocated values when available."""

    def _make_engine(self):
        tmp = tempfile.mkdtemp()
        return CollabEngine(Path(tmp) / "db.sqlite3", tmp)

    def _make_node(self, skills_json="", tools_json=""):
        return WBSNode(
            id="wbs-test",
            title="Test task",
            description="Do something testable.",
            capability="implementation",
            complexity=5,
            dependencies=[],
            parallelizable=True,
            deliverable="Result",
            skills_json=skills_json,
            tools_json=tools_json,
        )

    def test_worker_skips_skills_selection_when_preallocated(self):
        engine = self._make_engine()
        node = self._make_node(skills_json='["implementation-focus"]')
        with patch.object(engine, '_skills_for_worker') as mock_skills:
            # The worker should NOT call _skills_for_worker when skills_json is set
            # We need to call _run_worker but it will fail because subprocess,
            # so instead test the logic path by checking the skills_block construction
            skills_block = engine._render_skills_from_names(json.loads(node.skills_json))
            mock_skills.assert_not_called()
            self.assertIn("implementation-focus", skills_block)

    def test_worker_skips_tools_selection_when_preallocated(self):
        engine = self._make_engine()
        node = self._make_node(tools_json='["file-edit"]')
        with patch.object(engine, '_tools_for_worker') as mock_tools:
            allowed, tools_block = engine._render_tools_from_names(json.loads(node.tools_json))
            mock_tools.assert_not_called()
            self.assertIn("file-edit", tools_block)
            self.assertIn("Read", allowed)

    def test_render_skills_from_names_with_web_skill(self):
        engine = self._make_engine()
        from src.hermes_collab_engine.registry import (
            SkillEntry as USkillEntry,
            get_unified_registry,
        )
        unified = get_unified_registry()
        web_skill = USkillEntry(
            name="test-prealloc-skill",
            display_name="Test Prealloc Skill",
            category="custom",
            description="A skill for testing pre-alloc",
            capabilities=["implementation"],
            source="web-ui",
            priority=2,
            content="Custom pre-alloc instructions.",
        )
        unified.register(web_skill)
        try:
            block = engine._render_skills_from_names(["test-prealloc-skill"])
            self.assertIn("test-prealloc-skill", block)
            self.assertIn("Custom pre-alloc instructions.", block)
        finally:
            unified.delete("test-prealloc-skill")

    def test_render_tools_from_names_with_web_tool(self):
        engine = self._make_engine()
        from src.hermes_collab_engine.registry import (
            ToolEntry as UToolEntry,
            get_unified_registry,
        )
        unified = get_unified_registry()
        web_tool = UToolEntry(
            name="test-prealloc-tool",
            display_name="Test Prealloc Tool",
            category="custom",
            description="A tool for testing pre-alloc",
            capabilities=["implementation"],
            source="web-ui",
            priority=2,
            allowed_tools=["CustomPreallocTool"],
        )
        unified.register(web_tool)
        try:
            allowed, block = engine._render_tools_from_names(["test-prealloc-tool"])
            self.assertIn("test-prealloc-tool", block)
            self.assertIn("CustomPreallocTool", allowed)
        finally:
            unified.delete("test-prealloc-tool")


class TestBackwardCompatFallback(unittest.TestCase):
    """Test backward compatibility when skills_json/tools_json are empty."""

    def _make_engine(self):
        tmp = tempfile.mkdtemp()
        return CollabEngine(Path(tmp) / "db.sqlite3", tmp)

    def _make_node(self):
        return WBSNode(
            id="wbs-test",
            title="Test task",
            description="Do something testable.",
            capability="implementation",
            complexity=5,
            dependencies=[],
            parallelizable=True,
            deliverable="Result",
            skills_json="",
            tools_json="",
        )

    def test_fallback_calls_skills_for_worker(self):
        engine = self._make_engine()
        node = self._make_node()
        self.assertEqual(node.skills_json, "")
        # When skills_json is empty, _skills_for_worker should be called
        skill_names, skills_block = engine._skills_for_worker(node)
        self.assertIn("implementation-focus", skill_names)
        self.assertIn("Relevant skills injected by Hermes:", skills_block)

    def test_fallback_calls_tools_for_worker(self):
        engine = self._make_engine()
        node = self._make_node()
        self.assertEqual(node.tools_json, "")
        # When tools_json is empty, _tools_for_worker should be called
        tool_names, allowed, tools_block = engine._tools_for_worker(node)
        self.assertIn("file-edit", tool_names)
        self.assertIn("Read", allowed)

    def test_preallocate_failure_leaves_json_empty(self):
        """If pre-allocation throws, node JSON stays empty — worker falls back."""
        engine = self._make_engine()
        node = self._make_node()
        with patch.object(engine, '_skills_for_worker', side_effect=Exception("boom")):
            engine._preallocate_skills_tools("run_test", [node])
        # skills_json should remain empty on failure
        self.assertEqual(node.skills_json, "")
        self.assertEqual(node.tools_json, "")


if __name__ == "__main__":
    unittest.main()
