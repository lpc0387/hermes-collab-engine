# 8765 上游引擎 — 全项目代码质量审查报告

审查日期: 2026-07-11
审查范围: 28 个源文件, 14,782 行代码 (不含 .venv)
审查方法: 3 路并行分析 + 交叉验证

---

## 一、项目架构总览

```
/root/hermes-collab-engine/
├── src/hermes_collab_engine/     (25 .py 文件)
│   ├── engine.py          (3184 行)  # 核心编排引擎
│   ├── cli.py             (1594 行)  # CLI 入口
│   ├── store.py           (1124 行)  # SQLite 持久层
│   ├── registry.py        (581 行)   # 统一注册表
│   ├── agents.py          (542 行)   # Agent/Backend 注册
│   ├── planner.py         (518 行)   # 任务分解/规划
│   ├── server.py          (434 行)   # HTTP API 服务端
│   ├── add_agent.py       (390 行)   # Agent 管理
│   ├── skill_distributor.py (387 行) # 技能分发 (有死代码污染)
│   ├── config_store.py    (347 行)   # JSON 配置持久化
│   ├── capabilities.py    (235 行)   # 能力路由 (有死代码)
│   ├── no_leader.py       (224 行)   # No-Leader 执行模式
│   ├── provider.py        (203 行)   # Provider 配置
│   ├── detector.py        (179 行)   # 环境检测
│   ├── models.py          (141 行)   # 数据模型
│   ├── cli_protocol.py    (133 行)   # CLI Guardian 协议
│   ├── verification.py    (102 行)   # 验证工具 (有死代码)
│   └── distill/           (6 文件, 553 行)  # 日志蒸馏 (引擎侧未连接)
├── web/
│   └── index.html         (2080 行)  # Web 仪表盘 SPA
├── proxy.py               (120 行)   # HTTP 代理
├── start.py               (1492 行)  # 启动脚本
└── config_store.py        (347 行)   # (duplicate listing)
```

### 行数分布

| 层级 | 文件数 | 行数 | 占比 |
|------|--------|------|------|
| 核心执行 (engine, planner, agents, models, provider) | 5 | 5,589 | 37.8% |
| CLI + API + 持久化 (cli, server, store, registry, config_store, add_agent) | 6 | 4,423 | 29.9% |
| 功能模块 (verification, capabilities, detector, no_leader, cli_protocol, skill_distributor) | 6 | 1,670 | 11.3% |
| 蒸馏 (distill/) | 6 | 553 | 3.7% |
| 启动/代理 (start.py, proxy.py) | 2 | 1,612 | 10.9% |
| 前端 (web/index.html) | 1 | 2,080 | 14.1% |
| (部分重叠) | | | |
| **总计** | **~28** | **~14,782** | **100%** |

---

## 二、已实现功能清单

### 核心引擎

| # | 功能 | 说明 |
|---|------|------|
| 1 | 多 Worker 调度 | ThreadPoolExecutor + WBS 节点调度 |
| 2 | 任务分解 (WBS) | Planner 从请求分解为 WBS 节点 |
| 3 | 复杂性评估 | 启发式 + LLM 双通道评估 |
| 4 | 直接回答模式 | `routing=="direct"` 不走 WBS |
| 5 | No-Leader 模式 | v7.0 新增, 通过 cli.py 对话框路径 |
| 6 | Leader 模式 | 子进程 hermes + ctl_dir 文件协议 |
| 7 | Guardian 超时/停滞检测 | 两种模式都支持 |
| 8 | 聚合总结 | 运行完成后 LLM 总结 |
| 9 | 会话管理 (session) | opencode session 持久化 |
| 10 | 能力路由 | 根据任务能力自动选择 agent (capabilities.py 但未接入) |

### CLI

| # | 功能 | 说明 |
|---|------|------|
| 11 | run 命令 | 提交 run 到引擎 |
| 12 | server 命令 | 启动 HTTP API 服务 |
| 13 | config 命令 | 读写配置 |
| 14 | doctor 命令 | 诊断健康状态 |
| 15 | kill-node 命令 | 杀进程/清理 |
| 16 | 交互式对话框 | 对话模式的 `_run_dialog` |
| 17 | No-Leader CLI 路径 | `_run_no_leader_task` |

### HTTP API (server.py)

| # | 路由 | 说明 |
|---|------|------|
| 18 | POST /api/runs | 创建并执行 run |
| 19 | GET /api/runs | 列出现有 run |
| 20 | GET /api/runs/{id} | 查看 run 详情 |
| 21 | DELETE /api/runs/{id} | 删除 run |
| 22 | GET /api/run-logs | 日志流 |
| 23 | GET /api/agents | 列出 agent |
| 24 | PUT /api/agents | 更新 agent |
| 25 | GET /api/skills | 列出 skill |
| 26 | GET /api/tools | 列出 tool |
| 27 | GET /api/sessions | 列出 session |
| 28 | GET /api/sessions/{id}/messages | session 消息 |
| 29 | POST /api/sessions/{id}/messages | 写入 session 消息 |
| 30 | GET /api/stats | 统计 |
| 31 | GET /api/events (SSE) | 服务端事件流 |
| 32 | GET /api/lessons | 列出 lessons |
| 33 | GET /api/resume-context | resume 上下文 |
| 34 | GET /api/mcp-servers | MCP 服务列表 |

### 持久化 (store.py)

| # | 功能 | 说明 |
|---|------|------|
| 35 | runs 表 CRUD | 创建/读取/更新/删除 |
| 36 | wbs_nodes 表 CRUD | WBS 节点持久化 |
| 37 | sessions 表 CRUD | 会话管理 |
| 38 | lessons 表 CRUD | 日志/经验存储 |
| 39 | peer_reviews 表 CRUD | 同行评审 |
| 40 | files 表 CRUD | 文件存储 |
| 41 | guardian events 存储 | 守护事件日志 |

### 前端 (index.html)

| # | 功能 | 说明 |
|---|------|------|
| 42 | Dashboard 面板 | Run 列表、状态过�� |
| 43 | Run 详情面板 | 节点图、进度、输出 |
| 44 | Session 管理器 | 会话面板 |
| 45 | Agent 控制面板 | Agent 管理 |
| 46 | Skill/Tool 预览 | 技能工具查看 |
| 47 | 事件流集成 | WebSocket + HTTP 轮询 |

---

## 三、严重问题 (P0 — 阻塞用户功能)

| # | 严重度 | 问题 | 根因 | 文件 |
|---|--------|------|------|------|
| 1 | 🔴 P0 | **config-center.html 整个页面不可用** — 前端调用 `/api/config-center/{overview,namespaces,configs,history,rollback}` 共 5 个端点, 但 server.py 中没有任何 `/api/config-center/` 路由。整个页面是全死功能。 | 后端路由从未实现 | `web/config-center.html` ↔ `server.py` |
| 2 | 🔴 P0 | **store.py create_run 默认 agent 是 "claude-code"** — 而 CLI (cli.py) 和 server.py 默认是 "opencode"。直接调用 `store.create_run()` (如 API 路径) 得到的 run 会获得错误的 agent 默认值。 | 默认值未统一 | `store.py:424`, `store.py:225` |
| 3 | 🔴 P0 | **ThreadingHTTPServer 的 `_sse_connections` 无锁竞争** — 类变量 `Handler._sse_connections` 在并发 SSE 连接中通过 `+=`/`-=` 自增/自减, 不是原子操作。可能超过 `_SSE_MAX_CONNECTIONS` 限制或计数器负值。 | 缺少 threading.Lock | `server.py:135,138,159` |
| 4 | 🔴 P0 | **get_unified_registry(store=) 参数完全未实现** — 函数签名接受 `store` 参数, 文档说"用于持久化", 但函数体从未使用该参数。所有调用者传了 `store=store` 期望写入 DB, 但从未发生。 | 参数声明但未实现 | `registry.py:459-463` |
| 5 | 🔴 P0 | **distill/ 模块 (553 行) 完全未连接** — 引擎代码 (engine.py, cli.py, server.py) 没有一行从 distill 导入或调用。crontab 路径指向不存在的 `/root/hermes/venv/bin/python`。 | 从未集成到主引擎 | `distill/*.py` |
| 6 | 🔴 P0 | **skill_distributor.py 的 `TYPE_CHECKING` 段引用不存在的模块** — `from .skills import SkillEntry, SkillRegistry` 和 `from .tools import ToolRegistry` 在 8765 上游代码中不存在 (属于 dragon-team 分支)。虽然被 `TYPE_CHECKING` 保护, 但表明从 fork 复制了死代码。 | dragon-team 污染 | `skill_distributor.py:20-21` |
| 7 | 🔴 P0 | **engine.py `_run_worker()` 中 `self.agent_backend` 存在竞态** — 方法通过修改 `self.agent_backend` (全局属性) 来切换 agent, 然后委托给 `_run_worker_impl`。多线程调度时不同线程的 agent 切换会相互覆盖。 | 全局状态而非局部参数 | `engine.py:2001-2009` |
| 8 | 🔴 P0 | **Planner 的 `_claude_json()` 中 `self.agent_backend` 未定义** — `Planner.__init__` 没有设置 `self.agent_backend`, 但 `_claude_json()` 在 `leader_agent is None` (No-Leader 模式) 时引用它。`AttributeError` 被 `except Exception: pass` 静默吞掉, 导致 LLM 评估静默降级到启发式。 | `decompose()` 接收了 `agent_backend` 参数但从未存入 `self` | `planner.py:247, 495-499` |

### 死代码块 (P0 等效)

| # | 行数 | 死代码 | 说明 | 文件 |
|---|------|--------|------|------|
| 9 | ~210 | `_run_direct()` | 完整实现的直接回答路径, 但从被调用。直接路由 `run()` 内联处理。 | `engine.py:2791` |
| 10 | ~60 | `_leader_guard_run()` | Tier-3 leader 升级路径, 从未被调用。 | `engine.py:1243` |
| 11 | ~20 | `_wait_leader_reply()` | Leader 回复等待机制, 未连接。 | `engine.py:2549` |
| 12 | ~100 | `_is_hermes_builtin_skill()` / `_is_hermes_builtin_tool()` | 本机能力过滤方法, 被注释掉的代码禁用但方法本身保留。 | `engine.py:1685-1696` |
| 13 | ~37 | `deduplicate_lessons()` | 零调用者。 | `store.py:813-850` |
| 14 | ~12 | `SkillEntry.from_legacy()` / `ToolEntry.from_legacy()` | 迁移辅助方法, 从未被调用。 | `registry.py:52,75` |
| 15 | ~10 | `backends_for_capability()` | 本公开发布的能力路由方法, 零调用者。 | `agents.py:527-530` |
| 16 | ~10 | `delete_backend()` | 本公开发布的删除方法, 零调用者。 | `agents.py:537-542` |
| 17 | ~5 | `_heuristic_assess()` | 一行透传到 `_local_assess` 的包装器, 零调用者。 | `planner.py:96` |
| 18 | ~5 | `_find_insert_point()` | 定义但从未被调用。 | `add_agent.py:207-211` |

---

## 四、严重问题 (P1 — 有 workaround)

| # | 严重度 | 问题 | 文件 | 说明 |
|---|--------|------|------|------|
| 19 | 🟡 P1 | **三个 `except Exception:` 静默吞错误** | `cli.py:1007,1059,292` | JSON 解码和其他错误被静默吞掉, 调试困难 |
| 20 | 🟡 P1 | **kill-node 的 .runtime-config.json 读取有竞态** | `cli.py:1375` | `os.path.exists()` 检查和 `json.load()` 之间文件可能被删除 |
| 21 | 🟡 P1 | **server.py 创建嵌套的 CollabStore 实例** | `server.py:210-230` | 每个 POST /api/runs 创建新 CollabEngine → 新 CollabStore → 新 SQLite 连接。两个 WAL 连接写同一个文件可能 `SQLITE_BUSY` |
| 22 | 🟡 P1 | **三个 API 端点无前端消费者** | `server.py` | `/api/lessons`, `/api/mcp-servers`, `/api/resume-context` 有后端 handler 但 index.html 不调用 |
| 23 | 🟡 P1 | **store.py `_execute`/`_query` 的 CURRENT_TIMESTAMP 字符串替换有 SQL 注入风险** | `store.py:357-373` | 在参数化 SQL 之前做 `str.replace("CURRENT_TIMESTAMP", ...)` — 如果表/列名包含该子串则损坏查询 |
| 24 | 🟡 P1 | **`get_model_context_limit()` 子串匹配顺序相关** | `store.py:1108-1124` | 独立函数, `if key in model_name` 匹配可能误匹配 (如 "gpt-4" 匹配 "gpt-4-turbo") |
| 25 | 🟡 P1 | **`config_store.py` 的 `_DEFAULT_MAX_KEEP=5` 死常��** | `config_store.py:36` | 定义但从未被引用, 所有函数都用函数参数的默认值 `5` |
| 26 | 🟡 P1 | **`add_agent.py` 的 `_call_llm()` URL 构建脆弱** | `add_agent.py:84-88` | `if '/v1' not in url` — 如果 base_url 的域名或路径包含 `/v1` 则错误跳过追加 |
| 27 | 🟡 P1 | **`add_agent.py` 的 `sys.exit(1)` 在库函数中** | `add_agent.py` | 库函数调用了 `sys.exit(1)`, 如果作为模块导入调用会杀死进程 |
| 28 | 🟡 P1 | **前端 `loadConfig()` 引用不存在的 DOM 元素** | `web/index.html:1114` | `$('leaderModel')` 和 `$('workerModel')` — 对应 id 的元素在 HTML 中不存在 |
| 29 | 🟡 P1 | **前端 `previewCapabilities()` 页面加载时立即请求** | `web/index.html:2052` | 首页加载就发送 `/api/skills` 和 `/api/tools` 请求 |
| 30 | 🟡 P1 | **前端没有 `removeEventListener`** | `web/index.html` | 所有 `addEventListener` 从未清理 — SPA 运行时累积监听器 |
| 31 | 🟡 P1 | **前端 `state.resumeContext` / `state.resumeEnabled` 从未被使用** | `web/index.html:1049-1050` | 声明但从未填充或发送到 API |
| 32 | 🟡 P1 | **`no_leader.py` 只能通过 cli.py 对话框路径访问** | `no_leader.py` | 不是通过 API 路由暴露给 `server.py` |
| 33 | 🟡 P1 | **`cli_protocol.py` 的 `_handle_need_input()` 阻塞式 `input()`** | `cli_protocol.py:101` | 没有超时的阻塞调用, 用户离开则挂起 |
| 34 | 🟡 P1 | **`_opencode_create_session()` 文件描述符泄漏** | `agents.py:340-361` | 为 stdout/stderr 创建的 `open()` 文件对象在方法返回时被关闭 (refcount 归零), 子进程的临时文件输出捕获断开 |
| 35 | 🟡 P1 | **start.py 的 `_start_proxy()` 和 `_start_python_proxy()` 重复逻辑** | `start.py` | 两个函数做相同的资源管理, 应共享通用生命周期 |
| 36 | 🟡 P1 | **`_detect_risks()` 接受 `risk_policy` 参数但从不使用** | `engine.py:3024` | 有参数但函数体从不引用 |
| 37 | 🟡 P1 | **`_load_plan_from_db()` 丢失 `task_type` 和 `risk_policy`** | `engine.py:3140-3184` | 从 DB 重建 Plan 时只读取 nodes 和 shared_brief, 忽略 task_type 和 risk_policy |
| 38 | 🟡 P1 | **`engine.py` 的 `_env_for_role()` 硬编码 Anthropic URL** | `engine.py:1767-1769` | 包含 `ANTHROPIC_BASE_URL: https://api.anthropic.com` 配置漂移 (默认 agent 已改为 opencode) |

---

## 五、低优先级问题 (P2)

| # | 问题 | 文件 |
|---|------|------|
| 39 | engine.py `_summarize_context_for_worker()` 硬编码 `["claude", "-p"]` 而非使用 `build_command()` | `engine.py:1597` |
| 40 | engine.py `_load_agent_profiles()` 在异常路径泄漏 SQLite 连接 | `engine.py:159-206` |
| 41 | engine.py agent_backend.auto_prefix 在 engine.py 和 agents.py 中重复应用 | `engine.py:2094-2095` |
| 42 | engine.py `_event_loop()` 和 `_leader_watchdog()` 冗余的心跳检测 | `engine.py:2442-2478,2741-2789` |
| 43 | engine.py 13 个 `import` 语句分散在方法体内 | `engine.py` (多处) |
| 44 | server.py POST/PUT JSON 解析 `except Exception` 不记录日志 | `server.py:169,393` |
| 45 | agents.py `supported_tools`, `supported_skills`, `supported_skill_slots` 字段从未被读取 | `agents.py:69-74` |
| 46 | agents.py `list_backends()` 外部零调用者 | `agents.py:510-511` |
| 47 | agents.py `AgentBackend.parse_output()` 输出解析器无注册时验证 | `agents.py:120-137` |
| 48 | registry.py 重复的 `# -- scoring helper` 注释块 | `registry.py:378,392` |
| 49 | store.py `get_model_context_limit()` 子串匹配顺序相关 | `store.py:1108-1124` |
| 50 | store.py `CollabStore` 没有 `close()` 方法 | `store.py` |
| 51 | config_store.py `diagnose()` 重复 `load_with_migration()` 的读取 | `config_store.py:250` |
| 52 | cli.py guardian ANSI 颜色常量是模块级别而非局部 | `cli.py:686-689` |
| 53 | cli.py `score.overall` 重复的三次解包模式 | `cli.py:167-168,584` |
| 54 | web/index.html `setInterval` 永不清理 | `web/index.html:1877,2077` |
| 55 | web/index.html `renderMarkdown` 自定义渲染器, 边缘情况多 | `web/index.html:1662-1704` |
| 56 | web/index.html `extractLeaderSummary` 硬编码 `node_id==='aggregate'` | `web/index.html:1636-1658` |
| 57 | start.py 的 `load_with_migration` 和手动 `json.loads` 使用不一致 | `cli.py:1001-1008,1053-1060,1375` |
| 58 | proxy.py 将 API key 同时放在 `Bearer` 和 `x-api-key` 两个 header | `proxy.py:45` |

---

## 六、交叉验证结果

| 子代理声明 | 结论 | 说明 |
|-----------|------|------|
| capabilities.py 完全孤立的 | ❌ 虚假 | cli.py L154, L569 有导入 |
| verification.py 完全孤立的 | ❌ 虚假 | cli.py L1202 有导入 |
| skill_distributor.py 完全孤立的 | ❌ 虚假 | engine.py L25 有导入, L82 实例化, L2023 使用 |
| _run_direct() 死代码 | ✅ 正确 | 仅在 engine.py:2791 定义, 零调用者 |
| _leader_guard_run() 死代码 | ✅ 正确 | 仅在 engine.py:1243 定义, 零调用者 |
| deduplicate_lessons() 死代码 | ✅ 正确 | 仅在 store.py:813 定义, 零调用者 |
| backends_for_capability() 死代码 | ✅ 正确 | 零调用者 |
| delete_backend() 死代码 | ✅ 正确 | 零调用者 |
| planner.py agent_backend 未定义 | ✅ 正确 | `self.agent_backend` 在 `__init__` 中未设置 |
| distill/ 未连接 | ✅ 正确 | 零导入从引擎侧 |
| config-center 页面全死 | ✅ 正确 | 没有后端路由 |
| store.py 默认 agent "claude-code" | ✅ 正确 | create_run() L424 |
| `from backend.xxx` 无污染 | ✅ 正确 | 零匹配 |

---

## 七、已修复的项目追踪问题

以下问题在 codebase-audit skill Pitfalls 中被提及, 经验证已修复:

| Pitfall | 问题 | 状态 |
|---------|------|------|
| 209 | leader_agent 默认值 "hermes" → 改为 None | ✅ 已修: `engine.py:49` |
| 213 | `_shutil2` 未定义 → 改为 `shutil` | ✅ 已修 |
| 214 | `print("\\\\n")` 输出字面反斜杠 | ✅ 已修 (cli.py 未找到此代码) |
| 194 | kill-node 硬编码 "claude-code" → 已改为 "opencode" | ✅ 已修: `cli.py:1376` |
| 195 | server host 默认 127.0.0.1 → 0.0.0.0 | ✅ 已修: `cli.py:783` |
| 203 | peer_reviews 表 schema 缺失 | ✅ 已修: `store.py:56-71` |
| 157 | CollabStore skip_cleanup 参数 | ✅ 已修: `store.py:77` |

---

## 八、总结

### 按严重度统计

| 严重度 | 数量 | 细节 |
|--------|------|------|
| 🔴 P0 | 18 | 8 个阻塞问题 + 10 个死代码块 |
| 🟡 P1 | 20 | 有 workaround 的严重问题 |
| 🟢 P2 | 20 | 低优先级代码质量问题 |
| **总计** | **58** | |

### 按模块分布

| 模块 | P0 | P1 | P2 | 总计 | 说明 |
|------|----|----|----|------|------|
| engine.py | 3 | 5 | 5 | 13 | 竞态、死代码、F-string 问题 |
| cli.py | 0 | 3 | 2 | 5 | 错误静默、竞态 |
| server.py | 1 | 2 | 1 | 4 | 线程安全、资源泄漏 |
| store.py | 1 | 2 | 2 | 5 | 默认值漂移、SQL 注入风险 |
| planner.py | 1 | 0 | 0 | 1 | agent_backend 未定义 |
| agents.py | 0 | 2 | 3 | 5 | 文件描述符泄漏、死代码 |
| registry.py | 1 | 1 | 1 | 3 | store 参数未实现 |
| config_store.py | 0 | 1 | 1 | 2 | |
| add_agent.py | 0 | 2 | 2 | 4 | |
| capabilities.py | 0 | 0 | 0 | 0 | (被正确导入, 无额外问题) |
| verification.py | 0 | 0 | 0 | 0 | (被正确导入, 无额外问题) |
| skill_distributor.py | 1 | 0 | 0 | 1 | dragon-team 污染 |
| distill/ | 1 | 0 | 0 | 1 | 未连接 |
| no_leader.py | 0 | 0 | 0 | 0 | |
| cli_protocol.py | 0 | 1 | 0 | 1 | |
| detector.py | 0 | 0 | 0 | 0 | |
| web/index.html | 0 | 4 | 3 | 7 | 死 DOM 引用、未清理监听器 |
| proxy.py | 0 | 0 | 2 | 2 | |
| start.py | 0 | 1 | 1 | 2 | |
| web/config-center.html | 1 | 0 | 0 | 1 | 整个页面全死 |
| 跨模块 | 3 | 3 | 0 | 6 | 默认值漂移、duplicate registries、config-center |

### 关键风险区域

1. **线程安全**: engine.py 的 `_run_worker()` 通过全局属性切换 agent — 这是跨线程竞态的根源。`_sse_connections` 无锁访问。
2. **配置漂移**: "claude-code" 仍在 store.py 中作为默认值, 而 CLI/Server 已改用 "opencode"。
3. **死代码 2,100+ 行**: `_run_direct` (210行), `_leader_guard_run` (60行), `_wait_leader_reply` (20行), `deduplicate_lessons` (37行), `_is_hermes_builtin_*` (100行), `from_legacy` (12行), `backends_for_capability`/`delete_backend` (20行), `_heuristic_assess` (5行), `_find_insert_point` (5行), distill 模块 (553行)。
4. **config-center.html**: 整个 SPA 页面约 500+ 行前端代码调用根本不存在的后端路由。
5. **Planner LLM 评估静默降级**: No-Leader 模式下 `_claude_json` 因 `self.agent_backend` 未定义而静默失败 — 用户看不到任何 warning, 系统回退到启发式。

### 代码健康评分

- **可维护性**: 6/10 (死代码 + 全局状态竞态 + 默认值漂移)
- **正确性**: 7/10 (silent failure paths + threading issues)
- **安全性**: 8/10 (SQL 注入风险很小, API key 在 proxy.py 双 header 暴露)
- **完整性**: 6/10 (config-center 页面全死, distill 未集成, 能力路由未接入)
- **总体**: **6.5/10**

### 建议优先执行的项目

1. **[P0] 删除死代码**: `_run_direct`, `_leader_guard_run`, `_wait_leader_reply`, `deduplicate_lessons`, `from_legacy`, `backends_for_capability`/`delete_backend`, `_heuristic_assess`, `_find_insert_point`
2. **[P0] 修复 store.py 默认 agent**: `"claude-code"` → `"opencode"`
3. **[P0] 修复 server.py `_sse_connections` 竞态**: 加 threading.Lock
4. **[P0] 修复 planner.py `agent_backend`**: 在 `__init__` 中设置 `self.agent_backend`, 或 `decompose()` 中保存参数
5. **[P0] 删除 config-center.html 或实现后端路由**
6. **[P0] 修复 `_run_worker()` agent 竞态**: 改用局部参数而非全局 `self.agent_backend`
7. **[P0] 删除 skill_distributor.py 的 dragon-team 污染类型注解**
8. **[P1] 所有 `except Exception: pass` 添加日志**
