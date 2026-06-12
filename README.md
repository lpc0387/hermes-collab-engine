# Hermes Collab Engine

<p align="center">
  <a href="README.md">简体中文</a> |
  <a href="README.en.md">English</a> |
  <a href="README.ja.md">日本語</a>
</p>

> 面向多 Agent 的协同执行引擎：自动判断任务复杂度，按 WBS 拆解，多执行器并行分发，支持 Claude Code / Codex / OpenCode 等多种 Worker，超时自动拆分重试，SQLite 持久化，Skill 分发，MCP 工具管理，并提供可视化中文管理面板。

[![Python](https://img.shields.io/badge/Python-3.11%2B-blue)](#环境要求)
[![SQLite](https://img.shields.io/badge/SQLite-持久化-green)](#持久化)
[![Hermes](https://img.shields.io/badge/Hermes-Agent-purple)](#运行链路)
[![Multi-Agent](https://img.shields.io/badge/Worker-Claude%20Code%20%7C%20Codex%20%7C%20OpenCode-orange)](#agent-backend)

## 一键部署

```bash
curl -fsSL https://raw.githubusercontent.com/lpc0387/hermes-collab-engine/main/scripts/install.sh | bash
```

部署完成后启动：

```bash
opc
```

`opc` 会引导你完成：

1. 选择自动读取本机 Claude/Hermes 配置，或手动填写 BaseURL、API Key 和模型列表；
2. 选择 Leader Agent 模型；
3. 选择 Worker Agent 模型；
4. 选择 Worker Agent 类型（Claude Code / Codex / OpenCode / 自定义）；
5. 选择管理面板监听地址、端口和默认工作目录；
6. 启动协同引擎管理面板；
7. 选择操作方式：使用 Web 面板里的任务输入窗口，或进入官方 Hermes 命令行。

退出所选操作方式后，`opc` 会停止本次启动的管理面板服务。默认推荐使用 Web 面板操作，因为面板已经内置任务输入窗口；需要终端交互时再选择 Hermes 命令行。

## 它解决什么问题

单个 Agent 在处理大型任务时容易遇到：

- 任务边界不清，无法判断是否需要拆解；
- 所有工作串行执行，效率低；
- 长任务超时后缺少断点和重试策略；
- 多个 Worker 的状态不可见；
- 执行历史、失败原因、经验无法沉淀；
- 缺少统一面板查看日志、运行状态和任务图；
- 不同编码 Agent 无法统一调度；
- Worker 缺少领域技能和工具指导。

Hermes Collab Engine 将任务执行拆成"规划层"和"执行层"：

- **Leader Agent** 负责复杂度判断、WBS 拆解、Skill 分发、工具分配和结果聚合；
- **Worker Agent** 负责执行具体 WBS 节点，按需加载 Skill 和 MCP 工具；
- **Agent Backend** 抽象不同编码 Agent 的调用和输出解析；
- **SQLite** 记录运行状态、节点、执行器、日志、上下文快照和学习经验；
- **管理面板** 实时可视化展示运行状态和协同工作流。

## 运行链路

```text
用户
  ↓
Hermes 父代理
  ├─ 可选：delegate_task 预分析
  ↓ terminal 工具
Hermes Collab Engine
  ├─ Leader：复杂度评分 / WBS / Skill 分发 / 工具分配 / 主动拆分
  ├─ Scheduler：流式调度 / 干预控制
  ├─ Agent Backend：Claude Code / Codex / OpenCode / 自定义
  ├─ Skill Registry：按节点类型注入 Worker prompt
  ├─ MCP Tool Manager：根据工具特征推荐 Worker 使用
  ├─ SQLite：运行 / 节点 / 日志 / 快照 / scoped lessons
  └─ Watchdog：超时拆分
      ↓
Worker Agent 1..N（可混合不同 Agent 类型）
  ↓ 双轨输出（机器结果 + 人类交付物）
聚合结果
  ↓
返回用户
```

## Agent Backend

v4.0 引入可插拔 Agent Backend 系统，替代硬编码的 Worker 调用。每个 Backend 定义如何调用和解析特定编码 Agent 的输出。

### 内置 Backend

| Backend | 命令 | 输出解析 | 适用场景 |
|---|---|---|---|
| `claude-code` | `claude` | JSON envelope | 通用编码（默认） |
| `codex` | `codex` | JSON envelope | OpenAI Codex 编码 |
| `opencode` | `opencode` | 纯文本 | 轻量级编码 |

### 选择 Worker Agent

```bash
# 默认 Claude Code
hermes-collab run "task"

# 使用 Codex
hermes-collab run "task" --agent codex

# 查看可用 Agent
hermes-collab agents
hermes-collab agents --available
```

### 自定义 Agent

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

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/agents` | 列出可用 Agent Backend |

## 核心能力

| 能力 | 说明 |
|---|---|
| 复杂度判断 | 根据领域、步骤数、模糊度、耦合度、风险计算任务复杂度 |
| WBS 拆解 | 复杂任务自动拆成可执行的工作分解节点 |
| Agent Backend | 可插拔 Worker Agent：Claude Code / Codex / OpenCode / 自定义 |
| 并行分发 | 依赖满足的节点并行交给选定 Agent 执行 |
| 超时守护 | Worker 超时后自动进入拆分与重试流程 |
| 分片重试 | 超时节点会拆成范围、证据、实现、风险等聚焦分片 |
| 结果聚合 | 聚合父任务和分片结果，诚实报告成功、失败和超时 |
| SQLite 持久化 | 使用真实 SQLite 文件保存运行历史、节点结果和上下文快照 |
| 上下文快照 | 节点完成和压缩前自动保存上下文，支持压缩后恢复聚焦 |
| 自学习经验 | 从超时、慢任务、失败中沉淀经验，用于后续规划 |
| 管理面板 | 中文 Web 面板展示运行记录、日志、执行器和经验 |
| Leader 驱动评分 | Leader Agent 负责复杂度评分，按领域、步骤、模糊度、耦合度和风险决定执行策略 |
| 语义压缩拆解 | Planner 输出共享 brief 和节点 brief，把大任务压缩成 Worker 可执行的最小上下文 |
| 双轨输出 | Worker 同时产出机器可解析结果和人类可读交付物，便于调度、面板和最终汇报使用 |
| 分级上游上下文 | Worker prompt 自动带入 parent、grandparent 和已完成依赖结果，保持分片 lineage 可追溯 |
| 流式调度 | 调度器在依赖满足且有空闲槽位时立即派发节点，避免固定批次屏障拖慢下游链路 |
| 主动拆分 | 对预计超时或高风险节点可在执行前拆成聚焦分片，而不是等到超时后再补救 |
| 父代干预 | Parent / Operator 可通过 CLI 记录日志、kill、split、skip 运行中节点，并写入审计日志 |
| 经验作用域 | lessons 带有 global、project、run、node、wbs-family 作用域，避免局部经验污染全局规划 |
| 环境变量模型 | CLI 支持 HERMES_COLLAB_MODEL、HERMES_COLLAB_LEADER_MODEL、HERMES_COLLAB_WORKER_MODEL 与 ANTHROPIC_MODEL 作为模型回退 |

## 自我升级同步策略

AI / 协同引擎的稳定规则变更必须同步到 GitHub，作为备份和迁移能力；提交时采用 allowlist 最小提交策略，并禁止提交密钥、profiles、settings、运行数据库、日志或会话记录。详见 [AI / 协同引擎自我升级同步策略](docs/self-upgrade-policy.md)。

## 环境要求

- Linux / macOS / WSL
- Python 3.11+
- Git
- 至少一种 Worker Agent：Claude Code CLI (`claude`)、Codex CLI (`codex`) 或 OpenCode (`opencode`)
- 官方 Hermes Agent：`hermes`

无需 Node.js 依赖，无需 npm install。

## 启动器

```bash
opc
```

启动器支持两种 API 配置方式：

### 自动读取本机配置

会读取：

```text
~/.claude/settings.json
~/.claude/profiles/*.json
```

适合已经配置好 Claude Code / Hermes 的服务器。

### 手动填写配置

会提示输入：

- BaseURL
- API Key / Auth Token
- 可用模型列表，多个模型用英文逗号分隔

适合新服务器或不希望读取本机配置的场景。

## 模型选择

启动时会分别选择：

### Leader Agent 模型

用于：

- 复杂度判断；
- WBS 拆解；
- 结果聚合；
- 进入 Hermes 命令行时作为 Hermes 的默认模型。

### Worker Agent 模型

用于：

- Worker Agent 执行；
- WBS 节点处理；
- 超时后的分片重试。

## 命令行使用

### 运行一次任务

```bash
hermes-collab run "分析当前项目结构并给出改进建议" --cwd . --json
```

### 指定并行量和超时策略

```bash
hermes-collab run "实现一个协同任务" \
  --cwd . \
  --agent claude-code \
  --concurrency 4 \
  --timeout 900 \
  --max-retries 2 \
  --split-count 4 \
  --json
```

### 使用任务文件

```bash
hermes-collab run --request-file request.md --cwd . --json
```

### 启动管理面板

```bash
hermes-collab server --host 0.0.0.0 --port 8765 --cwd . --agent claude-code
```

访问：

```text
http://服务器IP:8765
```

### 查看 Agent Backend

```bash
hermes-collab agents                # 所有已注册
hermes-collab agents --available    # 仅 PATH 上可用的
hermes-collab agents --available --json
```

### 查看状态

```bash
hermes-collab status --json
```

### 管理经验

写入带作用域的经验：

```bash
hermes-collab lesson add \
  --scope project \
  --category planning \
  --lesson "类似任务优先拆成分析、实现、验证三段" \
  --source hermes-delegate-task \
  --evidence-json '{"run_id":"run_xxx"}'
```

查看经验：

```bash
hermes-collab lesson list --scope project --json
```

支持的 scope：`global`、`project`、`run`、`node`、`wbs-family`。

### 上下文快照

压缩上下文前手动保存：

```bash
hermes-collab save-snapshot <run_id> \
  --type pre_compaction \
  --decisions '["chose X over Y"]' \
  --user-instructions '["prefer concise responses"]'
```

查看快照：

```bash
hermes-collab context-snapshot <run_id> --latest
hermes-collab context-snapshot <run_id> --type pre_compaction
```

### 运行中干预

Parent / Operator 可以通过 CLI 对运行中的节点进行受控干预：

```bash
hermes-collab parent-log --run-id run_xxx --message "人工确认继续执行" --json
hermes-collab split-node --node-id wbs-1 --split-count 4 --reason "范围过大，主动拆分" --json
hermes-collab skip-node --node-id wbs-2 --reason "上游已确认无需执行" --json
hermes-collab kill-node --node-id wbs-3 --signal TERM --reason "执行方向错误，停止重试" --json
```

所有干预都会写入日志，最终汇报必须披露人工干预、跳过、取消或强制推进等事实。

## 管理面板

管理面板提供：

- 总运行次数；
- 正在运行数量；
- 运行中执行器数量；
- 学习经验数量；
- 运行记录列表；
- 运行详情；
- 实时日志；
- 自学习经验；
- 在线提交协同任务；
- SSE 实时更新。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/overview` | 总览指标 |
| GET | `/api/runs` | 运行记录 |
| GET | `/api/runs/:id` | 单次运行详情 |
| GET | `/api/logs` | 最近日志 |
| GET | `/api/lessons` | 自学习经验 |
| GET | `/api/agents` | 可用 Agent Backend |
| GET | `/api/events` | 实时事件流 |
| POST | `/api/runs` | 异步提交任务 |

## 持久化

默认数据库：

```text
data/collab.sqlite3
```

数据表：

| 表 | 用途 |
|---|---|
| `runs` | 顶层任务运行记录（含 agent 类型） |
| `wbs_nodes` | WBS 节点、依赖、状态、结果和上下文 |
| `workers` | 执行器生命周期、会话 ID、耗时和错误 |
| `logs` | 结构化日志 |
| `lessons` | 自学习经验（含 scope） |
| `metrics` | 扩展指标 |
| `node_results` | Worker 结果文本和结构化输出 |
| `run_state` | 运行时暂停和 checkpoint 状态 |
| `context_snapshots` | 上下文快照（抗压缩/重启） |

`lessons` 带有明确作用域：`global` 和 `project` 可被后续规划复用；`run`、`node`、`wbs-family` 只用于对应运行、节点或同一 WBS 家族，避免把局部经验误用为全局规则。

## 超时拆分策略

默认参数：

```text
--timeout 900
--max-retries 2
--split-count 4
```

当某个 Worker 超时，系统不会直接结束任务，而是将该节点拆成更小的分片：

| 分片 | 目标 |
|---|---|
| 范围分片 | 找到最小相关范围和入口 |
| 证据分片 | 收集文件、命令、符号和证据 |
| 实现分片 | 产出最小实现或补丁策略 |
| 风险分片 | 找出阻塞点、未知项和验证需求 |

分片会重新分发给 Worker，最后统一聚合。

## Agent 通信协议

本项目使用 ACP-Collab v0.3 约定 Hermes 父代理、协同引擎 Leader、Worker 与外部预分析层之间的通信边界。完整协议见 [Agent Communication Protocol](docs/agent-communication-protocol.md)。

核心约定：

- Request 通道：Parent 通过 CLI/API 提交自包含请求，Worker 不假设能读取父会话历史；
- Dual-Track Result 通道：Worker 输出同时包含机器可解析结果和人类可读交付物；
- Upstream-Context 通道：Engine 将 parent、grandparent 与已完成依赖结果注入下游 Worker；
- Scoped Lessons 通道：经验必须带 source 和 scope，Planner 只复用适用范围内的经验；
- Dispatch-Control 通道：流式调度、主动拆分和运行中干预都经 CLI/API 与 SQLite 状态机记录；
- Observability 通道：runs、wbs_nodes、workers、logs、lessons 是面板与 Parent 的统一观测路径；
- Agent-Backend 通道：可插拔 Worker Agent 通过统一接口调用，运行时可选择和注册；
- Skill-Distribution 通道：Leader 根据节点类型从 Skill 库选择技能注入 Worker prompt；
- MCP-Tool 通道：Leader 根据工具特征推荐 Worker 使用特定 MCP 工具集。

## 与 Hermes 集成

安装脚本会创建：

```text
~/.local/bin/hermes-collab
~/.local/bin/opc
```

可选集成脚本：

```bash
~/hermes-collab-engine/scripts/install-hermes-integration.sh
```

它会为 Hermes 写入：

- 本地 Skill；
- Memory；
- SOUL 行为提示；

让 Hermes 知道：遇到实现、分析、调试、审计、研究、规划、多步骤任务时，默认使用协同引擎。

## 安全边界

- 不上传或提交运行数据库；
- 不提交 `.runtime-config.json`；
- 不提交 API Key；
- 不修改用户业务项目；
- Worker 的实际行为由选定 Agent CLI 执行，应按需要设置权限策略和工作目录。

## 开发结构

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

## 许可证

MIT
