from __future__ import annotations

import unittest
from pathlib import Path

from src.hermes_collab_engine.models import WBSNode
from src.hermes_collab_engine.planner import Planner


def planner_node(node_id: str, title: str, capability: str, dependencies: list[str]) -> WBSNode:
    return WBSNode(node_id, title, title, capability, 5, dependencies, True, title)


class PlannerFallbackTests(unittest.TestCase):
    def test_fallback_wbs_short_request_has_three_serial_nodes(self) -> None:
        planner = Planner(cwd=Path("."))
        request = "给 README 增加一段安装说明"
        plan = planner.fallback_wbs(request)
        nodes = plan.nodes

        self.assertTrue(plan.shared_brief)
        self.assertEqual([n.id for n in nodes], ["wbs-1", "wbs-2", "wbs-verify"])
        self.assertEqual([n.dependencies for n in nodes], [[], ["wbs-1"], ["wbs-2"]])
        self.assertEqual({n.capability for n in nodes}, {"analysis", "implementation", "verification"})
        for node in nodes:
            self.assertIn(request, node.description)
            self.assertTrue(node.brief)
            self.assertTrue(node.estimated_duration)

    def test_fallback_wbs_long_request_has_four_serial_nodes_with_planning(self) -> None:
        planner = Planner(cwd=Path("."))
        request = "需要重新梳理协同引擎的复杂度评估、WBS 拆解、并行调度、SQLite 持久化、" * 6
        plan = planner.fallback_wbs(request)
        nodes = plan.nodes

        self.assertTrue(plan.shared_brief)
        self.assertEqual([n.id for n in nodes], ["wbs-1", "wbs-2", "wbs-3", "wbs-verify"])
        self.assertEqual([n.dependencies for n in nodes], [[], ["wbs-1"], ["wbs-2"], ["wbs-3"]])
        self.assertEqual([n.capability for n in nodes], ["analysis", "planning", "implementation", "verification"])
        head = request[:200]
        for node in nodes:
            self.assertIn(head, node.description)
            self.assertTrue(node.brief)
            self.assertTrue(node.estimated_duration)

    def test_fallback_wbs_truncates_oversized_request(self) -> None:
        planner = Planner(cwd=Path("."))
        request = "x" * 5000
        plan = planner.fallback_wbs(request)

        for node in plan.nodes:
            self.assertIn("x" * 1500, node.description)
            self.assertIn("…", node.description)
            self.assertNotIn(request, node.description)

    def test_decompose_single_routing_uses_local_template_without_llm(self) -> None:
        planner = Planner(cwd=Path("."))
        planner._claude_json = lambda _prompt: self.fail("single routing should not call leader model")

        plan = planner.decompose("实现一个小的 README 更新")

        self.assertEqual([n.id for n in plan.nodes], ["wbs-1", "wbs-2", "wbs-verify"])
        self.assertEqual([n.capability for n in plan.nodes], ["analysis", "implementation", "verification"])

    def test_deduplicate_nodes_merges_duplicate_fingerprints_and_rewrites_dependencies(self) -> None:
        planner = Planner(cwd=Path("."))
        nodes = [
            planner_node("wbs-1", "Analyze", "analysis", []),
            planner_node("wbs-2", "Implement dedup", "implementation", ["wbs-1"]),
            planner_node("wbs-3", "Implement dedup", "implementation", ["wbs-1"]),
            planner_node("wbs-4", "Verify", "verification", ["wbs-3"]),
        ]

        unique = planner._deduplicate_nodes(nodes)

        self.assertEqual([n.id for n in unique], ["wbs-1", "wbs-2", "wbs-4"])
        self.assertEqual(unique[-1].dependencies, ["wbs-2"])
        self.assertTrue(unique[1].fingerprint)


if __name__ == "__main__":
    unittest.main()
