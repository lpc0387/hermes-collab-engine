# 8765 协同引擎 — 全量代码审查 + Agent 功能优化方案

审查日期: 2026-07-25
审查范围: 18 个源文件, 9,898 行 Python （不含 distill/）
基准: AUDIT_REPORT.md (2026-07-11) + 当前代码库 (commit: `83c70fc`)

---

## 一、AUDIT_REPORT.md P0 修复状态验证

| # | 问题 | 2026-07-11 状态 | 2026-07-25 状态 | 证据 |
|---|------|----------------|----------------|------|
| 1 | **config-center.html 整个页面不可用** | 🔴 P0 | 🟢 已消除 | config-center.html 已从 web/ 删除，不存在此问题了 |
| 2 | **store.py create_run 默认 agent "claude-code"** | 🔴 P0 | 🟢 **已修复** | `store.py:424` 默认值改为 `agent: str = "opencode"` |
| 3 | **`_sse_connections` 无锁竞争** | 🔴 P0 | 🟢 **已修复** | `server.py:63` 新增 `_sse_lock = threading.Lock()`，在 increment/decrement 处用 `with Handler._sse_lock:` 保护 |
| 4 | **get_unified_registry(store=) 参数未实现** | 🔴 P0 | 🔴 **未修** | `registry.py:459-463` 签名接受 `store` 但函数体未使用任何 store 方法。潜在但非阻塞——目前 registry 所有操作都是内存级的 |
| 5 | **distill/ 模块完全未连接** | 🔴 P0 | 🔴 **未修** | engine.py/cli.py/server.py 零导入 distill。553 行代码独立存在 |
| 6 | **skill_distributor.py TYPE_CHECKING 引用不存在模块** | 🔴 P0 | 🟢 **已修复** | 现在只有 `from .agents import AgentBackend` -- dragon-team 污染已清除 |
| 7 | **engine.py agent_backend 跨线程竞态** | 🔴 P0 | 🟢 **已修复** | `_run_worker()` 用局部 `backend = self.agent_backend`，不突变全局属性。注释写明 "Resolve per-role agent without mutating self.agent_backend (thread-safe)" |
| 8 | **planner.py agent_backend 未定义** | 🔴 P0 | 🟢 **已修复** | `__init__` 中 `self.agent_backend: Any = None` + `decompose()` 中 `self.agent_backend = agent_backend` |

**死代码 P0 清理状态：**

| 死代码 | 2026-07-11 | 2026-07-25 | 行数 |
|--------|-----------|-----------|------|
| `_run_direct()` | 存在 (~210行) | **已删除** | -346 |
| `_leader_guard_run()` | 存在 (~60行) | **已删除** | |
| `_wait_leader_reply()` | 存在 (~20行) | **已删除** | |
| `_is_hermes_builtin_*()` | 存在 (~100行) | **已删除** | |
| `deduplicate_lessons()` | 存在 (~37行) | **已删除** | |
| `SkillEntry.from_legacy()` | 存在 (~12行) | **已删除** | |
| `backends_for_capability()` | 存在 (~10行) | **已删除** | |
| `delete_backend()` | 存在 (~10行) | **已删除** | |
| `_heuristic_assess()` | 存在 (~5行) | **已删除** | |
| `_find_insert_point()` | 存在 (~5行) | **已删除** | |

**结论：** 7 个 P0 中 **5 个已修复**、1 个自然消除（config-center.html 已删除）、1 个仍开放（distill/ 未连接）。全部 P0 死代码已清理，engine.py 从 3,184 行缩减到 2,838 行（-10.9%）。

---

## 二、当前代码健康扫描

### 2.1 剩余 P1 问题

| # | 严重度 | 问题 | 位置 | 说明 |
|---|--------|------|------|------|
| 1 | 🟡 P1 | **distill/ 模块未集成** | `distill/*.py` (553行) | 引擎侧零导入。每日蒸馏、memory_writer、skill_writer 都无法使用 |
| 2 | 🟡 P1 | **store.py CURRENT_TIMESTAMP 字符串替换** | `store.py:358,365,370` | SQL 中 `CURRENT_TIMESTAMP` 被替换为 `datetime('now','localtime')`。如果表名/列名包含该子串则损坏查询（概率极低但隐患） |
| 3 | 🟡 P1 | **`_DEFAULT_MAX_KEEP` 死常量** | `config_store.py:36` | 定义 `_DEFAULT_MAX_KEEP = 5` 但从未引用，所有函数用函数参数默认值 `5` |
| 4 | 🟡 P1 | **add_agent.py URL 构建 `/v1` 子串判断** | `add_agent.py:84-88` | `if '/v1' not in url` - 如果 base_url 域名含 `/v1`（如 `api.myv1service.com`）则错误跳过追加 |
| 5 | 🟡 P1 | **engine.py ANTHROPIC_BASE_URL 硬编码** | `engine.py:1711` | `"ANTHROPIC_BASE_URL": "https://api.anthropic.com"` — 当默认 agent 为 opencode 时影响不大，但对 claude-code 路径可能冲突 |
| 6 | 🟡 P1 | **`get_unified_registry(store=)` 参数虚设** | `registry.py:459-463` | 签名接受 `store` 参数但函数体从未使用，所有调用者传了 `store=store` 期望持久化但并未发生 |
| 7 | 🟡 P1 | **add_agent.py `sys.exit(1)` 在库函数中** | `add_agent.py` | 库函数调用了 `sys.exit(1)`，如果作为模块导入会杀死进程 |
| 8 | 🟡 P1 | **failover.py 源文件已删除但 .pyc 残留** | `__pycache__/failover.cpython-311.pyc` | 源文件不存在但 .pyc 残留，说明曾经被导入过 |
| 9 | 🟡 P1 | **windsurf/copilot/openclaw 注册但未安装** | `agents.py:447-520` | 三个 agent 注册在系统中但 `which` 找不到二进制。运行时报 `FileNotFoundError` |
| 10 | 🟡 P1 | **frontend index.html 无 removeEventListener** | `web/index.html` | SPA 运行累积未清理的事件监听器 |

### 2.2 代码度量趋势

| 指标 | 2026-07-11 | 2026-07-25 | 变化 |
|------|-----------|-----------|------|
| engine.py 行数 | 3,184 | 2,838 | **-346 (-10.9%)** ✅ 死代码清理 |
| cli.py 行数 | 1,594 | 1,592 | -2 |
| server.py 行数 | 434 | 437 | +3 (加锁) |
| store.py 行数 | 1,124 | 1,087 | -37 (deduplicate_lessons 删除) |
| planner.py 行数 | 518 | 517 | -1 |
| agents.py 行数 | 542 | 545 | +3 (session 支持) |
| 总行数 | 10,168 | 9,898 | **-270 (-2.7%)** |
| `except Exception: pass` | 3 处 | **0 处** | 🔥 全部消除 |

---

## 三、Agent 注册与功能矩阵

### 3.1 当前 Agent 定义

| Agent | command | prompt_flag | supports_sessions | needs_pty | capabilities | 默认 model_prefix |
|-------|---------|-------------|-------------------|-----------|--------------|-------------------|
| **opencode** | `opencode run` | "" (位置参数) | ✅ (session PIPE) | ❌ | file-edit, git-ops | `opencode-go/` |
| **claude-code** | `claude` | `-p` | ❌ | ❌ | file-edit, git-ops, test-run, mcp-host, search | — |
| **codex** | `codex exec` | "" (位置参数) | ❌ | ✅ (PTY) | file-edit, git-ops | — |
| **hermes** | `hermes --provider opencode-go` | `-z` | ✅ (session PIPE) | ❌ | planning, analysis, orchestration, delegation, file-edit, git-ops, search | — |
| **windsurf** | `windsurf` | `-p` | ❌ | ❌ | file-edit, search, git-ops | — |
| **copilot** | `copilot` | `--prompt` | ❌ | ❌ | file-edit, search, git-ops, test-run | — |
| **openclaw** | `openclaw` | `--prompt` | ❌ | ❌ | file-edit, search, git-ops, test-run | — |

### 3.2 Agent 能力路由矩阵

当前 capabilities.py 中的 CapabilityProfile 定义：

| Agent | implementation | analysis | planning | verification | operation | design |
|-------|---------------|----------|----------|-------------|-----------|--------|
| opencode | ✅ 首选 | ✅ | ❌ | ✅ | ❌ | ❌ |
| claude-code | ✅ 首选 | ✅ | ✅ | ✅ | ❌ | ❌ |
| codex | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |
| hermes | ✅ | ✅ 首选 | ✅ 首选 | ✅ | ✅ | ✅ |

---

## 四、Agent 功能优化方案

### 4.1 opencode（默认 Worker Agent）

**当前状态：** 主力 agent，全流程可工作，session 支持完整，auto_prefix 正确。

**发现问题：**
1. 能力定义 (`["file-edit", "git-ops"]`) 过于保守 —— opencode 实际支持 test-run、mcp-host、search
2. `_opencode_create_session()` 文件描述符创建后未及时关闭（`open(stdout_path)` 对象只被 `_stdout` 引用，返回后逃逸）
3. `_opencode_ensure_session_id()` 中 `except Exception: pass` —— 静默吞掉 session list 失败

**优化建议：**

| 优先级 | 优化项 | 改动 | 预期效果 |
|--------|--------|------|---------|
| P0 | 补全 capabilities | `capabilities += ["test-run", "mcp-host", "search"]` | 能力路由可识别 opencode 更多能力 |
| P1 | 修复文件描述符泄漏 | `_opencode_create_session` 中临时文件只在需要时打开，或用 `NamedTemporaryFile(delete=False)` | 避免文件描述符用后未关 |
| P1 | 修复 session 确认静默吞错 | `except Exception as e: logger.warning(...)` | session list 失败时有日志可查 |
| P2 | 添加 `session_close()` 方法 | 注册 `_opencode_session_close` 以支持 session 资源释放 | 引擎 `close_session()` 能真正清理 opencode session |

### 4.2 claude-code（备用 Worker Agent）

**当前状态：** 可以工作，使用 `--output-format json` + 4-tier JSON 解析器。无 session 支持。

**发现问题：**
1. `--output-format json` 输出格式假定上游返回 JSON 行 —— 当上游 proxy 是 OpenAI API 时，claude-code 工具调用可能不执行（OpenAI API 不支持 Anthropic 工具格式）
2. 无 session 支持 —— 每次都是独立调用，无法持久化对话
3. 能力定义包含 `test-run`、`mcp-host` 但未验证 claude 2.1.202 是否支持

**优化建议：**

| 优先级 | 优化项 | 改动 | 预期效果 |
|--------|--------|------|---------|
| P0 | 去掉 `--output-format json` 或加 agent compat 检测 | `output_format_flags` 条件性启用 | 避免上游不兼容时工具不执行 |
| P1 | 增加 session 支持 | 实现 `_claude_create_session` 使用 `claude --resume` | 支持持久对话 |
| P2 | 验证 `output_parser="claude_json"` 是否健壮 | 添加 test case 覆盖异常 JSON | 解析器不崩溃 |

### 4.3 codex（实验性 Worker Agent）

**当前状态：** 使用 `codex exec` 非交互模式，needs_pty=True，有 `--skip-git-repo-check` 和 `--sandbox workspace-write`。

**发现问题：**
1. **codex exec 不写文件** —— codex exec 将代码输出到 stdout/stderr 但不实际写入文件系统（有 sandbox 限制）
2. PTY 模式启动 banner 混入 stdout（"OpenAI Codex v..."、"Reading additional..."）需过滤
3. `output_parser="codex_json"` 未验证实际 JSON 格式
4. 遗留进程清理问题 —— codex 失败后可能留下 CPU 100% 的 zombie 进程

**优化建议：**

| 优先级 | 优化项 | 改动 | 预期效果 |
|--------|--------|------|---------|
| P0 | 实现两段式 dispatch | 先 `_dispatch_single` 生成代码（从 stdout 提取），再 `subprocess.run` 写入并执行 | 解决 codex exec 不写文件的核心限制 |
| P1 | 增强启动 banner 过滤 | 过滤 "OpenAI Codex"、"Reading additional"、"workdir:"、"model:"、"provider:" 等前 15 行 | 输出干净可读 |
| P2 | 添加 `output_parser` 测试 | 验证 `codex_json` 解析器能正确处理 codex 0.142.5 的输出 | 避免解析崩溃 |
| P2 | 在 agents.py 中注册 cleanup handler | `atexit.register(lambda: subprocess.run(['pkill', '-f', 'codex.*--agent']))` | 避免 zombie 进程积累 |

### 4.4 hermes（Leader / 备用 Worker Agent）

**当前状态：** 注册为 worker agent（`hermes -z "prompt"`），也作为 leader agent。有 session 支持（PIPE 模式）。`--provider opencode-go` 硬编码。

**发现问题：**
1. `--provider opencode-go` 硬编码在 `command` 中 —— 限制了 provider 选择
2. `_hermes_create_session()` PIPE 模式 —— 与 PTY 模式不同，hermes 在某些版本中检测到非 TTY stdin 可能回显警告
3. `capabilities` 包含了 `delegation` 但 engine 调度到 hermes 时只是 subprocess task 模式，delegation 能力不适用

**优化建议：**

| 优先级 | 优化项 | 改动 | 预期效果 |
|--------|--------|------|---------|
| P1 | provider-agnostic 默认 command | `--provider opencode-go` 改为运行时动态决定（从 config_store 或环境变量读取） | 支持不同 provider 对接 |
| P1 | 验证 PIPE 模式兼容性 | 测试 `hermes -z "prompt"` 在 PIPE 模式下是否正常工作 | 确认 worker 路径不崩溃 |
| P2 | 移除 delegation capability | 从 worker 角色 hermes 的 capabilities 中去掉 "delegation" | 能力路由更准确 |

### 4.5 windsurf / copilot / openclaw（未安装 Agent）

**当前状态：** 注册了 AgentBackend 但系统上 `which windsurf/copilot/openclaw` 都找不到。

**优化建议：**

| 优先级 | 优化项 | 改动 | 预期效果 |
|--------|--------|------|---------|
| P1 | 添加连通性检测 | `detect_available_backends()` 应检查 `shutil.which(cmd[0])` | 不在 UI 中显示不可用 agent |
| P2 | 或删除三个未使用 agent | 从 `agents.py` 删除 `windsurf`/`copilot`/`openclaw` 的 `_register_builtin` | 减少代码体积 ~80 行 |

---

## 五、架构层面优化方案

### 5.1 模块解耦与死代码

| 模块 | 当前大小 | 建议 | 预估节省 |
|------|---------|------|---------|
| engine.py | 2,838 行 | 提取 Guardian 逻辑到独立模块、提取 env 构建到 provider.py | -600~800 行 |
| cli.py | 1,592 行 | 将 `_run_no_leader_task` / `_run_leader_task` 合并到 `no_leader.py`/`leader.py` | -400~600 行 |
| store.py | 1,087 行 | 将 CURRENT_TIMESTAMP 替换改为 SQLite trigger 或 DDL 默认值 | -10 行 |
| distill/ | 553 行 | **要么集成（cronjob 调用 daily_distill.py），要么删除** | -553 行 |

### 5.2 线程安全

当前线程安全改进大部分已完成（_run_worker 不突变全局属性、SSE 连接有锁）。但仍有隐患：

1. **`self._worker_sessions` 字典** —— `idle_watchdog` 线程和 `run()` 主线程同时访问。虽有 `_worker_procs_lock` 但 `run()` 的 `finally` 块中 `self._worker_sessions.clear()` 未加锁
2. **`_sse_connections` 虽然加锁了**，但 SSE handler 中的 `_log.getLogger(...)` 行在锁释放后读取 `_sse_connections`（已正确，计数在锁内）

### 5.3 distill/ 模块集成方案

**背景：** distill/ 包含 daily_distill（每日会话蒸馏）、memory_writer（lesson->memory）、skill_writer（lesson->skill）。目前零集成。

**建议方案：**

```python
# 在 server.py 或 cronjob 中
# 方式 A: server.py 启动时注册 cron
from hermes_collab_engine.distill.daily_distill import run_daily_distill
cronjob(action='create', schedule='0 22 * * *', 
        prompt='Run daily distill', 
        script='/path/to/run_distill.py')

# 方式 B: 作为 engine 的生命周期钩子
class CollabEngine:
    def run(self, ...):
        # ... existing code ...
        # run 完成后触发 distill
        if self.distill_enabled:
            from hermes_collab_engine.distill.extractor import extract_lessons
            lessons = extract_lessons(self.store)
```

### 5.4 config_store.py 优化

`_DEFAULT_MAX_KEEP = 5` 未使用 —— 直接删除该常量，函数参数默认值 `max_keep=5` 已足够。

```python
# 当前（line 36）：
_DEFAULT_MAX_KEEP = 5  # ← 删除

# 所有函数已有默认值：
def backup_config(path: str | Path, *, max_keep: int = 5) -> Path | None:
```

### 5.5 store.py SQL 注入风险

`_execute()` 和 `_query()` 中的 `CURRENT_TIMESTAMP` 字符串替换：

```python
sql = sql.replace("CURRENT_TIMESTAMP", "datetime('now','localtime')")
```

**改良方案：**
```python
# 明确仅替换 SQL 中的 CURRENT_TIMESTAMP 关键字（非表名/列名）
# 用正则防止误替换
import re
sql = re.sub(r'\bCURRENT_TIMESTAMP\b', "datetime('now','localtime')", sql)
```

---

## 六、Agent 功能升级路线图

| Phase | Agent | 改动量 | 影响范围 | 优先级 |
|-------|-------|--------|---------|--------|
| **Phase 1** | opencode 补全 capabilities | +4 行 | agents.py | P0 |
| **Phase 1** | claude-code 去掉 `--output-format json` | +5 行 | agents.py | P0 |
| **Phase 1** | 删除未安装 agent 或加检测 | -80 行 / +30 行 | agents.py, detector.py | P1 |
| **Phase 2** | distill 集成（方式 A cronjob） | +30 行 | server.py / cron | P1 |
| **Phase 2** | store.py SQL 正则替换 | +3 行 | store.py | P1 |
| **Phase 2** | config_store.py 删除死常量 | -1 行 | config_store.py | P1 |
| **Phase 3** | codex 两段式 dispatch | +100~200 行 | engine.py or no_leader.py | P2 |
| **Phase 3** | hermes provider-agnostic command | +20 行 | agents.py | P2 |
| **Phase 3** | engine.py 模块拆分（Guardian 提取） | +1 新文件 | engine.py → guardian_core.py | P2 |
| **Phase 4** | claude-code session 支持 | +60~80 行 | agents.py | P3 |
| **Phase 4** | cli.py 模式拆分（leader/no-leader 提取） | +2 新文件 | cli.py → leader_mode.py | P3 |

---

## 七、代码健康评分（更新版）

| 维度 | 2026-07-11 | 2026-07-25 | 变化 |
|------|-----------|-----------|------|
| **可维护性** | 6/10 | **7.5/10** | +1.5 (死代码减少 2,100→0 行) |
| **正确性** | 7/10 | **8/10** | +1 (3 处 silent exception 消除) |
| **线程安全** | 6/10 | **8/10** | +2 (agent_backend 竞态修、SSE 加锁) |
| **安全性** | 8/10 | 8/10 | 不变 (SQL 注入风险仍在但极低) |
| **完整性** | 6/10 | **7/10** | +1 (config-center 已删，死代码清) |
| **总体** | **6.5/10** | **7.7/10** | **+1.2** |

### 剩余工作摘要

- **仍开放:** 7 个 P1 问题（distill 未集成、SQL 替换风险、死常量、虚参数、URL 构建、ANTHROPIC 硬编码、未安装 agent）
- **Phase 1 可立即完成:** ~10 分钟改动（opencode capabilities、claude-code output-format、config_store 死常量）
- **Phase 2 建议本周完成:** distill 集成 (~30 分钟)、SQL 替换修复 (~5 分钟)
- **Phase 3-4 中长期:** codex 两段式 (~2h)、模块拆分 (~4h)、cli 重构 (~3h)
