from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.hermes_collab_engine.engine import CollabEngine
from src.hermes_collab_engine.models import Plan, WBSNode


def _extract_prompt_from_cmd(cmd: list[str]) -> str:
    """Find the prompt string in a subprocess command list.

    opencode backend: prompt is positional arg (index 2 after ['opencode', 'run'])
    For backends with -p flag, prompt follows the flag.
    """
    for idx, token in enumerate(cmd):
        if token == "-p" and idx + 1 < len(cmd):
            return cmd[idx + 1]
    # Fallback: opencode-style positional prompt
    if len(cmd) >= 3 and cmd[0] == "opencode" and cmd[1] == "run":
        return cmd[2]
    # Last resort: longest string arg is the prompt
    return max((a for a in cmd if isinstance(a, str)), key=len)


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
        """Run _run_worker with a mocked subprocess.Popen and extract the prompt."""
        mock_proc = MagicMock(spec=subprocess.Popen)
        mock_proc.poll.return_value = 0          # process already exited
        mock_proc.returncode = 0
        mock_proc.stdout = MagicMock()
        mock_proc.stdout.readline.side_effect = [""]
        mock_proc.stderr = MagicMock()
        mock_proc.stderr.readline.side_effect = [""]
        mock_proc.communicate.return_value = ("", "")

        with patch(
            "src.hermes_collab_engine.engine.subprocess.Popen",
            return_value=mock_proc,
        ) as mock_popen:
            self.engine._run_worker("run_test", node, timeout=30)
            self.assertTrue(mock_popen.called, "subprocess.Popen was not invoked")
            cmd_list = mock_popen.call_args[0][0]
            return _extract_prompt_from_cmd(cmd_list)

    def test_shared_brief_is_in_implementation_prompt(self) -> None:
        prompt = self._prompt_for(self.implementation)

        self.assertIn("Completed work summary (upstream workers):\nshared implementation context", prompt)
        self.assertIn("Brief:\nimplementation-only", prompt)

    def test_shared_brief_is_not_in_analysis_prompt(self) -> None:
        prompt = self._prompt_for(self.analysis)

        self.assertNotIn("Completed work summary (upstream workers):\nshared implementation context", prompt)
        self.assertIn("Brief:\nanalysis-only", prompt)

    def test_shared_brief_is_not_in_verification_prompt(self) -> None:
        prompt = self._prompt_for(self.verification)

        self.assertNotIn("Completed work summary (upstream workers):\nshared implementation context", prompt)
        self.assertIn("Brief:\nverification-only", prompt)


if __name__ == "__main__":
    unittest.main()
