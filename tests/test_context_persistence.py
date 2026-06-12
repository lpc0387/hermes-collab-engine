from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from hermes_collab_engine.engine import CollabEngine
from hermes_collab_engine.models import Plan, WBSNode, WorkerResult
from hermes_collab_engine.store import CollabStore


def close_store(store: CollabStore) -> None:
    store.conn.close()


def make_node(node_id: str, deps: list[str] | None = None) -> dict:
    return {
        "id": node_id,
        "title": f"Title {node_id}",
        "description": f"Description {node_id}",
        "capability": "verification",
        "complexity": 3,
        "dependencies": deps or [],
        "parallelizable": True,
        "deliverable": f"Deliver {node_id}",
        "status": "pending",
    }


class ContextPersistenceTests(unittest.TestCase):
    def test_wbs_nodes_context_columns_roundtrip(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".sqlite3") as tmp:
            db_path = Path(tmp.name)
            store = CollabStore(db_path)
            store.create_run("run_1", "title", "request", {})
            result_struct = {"status": "ok", "summary": "structured result"}
            node = {
                **make_node("wbs-1"),
                "brief": "node brief",
                "shared_brief": "shared plan brief",
                "estimated_duration": 180,
                "result_struct_json": json.dumps(result_struct, ensure_ascii=False),
            }

            store.insert_wbs_node("run_1", node)
            close_store(store)

            restored = CollabStore(db_path)
            row = restored.get_node("run_1", "wbs-1")
            self.assertIsNotNone(row)
            self.assertEqual(row["brief"], "node brief")
            self.assertEqual(row["shared_brief"], "shared plan brief")
            self.assertEqual(row["estimated_duration"], 180)
            self.assertEqual(json.loads(row["result_struct_json"]), result_struct)
            close_store(restored)

    def test_node_results_roundtrip_restores_engine_caches(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".sqlite3") as tmp:
            db_path = Path(tmp.name)
            engine = CollabEngine(db_path, db_path.parent)
            engine.store.create_run("run_1", "title", "request", {})
            engine.store.insert_wbs_node("run_1", make_node("wbs-1"))
            result_struct = {"status": "ok", "summary": "structured result"}
            result = WorkerResult(
                "wbs-1",
                "Title wbs-1",
                True,
                "result text",
                None,
                0.01,
                0,
                "",
                1,
                result_struct,
            )

            engine._record_node_result("run_1", result)
            close_store(engine.store)

            restored = CollabEngine(db_path, db_path.parent)
            restored._load_plan_from_db("run_1")
            self.assertEqual(restored._node_results, {"wbs-1": "result text"})
            self.assertEqual(restored._node_results_struct, {"wbs-1": result_struct})
            close_store(restored.store)

    def test_run_state_pause_resume_roundtrip(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".sqlite3") as tmp:
            db_path = Path(tmp.name)
            engine = CollabEngine(db_path, db_path.parent)

            engine.pause_run("run_1")
            close_store(engine.store)

            paused = CollabEngine(db_path, db_path.parent)
            paused.restore_run_state("run_1")
            self.assertIn("run_1", paused._paused_runs)
            self.assertTrue(paused.store.load_run_state("run_1")["paused"])

            paused.resume_run("run_1")
            close_store(paused.store)

            resumed = CollabEngine(db_path, db_path.parent)
            resumed.restore_run_state("run_1")
            self.assertNotIn("run_1", resumed._paused_runs)
            self.assertFalse(resumed.store.load_run_state("run_1")["paused"])
            close_store(resumed.store)

    def test_redo_node_restores_structured_results_after_store_reopen(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".sqlite3") as tmp:
            db_path = Path(tmp.name)
            engine = CollabEngine(db_path, db_path.parent)
            engine.store.create_run("run_1", "title", "request", {})
            engine.store.insert_wbs_node("run_1", make_node("wbs-1"))
            engine.store.insert_wbs_node("run_1", make_node("wbs-2", ["wbs-1"]))
            engine.store.save_node_result("run_1", "wbs-1", "verbose text", {"summary": "structured parent"})
            close_store(engine.store)

            restored = CollabEngine(db_path, db_path.parent)
            upstream_contexts: list[str] = []

            def fake_run_worker(run_id, current, timeout, model_override=None):
                upstream_contexts.append(restored._build_upstream_context(current))
                return WorkerResult(current.id, current.title, True, "new result", None, 0.01, 0, "", current.attempt)

            restored._run_worker = fake_run_worker

            result = restored.redo_node("run_1", "wbs-2")

            self.assertEqual(result, {"node_id": "wbs-2", "attempt": 2, "status": "completed"})
            self.assertEqual(restored._node_results_struct["wbs-1"], {"summary": "structured parent"})
            self.assertIn("summary: structured parent", upstream_contexts[0])
            close_store(restored.store)

    def test_context_snapshots_write_and_read_roundtrip(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".sqlite3") as tmp:
            db_path = Path(tmp.name)
            store = CollabStore(db_path)
            snapshot = {
                "plan_summary": "shared plan",
                "nodes": {"wbs-1": {"status": "completed", "quality": "ok", "key_facts": ["fact"]}},
                "decisions": [{"action": "continue"}],
                "risk_assessments": [{"risk_level": "low", "description": "none", "action": "auto"}],
                "user_instructions": ["keep going"],
                "pending_actions": [],
            }

            store.save_context_snapshot("run_1", "node_completed", snapshot, "wbs-1")

            rows = store.load_context_snapshots("run_1")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["run_id"], "run_1")
            self.assertEqual(rows[0]["snapshot_type"], "node_completed")
            self.assertEqual(rows[0]["node_id"], "wbs-1")
            self.assertEqual(json.loads(rows[0]["snapshot_json"]), snapshot)
            self.assertIsNotNone(rows[0]["created_at"])
            close_store(store)

    def test_context_snapshots_type_filtering(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".sqlite3") as tmp:
            db_path = Path(tmp.name)
            store = CollabStore(db_path)
            node_snapshot = {"nodes": {"wbs-1": {"status": "completed"}}}
            checkpoint_snapshot = {"nodes": {"wbs-2": {"status": "completed"}}, "pending_actions": ["wbs-2"]}

            store.save_context_snapshot("run_1", "node_completed", node_snapshot, "wbs-1")
            store.save_context_snapshot("run_1", "checkpoint", checkpoint_snapshot, "wbs-2")
            store.save_context_snapshot("run_2", "checkpoint", {"other": True}, "wbs-9")

            checkpoint_rows = store.load_context_snapshots("run_1", "checkpoint")
            self.assertEqual(len(checkpoint_rows), 1)
            self.assertEqual(checkpoint_rows[0]["snapshot_type"], "checkpoint")
            self.assertEqual(checkpoint_rows[0]["node_id"], "wbs-2")
            self.assertEqual(json.loads(checkpoint_rows[0]["snapshot_json"]), checkpoint_snapshot)
            close_store(store)

    def test_context_snapshot_cli_latest_returns_newest_snapshot(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".sqlite3") as tmp:
            db_path = Path(tmp.name)
            store = CollabStore(db_path)
            store.save_context_snapshot("run_1", "node_completed", {"sequence": 1}, "wbs-1")
            store.save_context_snapshot("run_1", "checkpoint", {"sequence": 2}, "wbs-2")
            store.save_context_snapshot("run_1", "node_completed", {"sequence": 3}, "wbs-3")
            close_store(store)

            env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "hermes_collab_engine.cli",
                    "context-snapshot",
                    "--db",
                    str(db_path),
                    "run_1",
                    "--latest",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
            rows = json.loads(proc.stdout)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["node_id"], "wbs-3")
            self.assertEqual(json.loads(rows[0]["snapshot_json"]), {"sequence": 3})

    def test_context_snapshot_cli_filters_by_type(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".sqlite3") as tmp:
            db_path = Path(tmp.name)
            store = CollabStore(db_path)
            store.save_context_snapshot("run_1", "node_completed", {"kind": "node"}, "wbs-1")
            store.save_context_snapshot("run_1", "checkpoint", {"kind": "checkpoint"}, "wbs-2")
            close_store(store)

            env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "hermes_collab_engine.cli",
                    "context-snapshot",
                    "--db",
                    str(db_path),
                    "run_1",
                    "--type",
                    "checkpoint",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
            rows = json.loads(proc.stdout)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["snapshot_type"], "checkpoint")
            self.assertEqual(json.loads(rows[0]["snapshot_json"]), {"kind": "checkpoint"})

    def test_full_context_persistence_integration_reopens_all_state(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".sqlite3") as tmp:
            db_path = Path(tmp.name)
            engine = CollabEngine(db_path, db_path.parent)
            engine.store.create_run("run_1", "title", "request", {})
            engine.store.update_run("run_1", "running")
            node = WBSNode("wbs-1", "Title wbs-1", "Description wbs-1", "verification", 3, [], True, "Deliver wbs-1")
            node_data = {**node.to_dict(), "shared_brief": "integration shared brief"}
            engine.store.insert_wbs_node("run_1", node_data)
            engine._current_plan = Plan(nodes=[node], shared_brief="integration shared brief")
            result_struct = {"status": "ok", "summary": "persisted summary", "key_facts": ["fact one"]}
            result = WorkerResult("wbs-1", "Title wbs-1", True, "result text", "session-1", 0.01, 0, "", 1, result_struct)

            engine.store.update_node("wbs-1", "completed", result.result, result.session_id, result.duration_seconds)
            engine._record_node_result("run_1", result)
            engine.store.update_run("run_1", "completed")
            close_store(engine.store)

            restored = CollabEngine(db_path, db_path.parent)
            restored_plan = restored._load_plan_from_db("run_1")
            detail = restored.store.run_detail("run_1")
            snapshots = restored.store.load_context_snapshots("run_1")

            self.assertEqual(detail["run"]["status"], "completed")
            self.assertEqual(restored_plan.shared_brief, "integration shared brief")
            node_row = restored.store.get_node("run_1", "wbs-1")
            self.assertEqual(node_row["status"], "completed")
            self.assertEqual(node_row["result"], "result text")
            self.assertEqual(restored._node_results, {"wbs-1": "result text"})
            self.assertEqual(restored._node_results_struct, {"wbs-1": result_struct})
            self.assertEqual(len(snapshots), 1)
            self.assertEqual(snapshots[0]["snapshot_type"], "node_completed")
            self.assertEqual(snapshots[0]["node_id"], "wbs-1")
            snapshot = json.loads(snapshots[0]["snapshot_json"])
            self.assertEqual(snapshot["plan_summary"], "integration shared brief")
            self.assertEqual(snapshot["nodes"]["wbs-1"]["status"], "completed")
            self.assertEqual(snapshot["nodes"]["wbs-1"]["quality"], "ok")
            self.assertEqual(snapshot["nodes"]["wbs-1"]["key_facts"], ["fact one"])
            close_store(restored.store)


if __name__ == "__main__":
    unittest.main()
