"""SQLite storage layer for todo items.

Provides CRUD operations backed by a local SQLite database.
The database file path can be configured via the constructor.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable

from todo.models import Task

CREATE_TASKS_SQL = """\
CREATE TABLE IF NOT EXISTS tasks (
    id         TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    completed  INTEGER NOT NULL DEFAULT 0,
    priority   TEXT NOT NULL DEFAULT 'medium',
    created_at TEXT NOT NULL
)
"""

# Migration: add priority column to existing databases (safe no-op if already present)
ALTER_ADD_PRIORITY_SQL = (
    "ALTER TABLE tasks ADD COLUMN priority TEXT NOT NULL DEFAULT 'medium'"
)

INSERT_TASK_SQL = "INSERT INTO tasks (id, title, completed, priority, created_at) VALUES (?, ?, ?, ?, ?)"
SELECT_ALL_SQL = "SELECT id, title, completed, priority, created_at FROM tasks ORDER BY created_at"
SELECT_BY_ID_SQL = "SELECT id, title, completed, priority, created_at FROM tasks WHERE id = ?"
UPDATE_TASK_SQL = "UPDATE tasks SET title = ?, completed = ?, priority = ? WHERE id = ?"
DELETE_TASK_SQL = "DELETE FROM tasks WHERE id = ?"


class StorageError(Exception):
    """Raised when a storage operation fails."""


class NotFoundError(StorageError):
    """Raised when a task is not found."""


class TaskStore:
    """SQLite-backed persistent store for Task objects."""

    def __init__(self, db_path: str | Path = "todo.db") -> None:
        self._db_path = Path(db_path)
        self._connection: sqlite3.Connection | None = None

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def connect(self) -> sqlite3.Connection:
        """Open (or reuse) a connection and ensure the schema exists."""
        if self._connection is None:
            self._connection = sqlite3.connect(str(self._db_path))
            self._connection.row_factory = sqlite3.Row
            self._connection.execute(CREATE_TASKS_SQL)
            # Safe migration: add priority column if missing
            try:
                self._connection.execute(ALTER_ADD_PRIORITY_SQL)
            except sqlite3.OperationalError:
                pass  # column already exists
            self._connection.commit()
        return self._connection

    def close(self) -> None:
        """Close the database connection if open."""
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    # ------------------------------------------------------------------
    # CRUD operations
    # ------------------------------------------------------------------

    def add(self, task: Task) -> Task:
        """Insert a new task. Raises StorageError on duplicate id."""
        conn = self.connect()
        try:
            conn.execute(
                INSERT_TASK_SQL,
                (task.id, task.title, int(task.completed), task.priority, task.created_at),
            )
            conn.commit()
        except sqlite3.IntegrityError as exc:
            raise StorageError(f"Task with id '{task.id}' already exists") from exc
        return task

    def get(self, task_id: str) -> Task:
        """Retrieve a single task by id. Raises NotFoundError if missing."""
        conn = self.connect()
        row = conn.execute(SELECT_BY_ID_SQL, (task_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"Task '{task_id}' not found")
        return self._row_to_task(row)

    def list_all(self) -> list[Task]:
        """Return all tasks ordered by creation time."""
        conn = self.connect()
        rows = conn.execute(SELECT_ALL_SQL).fetchall()
        return [self._row_to_task(r) for r in rows]

    def update(self, task: Task) -> Task:
        """Update an existing task. Raises NotFoundError if missing."""
        conn = self.connect()
        cursor = conn.execute(UPDATE_TASK_SQL, (task.title, int(task.completed), task.priority, task.id))
        conn.commit()
        if cursor.rowcount == 0:
            raise NotFoundError(f"Task '{task.id}' not found")
        return task

    def delete(self, task_id: str) -> None:
        """Delete a task by id. Raises NotFoundError if missing."""
        conn = self.connect()
        cursor = conn.execute(DELETE_TASK_SQL, (task_id,))
        conn.commit()
        if cursor.rowcount == 0:
            raise NotFoundError(f"Task '{task_id}' not found")

    def count(self) -> int:
        """Return the total number of tasks."""
        conn = self.connect()
        row = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()
        return row[0] if row else 0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> Task:
        priority_raw = row["priority"] if "priority" in row.keys() else "medium"
        return Task(
            id=row["id"],
            title=row["title"],
            completed=bool(row["completed"]),
            priority=str(priority_raw),
            created_at=row["created_at"],
        )
