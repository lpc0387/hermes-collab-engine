from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.hermes_collab_engine.engine import CollabEngine
from src.hermes_collab_engine.models import ComplexityScore, Plan, WBSNode, WorkerResult


def make_node(estimated_duration=None) -> WBSNode:
    return WBSNode(
        "wbs-1",
        "Large node",
        "Do a large task",
        "verification",
        7,
        [],
        True,
        "Large deliverable",
        estimated_duration=estimated_duration,
    )


class ProactiveSplitTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.engine = CollabEngine(Path(self._tmp.name) / "db.sqlite3", self._tmp.name)

    def test_splits_when_2x_estimate_exceeds_timeout(self) -> None:
        self.assertTrue(self.engine._should_split_proactively(make_node(600), timeout=900, max_retries=1, split_count=2))

    def test_no_estimate_keeps_backward_compatibility(self) -> None:
        self.assertFalse(self.engine._should_split_proactively(make_node(None), timeout=900, max_retries=1, split_count=2))

    def test_within_estimate_does_not_split(self) -> None:
        self.assertFalse(self.engine._should_split_proactively(make_node(300), timeout=900, max_retries=1, split_count=2))

    def test_invalid_estimate_does_not_split(self) -> None:
        node = make_node("not-a-number")
        self.assertFalse(self.engine._should_split_proactively(node, timeout=900, max_retries=1, split_count=2))

    def test_no_retry_or_single_split_count_disables_proactive_split(self) -> None:
        # max_retries no longer gates splitting; only split_count does
        self.assertTrue(self.engine._should_split_proactively(make_node(600), timeout=900, max_retries=0, split_count=2))
        self.assertFalse(self.engine._should_split_proactively(make_node(600), timeout=900, max_retries=1, split_count=1))

    def test_runtime_duplicate_fingerprint_skips_second_worker(self) -> None:
        first = WBSNode("wbs-1", "Implement dedup", "Implement dedup", "implementation", 5, [], True, "done")
        second = WBSNode("wbs-2", "Implement dedup", "Implement dedup", "implementation", 5, [], True, "done")

        claimed = self.engine._claim_fingerprint(first)

        self.assertEqual(self.engine._duplicate_running_node(second), "wbs-1")
        self.assertEqual(self.engine._node_fingerprint(second), claimed)
        self.engine._release_fingerprint("wbs-1")
        self.assertIsNone(self.engine._duplicate_running_node(second))

    def test_split_node_creates_focused_shards(self) -> None:
        shards = self.engine._split_node(make_node(600), 5)

        self.assertEqual([shard.id for shard in shards], [
            "wbs-1-scope-1",
            "wbs-1-evidence-2",
            "wbs-1-impl-3",
            "wbs-1-impl-4",
            "wbs-1-impl-5",
        ])
        self.assertTrue(all(shard.parent_id == "wbs-1" for shard in shards))
        self.assertTrue(all(shard.attempt == 2 for shard in shards))
        self.assertTrue(all(shard.parallelizable for shard in shards))
        # Phase 1 shards (scope, evidence) have no dependencies
        self.assertEqual(shards[0].dependencies, [])
        self.assertEqual(shards[1].dependencies, [])
        # Phase 2 shards (impl) depend on phase 1
        for impl_shard in shards[2:]:
            self.assertIn("wbs-1-scope-1", impl_shard.dependencies)
            self.assertIn("wbs-1-evidence-2", impl_shard.dependencies)
        # Impl shards have implementation capability
        for impl_shard in shards[2:]:
            self.assertEqual(impl_shard.capability, "implementation")

    def test_over_estimate_logs_warning_and_runs_shards(self) -> None:
        node = make_node(600)
        self.engine.planner.assess = lambda request: ComplexityScore(5, 5, 5, 5, 5, 7, "wbs")
        self.engine.planner.decompose = lambda request, **kw: Plan(nodes=[node])
        calls: list[str] = []

        def fake_run_worker(run_id, current, timeout, model_override=None):
            calls.append(current.id)
            return WorkerResult(current.id, current.title, True, f"ok {current.id}", None, 0.01, 0, "", current.attempt)

        self.engine._run_worker = fake_run_worker

        result = self.engine.run("split this", concurrency=2, timeout=900, max_retries=1, split_count=4, aggregate=False)

        self.assertTrue(result["ok"])
        # split_count=4 → scope + evidence + 2 impl shards = 4 total
        self.assertIn("wbs-1-scope-1", calls)
        self.assertIn("wbs-1-evidence-2", calls)
        parent = self.engine.store._one("SELECT status, result FROM wbs_nodes WHERE id='wbs-1'")
        self.assertEqual(parent["status"], "completed")
        self.assertEqual(parent["result"], "Completed by proactive shards")
        logs = self.engine.store._query("SELECT message FROM logs WHERE node_id='wbs-1' ORDER BY id")
        self.assertIn("node estimated to exceed timeout; splitting proactively", [row["message"] for row in logs])


if __name__ == "__main__":
    unittest.main()
