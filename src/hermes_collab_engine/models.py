from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any


@dataclass
class ComplexityScore:
    domain: int
    steps: int
    ambiguity: int
    coupling: int
    risk: int
    overall: int
    routing: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WBSNode:
    id: str
    title: str
    description: str
    capability: str
    complexity: int
    dependencies: list[str]
    parallelizable: bool
    deliverable: str
    status: str = "pending"
    parent_id: str | None = None
    attempt: int = 1
    brief: str = ""
    estimated_duration: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WorkerResult:
    node_id: str
    title: str
    ok: bool
    result: str
    session_id: str | None
    duration_seconds: float
    returncode: int
    stderr: str
    attempt: int
    result_struct: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Plan:
    nodes: list[WBSNode]
    shared_brief: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
