"""Unit tests for the todo management system.

Run with:  pytest tests/test_todo.py -v
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from todo.models import PRIORITY_LEVELS, Task, validate_priority
from todo.storage import NotFoundError, StorageError, TaskStore


# ======================================================================
# models.py — Task dataclass
# ======================================================================


class TestTaskModel:
    def test_create_defaults(self) -> None:
        """A Task created with only a title gets auto-generated id & timestamp."""
        t = Task(title="Buy milk")
        assert t.title == "Buy milk"
        assert t.id is not None and len(t.id) == 12
        assert t.completed is False
        assert t.priority == "medium"
        assert t.created_at is not None  # ISO-8601 string

    def test_create_explicit_fields(self) -> None:
        t = Task(
            title="Write docs",
            id="abc123",
            completed=True,
            priority="high",
            created_at="2025-01-01T00:00:00",
        )
        assert t.title == "Write docs"
        assert t.id == "abc123"
        assert t.completed is True
        assert t.priority == "high"
        assert t.created_at == "2025-01-01T00:00:00"

    def test_mark_done(self) -> None:
        t = Task(title="Test")
        assert t.completed is False
        t.mark_done()
        assert t.completed is True

    def test_to_dict(self) -> None:
        t = Task(title="Dict test", id="x1")
        d = t.to_dict()
        assert d["id"] == "x1"
        assert d["title"] == "Dict test"
        assert d["completed"] is False
        assert d["priority"] == "medium"
        assert "created_at" in d

    def test_from_dict(self) -> None:
        data = {
            "id": "abc",
            "title": "From dict",
            "completed": True,
            "created_at": "2025-06-01T12:00:00",
        }
        t = Task.from_dict(data)
        assert t.id == "abc"
        assert t.title == "From dict"
        assert t.completed is True
        assert t.priority == "medium"  # fallback default
        assert t.created_at == "2025-06-01T12:00:00"

    def test_from_dict_with_priority(self) -> None:
        data = {
            "id": "xyz",
            "title": "Urgent",
            "completed": False,
            "priority": "high",
            "created_at": "2025-06-01T12:00:00",
        }
        t = Task.from_dict(data)
        assert t.priority == "high"

    def test_roundtrip_dict(self) -> None:
        original = Task(title="Roundtrip")
        d = original.to_dict()
        restored = Task.from_dict(d)
        assert restored.title == original.title
        assert restored.id == original.id
        assert restored.completed == original.completed
        assert restored.priority == original.priority
        assert restored.created_at == original.created_at

    def test_priority_default_medium(self) -> None:
        t = Task(title="Default priority")
        assert t.priority == "medium"

    def test_priority_custom_values(self) -> None:
        assert Task(title="L", priority="low").priority == "low"
        assert Task(title="M", priority="medium").priority == "medium"
        assert Task(title="H", priority="high").priority == "high"

    def test_validate_priority_valid(self) -> None:
        for p in ("low", "medium", "high"):
            assert validate_priority(p) == p
        assert validate_priority("LOW") == "low"
        assert validate_priority("  High  ") == "high"

    def test_validate_priority_invalid(self) -> None:
        with pytest.raises(ValueError, match="Invalid priority"):
            validate_priority("urgent")
        with pytest.raises(ValueError, match="Invalid priority"):
            validate_priority("")


# ======================================================================
# storage.py — TaskStore (SQLite)
# ======================================================================


@pytest.fixture
def store() -> TaskStore:
    """Create a TaskStore backed by a temporary database file."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    s = TaskStore(db_path=tmp.name)
    s.connect()  # ensure schema is created
    yield s
    s.close()
    Path(tmp.name).unlink(missing_ok=True)


class TestTaskStore:
    def test_add_and_get(self, store: TaskStore) -> None:
        t = Task(title="Test task")
        stored = store.add(t)
        assert stored.id == t.id

        fetched = store.get(t.id)
        assert fetched.title == "Test task"
        assert fetched.completed is False

    def test_add_duplicate_id(self, store: TaskStore) -> None:
        t = Task(title="Dupe", id="same-id")
        store.add(t)
        with pytest.raises(StorageError, match="already exists"):
            store.add(Task(title="Also dupe", id="same-id"))

    def test_get_not_found(self, store: TaskStore) -> None:
        with pytest.raises(NotFoundError, match="not found"):
            store.get("nonexistent")

    def test_list_all_empty(self, store: TaskStore) -> None:
        assert store.list_all() == []

    def test_list_all(self, store: TaskStore) -> None:
        t1 = Task(title="First")
        t2 = Task(title="Second")
        store.add(t1)
        store.add(t2)
        tasks = store.list_all()
        assert len(tasks) == 2
        assert tasks[0].title == "First"

    def test_update(self, store: TaskStore) -> None:
        t = Task(title="Original")
        store.add(t)

        t.title = "Updated"
        t.mark_done()
        store.update(t)

        fetched = store.get(t.id)
        assert fetched.title == "Updated"
        assert fetched.completed is True

    def test_update_not_found(self, store: TaskStore) -> None:
        t = Task(title="Orphan", id="ghost")
        with pytest.raises(NotFoundError, match="not found"):
            store.update(t)

    def test_delete(self, store: TaskStore) -> None:
        t = Task(title="Delete me")
        store.add(t)
        store.delete(t.id)
        assert store.count() == 0
        with pytest.raises(NotFoundError):
            store.get(t.id)

    def test_delete_not_found(self, store: TaskStore) -> None:
        with pytest.raises(NotFoundError, match="not found"):
            store.delete("nonexistent")

    def test_count(self, store: TaskStore) -> None:
        assert store.count() == 0
        store.add(Task(title="A"))
        assert store.count() == 1
        store.add(Task(title="B"))
        assert store.count() == 2

    def test_multiple_stores_isolated(self) -> None:
        """Each TaskStore uses its own database file."""
        tmp1 = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp2 = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp1.close()
        tmp2.close()
        p1, p2 = Path(tmp1.name), Path(tmp2.name)

        s1 = TaskStore(db_path=p1)
        s2 = TaskStore(db_path=p2)
        s1.connect()
        s2.connect()

        s1.add(Task(title="Only in s1"))
        assert s2.count() == 0

        s1.close()
        s2.close()
        p1.unlink(missing_ok=True)
        p2.unlink(missing_ok=True)

    def test_connect_idempotent(self, store: TaskStore) -> None:
        """Calling connect() multiple times reuses the same connection."""
        c1 = store.connect()
        c2 = store.connect()
        assert c1 is c2

    def test_close_reconnect(self, store: TaskStore) -> None:
        """After close, the next operation opens a new connection."""
        store.add(Task(title="Before close"))
        store.close()
        # Should reconnect automatically
        t = Task(title="After close")
        store.add(t)
        assert store.count() == 2

    # --- Priority-specific storage tests ---

    def test_add_with_priority(self, store: TaskStore) -> None:
        t_high = Task(title="High prio", priority="high")
        store.add(t_high)
        fetched = store.get(t_high.id)
        assert fetched.priority == "high"

    def test_update_preserves_priority(self, store: TaskStore) -> None:
        t = Task(title="Prio test", priority="low")
        store.add(t)
        t.title = "Updated"
        store.update(t)
        fetched = store.get(t.id)
        assert fetched.priority == "low"

    def test_update_changes_priority(self, store: TaskStore) -> None:
        t = Task(title="Change prio", priority="low")
        store.add(t)
        t.priority = "high"
        store.update(t)
        fetched = store.get(t.id)
        assert fetched.priority == "high"

    def test_list_all_priority_preserved(self, store: TaskStore) -> None:
        store.add(Task(title="Low", priority="low"))
        store.add(Task(title="High", priority="high"))
        tasks = store.list_all()
        priorities = {t.title: t.priority for t in tasks}
        assert priorities["Low"] == "low"
        assert priorities["High"] == "high"

    def test_db_priority_migration(self) -> None:
        """Verify that an old-format DB (no priority column) is auto-migrated."""
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        p = Path(tmp.name)
        # Create an old-format table (without priority column)
        import sqlite3
        conn = sqlite3.connect(str(p))
        conn.execute(
            "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL, "
            "completed INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)"
        )
        conn.execute("INSERT INTO tasks (id, title, completed, created_at) VALUES (?, ?, ?, ?)",
                     ("old1", "Legacy task", 0, "2025-01-01T00:00:00"))
        conn.commit()
        conn.close()

        # Open with TaskStore (triggers migration on connect)
        s = TaskStore(db_path=p)
        s.connect()
        fetched = s.get("old1")
        assert fetched.priority == "medium"  # default after migration
        s.close()
        p.unlink(missing_ok=True)


# ======================================================================
# app.py — CLI integration
# ======================================================================


@pytest.fixture
def cli_db() -> Path:
    """Return a path to a temporary database for CLI tests."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    return Path(tmp.name)


def run_cli(db_path: Path, *args: str) -> subprocess.CompletedProcess:
    """Run todo/app.py with the given arguments."""
    cmd = [sys.executable, "-m", "todo.app", "--db", str(db_path), *args]
    return subprocess.run(cmd, capture_output=True, text=True)


class TestCli:
    def test_add(self, cli_db: Path) -> None:
        result = run_cli(cli_db, "add", "Buy groceries")
        assert result.returncode == 0
        assert "Created task" in result.stdout
        assert "Buy groceries" in result.stdout

    def test_add_with_priority_flag(self, cli_db: Path) -> None:
        result = run_cli(cli_db, "add", "--priority", "high", "Urgent task")
        assert result.returncode == 0
        assert "[high]" in result.stdout
        result_low = run_cli(cli_db, "add", "-p", "low", "Low prio")
        assert result_low.returncode == 0
        assert "[low]" in result_low.stdout

    def test_list_empty(self, cli_db: Path) -> None:
        result = run_cli(cli_db, "list")
        assert result.returncode == 0
        assert "No tasks found" in result.stdout

    def test_list_with_tasks(self, cli_db: Path) -> None:
        run_cli(cli_db, "add", "Task A")
        run_cli(cli_db, "add", "Task B")
        result = run_cli(cli_db, "list")
        assert result.returncode == 0
        assert "Task A" in result.stdout
        assert "Task B" in result.stdout
        assert "[M]" in result.stdout  # default priority tag

    @staticmethod
    def _extract_task_id(list_output: str) -> str:
        """Parse the task id from list output: '[ ] [M] abc123  Title here'."""
        return list_output.strip().split()[3]

    def test_done(self, cli_db: Path) -> None:
        run_cli(cli_db, "add", "Do something")
        list_result = run_cli(cli_db, "list")
        task_id = self._extract_task_id(list_result.stdout)
        result = run_cli(cli_db, "done", task_id)
        assert result.returncode == 0
        assert "Marked task" in result.stdout

    def test_delete(self, cli_db: Path) -> None:
        run_cli(cli_db, "add", "Delete me")
        list_result = run_cli(cli_db, "list")
        task_id = self._extract_task_id(list_result.stdout)
        result = run_cli(cli_db, "delete", task_id)
        assert result.returncode == 0
        assert "Deleted task" in result.stdout
        # Verify list is empty
        list_after = run_cli(cli_db, "list")
        assert "No tasks found" in list_after.stdout

    def test_done_not_found(self, cli_db: Path) -> None:
        result = run_cli(cli_db, "done", "bad-id")
        assert result.returncode == 1
        assert "not found" in result.stderr

    def test_delete_not_found(self, cli_db: Path) -> None:
        result = run_cli(cli_db, "delete", "bad-id")
        assert result.returncode == 1
        assert "not found" in result.stderr

    def test_list_all_flag(self, cli_db: Path) -> None:
        """--all shows completed tasks too."""
        run_cli(cli_db, "add", "Visible")
        list_result = run_cli(cli_db, "list")
        task_id = self._extract_task_id(list_result.stdout)
        run_cli(cli_db, "done", task_id)

        # Default list hides completed
        default = run_cli(cli_db, "list")
        assert "No tasks found" in default.stdout

        # --all shows them
        all_result = run_cli(cli_db, "list", "--all")
        assert "Visible" in all_result.stdout

    def test_list_shows_priority_tag(self, cli_db: Path) -> None:
        """List output shows priority tags [L] [M] [H]."""
        run_cli(cli_db, "add", "-p", "high", "Very important")
        result = run_cli(cli_db, "list")
        assert "[H]" in result.stdout

    def test_list_priority_high_default_medium(self, cli_db: Path) -> None:
        """Tasks without --priority get [M] tag."""
        run_cli(cli_db, "add", "Normal task")
        result = run_cli(cli_db, "list")
        assert "[M]" in result.stdout

    def test_help(self, cli_db: Path) -> None:
        result = run_cli(cli_db, "--help")
        assert result.returncode == 0
        assert "usage:" in result.stdout
