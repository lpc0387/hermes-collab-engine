from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from src.hermes_collab_engine.models import WBSNode
from src.hermes_collab_engine.planner import Planner


class FakeStore:
    def __init__(self, lessons=None):
        self._lessons = lessons or []

    def lessons(self, limit=20, scope=None):
        return [lesson for lesson in self._lessons if scope is None or lesson.get("scope") == scope][:limit]


def node(node_id: str, title: str, capability: str = "analysis", complexity: int = 5, deps: list[str] | None = None) -> WBSNode:
    return WBSNode(node_id, title, f"Task {title}", capability, complexity, deps or [], True, f"Deliver {title}")


class PlannerCheckpointTests(unittest.TestCase):
    def test_assign_checkpoints_marks_high_complexity_nodes(self) -> None:
        planner = Planner(cwd=Path("."))
        nodes = [node("wbs-1", "risky", complexity=7), node("wbs-2", "simple", complexity=6)]

        planner._assign_checkpoints(nodes)

        self.assertTrue(nodes[0].checkpoint)
        self.assertFalse(nodes[1].checkpoint)

    def test_assign_checkpoints_marks_implementation_parent_chains(self) -> None:
        planner = Planner(cwd=Path("."))
        nodes = [
            node("wbs-1", "parent impl", "implementation", 4),
            node("wbs-2", "child impl", "implementation", 4, ["wbs-1"]),
        ]

        planner._assign_checkpoints(nodes)

        self.assertTrue(nodes[0].checkpoint)
        self.assertFalse(nodes[1].checkpoint)

    def test_assign_checkpoints_uses_engine_scoped_lesson_titles(self) -> None:
        planner = Planner(cwd=Path("."))
        store = FakeStore([
            {"scope": "engine", "lesson": "Checkpoint at migration nodes before downstream work"},
            {"scope": "project", "lesson": "ignored migration project lesson"},
        ])
        nodes = [node("wbs-1", "Database migration"), node("wbs-2", "Read docs")]

        planner._assign_checkpoints(nodes, store)

        self.assertTrue(nodes[0].checkpoint)
        self.assertFalse(nodes[1].checkpoint)

    def test_decompose_assigns_checkpoints_to_claude_plan(self) -> None:
        planner = Planner(cwd=Path("."))
        payload = {
            "shared_brief": "shared",
            "nodes": [
                {"id": "wbs-1", "title": "High", "description": "desc", "capability": "analysis", "complexity": 8, "dependencies": [], "parallelizable": True, "deliverable": "deliver"},
                {"id": "wbs-2", "title": "Low", "description": "desc", "capability": "analysis", "complexity": 3, "dependencies": [], "parallelizable": True, "deliverable": "deliver"},
            ],
        }

        with patch.object(Planner, "_claude_json", return_value=payload):
            plan = planner.decompose("request")

        self.assertEqual(plan.shared_brief, "shared")
        self.assertTrue(plan.nodes[0].checkpoint)
        self.assertFalse(plan.nodes[1].checkpoint)

    def test_fallback_wbs_assigns_checkpoints(self) -> None:
        planner = Planner(cwd=Path("."))

        plan = planner.fallback_wbs("short request")

        self.assertTrue(all(current.checkpoint for current in plan.nodes))


if __name__ == "__main__":
    unittest.main()
