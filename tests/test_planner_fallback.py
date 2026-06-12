from __future__ import annotations

from pathlib import Path

from hermes_collab_engine.planner import Planner


def test_fallback_wbs_short_request_has_three_serial_nodes():
    planner = Planner(cwd=Path("."))
    request = "给 README 增加一段安装说明"
    nodes = planner.fallback_wbs(request)

    assert [n.id for n in nodes] == ["wbs-1", "wbs-2", "wbs-verify"]
    assert [n.dependencies for n in nodes] == [[], ["wbs-1"], ["wbs-2"]]
    assert set(n.capability for n in nodes) == {"analysis", "implementation", "verification"}
    for node in nodes:
        assert request in node.description


def test_fallback_wbs_long_request_has_four_serial_nodes_with_planning():
    planner = Planner(cwd=Path("."))
    request = "需要重新梳理协同引擎的复杂度评估、WBS 拆解、并行调度、SQLite 持久化、" * 6
    nodes = planner.fallback_wbs(request)

    assert [n.id for n in nodes] == ["wbs-1", "wbs-2", "wbs-3", "wbs-verify"]
    assert [n.dependencies for n in nodes] == [[], ["wbs-1"], ["wbs-2"], ["wbs-3"]]
    assert [n.capability for n in nodes] == ["analysis", "planning", "implementation", "verification"]
    head = request[:200]
    for node in nodes:
        assert head in node.description


def test_fallback_wbs_truncates_oversized_request():
    planner = Planner(cwd=Path("."))
    request = "x" * 5000
    nodes = planner.fallback_wbs(request)

    for node in nodes:
        assert "x" * 1500 in node.description
        assert "…" in node.description
        assert request not in node.description
