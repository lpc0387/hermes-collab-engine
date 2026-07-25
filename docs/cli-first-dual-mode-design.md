# 8765 Hermes Collab Engine — CLI 优先 + 双模式架构设计方案

> 基于 V4 引擎架构（`/root/engine-v4-multi-agent-architecture.md`）和 dragon-team WBS/guardian 实战经验。

---

## 一、目标

1. **去除 8765 web 前端对话框** — CLI 对话模式为默认入口，web dashboard 保留为可选项（`--web`）
2. **`opc` 启动简化** — 不再要求输入 URL/key/model，自动检测 agent 连通性
3. **双模式** — 无 leader 模式（你分配任务 + 平权互评 + 你终检）和 leader 模式（你当 guardian + WBS + 引擎调度）

---

## 二、当前状态分析

### 2.1 web/index.html 现状

项目根 `/root/hermes-collab-engine/web/index.html`：
- 单页 Alpine.js dashboard（2219 行）
- 有对话框（输入请求 → POST /api/runs → 显示结果）
- 与 cli.py 的 `server` 命令绑定

### 2.2 cli.py 现状

```bash
$ ./hermes-collab --help
  run      → 一次性的 run，--json 输出
  server   → 启动 web dashboard（默认 :8765）
  status   → 查看运行状态
  ...
```

`server` 命令启动 `DashboardServer`（server.py），打开 web 页面。

### 2.3 启动流程现状

```
hermes-collab [options]
  → CollabEngine.__init__()
  → store.py (SQLite)
  → engine.run() → _run_direct / WBS dispatch
  → 返回结果（stdout 或 web 页面）
```

---

## 三、改动方案

### 3.1 去除 web 前端对话框

**改动文件**：`web/index.html`、`server.py`、`cli.py`

| 改动项 | 当前 | 改为 |
|--------|------|------|
| CLI 默认行为 | `server` 启动 web | 启动对话模式（类似 `run` 但交互式） |
| web dashboard | 对话框 + 历史 + 结果面板 | 只保留监控面板（run 列表/状态/日志），去掉输入框 |
| `server` 命令 | 默认启动 | `--web` 可选启动 |
| 对话模式 | 无 | 新增 `dialog` 子命令，stdin/stdout 交互 |

**web 前端改造**：
- `web/index.html`：移除输入对话框、技能市场、会话列表。只保留运行状态监控面板（实时日志、运行列表、节点状态）
- 添加 `--readonly` 参数：只读监控模式

### 3.2 `opc` 启动简化 + 连通性检测

**改动文件**：`cli.py`、`engine.py`、`agents.py`

**启动流程**：

```
$ opc
  → 1. 扫描已安装 agent:
       opencode    → which opencode   → /usr/local/bin/opencode  ✅
       claude-code → which claude     → /usr/local/bin/claude    ✅
       codex       → which codex      → not found                ❌
       hermes      → which hermes     → /root/.local/bin/hermes  ✅

  → 2. 连通性测试（对各 agent 发简单请求验证 API key 有效）：
       opencode    → test: "print 1+1"  → 2  ✅
       claude-code → test: "print 1+1"  → 2  ✅
       hermes      → test: "say hi"     → hi ✅

  → 3. 展示结果 + 选择模式：
       ╔══════════════════════════════════════╗
       ║  Agent Connectivity Report            ║
       ║──────────────────────────────────────║
       ║  opencode    ✅  0.8s  deepseek-v4   ║
       ║  claude-code ✅  1.2s  claude-sonnet ║
       ║  codex       ❌  not installed       ║
       ║  hermes      ⚠️  need DEEPSEEK_KEY  ║
       ║                                      ║
       ║  Mode: [1] No-Leader  [2] Leader     ║
       ╚══════════════════════════════════════╝

  → 4. 进入对话模式：
       >  请输入任务（或 Ctrl+C 退出）
       > implement a todo app with Flask
       ...
```

**详细设计**：

#### 3.2.1 连通性检测函数（新增 `engine/detector.py`）

```python
@dataclass
class AgentHealth:
    name: str
    installed: bool          # binary exists on PATH
    reachable: bool          # test request succeeded
    latency_ms: float        # test response time
    error: str | None        # error message if failed
    model: str | None        # detected model
    capabilities: list[str]  # agent capabilities

def detect_agent(agent_name: str) -> AgentHealth:
    """检测一个 agent 的安装和连通性"""
    backend = get_backend(agent_name)
    binary = backend.command[0]
    
    # 1. 检查安装
    installed = shutil.which(binary) is not None
    if not installed:
        return AgentHealth(name=agent_name, installed=False, ...)
    
    # 2. 发送测试请求
    try:
        start = time.time()
        result = run_simple_test(backend)
        elapsed = time.time() - start
        return AgentHealth(name=agent_name, installed=True,
                          reachable=True, latency_ms=elapsed*1000, ...)
    except Exception as e:
        return AgentHealth(name=agent_name, installed=True,
                          reachable=False, error=str(e), ...)

def detect_all_agents() -> list[AgentHealth]:
    """检测所有注册的 agent"""
    return [detect_agent(name) for name in REGISTERED_AGENTS]
```

#### 3.2.2 测试请求（轻量验证）

```python
TEST_PROMPTS = {
    "opencode": "输出数字 42，只输出数字不要其他",
    "claude-code": "Output the number 42, nothing else",
    "codex": "print(42)",
    "hermes": "Output 42",
}

def run_simple_test(backend: AgentBackend) -> str:
    """发一个极简请求验证 agent 可用"""
    cmd = backend.build_command(
        prompt=TEST_PROMPTS.get(backend.name, "42"),
        model=...,  # 用配置中的默认模型
        allowed_tools=[], provider=None, reasoning=False,
    )
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    # 验证输出包含 42
    assert "42" in proc.stdout, f"Unexpected output: {proc.stdout}"
    return proc.stdout.strip()
```

### 3.3 双模式设计

#### 3.3.1 Mode 1: No-Leader Mode（无领袖模式）

**架构**：

```
你 (Hermes Agent)  ←→  用户（本对话）
  │
  ├─ 分析任务 → 手动分解为 WBS 节点
  ├─ 为每个节点选择最佳 agent（基于能力画像）
  │   └─ dispatch: 直接调用 agent CLI（非引擎 run）
  ├─ 各 worker 独立产出
  ├─ worker 互评（peer review）
  │   └─ agent A 评 agent B 的产出
  │   └─ reject → 换人重做
  └─ 你终检 → 汇总 → 展示给用户
```

**实现**：

```bash
# opc no-leader 模式的实质：
# 我（当前对话中的 Hermes Agent）直接调度命令，
# 不需要 CollabEngine.run() 的完整 WBS 生命周期

# 示例 dispatch：
$ opencode run "实现一个 Flask todo app 的 models.py" --model opencode-go/deepseek-v4-flash
$ claude run "review the generated models.py" --model claude-sonnet-4
```

**你（Hermes Agent）的职责**：
1. 理解用户任务 → 分解为子任务
2. 为每个子任务选 agent（能力匹配 + 负载均衡）
3. 发起 worker 执行（`subprocess.Popen` 或 `delegate_task`）
4. 收集结果 → 组织 peer review
5. 根据 review 决定接受/重做
6. 终检 → 整合 → 交付

**Peer Review 流程**：

```
worker A 完成 WBS-1 → 你选 worker B 评审 WBS-1
  → B 接受 → 继续
  → B 拒绝 → 你分析原因 → 选 worker C 重做
  → 你终检 → 接受
```

**数据结构**（轻量，不依赖 DB）：

```python
@dataclass
class ReviewResult:
    node_id: str
    reviewer: str        # 谁评的
    target: str          # 评的谁
    verdict: str         # accept / reject / needs_improvement
    score: int           # 1-10
    suggestions: list[str]
```

#### 3.3.2 Mode 2: Leader Mode（领袖模式）

**架构**：

```
你 (Hermes Agent / Guardian)
  │  ← 守护整个 run 生命周期
  │
  └─ CollabEngine.run()  ← 引擎全权处理
       ├─ WBS 分解 + 能力路由
       ├─ 并行 dispatch workers
       ├─ Guardian 线程实时监控
       ├─ 你收到 leader_attention 事件
       │   └─ 你判断：continue / off_track / interrupt
       └─ Aggregator 汇总结果
```

**实现**：基于现有 engine.run()，但：

1. **CLI 交互替代 web** — guardian 事件实时推送到 CLI stdout（而非 web SSE）
2. **leader_attention 走 CLI** — 你收到 `[GUARDIAN] 需要判断：worker 持续输出相同内容 30s`，你决策
3. **无 web 依赖** — 所有状态通过 CLI 推送

**新增 CLI 交互协议**：

```
$ opc leader "实现一个配置中心"
  → WBS 分解为 6 个节点
  → Dispatch 3 workers (codex + claude-code + opencode)
  → [GUARDIAN] wbs-1: 运行中... (12s)
  → [GUARDIAN] wbs-2: 运行中... (8s)
  → [GUARDIAN] ⚠️ wbs-1 已运行 120s 无输出，是否中断？[y/N]
  > y
  → [GUARDIAN] wbs-1 已中断，重试中...
  → [GUARDIAN] wbs-2: ✅ 完成 (45s)
  → [GUARDIAN] Aggregate: 汇总中...
  → ✅ Run 完成 (128s)
  → 结果：
    ┌─────────────────────────────┐
    │ config_center/models.py     │
    │ config_center/storage.py    │
    │ config_center/api.py        │
    │ config_center/test_*.py     │
    └─────────────────────────────┘
```

**Guardian 事件推送**（新增 CLI 输出）：

```python
# engine/guardian.py → 新增 CLI ProtocolHandler
class CLIProtocol:
    """替代 web SSE 的 guardian 事件推送"""
    def emit(self, event_type: str, data: dict):
        if event_type == "guardian:stream":
            print(f"  → [{data['node_id']}] {data['detail']}")
        elif event_type == "guardian:leader_attention":
            print(f"  → [GUARDIAN] ⚠️ {data['detail']}")
            choice = input("  是否中断？[y/N] ")
            return choice.lower() == 'y'
        elif event_type == "guardian:completed":
            print(f"  → [{data['node_id']}] ✅ 完成 ({data['duration']}s)")
        elif event_type == "guardian:worker_error":
            print(f"  → [{data['node_id']}] ❌ 失败: {data['error']}")
```

### 3.4 CLI 改造

**新增子命令**：

| 子命令 | 用途 | 示例 |
|--------|------|------|
| `opc`（无子命令） | 进入对话模式，自动检测 + 模式选择 | `$ opc` |
| `opc no-leader` | 直接进入无 leader 对话模式 | `$ opc no-leader` |
| `opc leader "任务"` | 直接进入 leader 模式执行任务 | `$ opc leader "实现 X"` |
| `opc check` | 只检测连通性，不进入对话 | `$ opc check --json` |
| `opc server --web` | 启动 web 监控面板 | `$ opc server --web` |

**交互流程**：

```
$ opc
  ╔══════════════════════════════════════╗
  ║  Hermes Collab Engine v1.0           ║
  ║──────────────────────────────────────║
  ║  Scanning agents...                  ║
  ║                                      ║
  ║  opencode    ✅  0.8s               ║
  ║  claude-code ✅  1.2s               ║
  ║  codex       ❌  not installed       ║
  ║  hermes      ⚠️  need DEEPSEEK_KEY  ║
  ║                                      ║
  ║  Mode: [1] No-Leader  [2] Leader     ║
  ║  Enter mode number (default 1):      ║
  ╚══════════════════════════════════════╝
  > 1

  ┌─ No-Leader Mode ──────────────────────────────────┐
  │ 输入任务（Enter your task, or /help for commands）│
  └──────────────────────────────────────────────────┘
  > implement a todo app with Flask
  → 分析任务...
  → 分解为 4 个子任务:
      1. [opencode] 项目骨架 + models.py
      2. [codex]    路由 + API 实现
      3. [claude]   前端 HTML + 测试
      4. [hermes]   审查所有产出
  → 开始执行...
  ...
```

### 3.5 配置文件更新

`.runtime-config.json` 新增字段：

```json
{
  "mode": "no-leader | leader",
  "auto_detect": true,
  "agents": {
    "opencode": {
      "enabled": true,
      "model": "opencode-go/deepseek-v4-flash",
      "max_concurrency": 3
    },
    "claude-code": {
      "enabled": true,
      "model": "claude-sonnet-4",
      "max_concurrency": 2
    },
    "codex": {
      "enabled": false,
      "model": "deepseek-v4-flash",
      "max_concurrency": 2
    },
    "hermes": {
      "enabled": true,
      "model": "deepseek-v4-flash",
      "max_concurrency": 1,
      "is_leader": true
    }
  },
  "dispatch": {
    "peer_review": true,
    "retry_on_reject": true,
    "max_retries_per_node": 2
  }
}
```

---

## 四、改动文件清单

| 文件 | 改动 | 行数 |
|------|------|------|
| `cli.py` | 新增 `opc` 默认对话模式；新增 `no-leader`/`leader` 子命令；连通性检测入口 | ~150 |
| `engine/detector.py` | **新文件**：AgentHealth dataclass + detect_agent() + detect_all_agents() | ~100 |
| `engine/cli_protocol.py` | **新文件**：CLIProtocol handler，替代 web SSE | ~80 |
| `agents.py` | 新增 `capabilities` 字段（V4 能力画像） | ~50 |
| `server.py` | `--web` 可选启动，默认关闭 | ~20 |
| `web/index.html` | 移除对话框，保留只读监控面板 | ~500 |
| `config_store.py` | 新增 `mode`/`agents`/`dispatch` 配置 | ~30 |
| `planner.py` | 能力标注（capability 推断） | ~50 |

**总计**：~980 行新代码（含 index.html 裁剪）

---

## 五、执行步骤

| 步 | 内容 | 依赖 |
|----|------|------|
| **1** | 创建 `engine/detector.py` — 连通性检测 | 无 |
| **2** | 改造 `cli.py` — 默认对话模式 + `no-leader`/`leader` 子命令 | 步 1 |
| **3** | 创建 `engine/cli_protocol.py` — guardian CLI 推送 | 步 2 |
| **4** | 改造 `server.py` + `web/index.html` — 只读监控 + `--web` 可选 | 步 2 |
| **5** | `agents.py` 新增能力画像 + `config_store.py` 新增字段 | 步 1 |
| **6** | `planner.py` 能力标注 | 步 5 |
| **7** | 端到端测试 | 全部 |

---

## 六、注意事项

1. **No-Leader 模式下，你（Hermes Agent）是调度主体** — 不需要 engine.run() 的 WBS 生命周期。直接 `subprocess.run()` 调用 agent CLI 更快
2. **Leader 模式下，engine.run() 保留** — 但 guardian 事件走 CLIProtocol 而非 web SSE
3. **`--cwd` 不再需要** — 默认为当前目录，自动创建
4. **URL/key/model 不提示** — 优先从 `.runtime-config.json` 读取，然后从环境变量读取，最后用默认值。启动时不要求输入
5. **web 不删除** — 只是从默认改为可选，保留为监控工具
6. **PIPE vs PTY** — leader 模式中 guardian 事件输出用 CLI stdout 打印（PIPE 模式兼容），避免 PTY 依赖
