# Hermes Collab Engine

<p align="center">
  <a href="README.md">简体中文</a> |
  <a href="README.en.md">English</a> |
  <a href="README.ja.md">日本語</a>
</p>

> A multi-Agent collaborative execution engine: automatically assesses task complexity, decomposes via WBS, dispatches across multiple executors in parallel, supports various Workers including Claude Code / Codex / OpenCode, automatically splits and retries on timeout, persists via SQLite, distributes Skills, manages MCP tools, and provides a visual Chinese management panel.

[![Python](https://img.shields.io/badge/Python-3.11%2B-blue)](#environment-requirements)
[![SQLite](https://img.shields.io/badge/SQLite-Persistence-green)](#persistence)
[![Hermes](https://img.shields.io/badge/Hermes-Agent-purple)](#execution-pipeline)
[![Multi-Agent](https://img.shields.io/badge/Worker-Claude%20Code%20%7C%20Codex%20%7C%20OpenCode-orange)](#agent-backend)

## One-Click Deployment

```bash
curl -fsSL https://raw.githubusercontent.com/lpc0387/hermes-collab-engine/main/scripts/install.sh | bash
```

After deployment, start with:

```bash
opc
```

`opc` will guide you through:

1. Choosing to auto-read local Claude/Hermes configuration, or manually entering BaseURL, API Key, and model list;
2. Selecting the Leader Agent model;
3. Selecting the Worker Agent model;
4. Selecting the Worker Agent type (Claude Code / Codex / OpenCode / Custom);
5. Selecting the management panel listen address, port, and default working directory;
6. Starting the collaborative engine management panel;
7. Choosing the interaction mode: use the task input window in the Web panel, or enter the official Hermes command line.

After exiting the chosen interaction mode, `opc` will stop the management panel service started in this session. The Web panel is recommended by default since it has a built-in task input window; choose the Hermes command line when terminal interaction is needed.

## What Problem Does It Solve

A single Agent handling large tasks often encounters:

- Unclear task boundaries, unable to determine whether decomposition is needed;
- All work executed serially, resulting in low efficiency;
- No checkpoint or retry strategy for long-running tasks after timeout;
- Status of multiple Workers is not visible;
- Execution history, failure reasons, and experience cannot be accumulated;
- No unified panel to view logs, running status, and task graph;
- Different coding Agents cannot be scheduled uniformly;
- Workers lack domain skills and tool guidance.

Hermes Collab Engine separates task execution into a "planning layer" and an "execution layer":

- **Leader Agent** is responsible for complexity assessment, WBS decomposition, Skill distribution, tool allocation, and result aggregation;
- **Worker Agent** is responsible for executing specific WBS nodes, loading Skills and MCP tools as needed;
- **Agent Backend** abstracts the invocation and output parsing of different coding Agents;
- **SQLite** records running status, nodes, executors, logs, context snapshots, and learned experience;
- **Management Panel** provides real-time visualization of running status and collaborative workflows.

## Execution Pipeline

```text
User
  ↓
Hermes Parent Agent
  ├─ Optional: delegate_task pre-analysis
  ↓ terminal tool
Hermes Collab Engine
  ├─ Leader: Complexity Scoring / WBS / Skill Distribution / Tool Allocation / Proactive Splitting
  ├─ Scheduler: Streaming Dispatch / Intervention Control
  ├─ Agent Backend: Claude Code / Codex / OpenCode / Custom
  ├─ Skill Registry: Inject Worker prompts by node type
  ├─ MCP Tool Manager: Recommend tools for Workers based on tool characteristics
  ├─ SQLite: Runs / Nodes / Logs / Snapshots / scoped lessons
  └─ Watchdog: Timeout Splitting
      ↓
Worker Agent 1..N (can mix different Agent types)
  ↓ Dual-track output (machine result + human deliverable)
Aggregated Results
  ↓
Return to User
```

## Agent Backend

v4.0 introduces a pluggable Agent Backend system, replacing hardcoded Worker invocations. Each Backend defines how to invoke and parse the output of a specific coding Agent.

### Built-in Backends

| Backend | Command | Output Parsing | Use Case |
|---|---|---|---|
| `claude-code` | `claude` | JSON envelope | General coding (default) |
| `codex` | `codex` | JSON envelope | OpenAI Codex coding |
| `opencode` | `opencode` | Plain text | Lightweight coding |

### Selecting Worker Agent

```bash
# Default Claude Code
hermes-collab run "task"

# Using Codex
hermes-collab run "task" --agent codex

# View available Agents
hermes-collab agents
hermes-collab agents --available
```

### Custom Agent

```python
from hermes_collab_engine.agents import register_backend, AgentBackend

register_backend(AgentBackend(
    name="my-agent",
    display_name="My Custom Agent",
    command=["my-agent-cli"],
    prompt_flag="--prompt",
    output_parser="raw_text",
    ...
))
```

### API

| Method | Path | Description |
|---|---|---|
| GET | `/api/agents` | List available Agent Backends |

## Core Capabilities

| Capability | Description |
|---|---|
| Complexity Assessment | Calculates task complexity based on domain, number of steps, ambiguity, coupling, and risk |
| WBS Decomposition | Automatically decomposes complex tasks into executable work breakdown nodes |
| Agent Backend | Pluggable Worker Agents: Claude Code / Codex / OpenCode / Custom |
| Parallel Dispatch | Nodes with satisfied dependencies are dispatched in parallel to the selected Agent |
| Timeout Watchdog | Workers that time out automatically enter a splitting and retry process |
| Shard Retry | Timed-out nodes are split into focused shards: scope, evidence, implementation, and risk |
| Result Aggregation | Aggregates parent task and shard results, honestly reporting success, failure, and timeout |
| SQLite Persistence | Uses a real SQLite file to save run history, node results, and context snapshots |
| Context Snapshots | Automatically saves context before node completion and compaction, supporting post-compaction focus recovery |
| Self-Learning Experience | Accumulates experience from timeouts, slow tasks, and failures for use in subsequent planning |
| Management Panel | Chinese Web panel displaying run records, logs, executors, and experience |
| Leader-Driven Scoring | Leader Agent handles complexity scoring, determining execution strategy by domain, steps, ambiguity, coupling, and risk |
| Semantic Compression & Decomposition | Planner outputs a shared brief and node briefs, compressing large tasks into the minimal context Workers need to execute |
| Dual-Track Output | Workers simultaneously produce machine-parseable results and human-readable deliverables for scheduling, panel display, and final reporting |
| Tiered Upstream Context | Worker prompts automatically include parent, grandparent, and completed dependency results, maintaining traceable shard lineage |
| Streaming Dispatch | The scheduler dispatches nodes as soon as dependencies are satisfied and slots are available, avoiding fixed-batch barriers that slow downstream pipelines |
| Proactive Splitting | Nodes expected to time out or carry high risk can be split into focused shards before execution, rather than waiting for timeout remediation |
| Parent Intervention | Parent / Operator can log, kill, split, or skip running nodes via CLI, with all actions recorded in the audit log |
| Experience Scoping | Lessons carry global, project, run, node, or wbs-family scope, preventing local experience from polluting global planning |
| Environment Variable Models | CLI supports HERMES_COLLAB_MODEL, HERMES_COLLAB_LEADER_MODEL, HERMES_COLLAB_WORKER_MODEL, and ANTHROPIC_MODEL as model fallbacks |

## Self-Upgrade Synchronization Policy

Stable rule changes to the AI / collaborative engine must be synced to GitHub as backup and migration capability; commits follow an allowlist minimum-commit strategy, and submitting keys, profiles, settings, run databases, logs, or session records is prohibited. See [AI / Collaborative Engine Self-Upgrade Synchronization Policy](docs/self-upgrade-policy.md).

## Environment Requirements

- Linux / macOS / WSL
- Python 3.11+
- Git
- At least one Worker Agent: Claude Code CLI (`claude`), Codex CLI (`codex`), or OpenCode (`opencode`)
- Official Hermes Agent: `hermes`

No Node.js dependencies required, no npm install needed.

## Launcher

```bash
opc
```

The launcher supports two API configuration methods:

### Auto-Read Local Configuration

Reads from:

```text
~/.claude/settings.json
~/.claude/profiles/*.json
```

Suitable for servers already configured with Claude Code / Hermes.

### Manual Configuration Entry

Prompts for:

- BaseURL
- API Key / Auth Token
- Available model list, with multiple models separated by commas

Suitable for new servers or scenarios where you prefer not to read local configuration.

## Model Selection

During startup, you select separately:

### Leader Agent Model

Used for:

- Complexity assessment;
- WBS decomposition;
- Result aggregation;
- Serving as the default model for Hermes when entering the Hermes command line.

### Worker Agent Model

Used for:

- Worker Agent execution;
- WBS node processing;
- Shard retry after timeout.

## Command Line Usage

### Run a Single Task

```bash
hermes-collab run "Analyze the current project structure and provide improvement suggestions" --cwd . --json
```

### Specify Concurrency and Timeout Strategy

```bash
hermes-collab run "Implement a collaborative task" \
  --cwd . \
  --agent claude-code \
  --concurrency 4 \
  --timeout 900 \
  --max-retries 2 \
  --split-count 4 \
  --json
```

### Use a Task File

```bash
hermes-collab run --request-file request.md --cwd . --json
```

### Start the Management Panel

```bash
hermes-collab server --host 0.0.0.0 --port 8765 --cwd . --agent claude-code
```

Access at:

```text
http://server-ip:8765
```

### View Agent Backends

```bash
hermes-collab agents                # All registered
hermes-collab agents --available    # Only those available on PATH
hermes-collab agents --available --json
```

### View Status

```bash
hermes-collab status --json
```

### Manage Experience

Write scoped experience:

```bash
hermes-collab lesson add \
  --scope project \
  --category planning \
  --lesson "For similar tasks, prioritize splitting into analysis, implementation, and verification phases" \
  --source hermes-delegate-task \
  --evidence-json '{"run_id":"run_xxx"}'
```

View experience:

```bash
hermes-collab lesson list --scope project --json
```

Supported scopes: `global`, `project`, `run`, `node`, `wbs-family`.

### Context Snapshots

Manually save before compacting context:

```bash
hermes-collab save-snapshot <run_id> \
  --type pre_compaction \
  --decisions '["chose X over Y"]' \
  --user-instructions '["prefer concise responses"]'
```

View snapshots:

```bash
hermes-collab context-snapshot <run_id> --latest
hermes-collab context-snapshot <run_id> --type pre_compaction
```

### In-Run Intervention

Parent / Operator can perform controlled interventions on running nodes via CLI:

```bash
hermes-collab parent-log --run-id run_xxx --message "Manual confirmation to continue execution" --json
hermes-collab split-node --node-id wbs-1 --split-count 4 --reason "Scope too large, proactive split" --json
hermes-collab skip-node --node-id wbs-2 --reason "Upstream confirmed no need to execute" --json
hermes-collab kill-node --node-id wbs-3 --signal TERM --reason "Wrong execution direction, stop retry" --json
```

All interventions are logged, and the final report must disclose any manual intervention, skip, cancellation, or forced advancement.

## Management Panel

The management panel provides:

- Total run count;
- Number of currently running tasks;
- Number of active executors;
- Number of learned experience entries;
- Run records list;
- Run details;
- Real-time logs;
- Self-learning experience;
- Online task submission;
- SSE real-time updates.

## API

| Method | Path | Description |
|---|---|---|
| GET | `/api/overview` | Overview metrics |
| GET | `/api/runs` | Run records |
| GET | `/api/runs/:id` | Single run details |
| GET | `/api/logs` | Recent logs |
| GET | `/api/lessons` | Self-learning experience |
| GET | `/api/agents` | Available Agent Backends |
| GET | `/api/events` | Real-time event stream |
| POST | `/api/runs` | Submit task asynchronously |

## Persistence

Default database:

```text
data/collab.sqlite3
```

Data tables:

| Table | Purpose |
|---|---|
| `runs` | Top-level task run records (including agent type) |
| `wbs_nodes` | WBS nodes, dependencies, status, results, and context |
| `workers` | Executor lifecycle, session IDs, duration, and errors |
| `logs` | Structured logs |
| `lessons` | Self-learning experience (with scope) |
| `metrics` | Extended metrics |
| `node_results` | Worker result text and structured output |
| `run_state` | Runtime pause and checkpoint state |
| `context_snapshots` | Context snapshots (resilient to compaction/restarts) |

`lessons` have explicit scoping: `global` and `project` can be reused by subsequent planning; `run`, `node`, and `wbs-family` apply only to the corresponding run, node, or same WBS family, preventing local experience from being misapplied as global rules.

## Timeout Splitting Strategy

Default parameters:

```text
--timeout 900
--max-retries 2
--split-count 4
```

When a Worker times out, the system does not directly terminate the task. Instead, it splits the node into smaller shards:

| Shard | Objective |
|---|---|
| Scope Shard | Find the minimal relevant scope and entry point |
| Evidence Shard | Collect files, commands, symbols, and evidence |
| Implementation Shard | Produce the minimal implementation or patch strategy |
| Risk Shard | Identify blockers, unknowns, and verification needs |

Shards are redispatched to Workers and finally aggregated together.

## Agent Communication Protocol

This project uses ACP-Collab v0.3 to define the communication boundaries between the Hermes parent Agent, the collaborative engine Leader, Workers, and the external pre-analysis layer. The full protocol is documented in [Agent Communication Protocol](docs/agent-communication-protocol.md).

Core conventions:

- Request Channel: Parent submits self-contained requests via CLI/API; Workers do not assume access to the parent session history;
- Dual-Track Result Channel: Worker output includes both machine-parseable results and human-readable deliverables;
- Upstream-Context Channel: Engine injects parent, grandparent, and completed dependency results into downstream Workers;
- Scoped Lessons Channel: Experience must carry source and scope; Planner only reuses experience within applicable scope;
- Dispatch-Control Channel: Streaming dispatch, proactive splitting, and in-run interventions are all recorded via CLI/API and the SQLite state machine;
- Observability Channel: runs, wbs_nodes, workers, logs, and lessons are the unified observation path for the panel and Parent;
- Agent-Backend Channel: Pluggable Worker Agents are invoked through a unified interface, selectable and registrable at runtime;
- Skill-Distribution Channel: Leader selects skills from the Skill library by node type and injects them into Worker prompts;
- MCP-Tool Channel: Leader recommends specific MCP tool sets for Workers based on tool characteristics.

## Integration with Hermes

The installation script creates:

```text
~/.local/bin/hermes-collab
~/.local/bin/opc
```

Optional integration script:

```bash
~/hermes-collab-engine/scripts/install-hermes-integration.sh
```

It writes the following for Hermes:

- Local Skills;
- Memory;
- SOUL behavior prompts;

This lets Hermes know to use the collaborative engine by default when encountering implementation, analysis, debugging, auditing, research, planning, and multi-step tasks.

## Security Boundaries

- Do not upload or commit run databases;
- Do not commit `.runtime-config.json`;
- Do not commit API Keys;
- Do not modify user business projects;
- Worker behavior is executed by the selected Agent CLI; set permission policies and working directories as needed.

## Development Structure

```text
hermes-collab-engine/
├── hermes-collab
├── start.sh
├── start.py
├── scripts/
│   ├── install.sh
│   └── install-hermes-integration.sh
├── src/hermes_collab_engine/
│   ├── agents.py
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
