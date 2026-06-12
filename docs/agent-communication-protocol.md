# Agent Communication Protocol (ACP-Collab v0.1)

> 本协议定义 Hermes 父代理、协同引擎 Leader、协同引擎 Worker、以及外部预分析层（如 `delegate_task`）之间的消息形态、共享通道与可观测性约定。范围限定在 `/root/hermes-collab-engine`，不强加给上游 Hermes。

## 1. 角色

| 角色 | 来源 | 进程边界 | 主要职责 |
|---|---|---|---|
| Parent（父代理） | Hermes 主会话 | Hermes 进程 | 拆解、分发、监督、核验、与用户对话 |
| Preflight（预分析） | `delegate_task` 子代理 | Hermes 子进程 | 方案设计、风险审查、补丁草稿；**不**直接改文件 |
| Leader | `claude -p` 由 `Planner` 触发 | 一次性 Claude 进程 | 复杂度评分、WBS 拆解、aggregate 总结 |
| Worker | `claude -p` 由 `CollabEngine._run_worker` 触发 | 一次性 Claude 进程 | 执行单个 WBS 节点；可读写文件 |

约束：Worker 与 Leader 之间**没有直连**；所有协调都通过 Parent + SQLite 共享状态完成。

## 2. 通信通道

协议定义 4 个正交通道；每条消息必须明确归属某一通道。

### 2.1 Request 通道（Parent → Engine → Worker）

形态：**自然语言文本**，由 CLI 接受，可来自 `--request-file` 或命令行 positional。

```
parent ──run(request)──▶ CollabEngine.run
                              │
                              ├─ Planner.assess(request)  → ComplexityScore
                              ├─ Planner.decompose(request) → list[WBSNode]
                              └─ for each WBSNode:
                                    Worker prompt = render(node, upstream_context)
```

要求：
- request 必须自包含（worker 看不到父对话历史）。
- request 必须显式声明 allowlist、是否允许 git、是否允许 push。
- 长 request（>2KB）走 `--request-file`，避免命令行转义。

### 2.2 Upstream-Context 通道（Worker → Worker，经 Engine 中转）

由本次升级新增（`engine.py:_build_upstream_context`）。

形态：
```
Upstream context (from completed dependency nodes):
--- from <dep_node_id> ---
<dep_result_text，单条上限 800 字符，超出尾部截断并加 [truncated] 前缀>

--- from <dep_node_id_2> ---
...
```

规则：
- 仅 `parent.ok=True` 的节点结果入表；shard 不入表。
- 总长上限 3000 字符；尾部截断（worker 输出的 deliverable 通常在末尾）。
- 上游缺失静默跳过，不报错——兼容"依赖死锁强行推进"路径。
- 读写均经 `self._node_results_lock` 串行化。

降级语义：上游不可用时，下游 worker 仍能基于 `node.description` 独立完成任务。

### 2.3 Lessons 通道（任意来源 → SQLite → Dashboard）

形态：`lessons` 表行。

```python
{
  "category": "preflight" | "watchdog" | "planning" | "interrupt-cleanup" | <custom>,
  "lesson":   "<自然语言经验>",
  "evidence": {
      "source": "preflight" | "engine" | "manual" | "hermes-delegate-task",
      ...任意额外结构化字段...
  }
}
```

入口：
- 引擎自学习：`_learn()` 在 run 结束时按 timeouts / slow 写入。
- 外部预分析层：本次升级新增的 `hermes-collab lesson add` 子命令。
- 中断清理：`fail_stale_run` 写入 `interrupt-cleanup` 分类。

约定：`evidence.source` 字段是**强制**的，用于面板区分自学习 vs 外部注入。Parent 在落实 `delegate_task` 经验时必须设 `--source hermes-delegate-task`，禁止冒充 `engine`。

### 2.4 Observability 通道（Engine → SQLite/Dashboard / Parent）

| 表 | 写入方 | 内容 |
|---|---|---|
| `runs` | Engine | run 元数据 + 状态机（created/planning/running/completed/failed） |
| `wbs_nodes` | Engine | 每个 WBS 节点的 status/result/duration/error |
| `workers` | Engine | 每次 worker 进程的生命周期（含 timeout/failed） |
| `logs` | Engine | 结构化日志事件（level + message + data_json） |

Parent 监督方式（**唯一**官方读路径）：
- `hermes-collab status` (人读)
- `hermes-collab status --json` (机读)
- `GET /api/overview`, `GET /api/runs`, `GET /api/runs/<id>` (Dashboard)
- 直接 SQLite 只读（仅诊断时使用，写需经 CLI）

## 3. 消息时序

```
user
  │
  ▼
Parent (Hermes)
  │  optional: delegate_task → Preflight (绘制方案、补丁草稿)
  │       │
  │       └── lesson add --source hermes-delegate-task   (可选沉淀)
  │
  ├── hermes-collab run --request-file ...   (启动面板可见 run)
  │       │
  │       ▼
  │   Leader: Planner.assess        ──► ComplexityScore
  │   Leader: Planner.decompose     ──► list[WBSNode]
  │       │
  │       ▼
  │   Engine 主循环（ThreadPoolExecutor）
  │       │
  │       ├─ Worker(node_i):
  │       │     prompt = header + upstream_context(node_i) + task(node_i)
  │       │     claude -p ...
  │       │     stdout JSON → WorkerResult
  │       │     ok → store._node_results[node_i] = result   (供下游)
  │       │     timeout → split into shards, 重试
  │       │
  │       └─ Leader: _aggregate (虚拟节点 deps=[])
  │             ──► final report
  │
  ├── 核验：git diff / status, 测试, 面板 status
  └── 报告给 user / 决定 commit & push
```

## 4. 错误语义

| 事件 | 表现 | 谁负责 |
|---|---|---|
| Worker 超时（returncode 124） | `_split_node` 切 4 片重试 | Engine |
| Worker 失败但未超时 | 标记 failed，写入 error | Engine；Parent 可读 `error` 字段 |
| Parent 中断（Ctrl-C / 切话题） | `fail_stale_run` 把 running/pending 标记 failed | Engine（exception handler） |
| Leader 评分/拆解返回非法 JSON | 静默回退到 `_heuristic_assess` / `fallback_wbs` | Planner |
| 上游节点失败 | 下游不阻塞，`Upstream context` 跳过该条 | `_build_upstream_context` |
| `lesson add --evidence-json` 非法 | 退出码 2 + 错误信息 | CLI |

## 5. Schema 版本

当前协议版本：`ACP-Collab v0.1`。变更约定：

- **加字段**：minor bump (v0.2)，向后兼容，老 worker 忽略未知字段。
- **改语义**（如截断方向、降级行为）：major bump (v1.0)，需要双跑期。
- **删字段**：major bump，至少一个发布周期标 deprecated。

## 6. 反模式（禁止）

1. ❌ Worker 之间通过临时文件交换数据——必须走 `Upstream context` 通道，否则面板看不到、Parent 也无法核验。
2. ❌ Preflight 假装自己是 Worker（在 lesson 里写 `source: engine`）——Parent 用户已约定必须披露。
3. ❌ Parent 跳过 CLI 直接写 SQLite——破坏审计；如需脚本化批量写，加 CLI 子命令而非直连 DB。
4. ❌ 长 request 通过命令行而非 `--request-file`——shell 转义会丢字符。
5. ❌ 在 Worker 的 prompt 中嵌入 Parent 会话历史——子代理无父上下文是设计意图（隔离 + 可重放）。

## 7. 待办（未来版本）

- v0.2 计划：lessons 表加 `tags` 字段，CLI `lesson list --tag X`。
- v0.3 计划：worker → worker 直接消息（pub/sub via SQLite trigger），用于真正异步协作；当前序列化模型已能覆盖 90% 场景。
- v1.0 计划：把 ACP-Collab 抽到独立 schema 文件 + JSON-Schema 验证。
