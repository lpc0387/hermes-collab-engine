from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from src.hermes_collab_engine.models import Plan
from src.hermes_collab_engine.planner import Planner


class FakeStore:
    def __init__(self, lessons):
        self._lessons = lessons

    def lessons(self, limit: int = 100, scope: str | None = None):
        return self._lessons[:limit]


class PlannerBriefOutputTests(unittest.TestCase):
    def test_decompose_accepts_object_with_shared_and_node_briefs(self) -> None:
        planner = Planner(cwd=Path("/repo"))
        payload = {
            "shared_brief": "all workers share this",
            "nodes": [
                {
                    "id": "wbs-1",
                    "title": "Implement feature",
                    "description": "Change code",
                    "capability": "implementation",
                    "complexity": 4,
                    "dependencies": [],
                    "parallelizable": False,
                    "deliverable": "Patch",
                    "brief": "touch src only",
                    "estimated_duration": 450,
                }
            ],
        }

        with patch.object(Planner, "_claude_json", return_value=payload):
            plan = planner.decompose("add feature")

        self.assertIsInstance(plan, Plan)
        self.assertEqual(plan.shared_brief, "all workers share this")
        self.assertEqual(len(plan.nodes), 1)
        node = plan.nodes[0]
        self.assertEqual(node.id, "wbs-1")
        self.assertEqual(node.brief, "touch src only")
        self.assertEqual(node.estimated_duration, 450)
        self.assertEqual(node.write_targets, [])

    def test_decompose_accepts_write_targets(self) -> None:
        planner = Planner(cwd=Path("/repo"))
        payload = {
            "nodes": [
                {
                    "id": "wbs-1",
                    "title": "Implement feature",
                    "description": "Change code",
                    "capability": "implementation",
                    "complexity": 4,
                    "dependencies": [],
                    "parallelizable": True,
                    "deliverable": "Patch",
                    "write_targets": ["src/app.py", " docs "],
                }
            ],
        }

        with patch.object(Planner, "_claude_json", return_value=payload):
            plan = planner.decompose("add feature")

        self.assertEqual(plan.nodes[0].write_targets, ["src/app.py", "docs"])

    def test_decompose_deduplicates_nodes_and_rewrites_dependencies(self) -> None:
        planner = Planner(cwd=Path("/repo"))
        payload = {
            "nodes": [
                {
                    "id": "wbs-a",
                    "title": "Update scheduler dedup",
                    "description": "Update scheduler dedup logic",
                    "capability": "implementation",
                    "complexity": 4,
                    "dependencies": [],
                    "parallelizable": True,
                    "deliverable": "Patch",
                    "write_targets": ["src/hermes_collab_engine/engine.py"],
                },
                {
                    "id": "wbs-b",
                    "title": "Update scheduler dedup",
                    "description": "Update scheduler dedup logic",
                    "capability": "implementation",
                    "complexity": 4,
                    "dependencies": [],
                    "parallelizable": True,
                    "deliverable": "Patch",
                    "write_targets": ["tests/test_stream_scheduler.py"],
                },
                {
                    "id": "wbs-c",
                    "title": "Verify scheduler",
                    "description": "Verify scheduler behavior",
                    "capability": "verification",
                    "complexity": 3,
                    "dependencies": ["wbs-b"],
                    "parallelizable": True,
                    "deliverable": "Test report",
                },
            ],
        }

        with patch.object(Planner, "_claude_json", return_value=payload):
            plan = planner.decompose("dedup scheduler")

        self.assertEqual([node.id for node in plan.nodes], ["wbs-a", "wbs-c"])
        self.assertEqual(plan.nodes[0].write_targets, ["src/hermes_collab_engine/engine.py", "tests/test_stream_scheduler.py"])
        self.assertEqual(plan.nodes[1].dependencies, ["wbs-a"])
        self.assertTrue(plan.nodes[0].fingerprint)

    def test_decompose_keeps_backward_compatible_list_payload(self) -> None:
        planner = Planner(cwd=Path("/repo"))
        payload = [
            {
                "id": "legacy-1",
                "title": "Legacy shape",
                "description": "Old array response",
                "capability": "analysis",
                "complexity": 3,
                "dependencies": [],
                "parallelizable": True,
                "deliverable": "Notes",
            }
        ]

        with patch.object(Planner, "_claude_json", return_value=payload):
            plan = planner.decompose("inspect")

        self.assertIsInstance(plan, Plan)
        self.assertEqual(plan.shared_brief, "")
        self.assertEqual([node.id for node in plan.nodes], ["legacy-1"])
        self.assertEqual(plan.nodes[0].brief, "")
        self.assertIsNone(plan.nodes[0].estimated_duration)

    def test_decompose_falls_back_with_shared_brief_and_node_briefs(self) -> None:
        planner = Planner(cwd=Path("/repo"))

        with patch.object(Planner, "_claude_json", side_effect=RuntimeError("boom")):
            plan = planner.fallback_wbs("small change", score=type("Score", (), {"routing": "single", "overall": 4})())

        self.assertIsInstance(plan, Plan)
        self.assertTrue(plan.shared_brief)
        self.assertEqual([node.id for node in plan.nodes], ["wbs-1", "wbs-2", "wbs-verify"])
        self.assertTrue(all(node.brief for node in plan.nodes))
        self.assertTrue(all(node.estimated_duration for node in plan.nodes))

    def test_decompose_injects_only_reusable_scoped_lessons_into_prompt(self) -> None:
        store = FakeStore([
            {"scope": "global", "category": "planning", "lesson": "global guidance"},
            {"scope": "project", "category": "style", "lesson": "project guidance"},
            {"scope": "run", "category": "local", "lesson": "run-only guidance"},
            {"scope": "node", "category": "retry", "lesson": "node-only guidance"},
        ])
        planner = Planner(cwd=Path("/repo"), store=store)
        captured = {}

        def fake_json(prompt: str):
            captured["prompt"] = prompt
            raise RuntimeError("force fallback")

        with patch.object(Planner, "_claude_json", side_effect=fake_json):
            planner.decompose("request")

        prompt = captured["prompt"]
        self.assertIn("Recent planning lessons to apply:", prompt)
        self.assertIn("[global/planning] global guidance", prompt)
        self.assertIn("[project/style] project guidance", prompt)
        self.assertNotIn("run-only guidance", prompt)
        self.assertNotIn("node-only guidance", prompt)

    def test_decompose_without_store_has_no_lesson_block(self) -> None:
        planner = Planner(cwd=Path("/repo"))
        captured = {}

        def fake_json(prompt: str):
            captured["prompt"] = prompt
            raise RuntimeError("force fallback")

        with patch.object(Planner, "_claude_json", side_effect=fake_json):
            planner.decompose("request")

        self.assertNotIn("Recent planning lessons to apply:", captured["prompt"])


if __name__ == "__main__":
    unittest.main()
