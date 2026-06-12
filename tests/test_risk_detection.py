from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.hermes_collab_engine.engine import CollabEngine
from src.hermes_collab_engine.models import ComplexityScore, Plan, RiskPolicy, WBSNode, WorkerResult


def make_engine(tmp: str) -> CollabEngine:
    engine = CollabEngine(Path(tmp) / "db.sqlite3", tmp)
    engine.planner.assess = lambda request: ComplexityScore(5, 5, 5, 5, 5, 7, "wbs")
    return engine


def make_node(node_id: str = "wbs-1", *, checkpoint: bool = False, estimated_duration=None) -> WBSNode:
    return WBSNode(
        node_id,
        node_id,
        f"Task {node_id}",
        "implementation",
        5,
        [],
        True,
        f"Deliver {node_id}",
        checkpoint=checkpoint,
        estimated_duration=estimated_duration,
    )


class RiskDetectionTests(unittest.TestCase):
    def test_medium_risk_blocking_issues_triggers_notify(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = make_engine(tmp)
            node = make_node()
            engine._current_plan = Plan(nodes=[node])
            result_struct = {"status": "blocked", "blocking_issues": ["needs approval"]}
            policy = RiskPolicy(medium="notify", checkpoint_timeout=30)

            with patch("src.hermes_collab_engine.engine.threading.Timer") as timer:
                risks = engine._detect_risks(node, result_struct, policy)
                engine._apply_risk_policy("run_1", risks, policy)

            self.assertEqual(risks[0][0], "medium")
            self.assertIn("wbs-1", engine._checkpoint_paused_nodes)
            timer.assert_called_once()
            timer.return_value.start.assert_called_once()

    def test_high_risk_checkpoint_triggers_pause(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = make_engine(tmp)
            node = make_node(checkpoint=True)
            engine._current_plan = Plan(nodes=[node])

            with patch("src.hermes_collab_engine.engine.threading.Timer") as timer:
                risks = engine._detect_risks(node, {"status": "ok"}, RiskPolicy(high="pause"))
                engine._apply_risk_policy("run_1", risks, RiskPolicy(high="pause"))

            self.assertEqual(risks, [("high", "Checkpoint node wbs-1 (wbs-1) completed")])
            self.assertIn("wbs-1", engine._checkpoint_paused_nodes)
            timer.assert_not_called()

    def test_low_risk_auto_handled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = make_engine(tmp)
            node = make_node(estimated_duration=600)
            shards = engine._split_node(node, 2)

            engine._apply_risk_policy("run_1", [("low", "Node wbs-1 timed out; auto-split")], RiskPolicy(low="auto"))

            self.assertEqual(engine._checkpoint_paused_nodes, set())
            self.assertEqual([shard.id for shard in shards], ["wbs-1-scope-1", "wbs-1-evidence-2", "wbs-1-impl-3"])
            logs = engine.store._query("SELECT level, message FROM logs WHERE level='risk'")
            self.assertIn("action=auto", logs[0]["message"])

    def test_custom_risk_policy_from_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = make_engine(tmp)
            engine.store.set_setting("risk_policy", {
                "low": "auto",
                "medium": "notify",
                "high": "pause",
                "checkpoint_timeout": 12,
            })

            policy = engine.store.load_risk_policy()

            self.assertEqual(policy.low, "auto")
            self.assertEqual(policy.medium, "notify")
            self.assertEqual(policy.high, "pause")
            self.assertEqual(policy.checkpoint_timeout, 12)

    def test_auto_risk_level_logs_but_does_not_pause(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = make_engine(tmp)
            node = make_node()
            engine._current_plan = Plan(nodes=[node])
            risks = engine._detect_risks(node, {"notes": ["minor follow-up"]}, RiskPolicy(medium="auto"))

            engine._apply_risk_policy("run_1", risks, RiskPolicy(medium="auto"))

            self.assertEqual(engine._checkpoint_paused_nodes, set())
            logs = engine.store._query("SELECT level, message FROM logs WHERE level='risk'")
            self.assertEqual(len(logs), 1)
            self.assertIn("action=auto", logs[0]["message"])


if __name__ == "__main__":
    unittest.main()
