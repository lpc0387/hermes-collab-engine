# Agent Communication Protocol (ACP-Collab v0.2)

> 本协议定义 Hermes 父代理、协同引擎 Leader、协同引擎 Worker、以及外部预分析层（如 `delegate_task`）之间的消息形态、共享通道、调度控制与可观测性约定。范围限定在 `/root/hermes-collab-engine`，不强加给上游 Hermes。

## 1. 角色

| 角色 | 来源 | 进程边界 | 主要职责 |
|---|---|---|---|
| Parent（父代理） | Hermes 主会话 | Hermes 进程 | 拆解、分发、监督、核验、与用户对话；必要时通过干预 CLI 调整运行 |
| Preflight（预分析） | `delegate_task` 子代理 | Hermes 子进程 | 方案设计、风险审查、补丁草稿；**不**直接改文件 |
| Leader | `claude -p` 由 `Planner` 触发 | 一次性 Claude 进程 | 复杂度评分、WBS 拆解、aggregate 总结；不承担重型执行 |
| Worker | `claude -p` 由 `CollabEngine._run_worker` 触发 | 一次性 Claude 进程 | 执行单个 WBS 节点；可读写文件 |

约束：Worker 与 Leader 之间**没有直连**；所有协调都通过 Parent + Engine + SQLite 共享状态完成。

## 2. 通信通道

协议定义 6 个正交通道；每条消息必须明确归属某一通道。

### 2.1 Request 通道（Parent → Engine → Worker）

形态：**自然语言文本**，由 CLI 接受，可来自 `--request-file` 或命令行 positional。

```
parent ──run(request)──▶ CollabEngine.run
                              │
                              ├─ Planner.assess(request)  → ComplexityScore
                              ├─ Planner.decompose(request) → list[WBSNode]
                              └─ schedule ready WBSNode:
                                    Worker prompt = render(node, lineage, upstream_context)
```

要求：
- request 必须自包含（worker 看不到父对话历史）。
- request 必须显式声明 allowlist、是否允许 git、是否允许 push。
- 长 request（>2KB）走 `--request-file`，避免命令行转义。
- Parent 若携带 Preflight 结论，必须把它写入 request 或 lesson；不能假设 Engine 会读取 Hermes 子进程上下文。

### 2.2 Dual-Track Result 通道（Worker → Engine → SQLite / Parent）

v0.2 将 worker 输出明确拆成两条并行轨道：

| 轨道 | 载体 | 读者 | 用途 |
|---|---|---|---|
| Machine result | `claude -p --output-format json` 的 stdout envelope，经 `WorkerResult` 解析 | Engine、Dashboard、Parent 脚本 | `ok`、`returncode`、`session_id`、`duration_seconds`、`is_error`、节点状态机 |
| Human deliverable | JSON envelope 内的 `result` 文本，写入 `wbs_nodes.result` 与 aggregate 输入 | Parent、下游 Worker、用户 | 可读交付物、文件路径、验证结果、失败说明 |

规则：
- Worker 的最终 stdout 必须能被 Engine 解析为机器结果；如果解析失败，Engine 只能把 stdout 当普通文本降级处理。
- Human deliverable 必须独立可读：包含做了什么、改了哪些文件、哪些验证运行/跳过、剩余风险。
- Machine result 不得承载长篇推理；Human deliverable 不得替代 `ok/returncode/session_id` 等机器字段。
- Parent 汇报用户时优先使用 aggregate 的 Human deliverable，同时核对 Machine result 中的失败、超时和错误字段。

降级语义：JSON envelope 缺失或损坏时，节点可按文本失败/成功记录，但 Parent 必须把“机器轨道不可信”作为核验风险处理。

### 2.3 Upstream-Context 通道（Worker → Worker，经 Engine 中转，含 Parent/Grandparent lineage）

v0.2 的上游上下文包含两类信息：

1. **Lineage context**：当前节点的直接 `parent_id`（Parent shard / parent WBS）以及再上一层 grandparent（原始大节点）摘要，用于让超时分片、二次分片理解自己来自哪里。
2. **Dependency context**：本节点声明依赖中已完成且 `ok=True` 的节点 Human deliverable。

典型形态：

```
Lineage context:
Parent node: <parent_id> - <parent title>
Grandparent node: <grandparent_id> - <grandparent title>
Original task excerpt:
<由 parent/grandparent 描述裁剪出的原始任务摘要>

Upstream context (from completed dependency nodes):
--- from <dep_node_id> ---
<dep_result_text，单条上限 800 字符，超出尾部截断并加 [truncated] 前缀>

--- from <dep_node_id_2> ---
...
```

规则：
- 仅 `ok=True` 的父级/依赖结果可作为可信上游；失败节点只通过错误语义暴露，不注入为事实。
- Shard 的 lineage 可以入 prompt；shard 的结果默认不作为 sibling shard 的依赖，除非显式声明 dependencies。
- Dependency context 总长上限 3000 字符；尾部截断（worker 输出的 deliverable 通常在末尾）。
- 上游缺失静默跳过，不报错——兼容“依赖死锁强行推进”和人工干预路径。
- 读写均经 Engine 的 node-result 锁或 store 事务串行化。

降级语义：上游不可用时，下游 worker 仍能基于 `node.description` 与 lineage 独立完成任务；不能要求 Worker 通过临时文件自行找上游结论。

### 2.4 Scoped Lessons 通道（任意来源 → SQLite → Dashboard/Planner）

形态：`lessons` 表行；v0.2 要求 lesson 带明确 scope，避免把局部经验误用为全局规则。

```python
{
  "category": "preflight" | "watchdog" | "planning" | "interrupt-cleanup" | <custom>,
  "lesson":   "<自然语言经验>",
  "evidence": {
      "source": "preflight" | "engine" | "manual" | "hermes-delegate-task",
      "scope": "global" | "project" | "run" | "node" | "wbs-family",
      "run_id": "<required when scope is run/node/wbs-family>",
      "node_id": "<required when scope is node>",
      "parent_id": "<optional for wbs-family>",
      ...任意额外结构化字段...
  }
}
```

入口：
- 引擎自学习：`_learn()` 在 run 结束时按 timeouts / slow 写入，scope 默认为 `run`。
- 外部预分析层：`hermes-collab lesson add` 子命令，Parent 必须显式提供来源与合适 scope。
- 中断清理：`fail_stale_run` 写入 `interrupt-cleanup` 分类，scope 为 `run`。
- 人工复盘：Parent/Operator 可写 `manual`，但必须说明证据来源，禁止无证据的全局化规则。

约定：
- `evidence.source` 字段是**强制**的，用于面板区分自学习 vs 外部注入。
- `evidence.scope` 字段是**强制**的；缺失时按 `run` 处理，不得作为全局经验自动应用。
- Parent 在落实 `delegate_task` 经验时必须设 `--source hermes-delegate-task`，禁止冒充 `engine`。
- Planner/Parent 消费 lesson 时必须先检查 scope：`node` 只影响同节点重试，`wbs-family` 只影响同一 parent/grandparent 下的分片，`project/global` 才能跨 run 复用。

### 2.5 Dispatch-Control 通道（Engine Scheduler / Parent Intervention → SQLite）

v0.2 将调度控制提升为协议通道，覆盖 stream-scheduled dispatch、proactive split 与 intervention CLI。

#### Stream-scheduled dispatch

调度器应维护“ready 队列”而非固定批次屏障：

```
while run.active:
  when dependency satisfied and worker slot free:
    dispatch next ready node immediately
  when worker finishes:
    persist result → unblock dependents → dispatch newly-ready nodes
```

规则：
- 任何节点完成后，下游节点只要依赖满足且有并发槽位，就应立即调度。
- 慢节点不得阻塞同批次中已可运行的其他依赖链。
- 如果发生依赖死锁，Engine 可选择最小风险 pending 节点推进，但必须写 `logs`，并把缺失上游视为降级语义。

#### Proactive split

除 timeout 后拆分外，Engine/Planner 可在执行前主动拆分高风险节点：

| 触发 | 行为 |
|---|---|
| complexity 高、deliverable 宽、预计超时 | 先生成 scope/evidence/implementation/risks 等 focused shards |
| Worker 明确报告范围过大但未超时 | Parent 可通过干预 CLI 要求 split，而不是等待 timeout |
| 上游依赖显示任务已变大 | Scheduler 可在 dispatch 前拆分并保留 parent/grandparent lineage |

规则：
- Proactive shard 必须保留 `parent_id`，二次拆分必须可追溯 grandparent。
- Proactive split 不是失败；原节点应标记为 covered/split，而不是伪造 completed deliverable。
- Shard deliverable 必须窄化，禁止把原任务原样复制给多个 Worker 造成重复写冲突。

#### Intervention CLI

Parent/Operator 对运行中任务的写操作必须走 CLI/API 控制面，不能直写 SQLite。协议保留以下干预语义（具体命令名可随 CLI 演进，但语义必须稳定）：

| 干预 | 目标 | 语义 |
|---|---|---|
| pause/resume | run | 暂停或恢复新节点 dispatch；不杀已运行 worker，除非另有 cancel |
| cancel/fail | run/node | 标记未完成工作为 failed，并写 reason |
| retry | node | 以同一 node description 重新入队，attempt + 1 |
| split | node | 主动拆分节点，写 parent/grandparent lineage，并调度 shards |
| skip/force-ready | node | 人工解除阻塞；必须写 reason，且下游按上游缺失降级 |
| lesson add/list | scoped lesson | 写入或读取带 source/scope 的经验 |

干预记录必须进入 `logs`，并且 Parent 在最终报告中披露人工干预、跳过、取消、强制推进等事实。

### 2.6 Observability 通道（Engine → SQLite/Dashboard / Parent）

| 表 | 写入方 | 内容 |
|---|---|---|
| `runs` | Engine / intervention CLI | run 元数据 + 状态机（created/planning/running/completed/failed/paused 等） |
| `wbs_nodes` | Engine / intervention CLI | 每个 WBS 节点的 lineage、status/result/duration/error |
| `workers` | Engine | 每次 worker 进程的生命周期（含 timeout/failed） |
| `logs` | Engine / intervention CLI | 结构化日志事件（level + message + data_json），包含调度与人工干预记录 |
| `lessons` | Engine / Parent / Preflight | scoped lessons，含 source/scope evidence |

Parent 监督方式（**唯一**官方读路径）：
- `hermes-collab status` (人读)
- `hermes-collab status --json` (机读)
- `hermes-collab lesson list --json` (经验读路径)
- `GET /api/overview`, `GET /api/runs`, `GET /api/runs/<id>`, `GET /api/logs`, `GET /api/lessons`, `GET /api/events` (Dashboard/API)
- 直接 SQLite 只读（仅诊断时使用，写需经 CLI/API 控制面）

## 3. 消息时序

```
user
  │
  ▼
Parent (Hermes)
  │  optional: delegate_task → Preflight (绘制方案、补丁草稿)
  │       │
  │       └── lesson add --source hermes-delegate-task --scope ...   (可选沉淀)
  │
  ├── hermes-collab run --request-file ...   (启动面板可见 run)
  │       │
  │       ▼
  │   Leader: Planner.assess        ──► ComplexityScore
  │   Leader: Planner.decompose     ──► list[WBSNode]
  │       │
  │       ▼
  │   Engine scheduler（stream-scheduled ready queue）
  │       │
  │       ├─ optional proactive split(high-risk node)
  │       │       └─ shards keep parent/grandparent lineage
  │       │
  │       ├─ Worker(node_i):
  │       │     prompt = header + lineage + upstream_context(node_i) + task(node_i)
  │       │     claude -p --output-format json ...
  │       │     stdout JSON envelope → Machine result
  │       │     envelope.result      → Human deliverable
  │       │     ok → store node result   (供下游)
  │       │     timeout / intervention split → shards, 重试或窄化执行
  │       │
  │       └─ Leader: _aggregate (虚拟节点 deps=[])
  │             ──► final report
  │
  ├── optional: hermes-collab intervene ...  (pause/retry/split/skip/cancel)
  ├── 核验：git diff / status, 测试, 面板 status
  └── 报告给 user / 决定 commit & push
```

## 4. 错误语义

| 事件 | 表现 | 谁负责 |
|---|---|---|
| Worker 超时（returncode 124） | timeout result 写入 Machine result；Engine 可 timeout split 或 Parent proactive split | Engine / Parent |
| Worker 失败但未超时 | 标记 failed，写入 error；Human deliverable 作为失败说明 | Engine；Parent 可读 `error` 字段 |
| Machine result JSON 非法 | 降级为普通 stdout；Parent 视为“机器轨道不可信” | Engine / Parent |
| Parent 中断（Ctrl-C / 切话题） | `fail_stale_run` 把 running/pending 标记 failed | Engine（exception handler） |
| Leader 评分/拆解返回非法 JSON | 静默回退到 `_heuristic_assess` / `fallback_wbs` | Planner |
| 上游节点失败 | 下游不阻塞，`Upstream context` 跳过该条 | Scheduler / `_build_upstream_context` |
| 依赖死锁 | force-ready 最小风险节点并记录 warning | Engine / Parent |
| `lesson add --evidence-json` 非法 | 退出码 2 + 错误信息 | CLI |
| 干预 CLI 被拒绝或参数非法 | 不修改状态；返回非 0 并记录/显示原因 | CLI |

## 5. Schema 版本

当前协议版本：`ACP-Collab v0.2`。变更约定：

- **加字段/加通道**：minor bump，向后兼容，老 worker 忽略未知字段。
- **改语义**（如截断方向、调度屏障语义、lesson scope 默认值）：major bump (v1.0)，需要双跑期。
- **删字段**：major bump，至少一个发布周期标 deprecated。
- **CLI 干预语义**：命令名可 minor 调整；状态机语义变化必须 major bump。

v0.2 相对 v0.1 的新增协议点：
- Dual-track result（Machine result + Human deliverable）。
- Parent/grandparent lineage 进入 upstream prompt 语义。
- Scoped lessons（`evidence.source` + `evidence.scope`）。
- Stream-scheduled dispatch、proactive split、intervention CLI 控制面。

## 6. 反模式（禁止）

1. ❌ Worker 之间通过临时文件交换数据——必须走 `Upstream context` / result 通道，否则面板看不到、Parent 也无法核验。
2. ❌ Preflight 假装自己是 Worker（在 lesson 里写 `source: engine`）——Parent 用户已约定必须披露。
3. ❌ Parent 跳过 CLI 直接写 SQLite——破坏审计；如需脚本化批量写，加 CLI 子命令而非直连 DB。
4. ❌ 长 request 通过命令行而非 `--request-file`——shell 转义会丢字符。
5. ❌ 在 Worker 的 prompt 中嵌入 Parent 会话历史——子代理无父上下文是设计意图（隔离 + 可重放）。
6. ❌ 使用 Heavy Leader model 承担所有工作——Leader 只负责评分、拆解和聚合；重型推理/执行应下放到 Worker 或显式人工复核，否则成本高、调度慢且单点失败。
7. ❌ Batch-blocked scheduling——等待整个批次完成才派发下一批会让快路径空转；必须按 ready 队列流式派发，慢节点只阻塞真实依赖它的下游。
8. ❌ Proactive split 后把原任务原样复制给所有 shards——这会制造重复编辑和冲突；每个 shard 必须有窄化焦点和可追溯 lineage。
9. ❌ 把 node/run scoped lesson 当 global 规则复用——局部经验必须局部消费，除非人工提升 scope 并补充证据。

## 7. 待办（未来版本）

- v0.3 计划：lessons 表加 `tags` 字段，CLI `lesson list --tag X`。
- v0.4 计划：worker → worker 直接消息（pub/sub via SQLite trigger），用于真正异步协作；当前 Engine 中转模型已能覆盖 90% 场景。
- v1.0 计划：把 ACP-Collab 抽到独立 schema 文件 + JSON-Schema 验证。
