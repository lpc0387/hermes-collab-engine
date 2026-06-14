import tempfile
import unittest
from pathlib import Path

from src.hermes_collab_engine.engine import CollabEngine
from src.hermes_collab_engine.models import ComplexityScore, WBSNode, WorkerResult
from src.hermes_collab_engine.store import CollabStore


class InterruptCleanupTest(unittest.TestCase):
    def test_keyboard_interrupt_marks_running_work_failed(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "collab.sqlite3"
            engine = CollabEngine(db, td)
            nodes = [
                WBSNode("WBS-01", "first", "first task", "general", 1, [], True, "first result"),
                WBSNode("WBS-02", "second", "second task", "general", 1, [], True, "second result"),
            ]
            engine.planner.assess = lambda request: ComplexityScore(1, 1, 1, 1, 1, 1, "wbs")
            engine.planner.decompose = lambda request, **kw: nodes

            def fake_run_worker(run_id, node, timeout, model_override=None):
                worker_id = f"worker_{run_id}_{node.id}_{node.attempt}"
                engine.store.worker_start(worker_id, run_id, node.id)
                engine.store.update_node(node.id, "running")
                if node.id == "WBS-02":
                    raise KeyboardInterrupt()
                engine.store.worker_finish(worker_id, "completed", 0.1, "session-ok", None)
                return WorkerResult(node.id, node.title, True, "ok", "session-ok", 0.1, 0, "", node.attempt)

            engine._run_worker = fake_run_worker

            with self.assertRaises(KeyboardInterrupt):
                engine.run("interrupt me", concurrency=1, aggregate=False)

            store = CollabStore(db)
            overview = store.overview()
            self.assertEqual(overview["running"], 0)
            self.assertEqual(overview["workers_running"], 0)

            runs = store.list_runs()
            self.assertEqual(runs[0]["status"], "failed")
            self.assertIsNotNone(runs[0]["completed_at"])

            detail = store.run_detail(runs[0]["id"])
            node_statuses = {n["id"]: n["status"] for n in detail["nodes"]}
            self.assertEqual(node_statuses["WBS-01"], "completed")
            self.assertEqual(node_statuses["WBS-02"], "failed")
            lessons = store.lessons()
            self.assertEqual(lessons[0]["category"], "interrupt-cleanup")
            self.assertIn("ghost-running", lessons[0]["lesson"])


if __name__ == "__main__":
    unittest.main()
