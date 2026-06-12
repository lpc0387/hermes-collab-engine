from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .models import RiskPolicy

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY,title TEXT NOT NULL,request TEXT NOT NULL,status TEXT NOT NULL,complexity_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,completed_at TEXT);
CREATE TABLE IF NOT EXISTS wbs_nodes (id TEXT PRIMARY KEY,run_id TEXT NOT NULL,parent_id TEXT,title TEXT NOT NULL,description TEXT NOT NULL,capability TEXT NOT NULL,complexity INTEGER NOT NULL,dependencies_json TEXT NOT NULL DEFAULT '[]',parallelizable INTEGER NOT NULL DEFAULT 1,deliverable TEXT NOT NULL,status TEXT NOT NULL,attempt INTEGER NOT NULL DEFAULT 1,checkpoint INTEGER NOT NULL DEFAULT 0,result TEXT,session_id TEXT,duration_seconds REAL,error TEXT,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY(run_id) REFERENCES runs(id));
CREATE TABLE IF NOT EXISTS workers (id TEXT PRIMARY KEY,run_id TEXT NOT NULL,node_id TEXT,status TEXT NOT NULL,started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,duration_seconds REAL,session_id TEXT,error TEXT);
CREATE TABLE IF NOT EXISTS logs (id INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT,node_id TEXT,level TEXT NOT NULL,message TEXT NOT NULL,data_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS lessons (id INTEGER PRIMARY KEY AUTOINCREMENT,scope TEXT NOT NULL DEFAULT 'global',category TEXT NOT NULL,lesson TEXT NOT NULL,evidence_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS metrics (key TEXT PRIMARY KEY,value_json TEXT NOT NULL,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY,value_json TEXT NOT NULL,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
"""


class CollabStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            self.conn.executescript(SCHEMA)
            self._ensure_schema()
            self.conn.commit()

    def _ensure_schema(self) -> None:
        self._migrate_lessons_scope()
        self._migrate_wbs_checkpoint()

    def _migrate_lessons_scope(self) -> None:
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(lessons)").fetchall()}
        if "scope" not in columns:
            self.conn.execute("ALTER TABLE lessons ADD COLUMN scope TEXT NOT NULL DEFAULT 'global'")

    def _migrate_wbs_checkpoint(self) -> None:
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(wbs_nodes)").fetchall()}
        if "checkpoint" not in columns:
            self.conn.execute("ALTER TABLE wbs_nodes ADD COLUMN checkpoint INTEGER NOT NULL DEFAULT 0")

    def _execute(self, sql: str, params: tuple = ()):
        with self.lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def _query(self, sql: str, params: tuple = ()):
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def _one(self, sql: str, params: tuple = ()):
        with self.lock:
            return self.conn.execute(sql, params).fetchone()

    def log(self, run_id: str | None, level: str, message: str, data: dict[str, Any] | None = None, node_id: str | None = None) -> None:
        self._execute("INSERT INTO logs(run_id,node_id,level,message,data_json) VALUES(?,?,?,?,?)", (run_id, node_id, level, message, json.dumps(data or {}, ensure_ascii=False)))

    def get_setting(self, key: str) -> Any:
        row = self._one("SELECT value_json FROM settings WHERE key=?", (key,))
        return json.loads(row["value_json"]) if row else None

    def set_setting(self, key: str, value: Any) -> None:
        self._execute("INSERT OR REPLACE INTO settings(key,value_json,updated_at) VALUES(?,?,CURRENT_TIMESTAMP)", (key, json.dumps(value, ensure_ascii=False)))

    def list_settings(self) -> dict[str, Any]:
        return {row["key"]: json.loads(row["value_json"]) for row in self._query("SELECT key,value_json FROM settings ORDER BY key")}

    def load_risk_policy(self) -> RiskPolicy:
        return RiskPolicy.from_dict(self.get_setting("risk_policy"))

    def create_run(self, run_id: str, title: str, request: str, complexity: dict[str, Any]) -> None:
        self._execute("INSERT INTO runs(id,title,request,status,complexity_json) VALUES(?,?,?,?,?)", (run_id, title, request, "created", json.dumps(complexity, ensure_ascii=False)))
        self.log(run_id, "info", "run created", {"title": title})

    def update_run(self, run_id: str, status: str) -> None:
        completed_sql = ", completed_at=CURRENT_TIMESTAMP" if status in {"completed", "failed"} else ""
        self._execute(f"UPDATE runs SET status=?, updated_at=CURRENT_TIMESTAMP{completed_sql} WHERE id=?", (status, run_id))

    def fail_stale_run(self, run_id: str, reason: str) -> None:
        """Mark an interrupted run and any in-flight work as failed.

        This is intentionally conservative: completed nodes/workers are left intact,
        running work becomes failed, and unscheduled pending work is marked failed so
        dashboards never keep showing a parent process that was interrupted as live.
        """
        self._execute(
            "UPDATE workers SET status='failed', error=COALESCE(error, ?), updated_at=CURRENT_TIMESTAMP WHERE run_id=? AND status='running'",
            (reason, run_id),
        )
        self._execute(
            "UPDATE wbs_nodes SET status='failed', error=COALESCE(error, ?), updated_at=CURRENT_TIMESTAMP WHERE run_id=? AND status IN ('running','pending')",
            (reason, run_id),
        )
        self.update_run(run_id, "failed")
        self.log(run_id, "error", "run interrupted; stale running work marked failed", {"reason": reason})
        self.add_lesson(
            "interrupt-cleanup",
            "Interrupted parent runs must fail/close all running workers and pending/running WBS nodes; otherwise dashboards can show stale ghost-running work.",
            {"run_id": run_id, "reason": reason},
        )

    def insert_wbs_node(self, run_id: str, node: dict[str, Any]) -> None:
        self._execute(
            """INSERT OR REPLACE INTO wbs_nodes(id,run_id,parent_id,title,description,capability,complexity,dependencies_json,parallelizable,deliverable,status,attempt,checkpoint,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
            (node["id"], run_id, node.get("parent_id"), node["title"], node["description"], node["capability"], node["complexity"], json.dumps(node.get("dependencies", []), ensure_ascii=False), 1 if node.get("parallelizable", True) else 0, node["deliverable"], node.get("status", "pending"), node.get("attempt", 1), 1 if node.get("checkpoint", False) else 0),
        )

    def get_node(self, run_id: str, node_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM wbs_nodes WHERE run_id=? AND id=?", (run_id, node_id))
        return dict(row) if row else None

    def get_nodes(self, run_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self._query("SELECT * FROM wbs_nodes WHERE run_id=? ORDER BY id", (run_id,))]

    def update_node_result(self, run_id: str, node_id: str, result: str) -> None:
        self._execute("UPDATE wbs_nodes SET result=?, updated_at=CURRENT_TIMESTAMP WHERE run_id=? AND id=?", (result, run_id, node_id))

    def update_node_attempt(self, run_id: str, node_id: str, attempt: int) -> None:
        self._execute("UPDATE wbs_nodes SET attempt=?, updated_at=CURRENT_TIMESTAMP WHERE run_id=? AND id=?", (attempt, run_id, node_id))

    def update_node(self, node_id: str, status: str, result: str | None = None, session_id: str | None = None, duration_seconds: float | None = None, error: str | None = None) -> None:
        self._execute("""UPDATE wbs_nodes SET status=?, result=COALESCE(?, result), session_id=COALESCE(?, session_id), duration_seconds=COALESCE(?, duration_seconds), error=COALESCE(?, error), updated_at=CURRENT_TIMESTAMP WHERE id=?""", (status, result, session_id, duration_seconds, error, node_id))

    def worker_start(self, worker_id: str, run_id: str, node_id: str) -> None:
        self._execute("INSERT OR REPLACE INTO workers(id,run_id,node_id,status,updated_at) VALUES(?,?,?,?,CURRENT_TIMESTAMP)", (worker_id, run_id, node_id, "running"))

    def worker_finish(self, worker_id: str, status: str, duration_seconds: float | None = None, session_id: str | None = None, error: str | None = None) -> None:
        self._execute("UPDATE workers SET status=?, duration_seconds=?, session_id=?, error=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (status, duration_seconds, session_id, error, worker_id))

    def add_lesson(self, category: str, lesson: str, evidence: dict[str, Any] | None = None, scope: str = "global") -> None:
        self._execute("INSERT INTO lessons(scope,category,lesson,evidence_json) VALUES(?,?,?,?)", (scope, category, lesson, json.dumps(evidence or {}, ensure_ascii=False)))

    def overview(self) -> dict[str, Any]:
        def scalar(sql: str):
            return self._one(sql)[0]
        return {"runs": scalar("SELECT COUNT(*) FROM runs"), "running": scalar("SELECT COUNT(*) FROM runs WHERE status='running'"), "completed": scalar("SELECT COUNT(*) FROM runs WHERE status='completed'"), "failed": scalar("SELECT COUNT(*) FROM runs WHERE status='failed'"), "workers_running": scalar("SELECT COUNT(*) FROM workers WHERE status='running'"), "lessons": scalar("SELECT COUNT(*) FROM lessons")}

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        return [dict(r) for r in self._query("SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,))]

    def run_detail(self, run_id: str) -> dict[str, Any]:
        run = self._one("SELECT * FROM runs WHERE id=?", (run_id,))
        return {"run": dict(run) if run else None, "nodes": self.get_nodes(run_id), "workers": [dict(r) for r in self._query("SELECT * FROM workers WHERE run_id=? ORDER BY started_at DESC", (run_id,))], "logs": [dict(r) for r in self._query("SELECT * FROM logs WHERE run_id=? ORDER BY id DESC LIMIT 200", (run_id,))]}

    def recent_logs(self, limit: int = 200) -> list[dict[str, Any]]:
        return [dict(r) for r in self._query("SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,))]

    def lessons(self, limit: int = 100, scope: str | None = None) -> list[dict[str, Any]]:
        if scope is None:
            return [dict(r) for r in self._query("SELECT * FROM lessons ORDER BY id DESC LIMIT ?", (limit,))]
        return [dict(r) for r in self._query("SELECT * FROM lessons WHERE scope=? ORDER BY id DESC LIMIT ?", (scope, limit))]
