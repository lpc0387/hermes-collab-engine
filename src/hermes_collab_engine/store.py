from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .models import RiskPolicy

# ── Node / Worker / Run status string constants ──
NODE_STATUS_PENDING = "pending"
NODE_STATUS_RUNNING = "running"
NODE_STATUS_WAITING = "waiting"
NODE_STATUS_LEADER_DECIDING = "leader_deciding"
NODE_STATUS_COMPLETED = "completed"
NODE_STATUS_FAILED = "failed"

RUN_STATUS_CREATED = "created"
RUN_STATUS_RUNNING = "running"
RUN_STATUS_COMPLETED = "completed"
RUN_STATUS_FAILED = "failed"

SCHEMA = """PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY,title TEXT NOT NULL,request TEXT NOT NULL,status TEXT NOT NULL,complexity_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,completed_at TEXT);
CREATE TABLE IF NOT EXISTS wbs_nodes (id TEXT NOT NULL,run_id TEXT NOT NULL,parent_id TEXT,title TEXT NOT NULL,description TEXT NOT NULL,capability TEXT NOT NULL,complexity INTEGER NOT NULL,dependencies_json TEXT NOT NULL DEFAULT '[]',parallelizable INTEGER NOT NULL DEFAULT 1,deliverable TEXT NOT NULL,status TEXT NOT NULL,attempt INTEGER NOT NULL DEFAULT 1,checkpoint INTEGER NOT NULL DEFAULT 0,result TEXT,session_id TEXT,duration_seconds REAL,error TEXT,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,brief TEXT DEFAULT '',shared_brief TEXT DEFAULT '',estimated_duration INTEGER DEFAULT NULL,write_targets_json TEXT DEFAULT '[]',result_struct_json TEXT DEFAULT NULL,skills_json TEXT DEFAULT NULL,tools_json TEXT DEFAULT NULL,fingerprint TEXT DEFAULT '',PRIMARY KEY (id, run_id),FOREIGN KEY(run_id) REFERENCES runs(id));
CREATE TABLE IF NOT EXISTS workers (id TEXT PRIMARY KEY,run_id TEXT NOT NULL,node_id TEXT,status TEXT NOT NULL,started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,duration_seconds REAL,session_id TEXT,error TEXT);
CREATE TABLE IF NOT EXISTS logs (id INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT,node_id TEXT,level TEXT NOT NULL,message TEXT NOT NULL,data_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS lessons (id INTEGER PRIMARY KEY AUTOINCREMENT,scope TEXT NOT NULL DEFAULT 'global',category TEXT NOT NULL,lesson TEXT NOT NULL,evidence_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS metrics (key TEXT PRIMARY KEY,value_json TEXT NOT NULL,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY,value_json TEXT NOT NULL,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS node_results (node_id TEXT PRIMARY KEY,run_id TEXT NOT NULL,result_text TEXT DEFAULT '',result_struct_json TEXT DEFAULT NULL,updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS run_state (run_id TEXT PRIMARY KEY,paused INTEGER DEFAULT 0,checkpoint_paused_nodes_json TEXT DEFAULT '[]',updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS context_snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT NOT NULL,snapshot_type TEXT NOT NULL,node_id TEXT DEFAULT NULL,snapshot_json TEXT NOT NULL,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY,user_id TEXT NOT NULL DEFAULT 'default',title TEXT DEFAULT '',status TEXT DEFAULT 'active',created_at TEXT NOT NULL,updated_at TEXT);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id, updated_at DESC);
CREATE TABLE IF NOT EXISTS session_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    user_request TEXT NOT NULL,
    aggregate TEXT NOT NULL DEFAULT '',
    turn_index INTEGER NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_session_turns_session ON session_turns(session_id, turn_index);
CREATE TABLE IF NOT EXISTS run_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    file_path TEXT NOT NULL,
    file_size INTEGER NOT NULL DEFAULT 0,
    file_mtime TEXT NOT NULL DEFAULT '',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_run_files_run ON run_files(run_id);
"""


class CollabStore:
    def __init__(self, db_path: str | Path, *, skip_cleanup: bool = False):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        # Pinning synchronous=FULL forces every commit to wait for the fsync
        # of the WAL frame to durable storage. On production SSD hosts this
        # adds ~1-3 ms per commit; the cost is negligible compared to data loss.
        self.conn.execute("PRAGMA synchronous=FULL")
        # Record engine start timestamp so _cleanup_stale_workers can
        # distinguish "this incarnation's runs" from "previous incarnation's runs".
        # CRITICAL: format MUST match SQLite CURRENT_TIMESTAMP ('YYYY-MM-DD HH:MM:SS')
        # so string comparison is correct.
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        self._engine_start_ts = now.strftime("%Y-%m-%d %H:%M:%S")
        # Also keep a window (5s) for clock skew between cleanup check and insert
        import time as _time
        self._engine_start_ts_with_skew = now.strftime(
            "%Y-%m-%d %H:%M:%S"
        )  # use strict equality with created_at (no skew window)
        self._skip_cleanup = skip_cleanup
        with self.lock:
            self.conn.executescript(SCHEMA)
            self._ensure_schema()
            self.conn.commit()

    def _ensure_schema(self) -> None:
        # Order matters:
        # 1. Column migrations MUST run BEFORE the composite-PK migration,
        #    because the PK migration does a RENAME + CREATE + INSERT INTO
        #    ... SELECT *, and SELECT * will fail on columns that haven't
        #    been added to the old table yet.
        self._migrate_lessons_scope()
        self._migrate_lessons_tags()
        self._migrate_wbs_checkpoint()
        self._migrate_wbs_context_fields()
        self._migrate_runs_agent()
        self._migrate_runs_meta_json()
        self._migrate_runs_session_id()
        self._migrate_session_turns_messages()
        self._migrate_sessions_agent_session()
        # 2. Composite-PK migration last (it renames + recreates + drops)
        self._migrate_wbs_nodes_composite_pk()
        # 3. Stale-worker cleanup is safe to run after the schema is final
        if not self._skip_cleanup:
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
        # Filter by created_at < engine start ts so we never touch
        # runs created by THIS incarnation (avoids race with newly spawned run CLIs).
        stale_run_ids = [
            row["id"] for row in self._query(
                "SELECT id FROM runs WHERE status='running' AND created_at < ?",
                (self._engine_start_ts,)
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
            # Finally mark the runs themselves as failed (SCOPED to stale_run_ids)
            self._execute(
                f"""UPDATE runs SET status='failed',
                   updated_at=CURRENT_TIMESTAMP,
                   completed_at=CURRENT_TIMESTAMP
                   WHERE id IN ({placeholders})""",
                tuple(stale_run_ids),
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

    def _migrate_lessons_tags(self) -> None:
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(lessons)").fetchall()}
        if "tags" not in columns:
            self.conn.execute("ALTER TABLE lessons ADD COLUMN tags TEXT DEFAULT '[]'")

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

    def _migrate_runs_meta_json(self) -> None:
        """Add runs.meta_json so callers can store per-run metadata such as
        the selected package and its skill collection without redefining the
        runs table schema.
        """
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(runs)").fetchall()}
        if "meta_json" not in columns:
            self.conn.execute("ALTER TABLE runs ADD COLUMN meta_json TEXT NOT NULL DEFAULT '{}'")

    def _migrate_runs_session_id(self) -> None:
        """Add runs.session_id to link runs to sessions."""
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(runs)").fetchall()}
        if "session_id" not in columns:
            self.conn.execute("ALTER TABLE runs ADD COLUMN session_id TEXT DEFAULT NULL")

    def _migrate_session_turns_messages(self) -> None:
        """Add messages_json and run_ids_json columns to session_turns.

        These columns enable storing full message snapshots for each turn
        and tracking all run IDs associated with a session.
        """
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(session_turns)").fetchall()}
        if "messages_json" not in columns:
            try:
                self.conn.execute("ALTER TABLE session_turns ADD COLUMN messages_json TEXT DEFAULT '[]'")
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise
        if "run_ids_json" not in columns:
            try:
                self.conn.execute("ALTER TABLE session_turns ADD COLUMN run_ids_json TEXT DEFAULT '[]'")
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise

    def _migrate_sessions_agent_session(self) -> None:
        """Add agent_session_id and last_active columns to sessions table.

        agent_session_id stores the backend agent's persistent session ID so
        that idle runs can be resumed without losing the agent's context.
        last_active tracks the most recent user activity timestamp.
        """
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(sessions)").fetchall()}
        if "agent_session_id" not in columns:
            try:
                self.conn.execute("ALTER TABLE sessions ADD COLUMN agent_session_id TEXT DEFAULT ''")
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise
        if "last_active" not in columns:
            try:
                self.conn.execute("ALTER TABLE sessions ADD COLUMN last_active TEXT DEFAULT ''")
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise

    def _migrate_wbs_nodes_composite_pk(self) -> None:
        """Migrate wbs_nodes to a composite PRIMARY KEY (id, run_id).

        The original schema declared PRIMARY KEY (id) so that the same
        node_id (e.g. 'wbs-1', 'wbs-2') from **different runs** would
        collide via INSERT OR REPLACE, deleting old run's data. The fix
        changes the PK to (id, run_id) so cross-run node IDs coexist.
        """
        rows = self.conn.execute("PRAGMA table_info(wbs_nodes)").fetchall()
        if not rows:
            return
        pk_cols = [r[1] for r in rows if r[5] > 0]
        if set(pk_cols) == {"id", "run_id"}:
            return
        with self.lock:
            self.conn.execute("ALTER TABLE wbs_nodes RENAME TO wbs_nodes__old")
            self.conn.execute("""
                CREATE TABLE wbs_nodes (
                    id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    parent_id TEXT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    capability TEXT NOT NULL,
                    complexity INTEGER NOT NULL,
                    dependencies_json TEXT NOT NULL DEFAULT '[]',
                    parallelizable INTEGER NOT NULL DEFAULT 1,
                    deliverable TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 1,
                    checkpoint INTEGER NOT NULL DEFAULT 0,
                    result TEXT,
                    session_id TEXT,
                    duration_seconds REAL,
                    error TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                    brief TEXT DEFAULT '',
                    shared_brief TEXT DEFAULT '',
                    estimated_duration INTEGER DEFAULT NULL,
                    write_targets_json TEXT DEFAULT '[]',
                    result_struct_json TEXT DEFAULT NULL,
                    skills_json TEXT DEFAULT NULL,
                    tools_json TEXT DEFAULT NULL,
                    fingerprint TEXT DEFAULT '',
                    PRIMARY KEY (id, run_id),
                    FOREIGN KEY(run_id) REFERENCES runs(id)
                )
            """)
            self.conn.execute("""
                INSERT INTO wbs_nodes
                  (id, run_id, parent_id, title, description, capability,
                   complexity, dependencies_json, parallelizable, deliverable,
                   status, attempt, checkpoint, result, session_id,
                   duration_seconds, error, created_at, updated_at,
                   brief, shared_brief, estimated_duration, write_targets_json,
                   result_struct_json, skills_json, tools_json, fingerprint)
                SELECT id, run_id, parent_id, title, description, capability,
                   complexity, dependencies_json, parallelizable, deliverable,
                   status, attempt,
                   COALESCE(checkpoint, 0),
                   result, session_id, duration_seconds, error, created_at, updated_at,
                   COALESCE(brief, ''), COALESCE(shared_brief, ''),
                   COALESCE(estimated_duration, NULL),
                   COALESCE(write_targets_json, '[]'),
                   COALESCE(result_struct_json, NULL),
                   COALESCE(skills_json, NULL),
                   COALESCE(tools_json, NULL),
                   COALESCE(fingerprint, '')
                FROM wbs_nodes__old
            """)
            self.conn.execute("DROP TABLE wbs_nodes__old")
            self.conn.commit()

    def _execute(self, sql: str, params: tuple = ()):
        sql = sql.replace("CURRENT_TIMESTAMP", "datetime('now','localtime')")
        with self.lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def _query(self, sql: str, params: tuple = ()):
        sql = sql.replace("CURRENT_TIMESTAMP", "datetime('now','localtime')")
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def _one(self, sql: str, params: tuple = ()):
        sql = sql.replace("CURRENT_TIMESTAMP", "datetime('now','localtime')")
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
        self._execute("INSERT INTO logs(run_id,node_id,level,message,data_json,created_at) VALUES(?,?,?,?,?,CURRENT_TIMESTAMP)", (run_id, node_id, level, message, json.dumps(data or {}, ensure_ascii=False)))

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

    def create_run(self, run_id: str, title: str, request: str, complexity: dict[str, Any], agent: str = "claude-code", session_id: str | None = None) -> None:
        self._execute("INSERT INTO runs(id,title,request,status,complexity_json,agent,session_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)", (run_id, title, request, "created", json.dumps(complexity, ensure_ascii=False), agent, session_id))
        self.log(run_id, "info", "run created", {"title": title, "agent": agent, "session_id": session_id})

    def update_run(self, run_id: str, status: str) -> None:
        completed_sql = ", completed_at=CURRENT_TIMESTAMP" if status in {"completed", "failed"} else ""
        self._execute(f"UPDATE runs SET status=?, updated_at=CURRENT_TIMESTAMP{completed_sql} WHERE id=?", (status, run_id))

    def set_run_meta(self, run_id: str, meta: dict[str, Any]) -> None:
        """Persist a per-run metadata blob (e.g. selected package + skill set).

        The dict is stored as JSON in ``runs.meta_json`` and is overwritten on
        each call. Existing keys not present in *meta* are preserved.
        """
        existing = self.get_run_meta(run_id) or {}
        merged = dict(existing)
        merged.update(meta)
        self._execute(
            "UPDATE runs SET meta_json=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (json.dumps(merged, ensure_ascii=False), run_id),
        )

    def get_run_meta(self, run_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT meta_json FROM runs WHERE id=?", (run_id,))
        if not row:
            return None
        raw = row["meta_json"] or "{}"
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    def create_session(self, user_id: str, title: str = "") -> dict[str, Any]:
        import uuid
        session_id = f"ses_{uuid.uuid4().hex[:12]}"
        now = self._one("SELECT datetime('now','localtime')")[0]
        self._execute(
            "INSERT INTO sessions(id,user_id,title,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (session_id, user_id, title, "active", now, now),
        )
        return {"id": session_id, "created_at": now}

    def list_sessions(self, user_id: str) -> list[dict[str, Any]]:
        rows = self._query(
            """SELECT s.id, s.title, s.status, s.created_at, s.updated_at,
                      r.id AS latest_run_id, r.status AS latest_run_status,
                      r.created_at AS latest_run_created_at
               FROM sessions s
               LEFT JOIN runs r ON r.session_id = s.id
                 AND r.id = (SELECT r2.id FROM runs r2 WHERE r2.session_id = s.id ORDER BY r2.created_at DESC LIMIT 1)
               WHERE s.user_id = ?
               ORDER BY s.updated_at DESC""",
            (user_id,),
        )
        result = []
        for row in rows:
            session = {
                "id": row["id"],
                "title": row["title"] or "",
                "status": row["status"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            if row["latest_run_id"]:
                session["latest_run"] = {
                    "id": row["latest_run_id"],
                    "status": row["latest_run_status"],
                    "created_at": row["latest_run_created_at"],
                }
            else:
                session["latest_run"] = None
            result.append(session)
        return result

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        row = self._one(
            "SELECT id, user_id, title, status, created_at, updated_at FROM sessions WHERE id=?",
            (session_id,),
        )
        if not row:
            return None
        session = dict(row)
        latest = self._one(
            "SELECT id, status, created_at FROM runs WHERE session_id=? ORDER BY created_at DESC LIMIT 1",
            (session_id,),
        )
        session["latest_run"] = dict(latest) if latest else None
        return session

    def update_session(self, session_id: str, data: dict[str, Any]) -> dict[str, Any] | None:
        allowed = {"title", "status", "agent_session_id", "last_active"}
        updates = {k: v for k, v in data.items() if k in allowed}
        if not updates:
            return self.get_session(session_id)
        set_clause = ", ".join(f"{k}=?" for k in updates)
        values = list(updates.values()) + [session_id]
        self._execute(
            f"UPDATE sessions SET {set_clause}, updated_at=datetime('now','localtime') WHERE id=?",
            tuple(values),
        )
        return self.get_session(session_id)

    def save_agent_session(self, session_id: str, agent_session_id: str) -> None:
        """Persist the backend agent's session ID for later resume."""
        self._execute(
            "UPDATE sessions SET agent_session_id=?, updated_at=datetime('now','localtime') WHERE id=?",
            (agent_session_id, session_id),
        )

    def get_agent_session(self, session_id: str) -> str | None:
        """Return the saved agent session ID for a session, if any."""
        row = self._one("SELECT agent_session_id FROM sessions WHERE id=?", (session_id,))
        if row and row["agent_session_id"]:
            return row["agent_session_id"]
        return None

    def touch_session(self, session_id: str) -> None:
        """Update last_active and updated_at for a session to now."""
        self._execute(
            "UPDATE sessions SET last_active=datetime('now','localtime'), updated_at=datetime('now','localtime') WHERE id=?",
            (session_id,),
        )

    def delete_session(self, session_id: str) -> bool:
        cur = self._execute("DELETE FROM sessions WHERE id=?", (session_id,))
        return cur.rowcount > 0

    def add_session_turn(self, session_id: str, run_id: str, user_request: str, aggregate: str = "", messages: list | None = None) -> None:
        turn_index = self._one(
            "SELECT COALESCE(MAX(turn_index), 0) + 1 FROM session_turns WHERE session_id=?",
            (session_id,),
        )[0]
        _messages_json = json.dumps(messages or [], ensure_ascii=False)
        self._execute(
            "INSERT INTO session_turns(session_id,run_id,user_request,aggregate,turn_index,messages_json) VALUES(?,?,?,?,?,?)",
            (session_id, run_id, user_request, aggregate, turn_index, _messages_json),
        )

    def get_session_turns(self, session_id: str, limit: int = 5) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT * FROM session_turns WHERE session_id=? ORDER BY turn_index DESC LIMIT ?",
            (session_id, limit),
        )
        result = []
        for r in reversed(rows):
            turn = dict(r)
            # Decode messages_json if present
            raw_messages = turn.pop("messages_json", None) or "[]"
            try:
                turn["messages"] = json.loads(raw_messages) if isinstance(raw_messages, str) else (raw_messages or [])
            except (json.JSONDecodeError, TypeError):
                turn["messages"] = []
            result.append(turn)
        return result

    def add_run_to_session(self, session_id: str, run_id: str) -> None:
        """Associate a run_id with a session by adding it to run_ids_json.

        If the session has no turns yet, this is a no-op.
        The run_id is added to the latest turn's run_ids_json (deduplicated).
        """
        latest = self._one(
            "SELECT id, run_ids_json FROM session_turns WHERE session_id=? ORDER BY turn_index DESC LIMIT 1",
            (session_id,),
        )
        if not latest:
            return
        turn_id = latest["id"]
        raw = latest["run_ids_json"] or "[]"
        try:
            run_ids = json.loads(raw) if isinstance(raw, str) else (raw or [])
        except (json.JSONDecodeError, TypeError):
            run_ids = []
        if not isinstance(run_ids, list):
            run_ids = []
        if run_id not in run_ids:
            run_ids.append(run_id)
            self._execute(
                "UPDATE session_turns SET run_ids_json=? WHERE id=?",
                (json.dumps(run_ids, ensure_ascii=False), turn_id),
            )

    def save_run_files(self, run_id: str, files: list[dict]) -> None:
        self._execute("DELETE FROM run_files WHERE run_id=?", (run_id,))
        for f in files:
            self._execute(
                "INSERT INTO run_files(run_id,file_path,file_size,file_mtime) VALUES(?,?,?,?)",
                (run_id, f["path"], f["size"], f["mtime"]),
            )

    def get_run_files(self, run_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self._query(
            "SELECT file_path,file_size,file_mtime FROM run_files WHERE run_id=? ORDER BY file_path",
            (run_id,),
        )]

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

        All writes are wrapped in a single ``BEGIN IMMEDIATE`` ... ``COMMIT``
        transaction so a SIGKILL between statements cannot produce the "run terminal,
        wbs still running" partial-commit state. See engine-run-failure-modes.md.
        """
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                self.conn.execute(
                    "UPDATE workers SET status='failed', error=COALESCE(error, ?), updated_at=CURRENT_TIMESTAMP WHERE run_id=? AND status='running'",
                    (reason, run_id),
                )
                self.conn.execute(
                    "UPDATE wbs_nodes SET status='failed', error=COALESCE(error, ?), updated_at=CURRENT_TIMESTAMP WHERE run_id=? AND status IN ('running','pending')",
                    (reason, run_id),
                )
                self.conn.execute(
                    "UPDATE runs SET status='failed', updated_at=CURRENT_TIMESTAMP, completed_at=CURRENT_TIMESTAMP WHERE id=?",
                    (run_id,),
                )
                self.conn.execute(
                    "INSERT INTO logs(run_id,node_id,level,message,data_json,created_at) VALUES(?,?,?,?,?,CURRENT_TIMESTAMP)",
                    (run_id, None, "error", "run interrupted; stale running work marked failed", json.dumps({"reason": reason}, ensure_ascii=False)),
                )
                self.conn.execute(
                    "INSERT INTO lessons(scope,category,lesson,evidence_json,created_at) VALUES(?,?,?,?,CURRENT_TIMESTAMP)",
                    (
                        "engine",
                        "interrupt-cleanup",
                        "Interrupted parent runs must fail/close all running workers and pending/running WBS nodes; otherwise dashboards can show stale ghost-running work.",
                        json.dumps({"run_id": run_id, "reason": reason}, ensure_ascii=False),
                    ),
                )
                self.conn.commit()
            except Exception:
                try:
                    self.conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            try:
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass

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
            """INSERT INTO context_snapshots(run_id,snapshot_type,node_id,snapshot_json,created_at) VALUES(?,?,?,?,CURRENT_TIMESTAMP)""",
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

    def update_node_description(self, run_id: str, node_id: str, description: str) -> None:
        self._execute("UPDATE wbs_nodes SET description=?, updated_at=CURRENT_TIMESTAMP, status='pending', attempt=1 WHERE run_id=? AND id=?", (description, run_id, node_id))

    def worker_start(self, worker_id: str, run_id: str, node_id: str) -> None:
        self._execute("INSERT OR REPLACE INTO workers(id,run_id,node_id,status,updated_at) VALUES(?,?,?,?,CURRENT_TIMESTAMP)", (worker_id, run_id, node_id, "running"))

    def worker_finish(self, worker_id: str, status: str, duration_seconds: float | None = None, session_id: str | None = None, error: str | None = None) -> None:
        self._execute("UPDATE workers SET status=?, duration_seconds=?, session_id=?, error=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (status, duration_seconds, session_id, error, worker_id))

    def add_lesson(self, category: str, lesson: str, evidence: dict[str, Any] | None = None, scope: str = "global", tags: list[str] | None = None) -> None:
        self._execute(
            "INSERT INTO lessons(scope,category,lesson,evidence_json,tags,created_at) VALUES(?,?,?,?,?,CURRENT_TIMESTAMP)",
            (scope, category, lesson, json.dumps(evidence or {}, ensure_ascii=False), json.dumps(tags or [], ensure_ascii=False)),
        )

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

    def list_runs(self, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        columns = "id,title,status,created_at,updated_at,completed_at,agent,meta_json,session_id"
        if status:
            rows = [dict(r) for r in self._query(f"SELECT {columns} FROM runs WHERE status=? ORDER BY created_at DESC LIMIT ?", (status, limit))]
        else:
            rows = [dict(r) for r in self._query(f"SELECT {columns} FROM runs ORDER BY created_at DESC LIMIT ?", (limit,))]
        for r in rows:
            # Decode meta_json once on read so the dashboard can render package/skill tags
            raw = r.get("meta_json") or "{}"
            try:
                r["meta"] = json.loads(raw)
            except json.JSONDecodeError:
                r["meta"] = {}
        return rows

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
        # Build a compact summary (first 200 chars of request) so dashboard
        # resume banner can show something useful without dumping the full task.
        summary = (run_dict.get("request") or run_dict.get("title") or "")[:200]
        # Rough token estimate: 1 token ~= 4 chars of compact context.
        compact_chars = (
            len(summary)
            + sum(len(str(x.get("message", ""))) for x in recent_logs)
            + sum(len(str(x.get("title", ""))) for x in nodes)
        )
        estimated_tokens = max(1, compact_chars // 4)
        return {
            "run": run_dict,
            "summary": summary,
            "estimated_tokens": estimated_tokens,
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
            counts = {"total": 0, "pending": 0, "running": 0, "waiting": 0, "leader_deciding": 0, "completed": 0, "failed": 0, "skipped": 0}
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
        run_columns = "*" if full else "id,title,status,created_at,updated_at,completed_at,agent,session_id"
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


def get_model_context_limit(model_name: str) -> int:
    """常见模型的 context window 限制"""
    limits = {
        "deepseek-v4-flash": 65536,
        "deepseek-v4": 65536,
        "gpt-4": 8192,
        "gpt-4-turbo": 128000,
        "claude-3-opus": 200000,
        "claude-3-sonnet": 200000,
        "claude-4": 200000,
        # 默认
        "DEFAULT": 8192,
    }
    for key, val in limits.items():
        if key in model_name.lower():
            return val
    return limits["DEFAULT"]
