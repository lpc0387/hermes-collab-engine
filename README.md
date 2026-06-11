# Hermes Collab Engine

<p align="center">
  <a href="README.md">简体中文</a> |
  <a href="README.en.md">English</a> |
  <a href="README.ja.md">日本語</a>
</p>

> 面向官方 Hermes Agent 与 Claude Code 的协同执行引擎：自动判断任务复杂度，按 WBS 拆解，多执行器并行分发，超时自动拆分重试，SQLite 持久化，并提供中文管理面板。

[![Python](https://img.shields.io/badge/Python-3.11%2B-blue)](#环境要求)
[![SQLite](https://img.shields.io/badge/SQLite-持久化-green)](#持久化)
[![Hermes](https://img.shields.io/badge/Hermes-Agent-purple)](#运行链路)
[![Claude Code](https://img.shields.io/badge/Claude%20Code-Worker-orange)](#运行链路)

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
4. 选择管理面板监听地址、端口和默认工作目录；
5. 启动协同引擎管理面板；
6. 选择操作方式：使用 Web 面板里的任务输入窗口，或进入官方 Hermes 命令行。

退出所选操作方式后，`opc` 会停止本次启动的管理面板服务。默认推荐使用 Web 面板操作，因为面板已经内置任务输入窗口；需要终端交互时再选择 Hermes 命令行。

## 它解决什么问题

单个 Agent 在处理大型任务时容易遇到：

- 任务边界不清，无法判断是否需要拆解；
- 所有工作串行执行，效率低；
- 长任务超时后缺少断点和重试策略；
- 多个 Worker 的状态不可见；
- 执行历史、失败原因、经验无法沉淀；
- 缺少统一面板查看日志、运行状态和任务图。

Hermes Collab Engine 将任务执行拆成“规划层”和“执行层”：

- **Leader Agent** 负责复杂度判断、WBS 拆解和结果聚合；
- **Worker Agent** 负责执行具体 WBS 节点；
- **SQLite** 记录运行状态、节点、执行器、日志和学习经验；
- **管理面板** 实时展示运行状态。

## 运行链路

```text
用户
  ↓
官方 Hermes Agent
  ↓ terminal 工具
Hermes Collab Engine
  ↓ WBS / 调度 / SQLite / Watchdog
Claude Code Worker 1..N
  ↓
聚合结果
  ↓
返回用户
```

## 核心能力

| 能力 | 说明 |
|---|---|
| 复杂度判断 | 根据领域、步骤数、模糊度、耦合度、风险计算任务复杂度 |
| WBS 拆解 | 复杂任务自动拆成可执行的工作分解节点 |
| 并行分发 | 依赖满足的节点并行交给 Claude Code 执行器 |
| 超时守护 | Worker 超时后自动进入拆分与重试流程 |
| 分片重试 | 超时节点会拆成范围、证据、实现、风险等聚焦分片 |
| 结果聚合 | 聚合父任务和分片结果，诚实报告成功、失败和超时 |
| SQLite 持久化 | 使用真实 SQLite 文件保存运行历史和状态 |
| 自学习经验 | 从超时、慢任务、失败中沉淀经验，用于后续规划 |
| 管理面板 | 中文 Web 面板展示运行记录、日志、执行器和经验 |

## 自我升级同步策略

AI / 协同引擎的稳定规则变更必须同步到 GitHub，作为备份和迁移能力；提交时采用 allowlist 最小提交策略，并禁止提交密钥、profiles、settings、运行数据库、日志或会话记录。详见 [AI / 协同引擎自我升级同步策略](docs/self-upgrade-policy.md)。

## 环境要求

- Linux / macOS / WSL
- Python 3.11+
- Git
- Claude Code CLI：`claude`
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

- Claude Code Worker 执行；
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
hermes-collab server --host 0.0.0.0 --port 8765 --cwd .
```

访问：

```text
http://服务器IP:8765
```

### 查看状态

```bash
hermes-collab status --json
```

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
| `runs` | 顶层任务运行记录 |
| `wbs_nodes` | WBS 节点、依赖、状态和结果 |
| `workers` | 执行器生命周期、会话 ID、耗时和错误 |
| `logs` | 结构化日志 |
| `lessons` | 自学习经验 |
| `metrics` | 扩展指标 |

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
- Worker 的实际行为由 Claude Code CLI 执行，应按需要设置权限策略和工作目录。

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

## 许可证

MIT
