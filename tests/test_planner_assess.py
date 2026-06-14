from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from src.hermes_collab_engine.models import ComplexityScore
from src.hermes_collab_engine.planner import Planner


def _make_planner() -> Planner:
    return Planner(cwd=Path("/tmp"), model=None, timeout=5)


class PlannerAssessTests(unittest.TestCase):
    def test_assess_returns_claude_score_when_available(self) -> None:
        planner = _make_planner()
        expected = ComplexityScore(8, 9, 7, 8, 9, 8, "wbs")
        with patch.object(Planner, "_claude_assess", return_value=expected):
            result = planner.assess("implement scheduler dedup")
        self.assertEqual(result, expected)

    def test_assess_short_simple_request_skips_claude_and_routes_direct(self) -> None:
        planner = _make_planner()
        with patch.object(Planner, "_claude_assess", side_effect=AssertionError("should not call claude")):
            result = planner.assess("ping example.com")
        self.assertEqual(result.routing, "direct")
        self.assertLessEqual(result.overall, 3)

    def test_assess_falls_back_to_heuristic_when_claude_returns_none(self) -> None:
        planner = _make_planner()
        with patch.object(Planner, "_claude_assess", return_value=None):
            result = planner.assess("fix typo")
        self.assertIsInstance(result, ComplexityScore)
        self.assertEqual(result.routing, "direct")

    def test_assess_falls_back_to_heuristic_when_claude_raises(self) -> None:
        planner = _make_planner()
        with patch.object(Planner, "_claude_assess", side_effect=RuntimeError("boom")):
            result = planner.assess("实现 sqlite 持久化 worker 并集成 dashboard")
        self.assertIsInstance(result, ComplexityScore)
        self.assertEqual(result.routing, "wbs")

    def test_claude_assess_returns_score_from_valid_json(self) -> None:
        planner = _make_planner()
        payload = [{
            "domain": 6,
            "steps": 4,
            "ambiguity": 5,
            "coupling": 7,
            "risk": 6,
            "overall": 6,
            "routing": "single",
        }]
        with patch.object(Planner, "_claude_json", return_value=payload):
            result = planner._claude_assess("any request")
        self.assertEqual(result, ComplexityScore(6, 4, 5, 7, 6, 6, "single"))

    def test_claude_assess_clamps_and_coerces_invalid_fields(self) -> None:
        planner = _make_planner()
        payload = [{
            "domain": 99,
            "steps": -3,
            "ambiguity": "8",
            "coupling": None,
            "risk": 5,
            "overall": 20,
            "routing": "BOGUS",
        }]
        with patch.object(Planner, "_claude_json", return_value=payload):
            result = planner._claude_assess("any request")
        self.assertEqual(result.domain, 10)
        self.assertEqual(result.steps, 1)
        self.assertEqual(result.ambiguity, 8)
        self.assertEqual(result.coupling, 5)
        self.assertEqual(result.overall, 10)
        self.assertEqual(result.routing, "wbs")

    def test_claude_assess_returns_none_for_malformed_payloads(self) -> None:
        planner = _make_planner()
        for bad in ({"not": "a list"}, [], ["string not dict"]):
            with patch.object(Planner, "_claude_json", return_value=bad):
                self.assertIsNone(planner._claude_assess("any request"))


if __name__ == "__main__":
    unittest.main()
