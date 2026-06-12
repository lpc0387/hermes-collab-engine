from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.hermes_collab_engine.engine import CollabEngine
from src.hermes_collab_engine.models import Plan, WBSNode


def node(node_id: str, deps: list[str] | None = None) -> WBSNode:
    return WBSNode(
        id=node_id,
        title=node_id,
        description=f"Task {node_id}",
        capability="verification",
        complexity=3,
        dependencies=deps or [],
        parallelizable=True,
        deliverable=f"Deliver {node_id}",
    )


class UpstreamGrandparentTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.engine = CollabEngine(Path(self._tmp.name) / "db.sqlite3", self._tmp.name)

    def test_chain_order_labels_parent_before_grandparent(self) -> None:
        self.engine._current_plan = Plan(nodes=[node("wbs-1"), node("wbs-2", ["wbs-1"]), node("wbs-3", ["wbs-2"])])
        self.engine._node_results["wbs-1"] = "GRANDPARENT_RESULT"
        self.engine._node_results["wbs-2"] = "PARENT_RESULT"

        context = self.engine._build_upstream_context(node("wbs-3", ["wbs-2"]))

        self.assertIn("--- from wbs-2 (parent) ---\nPARENT_RESULT", context)
        self.assertIn("--- from wbs-1 (grandparent) ---\nGRANDPARENT_RESULT", context)
        self.assertLess(context.index("wbs-2 (parent)"), context.index("wbs-1 (grandparent)"))

    def test_grandparent_context_uses_tighter_truncation_cap(self) -> None:
        self.engine._current_plan = Plan(nodes=[node("wbs-1"), node("wbs-2", ["wbs-1"]), node("wbs-3", ["wbs-2"])])
        self.engine._node_results["wbs-1"] = "G" * 500 + "GRANDPARENT_TAIL"
        self.engine._node_results["wbs-2"] = "PARENT_RESULT"

        context = self.engine._build_upstream_context(node("wbs-3", ["wbs-2"]))

        grandparent_snippet = context.split("--- from wbs-1 (grandparent) ---\n", 1)[1].split("\n\n", 1)[0]
        self.assertTrue(grandparent_snippet.startswith("[truncated]\n"))
        self.assertEqual(len(grandparent_snippet), self.engine._UPSTREAM_GRANDPARENT_CAP)
        self.assertIn("GRANDPARENT_TAIL", grandparent_snippet)

    def test_deeper_ancestor_label_and_cap(self) -> None:
        self.engine._current_plan = Plan(nodes=[
            node("wbs-1"),
            node("wbs-2", ["wbs-1"]),
            node("wbs-3", ["wbs-2"]),
            node("wbs-4", ["wbs-3"]),
        ])
        self.engine._node_results["wbs-1"] = "A" * 250 + "ANCESTOR_TAIL"
        self.engine._node_results["wbs-2"] = "GRANDPARENT_RESULT"
        self.engine._node_results["wbs-3"] = "PARENT_RESULT"

        context = self.engine._build_upstream_context(node("wbs-4", ["wbs-3"]))

        ancestor_snippet = context.split("--- from wbs-1 (ancestor depth 3) ---\n", 1)[1].split("\n\n", 1)[0]
        self.assertTrue(ancestor_snippet.startswith("[truncated]\n"))
        self.assertEqual(len(ancestor_snippet), self.engine._UPSTREAM_ANCESTOR_CAP)
        self.assertIn("ANCESTOR_TAIL", ancestor_snippet)

    def test_diamond_dependency_deduplicates_shared_grandparent(self) -> None:
        self.engine._current_plan = Plan(nodes=[
            node("wbs-1"),
            node("wbs-2", ["wbs-1"]),
            node("wbs-3", ["wbs-1"]),
            node("wbs-4", ["wbs-2", "wbs-3"]),
        ])
        self.engine._node_results["wbs-1"] = "ROOT_RESULT"
        self.engine._node_results["wbs-2"] = "LEFT_PARENT"
        self.engine._node_results["wbs-3"] = "RIGHT_PARENT"

        context = self.engine._build_upstream_context(node("wbs-4", ["wbs-2", "wbs-3"]))

        self.assertEqual(context.count("--- from wbs-1 (grandparent) ---"), 1)
        self.assertIn("--- from wbs-2 (parent) ---", context)
        self.assertIn("--- from wbs-3 (parent) ---", context)

    def test_fallback_uses_direct_dependencies_when_plan_index_empty(self) -> None:
        self.engine._current_plan = None
        self.engine._node_results["wbs-parent"] = "FALLBACK_PARENT"

        context = self.engine._build_upstream_context(node("wbs-child", ["wbs-parent"]))

        self.assertIn("--- from wbs-parent (parent) ---\nFALLBACK_PARENT", context)

    def test_empty_index_with_no_results_returns_empty_context(self) -> None:
        self.engine._current_plan = None

        context = self.engine._build_upstream_context(node("wbs-child", ["wbs-missing"]))

        self.assertEqual(context, "")


if __name__ == "__main__":
    unittest.main()
