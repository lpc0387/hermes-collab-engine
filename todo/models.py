"""Task data model for the todo management system."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

# Valid priority levels
PRIORITY_LEVELS = ("low", "medium", "high")


def validate_priority(value: str) -> str:
    """Validate and normalize a priority string."""
    lowered = value.lower().strip()
    if lowered not in PRIORITY_LEVELS:
        raise ValueError(
            f"Invalid priority '{value}': must be one of {PRIORITY_LEVELS}"
        )
    return lowered


@dataclass
class Task:
    """Represents a single todo item.

    Attributes:
        id: Unique identifier (UUID string).
        title: Short description of the task.
        completed: Whether the task is done.
        priority: Task priority level (low, medium, high).
        created_at: ISO-8601 timestamp of creation.
    """

    title: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    completed: bool = False
    priority: str = "medium"
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def mark_done(self) -> None:
        """Mark the task as completed."""
        self.completed = True

    def to_dict(self) -> dict[str, str | bool]:
        """Serialize to a dictionary for storage."""
        return {
            "id": self.id,
            "title": self.title,
            "completed": self.completed,
            "priority": self.priority,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, str | bool]) -> Task:
        """Deserialize from a dictionary returned by storage."""
        priority_raw = data.get("priority", "medium")
        return cls(
            id=str(data["id"]),
            title=str(data["title"]),
            completed=bool(data["completed"]),
            priority=str(priority_raw) if isinstance(priority_raw, str) else "medium",
            created_at=str(data["created_at"]),
        )
