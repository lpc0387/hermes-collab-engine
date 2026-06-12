from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.hermes_collab_engine.engine import CollabEngine
from src.hermes_collab_engine.models import WBSNode, WorkerResult


def make_node(node_id: str, deps: list[str] | None = None, *, attempt: int = 1, checkpoint: bool = False) -> WBSNode:
    return WBSNode(
        node_id,
        node_id,
        f"Task {node_id}",
        "implementation",
        5,
        deps or [],
        True,
        f"Deliver {node_id}",
        attempt=attempt,
        checkpoint=checkpoint,
        brief=f"Brief {node_id}",
        estimated_duration=120,
    )


def seed_run(engine: CollabEngine, nodes: list[WBSNode]) -> None:
    engine.store.create_run("run_1", "title", "request", {})
    for current in nodes:
        engine.store.insert_wbs_node("run_1", current.to_dict())
        engine.store.update_node_result("run_1", current.id, f"old {current.id}")


class RedoNodeTests(unittest.TestCase):
    def test_redo_single_node_increments_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = CollabEngine(Path(tmp) / "db.sqlite3", tmp)
            seed_run(engine, [make_node("wbs-1")])
            calls: list[tuple[str, int]] = []

            def fake_run_worker(run_id, current, timeout, model_override=None):
                calls.append((current.id, current.attempt))
                return WorkerResult(current.id, current.title, True, "new result", None, 0.01, 0, "", current.attempt)

            engine._run_worker = fake_run_worker

            result = engine.redo_node("run_1", "wbs-1")

            self.assertEqual(result, {"node_id": "wbs-1", "attempt": 2, "status": "completed"})
            self.assertEqual(calls, [("wbs-1", 2)])
            row = engine.store.get_node("run_1", "wbs-1")
            self.assertEqual(row["attempt"], 2)
            self.assertEqual(row["result"], "new result")

    def test_record_node_result_persists_mirror_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = CollabEngine(Path(tmp) / "db.sqlite3", tmp)
            result = WorkerResult(
                "wbs-1",
                "title",
                True,
                "result text",
                None,
                0.01,
                0,
                "",
                1,
                {"summary": "structured"},
            )

            engine._record_node_result("run_1", result)

            rows = engine.store.load_node_results("run_1")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["node_id"], "wbs-1")
            self.assertEqual(rows[0]["result_text"], "result text")
            self.assertEqual(json.loads(rows[0]["result_struct_json"]), {"summary": "structured"})

    def test_redo_with_cascade_also_redownstream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = CollabEngine(Path(tmp) / "db.sqlite3", tmp)
            seed_run(engine, [make_node("wbs-1"), make_node("wbs-2", ["wbs-1"]), make_node("wbs-3", ["wbs-2"])])
            calls: list[str] = []

            def fake_run_worker(run_id, current, timeout, model_override=None):
                calls.append(current.id)
                return WorkerResult(current.id, current.title, True, f"new {current.id}", None, 0.01, 0, "", current.attempt)

            engine._run_worker = fake_run_worker

            result = engine.redo_node("run_1", "wbs-1", cascade=True)

            self.assertEqual(result["attempt"], 2)
            self.assertEqual(calls[0], "wbs-1")
            self.assertCountEqual(calls[1:], ["wbs-2", "wbs-3"])
            rows = {row["id"]: row for row in engine.store.get_nodes("run_1")}
            self.assertEqual(rows["wbs-1"]["attempt"], 2)
            self.assertEqual(rows["wbs-2"]["attempt"], 2)
            self.assertEqual(rows["wbs-3"]["attempt"], 2)

    def test_find_downstream_nodes_is_transitive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = CollabEngine(Path(tmp) / "db.sqlite3", tmp)
            seed_run(engine, [
                make_node("wbs-1"),
                make_node("wbs-2", ["wbs-1"]),
                make_node("wbs-3", ["wbs-2"]),
                make_node("wbs-4", ["wbs-1"]),
                make_node("wbs-5"),
            ])

            downstream = engine._find_downstream_nodes("run_1", "wbs-1")

            self.assertCountEqual(downstream, ["wbs-2", "wbs-3", "wbs-4"])

    def test_redo_missing_node_raises_value_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = CollabEngine(Path(tmp) / "db.sqlite3", tmp)
            seed_run(engine, [make_node("wbs-1")])

            with self.assertRaisesRegex(ValueError, "Node missing not found"):
                engine.redo_node("run_1", "missing")

    def test_load_plan_from_db_restores_v3_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = CollabEngine(Path(tmp) / "db.sqlite3", tmp)
            original = make_node("wbs-1", ["wbs-0"], attempt=3, checkpoint=True)
            seed_run(engine, [original])

            plan = engine._load_plan_from_db("run_1")

            loaded = plan.nodes[0]
            self.assertEqual(loaded.dependencies, ["wbs-0"])
            self.assertTrue(loaded.checkpoint)
            self.assertEqual(loaded.attempt, 3)
            self.assertEqual(loaded.brief, "Brief wbs-1")
            self.assertEqual(loaded.estimated_duration, 120)
            stored = engine.store.get_node("run_1", "wbs-1")
            self.assertEqual(json.loads(stored["dependencies_json"]), ["wbs-0"])

    def test_load_plan_from_db_restores_node_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = CollabEngine(Path(tmp) / "db.sqlite3", tmp)
            seed_run(engine, [make_node("wbs-1"), make_node("wbs-2")])
            engine.store.save_node_result("run_1", "wbs-1", "text result", {"summary": "structured"})
            engine.store.save_node_result("run_1", "wbs-2", "plain result", None)
            engine._node_results = {}
            engine._node_results_struct = {}

            engine._load_plan_from_db("run_1")

            self.assertEqual(engine._node_results, {"wbs-1": "text result", "wbs-2": "plain result"})
            self.assertEqual(engine._node_results_struct, {"wbs-1": {"summary": "structured"}, "wbs-2": None})

    def test_redo_node_uses_restored_structured_upstream_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = CollabEngine(Path(tmp) / "db.sqlite3", tmp)
            seed_run(engine, [make_node("wbs-1"), make_node("wbs-2", ["wbs-1"])])
            engine.store.save_node_result("run_1", "wbs-1", "verbose text", {"summary": "structured parent"})
            prompts: list[str] = []

            def fake_run_worker(run_id, current, timeout, model_override=None):
                prompts.append(engine._build_upstream_context(current))
                return WorkerResult(current.id, current.title, True, "new result", None, 0.01, 0, "", current.attempt)

            engine._run_worker = fake_run_worker

            engine.redo_node("run_1", "wbs-2")

            self.assertIn("summary: structured parent", prompts[0])


if __name__ == "__main__":
    unittest.main()
