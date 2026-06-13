# Hermes Collab Engine v5.0

[![Release v5.0.0](https://img.shields.io/badge/release-v5.0.0-blue)](CHANGELOG.md) [![Sandbox ready](https://img.shields.io/badge/sandbox-ready-success)](sandbox/README.md) [![License MIT](https://img.shields.io/badge/license-MIT-green)](#license) [![Security](https://img.shields.io/badge/security-policy-orange)](SECURITY.md)

Hermes Collab Engine v5.0 is the first formal public release of the **AI multi-agent collaboration engine** for the Hermes collaboration workflow: a Leader decomposes requests into **WBS** nodes, Workers run in parallel, and **Claude Code** / **Hermes Agent** / custom Agent Backends can join the same pipeline.

It ships with a real-time **dashboard**, isolated **sandbox**, Leader feedback diary, lightweight API, and **one-line install** path for decomposing, dispatching, auditing, and summarizing complex engineering work.

![Pixel Collab Office Dashboard](docs/screenshots/dashboard.png)

![Hermes collaboration flow demo](docs/demo/hermes-flow.svg)

## Release and community

If this project helps you, please star it on GitHub to follow the v5.0 release line. Read [`CONTRIBUTING.md`](CONTRIBUTING.md) before contributing, report security issues through [`SECURITY.md`](SECURITY.md), follow plans in [`ROADMAP.md`](ROADMAP.md), and review changes in [`CHANGELOG.md`](CHANGELOG.md). Optional community launch copy lives in [`docs/launch/v5.0-posts.md`](docs/launch/v5.0-posts.md).

## One-line deployment

```bash
curl -fsSL https://raw.githubusercontent.com/lpc0387/hermes-collab-engine/main/scripts/install.sh | bash
```

The installer checks dependencies, clones or updates the repository, creates a local virtual environment, and creates empty template directories. The repository **does not bundle runtime data, secrets, or real Hermes/Claude configuration**. To connect Hermes to the local engine, review the template installer first:

```bash
cd ~/hermes-collab-engine
./scripts/install-hermes-integration.sh --dry-run
```

## Quick start

```bash
# Launcher: choose config → choose Leader/Worker models → dashboard + Hermes CLI
opc

# Manual install
python3 -m pip install -e .

# Run a task directly
hermes-collab run "Analyze the current project structure" --cwd . --json
```

## Highlights

| Capability | Release note |
|---|---|
| WBS collaboration | Leader scores, decomposes, and dispatches nodes; Workers execute in parallel by dependency |
| Leader/Worker dual models | Pick separate Leader and Worker models at startup; the dashboard shows the active models |
| Real sandbox execution | `scripts/start_sandbox.sh --real` can run real workers within a limited quota; mock demo remains the default |
| Isolated DB / workspace | The sandbox uses demo SQLite; real mode writes `data/sandbox_real.sqlite3` and an isolated workspace, not production data |
| TTL cleanup | The sandbox defaults to 2 hours and stops automatically to avoid long-lived demo processes |
| Lightweight API payloads | Dashboard APIs return the necessary runs, nodes, Workers, logs, models, and feedback fields for embedding and proxying |
| Leader feedback diary | After completion, a pixel notebook shows the full Leader aggregate feedback with copy/download Markdown actions |
| one-line install | Use the `curl ... | bash` command above, then enable Hermes integration from reviewed templates if needed |

## Sandbox demo

The sandbox demonstrates the dashboard, run history, Worker state, model display, and Leader diary. It uses mock APIs and sanitized demo data, and **does not call real workers, write production data, or include real runtime data** by default.

```bash
# One-shot launcher (default: 2 hours, auto-stops on timeout)
./scripts/start_sandbox.sh

# Custom duration
./scripts/start_sandbox.sh 4              # 4 hours
./scripts/start_sandbox.sh 0.5            # 30 minutes
./scripts/start_sandbox.sh --hours 8      # 8 hours
./scripts/start_sandbox.sh --port 8877    # custom port
./scripts/start_sandbox.sh -i             # ask interactively

# Reuse an existing DB, or try real workers in an isolated DB/workspace
./scripts/start_sandbox.sh --no-reseed
./scripts/start_sandbox.sh --real
```

Then open: `http://127.0.0.1:8876/`. See [`sandbox/README.md`](sandbox/README.md).

## Core concepts

```text
User → Leader(AI) → WBS Decomposition → Worker(AI) × N in parallel → Aggregation → Result
```

- **Leader**: complexity scoring, WBS decomposition, result aggregation, and Skill/Tool dispatch.
- **Worker**: executes individual nodes and loads Skills plus tool whitelists as needed.
- **Agent Backend**: abstracts Claude Code / Hermes Agent / Codex / OpenCode / custom coding agents.
- **SQLite**: persists run state, node results, context snapshots, and lessons.
- **Dashboard**: shows the pipeline, Worker pool, Skill/Tool injection, models, and logs in real time.

## CLI commands

### Run a task

```bash
hermes-collab run "Analyze the current project structure" --cwd . --json
hermes-collab run --request-file request.md --cwd .
hermes-collab run "Implement a collaborative task" --agent claude-code --concurrency 4 --timeout 900
```

### Start the dashboard

```bash
hermes-collab server --host 0.0.0.0 --port 8765 --cwd .
```

### View Skills / Tools

```bash
hermes-collab skills                                # All skills
hermes-collab skills --node-type implementation      # Preview selected skills
hermes-collab tools                                 # All tool configurations
hermes-collab tools --node-type implementation       # Preview selected tools
```

### View Agents / Status

```bash
hermes-collab agents                # Registered backends
hermes-collab agents --available    # Available on PATH
hermes-collab status --json
```

### Lesson management

```bash
hermes-collab lessons                       # List lessons
hermes-collab lessons --scope global        # Filter by scope
hermes-collab add-lesson --category timeout --lesson "Split large files" --scope global
```

### Runtime interventions

```bash
hermes-collab kill-node <run_id> <node_id>  # Kill a node
hermes-collab split-node <run_id> <node_id> # Split a node
hermes-collab skip-node <run_id> <node_id>  # Skip a node
hermes-collab redo-node <run_id> <node_id>  # Redo a node
hermes-collab log <run_id> <node_id> "msg"  # Write a log entry
```

### Verification

```bash
hermes-collab verify-release # v5.0 release completeness check
```

## API

| Method | Path | Description |
|---|---|---|
| GET | `/api/overview` | Overview data |
| GET | `/api/runs` | Run records |
| GET | `/api/runs/:id` | Compact run details with nodes and recent logs for fast dashboard refresh |
| GET | `/api/runs/:id?full=1` | Full run details with Workers, complete logs, models, and Leader feedback |
| GET | `/api/logs` | Recent logs |
| GET | `/api/lessons` | Self-learning lessons |
| GET | `/api/agents` | Available Agent Backends |
| GET | `/api/skills?node_type=&task=` | Skill registry with selection preview |
| GET | `/api/tools?node_type=&task=` | Tool configuration with selection preview |
| GET | `/api/events` | SSE real-time event stream |
| POST | `/api/runs` | Submit a task asynchronously |

## Configuration sources

The launcher auto-detects API configuration in this priority order:

1. **`~/.hermes/.env`** — `ANTHROPIC_API_KEY` + `ANTHROPIC_BASE_URL` (recommended)
2. **`~/.hermes/config.yaml`** — `model.base_url` + `model.default`
3. **`~/.hermes/auth.json`** — Anthropic credentials from the credential pool
4. **`~/.claude/settings.json`** — Claude Code configuration (fallback)
5. **Manual input** — BaseURL + API Key + model list

Hermes is the Leader, so its configuration should be the primary source. Claude Code configuration is only a compatibility fallback. The repository only provides empty skeletons and `.example` files, including `templates/claude/settings.example.json`; it does not read, copy, or publish real Hermes/Claude secrets, tokens, sessions, auth files, logs, sqlite data, skills, or memories.

Environment variables:

```bash
HERMES_COLLAB_MODEL=glm-5.1           # Global model
HERMES_COLLAB_LEADER_MODEL=glm-5.1    # Leader model
HERMES_COLLAB_WORKER_MODEL=kimi-k2.6  # Worker model
ANTHROPIC_MODEL=glm-5.1               # Fallback
```

## Persistence and security boundaries

The SQLite file (default `data/collab.sqlite3`) stores runs, wbs_nodes, workers, logs, lessons, node_results, settings, and context_snapshots. API keys come only from environment variables or local configuration and are not written to the database.

- Workers run in isolated subprocesses constrained by `allowed_tools` whitelists.
- MCP tools are read-only by default (`mcp-readonly` profile).
- The sandbox uses an isolated demo database and workspace, with TTL cleanup.
- `git push` is restricted by the `git-write` tool profile and only available to implementation nodes.

## Agent Backend

| Backend | Command | Output parsing |
|---|---|---|
| claude-code | `claude -p` | session ID + text |
| codex | `codex` | JSON |
| opencode | `opencode` | text |

Custom Backend: implement the `AgentBackend` interface (`name`, `build_command`, `parse_output`, `default_allowed_tools`) and register it.

## Development

```bash
pip install -e .
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

```text
src/hermes_collab_engine/
├── cli.py           # CLI entry point
├── engine.py        # Core engine
├── server.py        # Web dashboard
├── store.py         # SQLite persistence
├── models.py        # Data models
├── skills.py        # Skill distribution
├── tools.py         # MCP tool management
├── agents/          # Agent Backend abstraction
├── verification.py  # v5.0 release completeness check
└── ...
web/
└── index.html       # Visualization dashboard
```

## License

MIT
