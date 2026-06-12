from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.hermes_collab_engine.engine import CollabEngine
from src.hermes_collab_engine.models import Plan, WBSNode


def _extract_prompt(call_args) -> str:
    args, _kwargs = call_args
    argv = args[0]
    return argv[argv.index("-p") + 1]


class ImplementationOnlyBriefTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.engine = CollabEngine(db_path=Path(self._tmp.name) / "db.sqlite3", cwd=self._tmp.name)
        self.analysis = WBSNode(
            "wbs-1",
            "Analyze",
            "Analyze the request.",
            "analysis",
            3,
            [],
            True,
            "Analysis",
            brief="analysis-only",
        )
        self.implementation = WBSNode(
            "wbs-2",
            "Implement",
            "Implement the request.",
            "implementation",
            5,
            ["wbs-1"],
            False,
            "Patch",
            brief="implementation-only",
        )
        self.verification = WBSNode(
            "wbs-3",
            "Verify",
            "Verify the request.",
            "verification",
            3,
            ["wbs-2"],
            False,
            "Report",
            brief="verification-only",
        )
        self.engine._current_plan = Plan(
            nodes=[self.analysis, self.implementation, self.verification],
            shared_brief="shared implementation context",
        )

    def _prompt_for(self, node: WBSNode) -> str:
        completed = subprocess.CompletedProcess(
            args=["claude"],
            returncode=0,
            stdout='{"result":"ok","session_id":"s1","is_error":false}',
            stderr="",
        )
        with patch("src.hermes_collab_engine.engine.subprocess.run", return_value=completed) as mock_run:
            self.engine._run_worker("run_test", node, timeout=30)
            return _extract_prompt(mock_run.call_args)

    def test_shared_brief_is_in_implementation_prompt(self) -> None:
        prompt = self._prompt_for(self.implementation)

        self.assertIn("Shared brief:\nshared implementation context", prompt)
        self.assertIn("Brief:\nimplementation-only", prompt)

    def test_shared_brief_is_not_in_analysis_prompt(self) -> None:
        prompt = self._prompt_for(self.analysis)

        self.assertNotIn("Shared brief:\nshared implementation context", prompt)
        self.assertIn("Brief:\nanalysis-only", prompt)

    def test_shared_brief_is_not_in_verification_prompt(self) -> None:
        prompt = self._prompt_for(self.verification)

        self.assertNotIn("Shared brief:\nshared implementation context", prompt)
        self.assertIn("Brief:\nverification-only", prompt)


if __name__ == "__main__":
    unittest.main()
