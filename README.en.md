# Hermes Collab Engine

<p align="center">
  <a href="README.md">简体中文</a> |
  <a href="README.en.md">English</a> |
  <a href="README.ja.md">日本語</a>
</p>

> A standalone collaboration engine for official Hermes Agent and Claude Code workers. It assesses task complexity, decomposes work into WBS nodes, dispatches multiple workers in parallel, retries timed-out work via smaller shards, persists state in SQLite, and provides a Chinese management dashboard.

[![Python](https://img.shields.io/badge/Python-3.11%2B-blue)](#requirements)
[![SQLite](https://img.shields.io/badge/SQLite-persistence-green)](#persistence)
[![Hermes](https://img.shields.io/badge/Hermes-Agent-purple)](#execution-flow)
[![Claude Code](https://img.shields.io/badge/Claude%20Code-Worker-orange)](#execution-flow)

## One-command install

```bash
curl -fsSL https://raw.githubusercontent.com/lpc0387/hermes-collab-engine/main/scripts/install.sh | bash
```

Start after installation:

```bash
opc
```

`opc` guides you through:

1. Choosing whether to read local Claude/Hermes configuration automatically or manually enter BaseURL, API key, and model names.
2. Selecting the Leader Agent model.
3. Selecting the Worker Agent model.
4. Choosing dashboard host, port, and default working directory.
5. Starting the collaboration engine dashboard.
6. Choosing how to operate: use the task input window in the Web dashboard, or enter the official Hermes CLI.

After you exit the selected operation mode, `opc` stops the dashboard service started by that session. Web dashboard mode is recommended by default because the dashboard already includes a task input window; choose Hermes CLI only when terminal interaction is needed.

## What it solves

A single agent can struggle with large tasks because:

- task boundaries are unclear;
- all work is executed serially;
- long tasks lack checkpoints and retry strategy;
- worker status is invisible;
- execution history, failure causes, and lessons are not persisted;
- there is no unified dashboard for logs, worker state, and task graphs.

Hermes Collab Engine separates execution into a planning layer and an execution layer:

- **Leader Agent** handles complexity assessment, WBS decomposition, and result aggregation.
- **Worker Agents** execute individual WBS nodes.
- **SQLite** records runs, nodes, workers, logs, and lessons.
- **Dashboard** shows run state in real time.

## Execution flow

```text
User
  ↓
Official Hermes Agent
  ↓ terminal tool
Hermes Collab Engine
  ↓ WBS / scheduler / SQLite / watchdog
Claude Code Worker 1..N
  ↓
Aggregated result
  ↓
Returned to user
```

## Core capabilities

| Capability | Description |
|---|---|
| Complexity assessment | Scores domain complexity, step count, ambiguity, coupling, and risk |
| WBS decomposition | Breaks complex tasks into executable work breakdown nodes |
| Parallel dispatch | Runs dependency-ready nodes in parallel through Claude Code workers |
| Timeout watchdog | Splits and retries timed-out worker tasks instead of silently failing |
| Sharded retry | Focused shards for scope, evidence, implementation, and risk |
| Result aggregation | Aggregates parent and shard outputs, honestly reporting success, failure, and timeout |
| SQLite persistence | Stores execution history and state in a real SQLite database |
| Self-learning lessons | Captures lessons from timeouts, slow tasks, failed runs, and interrupted runs |
| Dashboard | Chinese web dashboard for runs, logs, workers, and lessons |

## Self-upgrade sync policy

Stable AI / collaboration-engine rule changes must be synchronized to GitHub for backup and migration. Use an allowlist-based minimal commit strategy, and never commit secrets, profiles, settings, runtime databases, logs, or session records. See [AI / collaboration-engine self-upgrade sync policy](docs/self-upgrade-policy.md).

## Requirements

- Linux / macOS / WSL
- Python 3.11+
- Git
- Claude Code CLI: `claude`
- Official Hermes Agent: `hermes`

No Node.js dependency and no `npm install` are required.

## Launcher

```bash
opc
```

The launcher supports two API configuration modes:

### Read local configuration automatically

It reads:

```text
~/.claude/settings.json
~/.claude/profiles/*.json
```

This is suitable for servers that already have Claude Code / Hermes configured.

### Enter configuration manually

It prompts for:

- BaseURL
- API Key / Auth Token
- available model names, separated by commas

This is suitable for new servers or environments where local configuration should not be read.

## Model selection

The launcher asks you to select:

### Leader Agent model

Used for:

- complexity assessment;
- WBS decomposition;
- result aggregation;
- default Hermes model when entering Hermes CLI.

### Worker Agent model

Used for:

- Claude Code worker execution;
- WBS node handling;
- sharded retries after timeouts.

## CLI usage

### Run one task

```bash
hermes-collab run "Analyze the current project structure and suggest improvements" --cwd . --json
```

### Specify concurrency and timeout strategy

```bash
hermes-collab run "Implement a collaboration task" \
  --cwd . \
  --concurrency 4 \
  --timeout 900 \
  --max-retries 2 \
  --split-count 4 \
  --json
```

### Use a request file

```bash
hermes-collab run --request-file request.md --cwd . --json
```

### Start dashboard

```bash
hermes-collab server --host 0.0.0.0 --port 8765 --cwd .
```

Open:

```text
http://SERVER_IP:8765
```

### Show status

```bash
hermes-collab status --json
```

## Dashboard

The dashboard provides:

- total run count;
- active run count;
- running worker count;
- lesson count;
- run history;
- run detail;
- real-time logs;
- self-learning lessons;
- online task submission;
- SSE live updates.

## API

| Method | Path | Description |
|---|---|---|
| GET | `/api/overview` | Overview metrics |
| GET | `/api/runs` | Run list |
| GET | `/api/runs/:id` | Run detail |
| GET | `/api/logs` | Recent logs |
| GET | `/api/lessons` | Self-learning lessons |
| GET | `/api/events` | Server-sent events |
| POST | `/api/runs` | Submit an asynchronous run |

## Persistence

Default database:

```text
data/collab.sqlite3
```

Tables:

| Table | Purpose |
|---|---|
| `runs` | Top-level task runs |
| `wbs_nodes` | WBS nodes, dependencies, status, and results |
| `workers` | Worker lifecycle, session IDs, duration, and errors |
| `logs` | Structured logs |
| `lessons` | Self-learning lessons |
| `metrics` | Extension metrics |

## Timeout sharding strategy

Default parameters:

```text
--timeout 900
--max-retries 2
--split-count 4
```

When a worker times out, the system does not simply stop the task. It splits the node into smaller shards:

| Shard | Goal |
|---|---|
| Scope shard | Find the smallest relevant scope and entry points |
| Evidence shard | Collect files, commands, symbols, and evidence |
| Implementation shard | Produce a minimal implementation or patch strategy |
| Risk shard | Identify blockers, unknowns, and verification needs |

The shards are dispatched again and aggregated at the end.

## Hermes integration

The installer creates:

```text
~/.local/bin/hermes-collab
~/.local/bin/opc
```

Optional integration script:

```bash
~/hermes-collab-engine/scripts/install-hermes-integration.sh
```

It writes Hermes-side:

- a local Skill;
- Memory;
- SOUL behavior prompt;

so Hermes knows to use the collaboration engine by default for implementation, analysis, debugging, audits, research, planning, and other multi-step tasks.

## Safety boundaries

- Do not upload or commit the runtime database.
- Do not commit `.runtime-config.json`.
- Do not commit API keys.
- Do not modify user business projects unintentionally.
- Worker behavior is executed by Claude Code CLI; configure permissions and working directory as needed.

## Development structure

```text
hermes-collab-engine/
├── hermes-collab
├── start.sh
├── start.py
├── scripts/
│   ├── install.sh
│   └── install-hermes-integration.sh
├── src/hermes_collab_engine/
│   ├── cli.py
│   ├── engine.py
│   ├── models.py
│   ├── planner.py
│   ├── server.py
│   └── store.py
├── web/
│   └── index.html
├── examples/
│   └── im-bridge-request.md
└── data/
    └── .gitkeep
```

## License

MIT
