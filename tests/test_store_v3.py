from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.hermes_collab_engine.models import RiskPolicy, WBSNode
from src.hermes_collab_engine.store import CollabStore


class StoreV3Tests(unittest.TestCase):
    def test_load_risk_policy_defaults_without_setting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CollabStore(Path(tmp) / "db.sqlite3")

            policy = store.load_risk_policy()

            self.assertEqual(policy, RiskPolicy())

    def test_load_risk_policy_uses_settings_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CollabStore(Path(tmp) / "db.sqlite3")
            store.set_setting("risk_policy", {"low": "auto", "medium": "notify", "high": "pause", "checkpoint_timeout": 30})

            policy = store.load_risk_policy()

            self.assertEqual(policy.low, "auto")
            self.assertEqual(policy.medium, "notify")
            self.assertEqual(policy.high, "pause")
            self.assertEqual(policy.checkpoint_timeout, 30)

    def test_insert_wbs_node_persists_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CollabStore(Path(tmp) / "db.sqlite3")
            store.create_run("run_1", "title", "request", {})
            node = WBSNode("wbs-1", "title", "desc", "implementation", 5, [], True, "deliver", checkpoint=True)

            store.insert_wbs_node("run_1", node.to_dict())

            row = store.get_node("run_1", "wbs-1")
            self.assertEqual(row["checkpoint"], 1)

    def test_update_node_attempt_and_result_persist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CollabStore(Path(tmp) / "db.sqlite3")
            store.create_run("run_1", "title", "request", {})
            node = WBSNode("wbs-1", "title", "desc", "implementation", 5, [], True, "deliver")
            store.insert_wbs_node("run_1", node.to_dict())

            store.update_node_attempt("run_1", "wbs-1", 3)
            store.update_node_result("run_1", "wbs-1", "new result")

            row = store.get_node("run_1", "wbs-1")
            self.assertEqual(row["attempt"], 3)
            self.assertEqual(row["result"], "new result")

    def test_save_and_load_node_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CollabStore(Path(tmp) / "db.sqlite3")

            store.save_node_result("run_1", "wbs-1", "text", {"summary": "structured"})
            store.save_node_result("run_1", "wbs-2", "plain", None)

            rows = store.load_node_results("run_1")
            by_id = {row["node_id"]: row for row in rows}
            self.assertEqual(by_id["wbs-1"]["result_text"], "text")
            self.assertEqual(by_id["wbs-1"]["result_struct_json"], '{"summary": "structured"}')
            self.assertEqual(by_id["wbs-2"]["result_text"], "plain")
            self.assertIsNone(by_id["wbs-2"]["result_struct_json"])

    def test_existing_db_creates_node_results_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "legacy.sqlite3"
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE runs (id TEXT PRIMARY KEY,title TEXT NOT NULL,request TEXT NOT NULL,status TEXT NOT NULL,complexity_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,completed_at TEXT)")
            conn.commit()
            conn.close()

            store = CollabStore(db_path)
            tables = {row[0] for row in store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

            self.assertIn("node_results", tables)

    def test_save_and_load_context_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CollabStore(Path(tmp) / "db.sqlite3")
            snapshot = {
                "plan_summary": "plan",
                "nodes": {"wbs-1": {"status": "completed", "quality": "ok", "key_facts": ["fact"]}},
                "decisions": [],
                "risk_assessments": [],
                "user_instructions": [],
                "pending_actions": [],
            }

            store.save_context_snapshot("run_1", "node_completed", snapshot, "wbs-1")
            store.save_context_snapshot("run_1", "checkpoint", snapshot, "wbs-1")

            all_rows = store.load_context_snapshots("run_1")
            checkpoint_rows = store.load_context_snapshots("run_1", "checkpoint")
            self.assertEqual([row["snapshot_type"] for row in all_rows], ["node_completed", "checkpoint"])
            self.assertEqual(len(checkpoint_rows), 1)
            self.assertEqual(checkpoint_rows[0]["node_id"], "wbs-1")
            self.assertEqual(json.loads(checkpoint_rows[0]["snapshot_json"]), snapshot)

    def test_save_context_snapshot_rejects_unknown_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CollabStore(Path(tmp) / "db.sqlite3")

            with self.assertRaises(ValueError):
                store.save_context_snapshot("run_1", "pre_compaction", {}, None)

    def test_save_and_load_run_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CollabStore(Path(tmp) / "db.sqlite3")

            store.save_run_state("run_1", True, {"wbs-2", "wbs-1"})
            state = store.load_run_state("run_1")
            all_states = store.load_run_state()

            self.assertEqual(state, {"run_id": "run_1", "paused": True, "checkpoint_paused_nodes": ["wbs-1", "wbs-2"]})
            self.assertEqual(all_states, [state])

    def test_existing_db_creates_run_state_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "legacy.sqlite3"
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE runs (id TEXT PRIMARY KEY,title TEXT NOT NULL,request TEXT NOT NULL,status TEXT NOT NULL,complexity_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,completed_at TEXT)")
            conn.commit()
            conn.close()

            store = CollabStore(db_path)
            tables = {row[0] for row in store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

            self.assertIn("run_state", tables)
            self.assertIn("context_snapshots", tables)

    def test_existing_wbs_table_migrates_checkpoint_column(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "legacy.sqlite3"
            conn = sqlite3.connect(db_path)
            conn.execute(
                "CREATE TABLE wbs_nodes ("
                "id TEXT PRIMARY KEY,"
                "run_id TEXT NOT NULL,"
                "parent_id TEXT,"
                "title TEXT NOT NULL,"
                "description TEXT NOT NULL,"
                "capability TEXT NOT NULL,"
                "complexity INTEGER NOT NULL,"
                "dependencies_json TEXT NOT NULL DEFAULT '[]',"
                "parallelizable INTEGER NOT NULL DEFAULT 1,"
                "deliverable TEXT NOT NULL,"
                "status TEXT NOT NULL,"
                "attempt INTEGER NOT NULL DEFAULT 1,"
                "result TEXT,"
                "session_id TEXT,"
                "duration_seconds REAL,"
                "error TEXT,"
                "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,"
                "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
                ")"
            )
            conn.execute(
                "INSERT INTO wbs_nodes(id,run_id,title,description,capability,complexity,deliverable,status) VALUES(?,?,?,?,?,?,?,?)",
                ("wbs-1", "run_1", "title", "desc", "implementation", 5, "deliver", "pending"),
            )
            conn.commit()
            conn.close()

            store = CollabStore(db_path)
            columns = {row[1] for row in store.conn.execute("PRAGMA table_info(wbs_nodes)").fetchall()}
            row = store.get_node("run_1", "wbs-1")

            self.assertIn("checkpoint", columns)
            self.assertEqual(row["checkpoint"], 0)


if __name__ == "__main__":
    unittest.main()
