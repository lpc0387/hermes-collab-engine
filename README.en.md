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
Hermes Parent Agent
  ├─ optional: delegate_task preflight analysis
  ↓ terminal tool
Hermes Collab Engine
  ├─ Leader: complexity scoring / WBS / proactive split
  ├─ Scheduler: stream scheduling / intervention control
  ├─ SQLite: runs / nodes / logs / scoped lessons
  └─ Watchdog: timeout sharding
      ↓
Claude Code Worker 1..N
  ↓ dual-track output (machine result + human deliverable)
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
| Leader-driven scoring | The Leader Agent scores complexity and decides the execution strategy by domain, steps, ambiguity, coupling, and risk |
| Semantic compression decomposition | The Planner outputs a shared brief and node briefs, compressing large tasks into minimal context that Workers can execute |
| Dual-track output | Workers produce both machine-parseable results and human-readable deliverables for scheduling, dashboard, and final reporting |
| Tiered upstream context | Worker prompts automatically include parent, grandparent, and completed dependency results, keeping shard lineage traceable |
| Stream-scheduled dispatch | The scheduler immediately dispatches nodes when dependencies are satisfied and worker slots are free, avoiding fixed batch barriers that slow downstream chains |
| Proactive split | Nodes expected to time out or carry high risk can be split into focused shards before execution instead of waiting for timeout recovery |
| Parent intervention | Parent / Operator can use the CLI to log, kill, split, and skip running nodes, with all actions written to the audit log |
| Scoped lessons | Lessons carry global, project, run, node, and wbs-family scopes to prevent local lessons from polluting global planning |
| Env-var model fallback | The CLI supports HERMES_COLLAB_MODEL, HERMES_COLLAB_LEADER_MODEL, HERMES_COLLAB_WORKER_MODEL, and ANTHROPIC_MODEL as model fallbacks |

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

### Manage lessons

Write a scoped lesson:

```bash
hermes-collab lesson add \
  --scope project \
  --category planning \
  --lesson "For similar tasks, split into analysis, implementation, and verification first" \
  --source hermes-delegate-task \
  --evidence-json '{"run_id":"run_xxx"}'
```

List lessons:

```bash
hermes-collab lesson list --scope project --json
```

Supported scopes: `global`, `project`, `run`, `node`, `wbs-family`.

### In-run intervention

Parent / Operator can use the CLI to perform controlled intervention on running nodes:

```bash
hermes-collab parent-log --run-id run_xxx --message "Manually confirmed continuation" --json
hermes-collab split-node --node-id wbs-1 --split-count 4 --reason "Scope is too broad; proactively splitting" --json
hermes-collab skip-node --node-id wbs-2 --reason "Upstream confirmed this no longer needs execution" --json
hermes-collab kill-node --node-id wbs-3 --signal TERM --reason "Execution direction is wrong; stop retrying" --json
```

All interventions are written to logs, and the final report must disclose manual intervention, skipped work, cancellation, or forced progress.

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

`lessons` have explicit scopes: `global` and `project` can be reused by later planning; `run`, `node`, and `wbs-family` are limited to the corresponding run, node, or WBS family, preventing local lessons from being misused as global rules.

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

## Agent Communication Protocol

This project uses ACP-Collab v0.2 to define the communication boundaries between the Hermes Parent agent, collaboration-engine Leader, Workers, and external preflight layers. See the full protocol in [Agent Communication Protocol](docs/agent-communication-protocol.md).

Core conventions:

- Request channel: Parent submits self-contained requests through CLI/API, and Workers do not assume they can read the parent session history;
- Dual-Track Result channel: Worker output includes both machine-parseable results and human-readable deliverables;
- Upstream-Context channel: Engine injects parent, grandparent, and completed dependency results into downstream Workers;
- Scoped Lessons channel: lessons must carry source and scope, and the Planner only reuses lessons within their applicable scope;
- Dispatch-Control channel: stream scheduling, proactive split, and in-run intervention are all recorded through CLI/API and the SQLite state machine;
- Observability channel: runs, wbs_nodes, workers, logs, and lessons are the unified observation path for the dashboard and Parent.

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
├── docs/
│   ├── agent-communication-protocol.md
│   └── self-upgrade-policy.md
├── examples/
│   └── im-bridge-request.md
└── data/
    └── .gitkeep
```

## License

MIT
