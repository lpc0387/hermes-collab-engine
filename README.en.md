# Hermes Collab Engine v5.5 — Multi-Agent AI Collaboration Engine

[![中文](https://img.shields.io/badge/中文-README.md-blue)](README.md) [![Release v5.5.0](https://img.shields.io/badge/release-v5.5.0-blue)](CHANGELOG.md) [![Sandbox ready](https://img.shields.io/badge/sandbox-ready-success)](sandbox/README.md) [![License MIT](https://img.shields.io/badge/license-MIT-green)](#license) [![Security](https://img.shields.io/badge/security-policy-orange)](SECURITY.md)

> Open-source multi-agent orchestration for Claude Code, Hermes, Codex & custom AI agents — plan, split, dispatch, supervise, aggregate.

Hermes Collab Engine v5.5 is the first official public release of the **AI multi-agent collaboration engine** for the **Hermes collaboration engine**: the Leader breaks down requirements into a WBS, Workers execute in parallel, and Claude Code / Hermes Agent / custom Agent Backends can join the same collaboration pipeline.

It also provides a real-time **dashboard**, isolated **sandbox**, Leader feedback notebook, lightweight API, and one-line deployment — ideal for decomposing complex development tasks, dispatching, auditing, and aggregating results into readable deliverables.

![Pixel Collaboration Workstation Dashboard](docs/screenshots/dashboard.png)

![Hermes Collaboration Flow Demo](docs/demo/hermes-flow.svg)

## Release & Community

If this project helps you, feel free to star and follow the v5.0 release line on GitHub. Please read [`CONTRIBUTING.md`](CONTRIBUTING.md) before participating, report security issues via [`SECURITY.md`](SECURITY.md), check the roadmap in [`ROADMAP.md`](ROADMAP.md), and see version changes in [`CHANGELOG.md`](CHANGELOG.md). Community sharing copy can be found at [`docs/launch/v5.0-posts.md`](docs/launch/v5.0-posts.md).

## One-Line Deployment

```bash
curl -fsSL https://raw.githubusercontent.com/lpc0387/hermes-collab-engine/main/scripts/install.sh | bash
```

The install script checks dependencies, clones/updates the repo, creates a local virtual environment, and creates empty template directories; the repo **does not bundle any runtime data, keys, or real Hermes/Claude configurations**. To connect Hermes to the local collaboration engine, please review the template script first:

```bash
cd ~/hermes-collab-engine
./scripts/install-hermes-integration.sh --dry-run
```

## Quick Start

```bash
# Launch selector: choose config → choose Leader/Worker model → dashboard + Hermes CLI
opc

# Manual installation
python3 -m pip install -e .

# Run a task directly
hermes-collab run "Analyze current project structure" --cwd . --json
```

## Highlights

| Capability | Release Notes |
|---|---|
| WBS Collaboration | Leader scores, decomposes, and dispatches nodes; Workers execute in parallel by dependency |
| Leader/Worker Dual Models | Select Leader and Worker models at startup; dashboard displays current models |
| Real Sandbox Execution | `scripts/start_sandbox.sh --real` launches real workers within limited quotas; default mode remains mock demo |
| Isolated DB / Workspace | Sandbox uses demo SQLite; real execution writes to `data/sandbox_real.sqlite3` and an independent workspace, no read/write to production database |
| TTL Cleanup | Sandbox defaults to 2 hours, auto-stops on expiry to prevent demo processes from lingering |
| Lightweight API Payload | Dashboard API returns necessary fields for runs, nodes, Workers, and logs — easy to embed and proxy |
| Leader Feedback Notebook | After task completion, a pixel notebook pops up displaying full Leader aggregation feedback, with copy/download Markdown support |
| One-Line curl Deployment | Use `curl ... | bash` above to install; template scripts then connect Hermes as needed |

## v5.5 Preview New Features

> v5.5 is a developer preview version with the following new features. Feedback is welcome.

### UnifiedRegistry
- Unified management of Skills, Tools, and MCP with capability tag indexing
- Web UI registration → auto-persisted, survives restarts
- Leader auto-discovers available skills/tools and pre-assigns them during WBS phase

### Agent Management
- Built-in Agents (claude-code, hermes, codex, opencode)
- Web UI registration of custom Agents with strict validation (name/command/capabilities)
- Enable status display, capability tags

### Session Chains
- Form continuous conversation chains via "connect to previous session"
- Group display of multiple run statuses and progress by resume chain
- Dashboard auto-hides when no continuous conversations exist

### Lessons Self-Learning System
- Engine auto-records run experiences and deduplicates/refines them (run_id normalization)
- Read-only node risk detection fix (no longer false-triggers checkpoint)
- Atomic persistence of checkpoint state
- `lessons_learned` field auto-output to run results

### Skill/MCP Tool Injection
- Leader pre-assigns skills and MCP tools for each node during WBS phase
- Web UI supports file import registration (.md/.txt for skill, .json for MCP)
- Tool whitelist (permission whitelist) not affected by native capability filtering

### One-Click Sandbox Launch
```bash
sandbox              # Default 2 hours, port 8876
sandbox 4            # Run 4 hours
sandbox --port 8877  # Custom port
```
Sandbox is fully isolated from production (independent DB, workspace, port), synced with all v5.5 Web UI features.

## Sandbox Demo

The sandbox is used to demo the dashboard, run history, Worker status, model display, and Leader notebook. It uses mock API and desensitized demo data, **does not call real workers, does not write production data, and does not contain real runtime data**.

To experience the sandbox environment online, contact the author via WeChat: `lg19961117` for access.

```bash
# One-click launch (default 2 hours, auto-stop on timeout)
./scripts/start_sandbox.sh

# Custom hours
./scripts/start_sandbox.sh 4              # 4 hours
./scripts/start_sandbox.sh 0.5            # 30 minutes
./scripts/start_sandbox.sh --hours 8      # 8 hours
./scripts/start_sandbox.sh --port 8877    # Change port
./scripts/start_sandbox.sh -i             # Interactive prompt for duration

# Reuse existing database without reseeding; or run real workers in isolated DB/workspace
./scripts/start_sandbox.sh --no-reseed
./scripts/start_sandbox.sh --real
```

After launch, visit: `http://127.0.0.1:8876/`. See [`sandbox/README.md`](sandbox/README.md) for details.

v5.5 adds the `sandbox` one-click launch command, alongside `opc`:

```bash
sandbox              # Default 2 hours, port 8876
sandbox 4            # Run 4 hours
sandbox --port 8877  # Custom port
sandbox --real       # Enable real worker execution
```

## Core Concepts

```text
User → Leader(AI) → WBS Decomposition → Worker(AI) × N Parallel → Aggregation → Result
```

- **Leader**: Complexity scoring, WBS decomposition, result aggregation, Skill/Tool dispatch.
- **Worker**: Executes specific nodes, loads Skills and tool whitelists as needed.
- **Agent Backend**: Abstraction for Claude Code / Codex / OpenCode / custom coding Agents.
- **SQLite**: Persists run state, node results, context snapshots, and lessons.
- **Dashboard**: Real-time display of pipeline, Worker pool, Skill/Tool injection, models, and logs.

## CLI Commands

### Run Tasks

```bash
hermes-collab run "Analyze current project structure" --cwd . --json
hermes-collab run --request-file request.md --cwd .
hermes-collab run "Implement collaboration task" --agent claude-code --concurrency 4 --timeout 900
```

### Launch Dashboard

```bash
hermes-collab server --host 0.0.0.0 --port 8765 --cwd .
```

### View Skills / Tools

```bash
hermes-collab skills                                # All skills
hermes-collab skills --node-type implementation      # Preview selected skills
hermes-collab tools                                 # All tool configs
hermes-collab tools --node-type implementation       # Preview selected tools
```

### View Agents / Status

```bash
hermes-collab agents                # Registered backends
hermes-collab agents --available    # Available on PATH
hermes-collab status --json
```

### Lessons Management

```bash
hermes-collab lessons                       # List lessons
hermes-collab lessons --scope global        # Filter by scope
hermes-collab add-lesson --category timeout --lesson "Split large files" --scope global
```

### Runtime Intervention

```bash
hermes-collab kill-node <run_id> <node_id>  # Kill node
hermes-collab split-node <run_id> <node_id> # Split node
hermes-collab skip-node <run_id> <node_id>  # Skip node
hermes-collab redo-node <run_id> <node_id>  # Redo node
hermes-collab log <run_id> <node_id> "msg"  # Write log
```

### Verification

```bash
hermes-collab verify-release # v5.0 release integrity check
```

## API

| Method | Path | Description |
|---|---|---|
| GET | `/api/overview` | Overview data |
| GET | `/api/runs` | Run records |
| GET | `/api/runs/:id` | Lightweight run details (nodes and recent logs, suitable for dashboard quick refresh) |
| GET | `/api/runs/:id?full=1` | Full run details (including Workers, complete logs, models and Leader feedback) |
| GET | `/api/logs` | Recent logs |
| GET | `/api/lessons` | Self-learning lessons |
| GET | `/api/agents` | Available Agent Backends |
| GET | `/api/skills?node_type=&task=` | Skill registry (preview selectable) |
| GET | `/api/tools?node_type=&task=` | Tool configuration (preview selectable) |
| GET | `/api/events` | SSE real-time event stream |
| POST | `/api/runs` | Async task submission |

## Configuration Sources

The launcher auto-detects API configuration in the following priority:

1. **`~/.hermes/.env`** — `ANTHROPIC_API_KEY` + `ANTHROPIC_BASE_URL` (recommended)
2. **`~/.hermes/config.yaml`** — `model.base_url` + `model.default`
3. **`~/.hermes/auth.json`** — anthropic credentials from credential pool
4. **`~/.claude/settings.json`** — Claude Code configuration (fallback)
5. **Manual input** — BaseURL + API Key + model list

Hermes is the Leader, and its configuration should be the primary source. Claude Code configuration is only a compatibility fallback. The repo only provides empty templates and `.example` files, and does not read, copy, or publish real Hermes/Claude secrets, tokens, sessions, auth, logs, or sqlite data.

Environment variables:

```bash
HERMES_COLLAB_MODEL=glm-5.1           # Global model
HERMES_COLLAB_LEADER_MODEL=glm-5.1    # Leader model
HERMES_COLLAB_WORKER_MODEL=kimi-k2.6  # Worker model
ANTHROPIC_MODEL=glm-5.1               # Fallback

# Optional: Worker git HTTPS credentials. Injected by runtime/secret manager, not written to repo.
HERMES_COLLAB_WORKER_GIT_TOKEN=ghp_xxx
HERMES_COLLAB_WORKER_GIT_USERNAME=x-access-token
HERMES_COLLAB_WORKER_GIT_ALLOWED_HOSTS=github.com
# Or provide an external helper (e.g. !/path/to/helper), takes precedence over built-in env-backed helper.
HERMES_COLLAB_WORKER_GIT_CREDENTIAL_HELPER='!/path/to/git-credential-helper'
```

## Persistence & Security Boundary

SQLite file (default `data/collab.sqlite3`) stores runs, wbs_nodes, workers, logs, lessons, node_results, settings, context_snapshots. API Keys come only from environment variables or local configuration, not written to the database.

- Workers execute in independent subprocesses, constrained by `allowed_tools` whitelist.
- MCP tools default to read-only (`mcp-readonly` profile).
- Sandbox uses independent demo database and workspace, cleanable via TTL.
- `git push` / `git clone` is restricted by `git-write` tool profile, only available to implementation nodes when the task explicitly requires git write/clone.
- Worker git credentials are derived to subprocess environment via `HERMES_COLLAB_<ROLE>_GIT_TOKEN` and injected into in-memory credential helper using Git's `GIT_CONFIG_*`; the helper script only references environment variables and does not write tokens in plaintext to the repo or git config files. You can also use `HERMES_COLLAB_<ROLE>_GIT_CREDENTIAL_HELPER` to point to an external helper.

## Agent Backend

| Backend | Command | Output Parsing |
|---|---|---|
| claude-code | `claude -p` | session ID + text |
| codex | `codex` | JSON |
| opencode | `opencode` | text |

Custom Backend: Implement the `AgentBackend` interface (`name`, `build_command`, `parse_output`, `default_allowed_tools`) and register it.

v5.5 adds built-in Hermes Agent registration, supporting planning/orchestration/delegation capability tags.

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
├── skills.py        # Skill dispatch
├── tools.py         # MCP tool management
├── agents/          # Agent Backend abstraction
├── verification.py  # v5.0 release integrity check
└── ...
web/
└── index.html       # Visualization dashboard
```

> **GitHub Topics Recommended:** `multi-agent`, `claude-code`, `ai-orchestration`, `wbs`, `llm`, `agentic-ai` — Suggested to add in repository Settings → Topics.

## Contact & Support

Primary contact: WeChat `lg19961117`

<details>
<summary>Optional sponsorship to support maintenance</summary>

<img src="docs/assets/money.png" alt="Sponsorship QR code" width="260">

</details>

## License

MIT
