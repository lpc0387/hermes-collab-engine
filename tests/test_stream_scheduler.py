from __future__ import annotations

import threading
import tempfile
import time
import unittest
from pathlib import Path

from src.hermes_collab_engine.engine import CollabEngine
from src.hermes_collab_engine.models import ComplexityScore, Plan, WBSNode, WorkerResult


def node(node_id: str, deps: list[str] | None = None) -> WBSNode:
    return WBSNode(node_id, node_id, f"Task {node_id}", "verification", 3, deps or [], True, f"Deliver {node_id}")


class StreamSchedulerTests(unittest.TestCase):
    def make_engine(self) -> CollabEngine:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        engine = CollabEngine(Path(tmp.name) / "db.sqlite3", tmp.name)
        engine.planner.assess = lambda request: ComplexityScore(5, 5, 5, 5, 5, 7, "wbs")
        return engine

    def test_independent_nodes_start_concurrently(self) -> None:
        engine = self.make_engine()
        nodes = [node("wbs-1"), node("wbs-2"), node("wbs-3")]
        engine.planner.decompose = lambda request, **kw: Plan(nodes=nodes)
        start_gate = threading.Barrier(3)
        started: list[str] = []
        lock = threading.Lock()

        def fake_run_worker(run_id, current, timeout, model_override=None):
            with lock:
                started.append(current.id)
            start_gate.wait(timeout=2)
            return WorkerResult(current.id, current.title, True, f"ok {current.id}", None, 0.01, 0, "", current.attempt)

        engine._run_worker = fake_run_worker

        result = engine.run("parallel", concurrency=3, aggregate=False)

        self.assertTrue(result["ok"])
        self.assertCountEqual(started, ["wbs-1", "wbs-2", "wbs-3"])

    def test_serial_fallback_avoids_dependency_deadlock(self) -> None:
        engine = self.make_engine()
        nodes = [node("wbs-1", ["wbs-2"]), node("wbs-2", ["wbs-1"])]
        engine.planner.decompose = lambda request, **kw: Plan(nodes=nodes)
        calls: list[str] = []

        def fake_run_worker(run_id, current, timeout, model_override=None):
            calls.append(current.id)
            return WorkerResult(current.id, current.title, True, f"ok {current.id}", None, 0.01, 0, "", current.attempt)

        engine._run_worker = fake_run_worker

        result = engine.run("deadlock", concurrency=2, aggregate=False)

        self.assertTrue(result["ok"])
        self.assertEqual(calls, ["wbs-1", "wbs-2"])
        logs = engine.store._query("SELECT message FROM logs WHERE level='warning' ORDER BY id")
        self.assertIn("dependency deadlock avoided", [row["message"] for row in logs])

    def test_write_targets_do_not_overlap_concurrently(self) -> None:
        engine = self.make_engine()
        nodes = [
            WBSNode("wbs-1", "one", "Task one", "implementation", 3, [], True, "Deliver one", write_targets=["src"]),
            WBSNode("wbs-2", "two", "Task two", "implementation", 3, [], True, "Deliver two", write_targets=["src/pkg"]),
            WBSNode("wbs-3", "three", "Task three", "implementation", 3, [], True, "Deliver three", write_targets=["docs"]),
        ]
        engine.planner.decompose = lambda request, **kw: Plan(nodes=nodes)
        src_running = threading.Event()
        allow_src_finish = threading.Event()
        overlapping: list[str] = []
        started: list[str] = []
        lock = threading.Lock()

        def fake_run_worker(run_id, current, timeout, model_override=None):
            with lock:
                started.append(current.id)
                if current.id == "wbs-2" and src_running.is_set():
                    overlapping.append(current.id)
            if current.id == "wbs-1":
                src_running.set()
                allow_src_finish.wait(timeout=2)
                src_running.clear()
            return WorkerResult(current.id, current.title, True, f"ok {current.id}", None, 0.01, 0, "", current.attempt)

        engine._run_worker = fake_run_worker

        runner = threading.Thread(target=lambda: engine.run("writes", concurrency=3, aggregate=False))
        runner.start()
        self.assertTrue(src_running.wait(timeout=2))
        time.sleep(0.05)
        with lock:
            self.assertIn("wbs-3", started)
            self.assertNotIn("wbs-2", started)
        allow_src_finish.set()
        runner.join(timeout=2)

        self.assertFalse(runner.is_alive())
        self.assertEqual(overlapping, [])
        self.assertIn("wbs-2", started)

    def test_duplicate_ready_node_is_killed_before_worker_launch(self) -> None:
        engine = self.make_engine()
        nodes = [
            WBSNode("wbs-1", "one", "same work", "analysis", 3, [], True, "Deliver one", fingerprint="same"),
            WBSNode("wbs-2", "two", "same work", "analysis", 3, [], True, "Deliver two", fingerprint="same"),
        ]
        engine.planner.decompose = lambda request, **kw: Plan(nodes=nodes)
        first_running = threading.Event()
        allow_first_finish = threading.Event()
        calls: list[str] = []

        def fake_run_worker(run_id, current, timeout, model_override=None):
            calls.append(current.id)
            first_running.set()
            allow_first_finish.wait(timeout=2)
            return WorkerResult(current.id, current.title, True, f"ok {current.id}", None, 0.01, 0, "", current.attempt)

        engine._run_worker = fake_run_worker

        runner_result = {}
        runner = threading.Thread(target=lambda: runner_result.update(engine.run("dupes", concurrency=2, aggregate=False)))
        runner.start()
        self.assertTrue(first_running.wait(timeout=2))
        time.sleep(0.05)
        allow_first_finish.set()
        runner.join(timeout=2)

        self.assertFalse(runner.is_alive())
        self.assertTrue(runner_result["ok"])
        self.assertEqual(calls, ["wbs-1"])
        self.assertEqual([result["node_id"] for result in runner_result["results"]], ["wbs-2", "wbs-1"])
        logs = engine.store._query("SELECT message FROM logs WHERE level='warning' ORDER BY id")
        self.assertIn("duplicate worker killed before launch", [row["message"] for row in logs])

    def test_dependency_chaining_runs_children_after_parent_completes(self) -> None:
        engine = self.make_engine()
        nodes = [node("wbs-1"), node("wbs-2", ["wbs-1"]), node("wbs-3", ["wbs-1"])]
        engine.planner.decompose = lambda request, **kw: Plan(nodes=nodes)
        parent_finished = threading.Event()
        child_started_before_parent_finished: list[str] = []
        started: list[str] = []
        lock = threading.Lock()

        def fake_run_worker(run_id, current, timeout, model_override=None):
            with lock:
                started.append(current.id)
                if current.id != "wbs-1" and not parent_finished.is_set():
                    child_started_before_parent_finished.append(current.id)
            if current.id == "wbs-1":
                time.sleep(0.05)
                parent_finished.set()
            return WorkerResult(current.id, current.title, True, f"ok {current.id}", None, 0.01, 0, "", current.attempt)

        engine._run_worker = fake_run_worker

        result = engine.run("deps", concurrency=3, aggregate=False)

        self.assertTrue(result["ok"])
        self.assertEqual(started[0], "wbs-1")
        self.assertEqual(child_started_before_parent_finished, [])
        self.assertCountEqual(started[1:], ["wbs-2", "wbs-3"])


if __name__ == "__main__":
    unittest.main()
