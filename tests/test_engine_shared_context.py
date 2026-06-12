"""Tests for upstream context plumbing through CollabEngine._run_worker."""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.hermes_collab_engine.engine import CollabEngine
from src.hermes_collab_engine.models import WBSNode


def make_node(
    node_id: str = "wbs-2",
    title: str = "Downstream node",
    description: str = "Do downstream work.",
    capability: str = "testing",
    complexity: int = 3,
    dependencies: list[str] | None = None,
    parallelizable: bool = True,
    deliverable: str = "downstream deliverable",
    wbs_index: int | None = None,
) -> WBSNode:
    if wbs_index is not None and dependencies is None:
        dependencies = [f"wbs-{wbs_index - 1}"] if wbs_index > 1 else []
    return WBSNode(
        id=node_id,
        title=title,
        description=description,
        capability=capability,
        complexity=complexity,
        dependencies=list(dependencies) if dependencies is not None else [],
        parallelizable=parallelizable,
        deliverable=deliverable,
    )


def _extract_prompt(call_args) -> str:
    """Find the prompt string in the subprocess.run call argv."""
    args, _kwargs = call_args
    argv = args[0]
    # cmd = ["claude", "-p", prompt, ...] — prompt follows the "-p" flag
    for idx, token in enumerate(argv):
        if token == "-p" and idx + 1 < len(argv):
            return argv[idx + 1]
    # Fallback: longest string argument is almost certainly the prompt.
    return max((a for a in argv if isinstance(a, str)), key=len)


class EngineSharedContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        db_path = Path(self._tmp.name) / "db.sqlite3"
        self.engine = CollabEngine(db_path=str(db_path), cwd=Path("."))

    def _run_with_mock(self, node: WBSNode) -> str:
        completed = subprocess.CompletedProcess(
            args=["claude"], returncode=0, stdout='{"result":"ok"}', stderr=""
        )
        with patch(
            "src.hermes_collab_engine.engine.subprocess.run",
            return_value=completed,
        ) as mock_run:
            self.engine._run_worker("run_test", node, timeout=10)
            self.assertTrue(mock_run.called, "subprocess.run was not invoked")
            return _extract_prompt(mock_run.call_args)

    def test_upstream_context_included_when_parent_result_present(self) -> None:
        self.engine._node_results["wbs-1"] = "PARENT_RESULT_PAYLOAD"
        node = make_node(node_id="wbs-2", dependencies=["wbs-1"])

        prompt = self._run_with_mock(node)

        self.assertIn("Upstream context", prompt)
        self.assertIn("PARENT_RESULT_PAYLOAD", prompt)
        self.assertIn("from wbs-1", prompt)

    def test_wbs_index_param_builds_previous_node_dependency(self) -> None:
        self.engine._node_results["wbs-1"] = "INDEX_PARENT_PAYLOAD"
        node = make_node(node_id="wbs-2", wbs_index=2)

        prompt = self._run_with_mock(node)

        self.assertEqual(node.dependencies, ["wbs-1"])
        self.assertIn("Upstream context", prompt)
        self.assertIn("INDEX_PARENT_PAYLOAD", prompt)
        self.assertIn("from wbs-1", prompt)

    def test_no_upstream_context_when_results_empty(self) -> None:
        # dependencies declared but no recorded results from upstream
        node = make_node(node_id="wbs-2", dependencies=["wbs-1"])
        self.assertEqual(self.engine._node_results, {})

        prompt = self._run_with_mock(node)

        self.assertNotIn("Upstream context", prompt)

    def test_wbs_index_one_keeps_empty_dependencies_for_backward_compat(self) -> None:
        node = make_node(node_id="wbs-1", wbs_index=1)

        prompt = self._run_with_mock(node)

        self.assertEqual(node.dependencies, [])
        self.assertNotIn("Upstream context", prompt)

    def test_no_upstream_context_when_no_dependencies(self) -> None:
        # Even if some results happen to be present, a depless node should not pull context.
        self.engine._node_results["wbs-1"] = "SHOULD_NOT_LEAK"
        node = make_node(node_id="wbs-solo", dependencies=[])

        prompt = self._run_with_mock(node)

        self.assertNotIn("Upstream context", prompt)
        self.assertNotIn("SHOULD_NOT_LEAK", prompt)

    def test_upstream_context_truncated_keeps_tail(self) -> None:
        # Per-dep cap small enough to force truncation; tail must be kept.
        self.engine._UPSTREAM_PER_CAP = 100
        self.engine._node_results["wbs-1"] = ("A" * 500) + "TAIL_MARKER_END"
        node = make_node(node_id="wbs-2", dependencies=["wbs-1"])

        prompt = self._run_with_mock(node)

        self.assertIn("Upstream context", prompt)
        self.assertIn("[truncated]", prompt)
        self.assertIn("TAIL_MARKER_END", prompt)


if __name__ == "__main__":
    unittest.main()
