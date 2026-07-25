# ADR: Hermes Collab Engine — Data Model, Schema, CLI & Test Strategy

**Date:** 2026-07-09  
**Scope:** Core data model (Python dataclasses), SQLite DDL, argparse command tree, and test isolation strategy.

---

## 1. Task Field Types & Constraints (Python Dataclasses)

Every core model is a frozen `@dataclass` in `src/hermes_collab_engine/models.py`. None are frozen (they are mutated throughout the lifecycle), but `asdict` is used for JSON/DB serialization.

### `WBSNode` — the primary task/work-breakdown node

```python
@dataclass
class WBSNode:
    id: str                                        # Unique within run (e.g. "wbs-1", "wbs-2a")
    title: str                                     # NOT NULL, short label
    description: str                               # NOT NULL, full instruction for the worker
    capability: str                                # NOT NULL, capability tag (implementation, analysis, testing...)
    complexity: int                                # NOT NULL, 1-10 scale
    dependencies: list[str]                        # JSON array stored in dependencies_json; list of node IDs
    parallelizable: bool                           # INTEGER 0/1 in DB
    deliverable: str                               # NOT NULL, expected output description
    status: str = "pending"                        # pending|running|leader_deciding|waiting|completed|failed|skipped
    parent_id: str | None = None                   # Parent node ID for shard hierarchy
    checkpoint: bool = False                       # INTEGER 0/1; if True, execution pauses here for review
    attempt: int = 1                               # Retry counter
    brief: str = ""                                # Short summary for context injection
    estimated_duration: int | None = None          # Planner estimate in seconds
    write_targets: list[str] = field(default_factory=list)  # JSON array of file paths this node writes to
    fingerprint: str = ""                          # Dedup hash of the node content
    skills_json: str = ""                          # Serialized skill names selected for this node
    tools_json: str = ""                           # Serialized tool profiles selected for this node
```

**Constraints (enforced at the DB level, not Python):**
- Composite PK `(id, run_id)` — same node ID can exist in different runs
- `run_id` is a foreign key to `runs(id)`
- `status` is a free-text string (not an enum); downstream code uses string constants in `store.py`

### `ComplexityScore`

```python
@dataclass
class ComplexityScore:
    domain: int         # 1-10 domain knowledge required
    steps: int          # 1-10 number of steps
    ambiguity: int      # 1-10 ambiguity level
    coupling: int       # 1-10 cross-node coupling
    risk: int           # 1-10 risk level
    overall: int        # 1-10 aggregate score (drives WBS decision: ≤3 → single dispatch)
    routing: str        # free-text routing hint
```

### `RiskPolicy`

```python
@dataclass
class RiskPolicy:
    low: str = "continue"                        # continue|notify|pause|checkpoint
    medium: str = "continue"                     # same
    high: str = "continue"                       # same
    checkpoint_timeout: int = 900                # seconds before auto-resume
```

### `CheckpointDecision`

```python
@dataclass
class CheckpointDecision:
    run_id: str
    node_id: str
    action: str                                  # continue|redo|skip_downstream|abort
    reason: str = ""

    def __post_init__(self):
        allowed = {"continue", "redo", "skip_downstream", "abort"}
        assert self.action in allowed
```

### `Plan`

```python
@dataclass
class Plan:
    nodes: list[WBSNode]
    shared_brief: str = ""
    risk_policy: dict[str, Any] = field(default_factory=dict)
    task_type: str = "development"
```

### `WorkerResult`

```python
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
```

---

## 2. SQLite Table DDL

Defined in `src/hermes_collab_engine/store.py` as the `SCHEMA` constant + additive migrations in `_ensure_schema()`.

### Core Tables (from `SCHEMA` constant)

```sql
-- WAL mode for concurrent reads
PRAGMA journal_mode=WAL;

-- ── Runs: top-level task execution ──
CREATE TABLE IF NOT EXISTS runs (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    request         TEXT NOT NULL,
    status          TEXT NOT NULL,                  -- created|running|completed|failed
    complexity_json TEXT NOT NULL DEFAULT '{}',
    agent           TEXT DEFAULT 'claude-code',     -- added via migration
    session_id      TEXT DEFAULT NULL,              -- added via migration, FK to sessions
    meta_json       TEXT NOT NULL DEFAULT '{}',     -- added via migration
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at    TEXT
);

-- ── WBS Nodes: individual work items within a run ──
CREATE TABLE IF NOT EXISTS wbs_nodes (
    id                  TEXT NOT NULL,
    run_id              TEXT NOT NULL,
    parent_id           TEXT,
    title               TEXT NOT NULL,
    description         TEXT NOT NULL,
    capability          TEXT NOT NULL,
    complexity          INTEGER NOT NULL,
    dependencies_json   TEXT NOT NULL DEFAULT '[]',
    parallelizable      INTEGER NOT NULL DEFAULT 1,
    deliverable         TEXT NOT NULL,
    status              TEXT NOT NULL,
    attempt             INTEGER NOT NULL DEFAULT 1,
    checkpoint          INTEGER NOT NULL DEFAULT 0,
    result              TEXT,
    session_id          TEXT,
    duration_seconds    REAL,
    error               TEXT,
    brief               TEXT DEFAULT '',
    shared_brief        TEXT DEFAULT '',
    estimated_duration  INTEGER DEFAULT NULL,
    write_targets_json  TEXT DEFAULT '[]',
    result_struct_json  TEXT DEFAULT NULL,
    skills_json         TEXT DEFAULT NULL,
    tools_json          TEXT DEFAULT NULL,
    fingerprint         TEXT DEFAULT '',
    created_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id, run_id),
    FOREIGN KEY (run_id) REFERENCES runs(id)
);

-- ── Workers: per-node agent process tracking ──
CREATE TABLE IF NOT EXISTS workers (
    id              TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL,
    node_id         TEXT,
    status          TEXT NOT NULL,                  -- running|completed|failed
    started_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    duration_seconds REAL,
    session_id      TEXT,
    error           TEXT
);

-- ── Logs: structured event log ──
CREATE TABLE IF NOT EXISTS logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT,
    node_id     TEXT,
    level       TEXT NOT NULL,                      -- info|warning|error|debug|review
    message     TEXT NOT NULL,
    data_json   TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- ── Lessons: learned experience records ──
CREATE TABLE IF NOT EXISTS lessons (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    scope         TEXT NOT NULL DEFAULT 'global',   -- global|project|run|node|wbs-family|engine
    category      TEXT NOT NULL,
    lesson        TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    tags          TEXT DEFAULT '[]',
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- ── Metrics & Settings: key-value stores ──
CREATE TABLE IF NOT EXISTS metrics (
    key        TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- ── Node Results: separate from wbs_nodes for structured result storage ──
CREATE TABLE IF NOT EXISTS node_results (
    node_id           TEXT PRIMARY KEY,
    run_id            TEXT NOT NULL,
    result_text       TEXT DEFAULT '',
    result_struct_json TEXT DEFAULT NULL,
    updated_at        TEXT DEFAULT CURRENT_TIMESTAMP
);

-- ── Run State: pause/checkpoint coordination ──
CREATE TABLE IF NOT EXISTS run_state (
    run_id                     TEXT PRIMARY KEY,
    paused                     INTEGER DEFAULT 0,
    checkpoint_paused_nodes_json TEXT DEFAULT '[]',
    updated_at                 TEXT DEFAULT CURRENT_TIMESTAMP
);

-- ── Context Snapshots: serialized planner state for resume ──
CREATE TABLE IF NOT EXISTS context_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL,
    snapshot_type TEXT NOT NULL,                    -- pre_compaction|node_completed|checkpoint
    node_id       TEXT DEFAULT NULL,
    snapshot_json TEXT NOT NULL,
    created_at    TEXT DEFAULT CURRENT_TIMESTAMP
);

-- ── Sessions: user conversation sessions ──
CREATE TABLE IF NOT EXISTS sessions (
    id               TEXT PRIMARY KEY,
    user_id          TEXT NOT NULL DEFAULT 'default',
    title            TEXT DEFAULT '',
    status           TEXT DEFAULT 'active',         -- active|closed
    agent_session_id TEXT DEFAULT '',               -- added via migration
    last_active      TEXT DEFAULT '',               -- added via migration
    created_at       TEXT NOT NULL,
    updated_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id, updated_at DESC);

-- ── Session Turns: per-turn history within a session ──
CREATE TABLE IF NOT EXISTS session_turns (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT NOT NULL,
    run_id        TEXT NOT NULL,
    user_request  TEXT NOT NULL,
    aggregate     TEXT NOT NULL DEFAULT '',
    turn_index    INTEGER NOT NULL,
    messages_json TEXT DEFAULT '[]',                -- added via migration
    run_ids_json  TEXT DEFAULT '[]',                -- added via migration
    created_at    TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_session_turns_session ON session_turns(session_id, turn_index);

-- ── Run Files: file inventory captured at run completion ──
CREATE TABLE IF NOT EXISTS run_files (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id    TEXT NOT NULL,
    file_path TEXT NOT NULL,
    file_size INTEGER NOT NULL DEFAULT 0,
    file_mtime TEXT NOT NULL DEFAULT '',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_run_files_run ON run_files(run_id);
```

### Migration Strategy

All schema evolution is additive (`ALTER TABLE ADD COLUMN`) wrapped in `try/except OperationalError` for idempotency. The one exception is the composite-PK migration on `wbs_nodes`, which does a `RENAME → CREATE → INSERT INTO ... SELECT → DROP` cycle. Migrations run in `_ensure_schema()` in dependency order:

1. `_migrate_lessons_scope()` / `_migrate_lessons_tags()`
2. `_migrate_wbs_checkpoint()` / `_migrate_wbs_context_fields()`
3. `_migrate_runs_agent()` / `_migrate_runs_meta_json()` / `_migrate_runs_session_id()`
4. `_migrate_session_turns_messages()` / `_migrate_sessions_agent_session()`
5. `_migrate_wbs_nodes_composite_pk()` (last — destructive)
6. `_cleanup_stale_workers()` (runtime, not schema)

---

## 3. argparse Command Tree

Defined in `src/hermes_collab_engine/cli.py:main()`. Root parser is `hermes-collab`; no subcommand → enters interactive dialog mode.

```
hermes-collab
├── dialog                          # [default] Interactive dialog mode (agent detection + mode selection)
│   ├── --cwd, --db, --model, --quiet
│
├── run                             # Execute a collaboration task
│   ├── request [nargs=*]
│   ├── --request-file, --title
│   ├── --cwd, --db, --model
│   ├── --leader-model, --worker-model
│   ├── --agent, --leader-agent, --worker-agent
│   ├── --concurrency, --global-max-concurrent
│   ├── --timeout, --max-retries, --split-count
│   ├── --no-aggregate, --json
│   └── --provider, --provider-base-url, --provider-api-key, --provider-model
│
├── server                          # Launch web management dashboard
│   ├── --host, --port
│   ├── --cwd, --db, --model
│   ├── --leader-model, --worker-model
│   └── --agent
│
├── status                          # Show engine overview
│   ├── --db
│   └── --json
│
├── lesson                          # Manage lessons learned (subcommand required)
│   ├── add
│   │   ├── --db, --scope, --category, --lesson, --source, --evidence-json
│   └── list
│       ├── --db, --limit, --category, --scope
│       └── --json
│
├── parent-log                      # Write an operator log entry
│   ├── --db, --run-id, --node-id
│   ├── --level (debug|info|warning|error)
│   ├── --message, --data-json
│   └── --json
│
├── kill-node                       # Kill a running worker process
│   ├── --db, --node-id, --run-id
│   ├── --reason, --signal (TERM|KILL|INT)
│   └── --json
│
├── split-node                      # Proactively split a WBS node
│   ├── --db, --node-id, --run-id
│   ├── --split-count, --reason
│   └── --json
│
├── skip-node                       # Mark a node failed, continue downstream
│   ├── --db, --node-id, --run-id
│   ├── --reason
│   └── --json
│
├── pause-run                       # Pause worker dispatch for a run
│   ├── --db, --cwd, --run-id, --reason
│   └── --json
│
├── resume-run                      # Resume a paused run
│   ├── --db, --cwd, --run-id, --reason
│   └── --json
│
├── snapshot                        # Show persisted run state
│   ├── --db, --run-id
│   └── --json
│
├── context-snapshot                # Show context snapshots for a run
│   ├── --db, [run_id]
│   ├── --latest
│   └── --type (pre_compaction|node_completed|checkpoint)
│
├── save-snapshot                   # Manually save a context snapshot
│   ├── --db, --cwd, run_id
│   ├── --type, --node-id
│   ├── --decisions, --user-instructions
│   └── --json
│
├── agents                          # List agent backends
│   ├── --db, --available
│   └── --json
│
├── skills                          # List worker skills
│   ├── --node-type, --task
│   └── --json
│
├── tools                           # List tool/MCP profiles
│   ├── --node-type, --task
│   └── --json
│
├── mcp-server                      # Manage registered MCP servers (subcommand required)
│   ├── list
│   │   ├── --db
│   │   └── --json
│   └── add
│       ├── --db, --name, --command, --args, --env
│       ├── --tools, --description, --display-name, --capabilities
│       └── --json
│   └── remove
│       ├── --db, --name
│       └── --json
│
├── verify-release                  # Run release verification checks
│   └── --json
│
├── verify-v45                      # Alias for verify-release
│   └── --json
│
├── redo-node                       # Create a redo node keeping source for audit
│   ├── --db, --cwd, --run-id, --node-id
│   ├── --reason, --description-delta, --cascade, --worker-model
│   └── --json
│
├── doctor                          # Diagnose .runtime-config.json
│   ├── --config
│   └── --json
│
├── config                          # Manage runtime config (subcommand required)
│   ├── show
│   │   ├── --config
│   │   └── --json
│   ├── set
│   │   ├── --config
│   │   ├── field (worker-model|leader-model|active-provider|worker-agent)
│   │   ├── value
│   │   └── --json
│   └── add-provider
│       ├── --config, name
│       ├── --base-url, --api-key, --protocol, --default-model
│       └── --json
│
├── setting                         # Manage persistent engine settings (subcommand required)
│   ├── get
│   │   ├── --db
│   │   └── key
│   ├── set
│   │   ├── --db, key, value
│   └── list
│       └── --db
│
├── risk-policy                     # Show/update risk policy (subcommand required)
│   ├── show
│   │   └── --db
│   └── set
│       ├── --db
│       └── --low, --medium, --high, --checkpoint-timeout
│
└── python-compat                   # Check Python 3.13+ feature compatibility
    └── --json
```

**Routing pattern:** Each subcommand is dispatched via `if args.cmd == "..."` blocks in `main()`. The handler typically instantiates `CollabStore(db_path)`, performs the operation, and prints either JSON (`--json`) or human-readable text.

---

## 4. Test Isolation Approach (tempfile + setUp/tearDown)

### Pattern

The project uses **`unittest.TestCase`** exclusively (no pytest fixtures). Every test that touches a database or filesystem follows this strict pattern:

```python
import tempfile
import unittest
from pathlib import Path
from src.hermes_collab_engine.store import CollabStore

class MyTests(unittest.TestCase):

    def test_something(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CollabStore(Path(tmp) / "db.sqlite3")
            # ... test logic ...
            # Assertions run inside the `with` block
```

### Rationale

| Concern | Approach |
|---|---|
| **DB isolation** | Each test creates its own SQLite DB in a unique `tempfile.TemporaryDirectory()`. The `with` block guarantees cleanup on exit, even on assertion failure. |
| **No shared state** | `CollabStore` constructor runs `SCHEMA` and all migrations on every open, so each test gets a pristine schema. |
| **Thread safety** | `CollabStore` uses `threading.RLock` internally; separate DB files means zero lock contention across tests. |
| **Filesystem isolation** | `tempfile.mkdtemp()` is used when the test needs a directory to pass as `cwd` (see `test_engine_registry_bridge.py:_make_engine()`). |
| **CLI subprocess isolation** | CLI integration tests in `test_cli_config.py` use `TemporaryDirectory()` + write a `.runtime-config.json` there + invoke via `subprocess.run([sys.executable, "-m", "hermes_collab_engine.cli", ...])`. Each subprocess gets its own config and DB. |

### Canonical Example (Store Test)

```python
class StoreV3Tests(unittest.TestCase):

    def test_insert_wbs_node_persists_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CollabStore(Path(tmp) / "db.sqlite3")
            store.create_run("run_1", "title", "request", {})
            node = WBSNode("wbs-1", "title", "desc", "implementation",
                           5, [], True, "deliver", checkpoint=True)
            store.insert_wbs_node("run_1", node.to_dict())
            row = store.get_node("run_1", "wbs-1")
            self.assertEqual(row["checkpoint"], 1)
```

### Canonical Example (CLI Test)

```python
class DoctorCommandTests(unittest.TestCase):

    def test_doctor_json_emits_required_fields(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / ".runtime-config.json"
            cfg.write_text(json.dumps({"worker_model": "test-model"}))
            r = _run_cli("doctor", "--config", str(cfg), "--json")
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            report = json.loads(r.stdout)
            self.assertTrue(report["valid_json"])
```

### Legacy DB Migration Tests

When testing migration logic, the test creates a legacy DB with `sqlite3.connect()` + `conn.execute()` to write old-format tables, then opens it via `CollabStore(db_path)` and asserts the migrated schema/columns:

```python
def test_existing_db_creates_node_results_table(self):
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "legacy.sqlite3"
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE runs (...)")
        conn.commit()
        conn.close()
        store = CollabStore(db_path)
        tables = {row[0] for row in store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'" )}
        self.assertIn("node_results", tables)
```

### Summary of Rules

1. **Every test that writes to disk** wraps its body in `with tempfile.TemporaryDirectory() as tmp:`.
2. **Store tests** create `CollabStore(Path(tmp) / "db.sqlite3")` inside the block.
3. **CLI tests** write config files + invoke via subprocess inside the block.
4. **No test-level setUp/tearDown** is used for DB cleanup — the `with` block is sufficient.
5. **Engine tests** that need a full `CollabEngine` use `tempfile.mkdtemp()` in a helper method (`_make_engine`), but the TemporaryDirectory pattern is preferred for single-test methods.
6. **~/`.opencode` or system config is never touched** — all config is scoped to the temp directory.
7. **Assertions run inside the `with` block** — never outside, where the temp directory would be cleaned up.
