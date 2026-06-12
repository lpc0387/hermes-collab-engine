from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.hermes_collab_engine.engine import CollabEngine


class WorkerDualTrackParseTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.engine = CollabEngine(Path(self._tmp.name) / "db.sqlite3", self._tmp.name)

    def test_valid_contract_on_final_line(self) -> None:
        parsed, error = self.engine._parse_result_contract(
            'Human-readable result.\nHERMES-COLLAB-RESULT:{"status":"ok","summary":"done"}'
        )

        self.assertIsNone(error)
        self.assertEqual(parsed, {"status": "ok", "summary": "done"})

    def test_valid_contract_in_json_fence(self) -> None:
        parsed, error = self.engine._parse_result_contract(
            'Human-readable result.\nHERMES-COLLAB-RESULT:\n```json\n{"status":"ok","summary":"done"}\n```'
        )

        self.assertIsNone(error)
        self.assertEqual(parsed, {"status": "ok", "summary": "done"})

    def test_missing_contract_marker_reports_error(self) -> None:
        parsed, error = self.engine._parse_result_contract("plain worker output only")

        self.assertIsNone(parsed)
        self.assertEqual(error, "missing HERMES-COLLAB-RESULT marker")

    def test_invalid_json_reports_error(self) -> None:
        parsed, error = self.engine._parse_result_contract(
            "result\nHERMES-COLLAB-RESULT:{not-json}"
        )

        self.assertIsNone(parsed)
        self.assertIsNotNone(error)
        self.assertIn("invalid HERMES-COLLAB-RESULT JSON", error)

    def test_partial_empty_json_payload_reports_error(self) -> None:
        parsed, error = self.engine._parse_result_contract("result\nHERMES-COLLAB-RESULT:   ")

        self.assertIsNone(parsed)
        self.assertEqual(error, "empty HERMES-COLLAB-RESULT payload")

    def test_non_object_json_payload_reports_error(self) -> None:
        parsed, error = self.engine._parse_result_contract("HERMES-COLLAB-RESULT:[1, 2, 3]")

        self.assertIsNone(parsed)
        self.assertEqual(error, "HERMES-COLLAB-RESULT payload is not an object")

    def test_last_marker_wins_when_output_contains_multiple_markers(self) -> None:
        parsed, error = self.engine._parse_result_contract(
            'HERMES-COLLAB-RESULT:{"status":"failed"}\n'
            'real final line\nHERMES-COLLAB-RESULT:{"status":"ok","summary":"last"}'
        )

        self.assertIsNone(error)
        self.assertEqual(parsed, {"status": "ok", "summary": "last"})


if __name__ == "__main__":
    unittest.main()
