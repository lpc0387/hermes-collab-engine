from __future__ import annotations

import io
import json
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from src.hermes_collab_engine import cli
from src.hermes_collab_engine.engine import CollabEngine
from src.hermes_collab_engine.models import ComplexityScore, Plan, RiskPolicy, WBSNode, WorkerResult


def make_engine(tmp: str) -> CollabEngine:
    engine = CollabEngine(Path(tmp) / "db.sqlite3", tmp)
    engine.planner.assess = lambda request: ComplexityScore(5, 5, 5, 5, 5, 7, "wbs")
    return engine


def node(node_id: str, deps: list[str] | None = None, *, checkpoint: bool = False) -> WBSNode:
    return WBSNode(
        node_id,
        node_id,
        f"Task {node_id}",
        "verification",
        3,
        deps or [],
        True,
        f"Deliver {node_id}",
        checkpoint=checkpoint,
    )


class CheckpointPauseResumeTests(unittest.TestCase):
    def test_checkpoint_node_triggers_pause(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = make_engine(tmp)
            checkpoint = node("wbs-1", checkpoint=True)
            engine._current_plan = Plan(nodes=[checkpoint])

            risks = engine._detect_risks(checkpoint, {"status": "ok"}, RiskPolicy())
            engine._apply_risk_policy("run_1", risks, RiskPolicy(high="pause"))

            self.assertIn("wbs-1", engine._checkpoint_paused_nodes)
            state = engine.store.load_run_state("run_1")
            self.assertEqual(state["checkpoint_paused_nodes"], ["wbs-1"])
            logs = engine.store._query("SELECT level, node_id FROM logs WHERE level='checkpoint'")
            self.assertEqual(logs[0]["node_id"], "wbs-1")

    def test_auto_resume_after_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = make_engine(tmp)
            engine.store.set_setting("risk_policy", RiskPolicy(checkpoint_timeout=1).to_dict())
            engine._checkpoint_paused_nodes.add("wbs-1")

            timer = threading.Timer(0.01, engine._auto_resume_checkpoint, args=("run_1", "wbs-1"))
            timer.start()
            timer.join(timeout=1)

            self.assertNotIn("wbs-1", engine._checkpoint_paused_nodes)
            state = engine.store.load_run_state("run_1")
            self.assertEqual(state["checkpoint_paused_nodes"], [])
            lesson = engine.store._one("SELECT scope, category FROM lessons WHERE category='checkpoint-timeout'")
            self.assertEqual(lesson["scope"], "engine")

    def test_manual_resume_via_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "db.sqlite3"
            output = io.StringIO()
            paused_engine = CollabEngine(db_path, tmp)
            paused_engine._paused_runs.add("run_1")
            paused_engine._checkpoint_paused_nodes.add("wbs-1")

            with patch.object(sys, "argv", ["hermes-collab", "resume-run", "--db", str(db_path), "--cwd", tmp, "--run-id", "run_1", "--json"]), \
                 patch("src.hermes_collab_engine.cli.CollabEngine", return_value=paused_engine), \
                 redirect_stdout(output):
                code = cli.main()

            self.assertEqual(code, 0)
            self.assertNotIn("run_1", paused_engine._paused_runs)
            self.assertEqual(paused_engine._checkpoint_paused_nodes, set())
            self.assertEqual(json.loads(output.getvalue()).get("action"), "resumed")

    def test_pause_and_resume_persist_run_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = make_engine(tmp)

            engine.pause_run("run_1")
            paused = engine.store.load_run_state("run_1")
            engine.resume_run("run_1")
            resumed = engine.store.load_run_state("run_1")

            self.assertTrue(paused["paused"])
            self.assertFalse(resumed["paused"])
            self.assertEqual(resumed["checkpoint_paused_nodes"], [])

    def test_engine_restores_paused_state_on_startup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "db.sqlite3"
            first = CollabEngine(db_path, tmp)
            first.store.save_run_state("run_1", True, {"wbs-1"})

            restored = CollabEngine(db_path, tmp)

            self.assertIn("run_1", restored._paused_runs)
            self.assertEqual(restored._checkpoint_paused_nodes, {"wbs-1"})

    def test_paused_run_blocks_scheduling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = make_engine(tmp)
            run_id = "run_1"
            engine.pause_run(run_id)
            current = node("wbs-1")
            engine.store.create_run(run_id, "title", "request", {})
            engine.store.insert_wbs_node(run_id, current.to_dict())
            engine.store.update_run(run_id, "running")
            pending = {current.id: current}
            running = {}

            while pending and len(running) < 1:
                if run_id in engine._paused_runs:
                    break
                pending.pop(current.id)

            self.assertEqual(set(pending), {"wbs-1"})
            self.assertEqual(running, {})

    def test_checkpoint_pause_blocks_downstream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = make_engine(tmp)
            nodes = [node("wbs-1", checkpoint=True), node("wbs-2", ["wbs-1"])]
            engine.planner.decompose = lambda request, **kw: Plan(nodes=nodes)
            engine.store.set_setting("risk_policy", RiskPolicy(high="pause").to_dict())
            calls: list[str] = []

            def fake_run_worker(run_id, current, timeout, model_override=None):
                calls.append(current.id)
                return WorkerResult(current.id, current.title, True, f"ok {current.id}", None, 0.01, 0, "", current.attempt)

            engine._run_worker = fake_run_worker

            result = engine.run("checkpoint downstream", concurrency=1, aggregate=False)

            snapshots = engine.store.load_context_snapshots(result["run_id"])
            self.assertEqual(calls, ["wbs-1"])
            self.assertEqual([row["snapshot_type"] for row in snapshots], ["node_completed", "checkpoint"])
            checkpoint_snapshot = json.loads(snapshots[1]["snapshot_json"])
            self.assertEqual(checkpoint_snapshot["plan_summary"], "")
            self.assertEqual(checkpoint_snapshot["nodes"]["wbs-1"]["status"], "completed")
            self.assertEqual(checkpoint_snapshot["nodes"]["wbs-1"]["key_facts"], "ok wbs-1")
            self.assertEqual(checkpoint_snapshot["pending_actions"], ["wbs-1"])
            self.assertEqual(checkpoint_snapshot["risk_assessments"][0]["risk_level"], "high")


if __name__ == "__main__":
    unittest.main()
