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
CREATE TABLE IF NOT EXISTS node_results (node_id TEXT PRIMARY KEY,run_id TEXT NOT NULL,result_text TEXT DEFAULT '',result_struct_json TEXT DEFAULT NULL,updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS run_state (run_id TEXT PRIMARY KEY,paused INTEGER DEFAULT 0,checkpoint_paused_nodes_json TEXT DEFAULT '[]',updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS context_snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT NOT NULL,snapshot_type TEXT NOT NULL,node_id TEXT DEFAULT NULL,snapshot_json TEXT NOT NULL,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
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
        self._migrate_wbs_context_fields()
        self._migrate_runs_agent()
        self._cleanup_stale_workers()

    def _cleanup_stale_workers(self) -> None:
        """On startup, mark any orphaned 'running' workers as failed.

        A worker is stale if its status is 'running' but either:
        (a) its parent run has reached a terminal state (completed/failed), or
        (b) its parent run is still 'running' — which means the previous engine
            process died without cleaning up (engine restart scenario).

        Since this runs at store init time (before any new work is scheduled),
        ALL 'running' workers are guaranteed to be from a previous incarnation.
        """
        # Case 1: workers whose parent run is already terminal
        self._execute(
            """UPDATE workers SET status='failed',
               error='auto-cleanup: stale orphan from non-running parent',
               updated_at=CURRENT_TIMESTAMP
               WHERE status='running'
                 AND run_id IN (SELECT id FROM runs WHERE status IN ('completed','failed'))"""
        )
        # Case 2: workers whose parent run is still 'running' — previous engine crashed.
        # These runs may have pending/running nodes that also need cleanup.
        stale_run_ids = [
            row["id"] for row in self._query(
                "SELECT id FROM runs WHERE status = 'running'"
            )
        ]
        if stale_run_ids:
            placeholders = ",".join("?" * len(stale_run_ids))
            # Mark stale workers as failed
            self._execute(
                f"""UPDATE workers SET status='failed',
                   error='auto-cleanup: stale worker from previous engine incarnation',
                   updated_at=CURRENT_TIMESTAMP
                   WHERE status='running'
                     AND run_id IN ({placeholders})""",
                tuple(stale_run_ids),
            )
            # Mark pending/running nodes as failed
            self._execute(
                f"""UPDATE wbs_nodes SET status='failed',
                   error='auto-cleanup: engine restarted while run was active',
                   updated_at=CURRENT_TIMESTAMP
                   WHERE status IN ('running','pending')
                     AND run_id IN ({placeholders})""",
                tuple(stale_run_ids),
            )
            # Finally mark the runs themselves as failed
            self._execute(
                """UPDATE runs SET status='failed',
                   updated_at=CURRENT_TIMESTAMP,
                   completed_at=CURRENT_TIMESTAMP
                   WHERE status='running'"""
            )
        # Case 3: orphaned pending nodes in terminal runs.
        # These are nodes whose dependencies can never be satisfied because
        # the parent run already completed/failed (e.g. a shard plan where
        # the engine crashed after partial execution but before cleanup).
        self._execute(
            """UPDATE wbs_nodes SET status='failed',
               error='auto-cleanup: orphaned pending node in terminal run',
               updated_at=CURRENT_TIMESTAMP
               WHERE status='pending'
                 AND run_id IN (SELECT id FROM runs WHERE status IN ('completed','failed'))"""
        )

    def _migrate_lessons_scope(self) -> None:
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(lessons)").fetchall()}
        if "scope" not in columns:
            self.conn.execute("ALTER TABLE lessons ADD COLUMN scope TEXT NOT NULL DEFAULT 'global'")

    def _migrate_wbs_checkpoint(self) -> None:
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(wbs_nodes)").fetchall()}
        if "checkpoint" not in columns:
            self.conn.execute("ALTER TABLE wbs_nodes ADD COLUMN checkpoint INTEGER NOT NULL DEFAULT 0")

    def _migrate_wbs_context_fields(self) -> None:
        for sql in (
            "ALTER TABLE wbs_nodes ADD COLUMN brief TEXT DEFAULT ''",
            "ALTER TABLE wbs_nodes ADD COLUMN shared_brief TEXT DEFAULT ''",
            "ALTER TABLE wbs_nodes ADD COLUMN estimated_duration INTEGER DEFAULT NULL",
            "ALTER TABLE wbs_nodes ADD COLUMN write_targets_json TEXT DEFAULT '[]'",
            "ALTER TABLE wbs_nodes ADD COLUMN result_struct_json TEXT DEFAULT NULL",
            "ALTER TABLE wbs_nodes ADD COLUMN skills_json TEXT DEFAULT NULL",
            "ALTER TABLE wbs_nodes ADD COLUMN tools_json TEXT DEFAULT NULL",
            "ALTER TABLE wbs_nodes ADD COLUMN fingerprint TEXT DEFAULT ''",
        ):
            try:
                self.conn.execute(sql)
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise

    def _migrate_runs_agent(self) -> None:
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(runs)").fetchall()}
        if "agent" not in columns:
            self.conn.execute("ALTER TABLE runs ADD COLUMN agent TEXT DEFAULT 'claude-code'")

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

    def _decode_json(self, value: Any, default: Any) -> Any:
        if value in (None, ""):
            return default
        if isinstance(value, (dict, list)):
            return value
        try:
            return json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return default

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

    def save_run_state(self, run_id: str, paused: bool, checkpoint_paused_nodes: set[str] | list[str]) -> None:
        nodes_json = json.dumps(sorted(checkpoint_paused_nodes), ensure_ascii=False)
        self._execute(
            "INSERT OR REPLACE INTO run_state(run_id,paused,checkpoint_paused_nodes_json,updated_at) VALUES(?,?,?,CURRENT_TIMESTAMP)",
            (run_id, 1 if paused else 0, nodes_json),
        )

    def load_run_state(self, run_id: str | None = None) -> dict[str, Any] | list[dict[str, Any]] | None:
        if run_id is None:
            rows = self._query("SELECT run_id,paused,checkpoint_paused_nodes_json FROM run_state")
            return [self._run_state_from_row(row) for row in rows]
        row = self._one("SELECT run_id,paused,checkpoint_paused_nodes_json FROM run_state WHERE run_id=?", (run_id,))
        return self._run_state_from_row(row) if row else None

    def _run_state_from_row(self, row) -> dict[str, Any]:
        try:
            nodes = json.loads(row["checkpoint_paused_nodes_json"] or "[]")
        except json.JSONDecodeError:
            nodes = []
        if not isinstance(nodes, list):
            nodes = []
        return {"run_id": row["run_id"], "paused": bool(row["paused"]), "checkpoint_paused_nodes": [str(node) for node in nodes]}

    def create_run(self, run_id: str, title: str, request: str, complexity: dict[str, Any], agent: str = "claude-code") -> None:
        self._execute("INSERT INTO runs(id,title,request,status,complexity_json,agent) VALUES(?,?,?,?,?,?)", (run_id, title, request, "created", json.dumps(complexity, ensure_ascii=False), agent))
        self.log(run_id, "info", "run created", {"title": title, "agent": agent})

    def update_run(self, run_id: str, status: str) -> None:
        completed_sql = ", completed_at=CURRENT_TIMESTAMP" if status in {"completed", "failed"} else ""
        self._execute(f"UPDATE runs SET status=?, updated_at=CURRENT_TIMESTAMP{completed_sql} WHERE id=?", (status, run_id))

    def latest_run_id(self) -> str | None:
        row = self._one("SELECT id FROM runs ORDER BY created_at DESC LIMIT 1")
        return row["id"] if row else None

    def resume_context(self, run_id: str | None = None, *, node_limit: int = 4, log_limit: int = 8) -> dict[str, Any] | None:
        run_id = run_id or self.latest_run_id()
        if not run_id:
            return None
        run = self._one("SELECT id,title,request,status,created_at,updated_at,completed_at FROM runs WHERE id=?", (run_id,))
        if not run:
            return None
        node_rows = self._query(
            """SELECT id,title,status,result,updated_at FROM wbs_nodes
               WHERE run_id=? AND result IS NOT NULL AND result!=''
               ORDER BY updated_at DESC LIMIT ?""",
            (run_id, node_limit),
        )
        log_rows = self._query(
            """SELECT id,node_id,level,message,data_json,created_at FROM logs
               WHERE run_id=? ORDER BY id DESC LIMIT ?""",
            (run_id, log_limit),
        )
        nodes = []
        for row in node_rows:
            result = str(row["result"] or "")
            nodes.append({
                "id": row["id"],
                "title": row["title"],
                "status": row["status"],
                "result_excerpt": result[:800],
                "updated_at": row["updated_at"],
            })
        logs = [self._log_from_row(row) for row in reversed(log_rows)]
        summary_lines = [
            f"Previous run {run['id']} ({run['status']}): {run['title']}",
            f"Original request: {str(run['request'] or '')[:600]}",
        ]
        for node in nodes:
            summary_lines.append(f"- {node['id']} {node['status']}: {node['title']} — {node['result_excerpt'][:300]}")
        summary = "\n".join(summary_lines)
        return {
            "run": dict(run),
            "summary": summary,
            "recent_interactions": logs,
            "estimated_tokens": max(1, (len(summary) + sum(len(str(item)) for item in logs)) // 4),
            "limits": {"nodes": node_limit, "logs": log_limit, "result_excerpt_chars": 800},
        }

    def resume_prompt(self, request: str, run_id: str | None = None) -> tuple[str, dict[str, Any] | None]:
        context = self.resume_context(run_id)
        if not context:
            return request, None
        interactions = "\n".join(
            f"- {item.get('created_at', '')} {item.get('level', '')} {item.get('node_id') or ''}: {item.get('message', '')}"
            for item in context["recent_interactions"][-8:]
        )
        prompt = (
            "Session resume context (bounded summary only; do not assume omitted full context):\n"
            f"{context['summary']}\n\nRecent interactions:\n{interactions}\n\n"
            f"New user request:\n{request}"
        )
        return prompt, context

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
            """INSERT OR REPLACE INTO wbs_nodes(id,run_id,parent_id,title,description,capability,complexity,dependencies_json,parallelizable,deliverable,brief,shared_brief,estimated_duration,write_targets_json,result_struct_json,skills_json,tools_json,fingerprint,status,attempt,checkpoint,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
            (node["id"], run_id, node.get("parent_id"), node["title"], node["description"], node["capability"], node["complexity"], json.dumps(node.get("dependencies", []), ensure_ascii=False), 1 if node.get("parallelizable", True) else 0, node["deliverable"], node.get("brief", ""), node.get("shared_brief", ""), node.get("estimated_duration"), json.dumps(node.get("write_targets", []), ensure_ascii=False), node.get("result_struct_json"), node.get("skills_json"), node.get("tools_json"), node.get("fingerprint", ""), node.get("status", "pending"), node.get("attempt", 1), 1 if node.get("checkpoint", False) else 0),
        )

    def get_node(self, run_id: str, node_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM wbs_nodes WHERE run_id=? AND id=?", (run_id, node_id))
        return dict(row) if row else None

    def get_nodes(self, run_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self._query("SELECT * FROM wbs_nodes WHERE run_id=? ORDER BY id", (run_id,))]

    def update_node_result(self, run_id: str, node_id: str, result: str) -> None:
        self._execute("UPDATE wbs_nodes SET result=?, updated_at=CURRENT_TIMESTAMP WHERE run_id=? AND id=?", (result, run_id, node_id))

    def save_node_result(self, run_id: str, node_id: str, result_text: str, result_struct: dict[str, Any] | None) -> None:
        self._execute(
            """INSERT OR REPLACE INTO node_results(node_id,run_id,result_text,result_struct_json,updated_at) VALUES(?,?,?,?,CURRENT_TIMESTAMP)""",
            (node_id, run_id, result_text, json.dumps(result_struct, ensure_ascii=False) if result_struct is not None else None),
        )

    def load_node_results(self, run_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self._query("SELECT * FROM node_results WHERE run_id=? ORDER BY node_id", (run_id,))]

    def save_context_snapshot(self, run_id: str, snapshot_type: str, snapshot: dict[str, Any], node_id: str | None = None) -> None:
        if snapshot_type not in {"node_completed", "checkpoint", "pre_compaction"}:
            raise ValueError("snapshot_type must be 'node_completed', 'checkpoint', or 'pre_compaction'")
        self._execute(
            """INSERT INTO context_snapshots(run_id,snapshot_type,node_id,snapshot_json) VALUES(?,?,?,?)""",
            (run_id, snapshot_type, node_id, json.dumps(snapshot, ensure_ascii=False)),
        )

    def load_context_snapshots(self, run_id: str, snapshot_type: str | None = None) -> list[dict[str, Any]]:
        if snapshot_type is None:
            rows = self._query("SELECT * FROM context_snapshots WHERE run_id=? ORDER BY id", (run_id,))
        else:
            rows = self._query("SELECT * FROM context_snapshots WHERE run_id=? AND snapshot_type=? ORDER BY id", (run_id, snapshot_type))
        return [dict(r) for r in rows]

    def _log_from_row(self, row) -> dict[str, Any]:
        item = dict(row)
        raw = item.pop("data_json", "{}") or "{}"
        try:
            item["data"] = json.loads(raw)
        except json.JSONDecodeError:
            item["data"] = {}
        return item

    def update_node_attempt(self, run_id: str, node_id: str, attempt: int) -> None:
        self._execute("UPDATE wbs_nodes SET attempt=?, updated_at=CURRENT_TIMESTAMP WHERE run_id=? AND id=?", (attempt, run_id, node_id))

    def update_node(self, node_id: str, status: str, result: str | None = None, session_id: str | None = None, duration_seconds: float | None = None, error: str | None = None, run_id: str | None = None) -> None:
        if run_id is None:
            self._execute("""UPDATE wbs_nodes SET status=?, result=COALESCE(?, result), session_id=COALESCE(?, session_id), duration_seconds=COALESCE(?, duration_seconds), error=COALESCE(?, error), updated_at=CURRENT_TIMESTAMP WHERE id=?""", (status, result, session_id, duration_seconds, error, node_id))
            return
        self._execute("""UPDATE wbs_nodes SET status=?, result=COALESCE(?, result), session_id=COALESCE(?, session_id), duration_seconds=COALESCE(?, duration_seconds), error=COALESCE(?, error), updated_at=CURRENT_TIMESTAMP WHERE run_id=? AND id=?""", (status, result, session_id, duration_seconds, error, run_id, node_id))

    def update_node_skills_tools(self, node_id: str, skills_json: str | None = None, tools_json: str | None = None, run_id: str | None = None) -> None:
        if run_id is None:
            self._execute("""UPDATE wbs_nodes SET skills_json=COALESCE(?, skills_json), tools_json=COALESCE(?, tools_json), updated_at=CURRENT_TIMESTAMP WHERE id=?""", (skills_json, tools_json, node_id))
            return
        self._execute("""UPDATE wbs_nodes SET skills_json=COALESCE(?, skills_json), tools_json=COALESCE(?, tools_json), updated_at=CURRENT_TIMESTAMP WHERE run_id=? AND id=?""", (skills_json, tools_json, run_id, node_id))

    def worker_start(self, worker_id: str, run_id: str, node_id: str) -> None:
        self._execute("INSERT OR REPLACE INTO workers(id,run_id,node_id,status,updated_at) VALUES(?,?,?,?,CURRENT_TIMESTAMP)", (worker_id, run_id, node_id, "running"))

    def worker_finish(self, worker_id: str, status: str, duration_seconds: float | None = None, session_id: str | None = None, error: str | None = None) -> None:
        self._execute("UPDATE workers SET status=?, duration_seconds=?, session_id=?, error=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (status, duration_seconds, session_id, error, worker_id))

    def add_lesson(self, category: str, lesson: str, evidence: dict[str, Any] | None = None, scope: str = "global") -> None:
        self._execute("INSERT INTO lessons(scope,category,lesson,evidence_json) VALUES(?,?,?,?)", (scope, category, lesson, json.dumps(evidence or {}, ensure_ascii=False)))

    def deduplicate_lessons(self) -> int:
        """Remove duplicate lessons, keeping the newest per group.

        Groups lessons by (category, scope). Within each group, lessons whose
        normalized text matches are considered duplicates. Normalization strips
        run IDs and variable numbers so "Run run_xxx: 3 slow workers" and
        "Run run_yyy: 1 slow workers" collapse into one.

        Returns:
            Number of duplicate records removed.
        """
        import re as _re
        rows = self._query(
            "SELECT id, category, scope, lesson FROM lessons ORDER BY id ASC"
        )
        def _normalize(text: str) -> str:
            """Strip run IDs and numbers for dedup comparison."""
            t = _re.sub(r'run_[a-f0-9]+', 'run_*', text or '')
            t = _re.sub(r'\d+', 'N', t)
            return t[:80]
        # Track: (category, scope, normalized) -> newest id
        keep: dict[tuple[str, str, str], int] = {}
        to_delete: list[int] = []
        for row in rows:
            key = (row["category"], row["scope"], _normalize(row["lesson"]))
            if key in keep:
                # Duplicate: the earlier entry is older (lower id) — delete it
                to_delete.append(keep[key])
                keep[key] = row["id"]
            else:
                keep[key] = row["id"]
        if to_delete:
            placeholders = ",".join("?" * len(to_delete))
            self._execute(
                f"DELETE FROM lessons WHERE id IN ({placeholders})",
                tuple(to_delete),
            )
        return len(to_delete)

    def overview(self) -> dict[str, Any]:
        def scalar(sql: str):
            return self._one(sql)[0]
        return {"runs": scalar("SELECT COUNT(*) FROM runs"), "running": scalar("SELECT COUNT(*) FROM runs WHERE status='running'"), "completed": scalar("SELECT COUNT(*) FROM runs WHERE status='completed'"), "failed": scalar("SELECT COUNT(*) FROM runs WHERE status='failed'"), "workers_running": scalar("SELECT COUNT(*) FROM workers WHERE status='running'"), "lessons": scalar("SELECT COUNT(*) FROM lessons")}

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        columns = "id,title,status,created_at,updated_at,completed_at,agent"
        return [dict(r) for r in self._query(f"SELECT {columns} FROM runs ORDER BY created_at DESC LIMIT ?", (limit,))]

    def latest_resumable_run(self) -> dict[str, Any] | None:
        row = self._one(
            """SELECT id,title,request,status,created_at,updated_at,completed_at,agent
               FROM runs
               ORDER BY updated_at DESC, created_at DESC
               LIMIT 1"""
        )
        return dict(row) if row else None

    def session_resume_context(self, run_id: str | None = None, *, recent_limit: int = 6) -> dict[str, Any] | None:
        run = self.latest_resumable_run() if run_id is None else self._one("SELECT * FROM runs WHERE id=?", (run_id,))
        if not run:
            return None
        run_dict = dict(run)
        rid = run_dict["id"]
        recent_logs = [dict(r) for r in self._query(
            "SELECT level,message,node_id,created_at,data_json FROM logs WHERE run_id=? ORDER BY id DESC LIMIT ?",
            (rid, recent_limit),
        )]
        nodes = [dict(r) for r in self._query(
            "SELECT id,title,capability,status,session_id,duration_seconds,error,updated_at FROM wbs_nodes WHERE run_id=? ORDER BY updated_at DESC LIMIT ?",
            (rid, recent_limit),
        )]
        snapshots = [dict(r) for r in self._query(
            "SELECT snapshot_type,node_id,snapshot_json,created_at FROM context_snapshots WHERE run_id=? ORDER BY id DESC LIMIT ?",
            (rid, min(3, recent_limit)),
        )]
        return {
            "run": run_dict,
            "recent_interactions": list(reversed(recent_logs)),
            "recent_nodes": nodes,
            "context_snapshots": snapshots,
            "resume_note": "Resume uses only the run summary, recent interactions, and compact snapshots; full context is not replayed.",
        }

    def task_sets(self, limit: int = 20) -> list[dict[str, Any]]:
        runs = self.list_runs(limit)
        task_sets: list[dict[str, Any]] = []
        for run in runs:
            rid = run["id"]
            counts = {"total": 0, "pending": 0, "running": 0, "completed": 0, "failed": 0, "skipped": 0}
            for row in self._query("SELECT status, COUNT(*) AS count FROM wbs_nodes WHERE run_id=? GROUP BY status", (rid,)):
                status = row["status"] or "pending"
                count = int(row["count"])
                counts["total"] += count
                if status in counts:
                    counts[status] += count
            token_totals = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
            for row in self._query("SELECT data_json FROM logs WHERE run_id=?", (rid,)):
                data = self._decode_json(row["data_json"], {})
                if not isinstance(data, dict):
                    continue
                usage = data.get("usage") if isinstance(data.get("usage"), dict) else data
                for key in token_totals:
                    try:
                        token_totals[key] += int(usage.get(key, 0) or 0)
                    except (AttributeError, TypeError, ValueError):
                        pass
            dedup_events = [dict(r) for r in self._query(
                "SELECT node_id,message,data_json,created_at FROM logs WHERE run_id=? AND (message LIKE '%duplicate%' OR message LIKE '%killed%') ORDER BY id DESC LIMIT 8",
                (rid,),
            )]
            for event in dedup_events:
                event["data"] = self._decode_json(event.pop("data_json", None), {})
            task_sets.append({"run": run, "counts": counts, "tokens": token_totals, "dedup_kill_events": dedup_events})
        return task_sets

    def get_node_summaries(self, run_id: str) -> list[dict[str, Any]]:
        columns = "id,run_id,parent_id,title,capability,complexity,dependencies_json,parallelizable,deliverable,brief,shared_brief,estimated_duration,write_targets_json,skills_json,tools_json,fingerprint,status,attempt,checkpoint,session_id,duration_seconds,error,created_at,updated_at"
        return [dict(r) for r in self._query(f"SELECT {columns} FROM wbs_nodes WHERE run_id=? ORDER BY id", (run_id,))]

    def run_detail(self, run_id: str, full: bool = True, log_limit: int = 200, include_workers: bool = True) -> dict[str, Any]:
        run_columns = "*" if full else "id,title,status,created_at,updated_at,completed_at,agent"
        run = self._one(f"SELECT {run_columns} FROM runs WHERE id=?", (run_id,))
        nodes = self.get_nodes(run_id) if full else self.get_node_summaries(run_id)
        workers = [dict(r) for r in self._query("SELECT * FROM workers WHERE run_id=? ORDER BY started_at DESC", (run_id,))] if include_workers else []
        log_columns = "*" if full else "id,run_id,node_id,level,message,created_at"
        logs = [dict(r) for r in self._query(f"SELECT {log_columns} FROM logs WHERE run_id=? ORDER BY id DESC LIMIT ?", (run_id, log_limit))]
        task_set = self.task_set(run_id, nodes=nodes)
        return {"run": dict(run) if run else None, "nodes": nodes, "workers": workers, "logs": logs, "task_set": task_set}

    def task_set(self, run_id: str, *, nodes: list[dict[str, Any]] | None = None, workers: list[dict[str, Any]] | None = None, logs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        nodes = nodes if nodes is not None else self.get_node_summaries(run_id)
        workers = workers if workers is not None else [dict(r) for r in self._query("SELECT * FROM workers WHERE run_id=?", (run_id,))]
        if logs is None:
            logs = [self._log_from_row(r) for r in self._query("SELECT id,run_id,node_id,level,message,data_json,created_at FROM logs WHERE run_id=? ORDER BY id DESC LIMIT 120", (run_id,))]
        counts: dict[str, int] = {}
        for node in nodes:
            status = str(node.get("status") or "unknown")
            counts[status] = counts.get(status, 0) + 1
        token_total = 0
        dedup_kills = []
        for log in logs:
            data = log.get("data")
            if data is None and log.get("data_json") is not None:
                try:
                    data = json.loads(log.get("data_json") or "{}")
                except json.JSONDecodeError:
                    data = {}
            if isinstance(data, dict):
                usage = data.get("usage") or data.get("tokens") or {}
                if isinstance(usage, dict):
                    token_total += int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
                    token_total += int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
                if data.get("total_tokens"):
                    token_total += int(data.get("total_tokens") or 0)
            message = str(log.get("message") or "")
            if "duplicate" in message.lower() or "kill" in message.lower() or "killed" in message.lower():
                dedup_kills.append({
                    "created_at": log.get("created_at"),
                    "node_id": log.get("node_id"),
                    "level": log.get("level"),
                    "message": message,
                    "data": data if isinstance(data, dict) else {},
                })
        return {
            "counts": counts,
            "workers": {"total": len(workers), "running": sum(1 for worker in workers if worker.get("status") == "running")},
            "tokens": {"total": token_total, "source": "scheduler event logs"},
            "dedup_kills": dedup_kills[:20],
        }

    def recent_logs(self, limit: int = 200) -> list[dict[str, Any]]:
        columns = "id,run_id,node_id,level,message,created_at"
        return [dict(r) for r in self._query(f"SELECT {columns} FROM logs ORDER BY id DESC LIMIT ?", (limit,))]

    def lessons(self, limit: int = 100, scope: str | None = None) -> list[dict[str, Any]]:
        if scope is None:
            return [dict(r) for r in self._query("SELECT * FROM lessons ORDER BY id DESC LIMIT ?", (limit,))]
        return [dict(r) for r in self._query("SELECT * FROM lessons WHERE scope=? ORDER BY id DESC LIMIT ?", (scope, limit))]

    def session_chains(self, limit: int = 5) -> list[dict[str, Any]]:
        """Find conversation chains: groups of runs connected by resume.

        A chain starts with a run that was resumed by a later run.
        Returns only chains with 2+ runs (single runs are not chains).
        """
        # Find all resume links: (new_run_id, source_run_id)
        links = self._query(
            "SELECT run_id, data_json FROM logs WHERE message='run resumed previous context' ORDER BY id"
        )
        # Build adjacency: source -> [new_runs]
        children: dict[str, list[str]] = {}
        all_in_chain: set[str] = set()
        for row in links:
            try:
                data = json.loads(row["data_json"] or "{}")
                source = data.get("source_run_id", "")
                new_run = row["run_id"]
                if source:
                    children.setdefault(source, []).append(new_run)
                    all_in_chain.add(source)
                    all_in_chain.add(new_run)
            except (json.JSONDecodeError, KeyError):
                continue
        if not all_in_chain:
            return []
        # Find chain roots (nodes with no parent)
        child_set = set()
        for kids in children.values():
            child_set.update(kids)
        roots = all_in_chain - child_set
        # BFS to collect chains
        chains = []
        for root in roots:
            chain_runs = []
            queue = [root]
            while queue:
                rid = queue.pop(0)
                run = self._one("SELECT id,title,status,created_at FROM runs WHERE id=?", (rid,))
                if run:
                    node_count = self._one("SELECT COUNT(*) FROM wbs_nodes WHERE run_id=?", (rid,))[0]
                    done_count = self._one("SELECT COUNT(*) FROM wbs_nodes WHERE run_id=? AND status='completed'", (rid,))[0]
                    chain_runs.append({
                        "id": run["id"],
                        "title": (run["title"] or "")[:60],
                        "status": run["status"],
                        "created_at": run["created_at"],
                        "node_count": node_count,
                        "completed_nodes": done_count,
                    })
                    queue.extend(children.get(rid, []))
            if len(chain_runs) >= 2:
                chains.append({"runs": chain_runs, "count": len(chain_runs)})
        chains.sort(key=lambda c: c["runs"][-1]["created_at"] if c["runs"] else "", reverse=True)
        return chains[:limit]
