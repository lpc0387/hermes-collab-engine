import tempfile
import unittest
from pathlib import Path

from src.hermes_collab_engine.store import CollabStore


class DashboardRunDetailPayloadTest(unittest.TestCase):
    def test_compact_run_detail_omits_heavy_node_fields_and_limits_logs(self):
        with tempfile.TemporaryDirectory() as td:
            store = CollabStore(Path(td) / "collab.sqlite3")
            store.create_run("run_1", "title", "request", {})
            store.insert_wbs_node(
                "run_1",
                {
                    "id": "wbs-1",
                    "title": "node",
                    "description": "large description" * 100,
                    "capability": "implementation",
                    "complexity": 1,
                    "dependencies": [],
                    "parallelizable": True,
                    "deliverable": "code",
                    "brief": "brief",
                    "shared_brief": "shared",
                    "status": "completed",
                },
            )
            store.update_node_result("run_1", "wbs-1", "large result" * 1000)
            store.worker_start("worker-1", "run_1", "wbs-1")
            store.log("run_1", "info", "first", {"result": "large log result" * 1000})
            store.log("run_1", "info", "second", {"result": "large log result" * 1000})

            compact = store.run_detail("run_1", full=False, log_limit=1, include_workers=False)
            full = store.run_detail("run_1")
            recent_logs = store.recent_logs()

            self.assertNotIn("request", compact["run"])
            self.assertEqual(len(compact["nodes"]), 1)
            self.assertNotIn("description", compact["nodes"][0])
            self.assertNotIn("result", compact["nodes"][0])
            self.assertEqual(compact["workers"], [])
            self.assertEqual(len(compact["logs"]), 1)
            self.assertEqual(compact["logs"][0]["message"], "second")
            self.assertNotIn("data_json", compact["logs"][0])
            self.assertIn("description", full["nodes"][0])
            self.assertEqual(full["nodes"][0]["result"], "large result" * 1000)
            self.assertEqual(len(full["workers"]), 1)
            self.assertIn("data_json", full["logs"][0])
            self.assertNotIn("data_json", recent_logs[0])

    def test_update_node_can_be_scoped_to_run_for_reused_node_ids(self):
        with tempfile.TemporaryDirectory() as td:
            store = CollabStore(Path(td) / "collab.sqlite3")
            for run_id in ("run_1", "run_2"):
                store.create_run(run_id, "title", "request", {})
                store.insert_wbs_node(
                    run_id,
                    {
                        "id": "wbs-1",
                        "title": "node",
                        "description": "description",
                        "capability": "implementation",
                        "complexity": 1,
                        "dependencies": [],
                        "parallelizable": True,
                        "deliverable": "code",
                    },
                )

            store.update_node("wbs-1", "completed", "run 1 result", run_id="run_1")

            run_1 = store.get_node("run_1", "wbs-1")
            run_2 = store.get_node("run_2", "wbs-1")
            self.assertEqual(run_1["status"], "completed")
            self.assertEqual(run_1["result"], "run 1 result")
            self.assertEqual(run_2["status"], "pending")
            self.assertIsNone(run_2["result"])


if __name__ == "__main__":
    unittest.main()
