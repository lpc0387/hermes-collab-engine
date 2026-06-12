from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from hermes_collab_engine.models import ComplexityScore
from hermes_collab_engine.planner import Planner


def _make_planner() -> Planner:
    return Planner(cwd=Path("/tmp"), model=None, timeout=5)


def test_assess_returns_claude_score_when_available():
    planner = _make_planner()
    expected = ComplexityScore(8, 9, 7, 8, 9, 8, "wbs")
    with patch.object(Planner, "_claude_assess", return_value=expected):
        result = planner.assess("any request text")
    assert result == expected


def test_assess_falls_back_to_heuristic_when_claude_returns_none():
    planner = _make_planner()
    with patch.object(Planner, "_claude_assess", return_value=None):
        result = planner.assess("fix typo")
    assert isinstance(result, ComplexityScore)
    assert result.routing == "direct"


def test_assess_falls_back_to_heuristic_when_claude_raises():
    planner = _make_planner()
    with patch.object(Planner, "_claude_assess", side_effect=RuntimeError("boom")):
        result = planner.assess("实现 sqlite 持久化 worker 并集成 dashboard")
    assert isinstance(result, ComplexityScore)
    assert result.routing == "wbs"


def test_claude_assess_returns_score_from_valid_json():
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
    assert result == ComplexityScore(6, 4, 5, 7, 6, 6, "single")


def test_claude_assess_clamps_and_coerces_invalid_fields():
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
    assert result.domain == 10
    assert result.steps == 1
    assert result.ambiguity == 8
    assert result.coupling == 5
    assert result.overall == 10
    assert result.routing == "wbs"


def test_claude_assess_returns_none_for_malformed_payloads():
    planner = _make_planner()
    for bad in ({"not": "a list"}, [], ["string not dict"]):
        with patch.object(Planner, "_claude_json", return_value=bad):
            assert planner._claude_assess("any request") is None
