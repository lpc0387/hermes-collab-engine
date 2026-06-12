from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.hermes_collab_engine.store import CollabStore


class LessonsScopeTests(unittest.TestCase):
    def test_add_lesson_defaults_to_global_scope_and_lists_all_scopes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CollabStore(Path(tmp) / "db.sqlite3")

            store.add_lesson("planning", "default scope")
            store.add_lesson("planning", "project scope", scope="project")

            rows = store.lessons()

        self.assertEqual([row["lesson"] for row in rows], ["project scope", "default scope"])
        self.assertEqual(rows[0]["scope"], "project")
        self.assertEqual(rows[1]["scope"], "global")

    def test_lessons_can_filter_by_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CollabStore(Path(tmp) / "db.sqlite3")
            store.add_lesson("planning", "global lesson", scope="global")
            store.add_lesson("planning", "run lesson", scope="run")
            store.add_lesson("planning", "project lesson", scope="project")

            project_rows = store.lessons(scope="project")
            run_rows = store.lessons(scope="run")

        self.assertEqual([row["lesson"] for row in project_rows], ["project lesson"])
        self.assertEqual([row["lesson"] for row in run_rows], ["run lesson"])

    def test_existing_lessons_table_migrates_scope_column(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "legacy.sqlite3"
            conn = sqlite3.connect(db_path)
            conn.execute(
                "CREATE TABLE lessons ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "category TEXT NOT NULL,"
                "lesson TEXT NOT NULL,"
                "evidence_json TEXT NOT NULL DEFAULT '{}',"
                "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
                ")"
            )
            conn.execute(
                "INSERT INTO lessons(category,lesson,evidence_json) VALUES(?,?,?)",
                ("legacy", "old row", "{}"),
            )
            conn.commit()
            conn.close()

            store = CollabStore(db_path)
            columns = {row[1] for row in store.conn.execute("PRAGMA table_info(lessons)").fetchall()}
            rows = store.lessons()

        self.assertIn("scope", columns)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["lesson"], "old row")
        self.assertEqual(rows[0]["scope"], "global")


if __name__ == "__main__":
    unittest.main()
