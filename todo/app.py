"""Command-line interface for the todo management system.

Usage:
    python -m todo.app add "Buy milk"
    python -m todo.app list
    python -m todo.app done <id>
    python -m todo.app delete <id>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from todo.models import PRIORITY_LEVELS, Task, validate_priority
from todo.storage import NotFoundError, TaskStore


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser with subcommands."""
    parser = argparse.ArgumentParser(
        prog="todo",
        description="A simple todo management system.",
    )
    parser.add_argument(
        "--db",
        default=Path.home() / ".todo.db",
        type=Path,
        help="Path to the SQLite database file (default: ~/.todo.db)",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # --- add ---
    add_parser = sub.add_parser("add", help="Add a new task")
    add_parser.add_argument("title", type=str, help="Task title")
    add_parser.add_argument(
        "--priority", "-p",
        default="medium",
        choices=list(PRIORITY_LEVELS),
        help="Task priority (default: medium)",
    )

    # --- list ---
    list_parser = sub.add_parser("list", help="List all tasks")
    list_parser.add_argument(
        "--all", action="store_true", dest="show_all",
        help="Show both pending and completed tasks (default: pending only)",
    )

    # --- done ---
    done_parser = sub.add_parser("done", help="Mark a task as completed")
    done_parser.add_argument("task_id", type=str, help="Task id to mark done")

    # --- delete ---
    delete_parser = sub.add_parser("delete", help="Delete a task")
    delete_parser.add_argument("task_id", type=str, help="Task id to delete")

    return parser


def cmd_add(store: TaskStore, title: str, priority: str = "medium") -> None:
    """Add a new task and print its id."""
    priority = validate_priority(priority)
    task = Task(title=title, priority=priority)
    store.add(task)
    print(f"Created task {task.id}: {task.title} [{task.priority}]")


def cmd_list(store: TaskStore, show_all: bool = False) -> None:
    """List tasks. By default shows only pending tasks."""
    tasks = store.list_all()
    if not show_all:
        tasks = [t for t in tasks if not t.completed]

    if not tasks:
        print("No tasks found.")
        return

    for t in tasks:
        status = "✓" if t.completed else " "
        prio_tag = f" [{t.priority[0].upper()}]"  # [L] [M] [H]
        print(f"  [{status}]{prio_tag} {t.id}  {t.title}")


def cmd_done(store: TaskStore, task_id: str) -> None:
    """Mark a task as completed."""
    task = store.get(task_id)
    task.mark_done()
    store.update(task)
    print(f"Marked task {task.id} as done.")


def cmd_delete(store: TaskStore, task_id: str) -> None:
    """Delete a task."""
    store.delete(task_id)
    print(f"Deleted task {task_id}.")


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    store = TaskStore(db_path=args.db)

    try:
        if args.command == "add":
            cmd_add(store, args.title, priority=args.priority)
        elif args.command == "list":
            cmd_list(store, show_all=args.show_all)
        elif args.command == "done":
            cmd_done(store, args.task_id)
        elif args.command == "delete":
            cmd_delete(store, args.task_id)
        else:
            parser.print_help()
            return 1
    except NotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr)
        return 1
    finally:
        store.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
