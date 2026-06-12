from __future__ import annotations

from dataclasses import dataclass, asdict, field, fields
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
    checkpoint: bool = False
    attempt: int = 1
    brief: str = ""
    estimated_duration: int | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WBSNode":
        defaults = {
            "status": "pending",
            "parent_id": None,
            "checkpoint": False,
            "attempt": 1,
            "brief": "",
            "estimated_duration": None,
        }
        return cls(**{**defaults, **data})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RiskPolicy:
    low: str = "continue"
    medium: str = "checkpoint"
    high: str = "checkpoint"
    checkpoint_timeout: int = 900

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "RiskPolicy":
        data = data or {}
        defaults = cls()
        names = {field.name for field in fields(cls)}
        values = {name: data.get(name, getattr(defaults, name)) for name in names}
        return cls(**values)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CheckpointDecision:
    run_id: str
    node_id: str
    action: str
    reason: str = ""

    def __post_init__(self) -> None:
        allowed = {"continue", "redo", "skip_downstream", "abort"}
        if self.action not in allowed:
            raise ValueError(f"action must be one of {sorted(allowed)}")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CheckpointDecision":
        return cls(**data)

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
    risk_policy: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Plan":
        nodes = [node if isinstance(node, WBSNode) else WBSNode.from_dict(node) for node in data.get("nodes", [])]
        return cls(
            nodes=nodes,
            shared_brief=data.get("shared_brief", ""),
            risk_policy=data.get("risk_policy", {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
