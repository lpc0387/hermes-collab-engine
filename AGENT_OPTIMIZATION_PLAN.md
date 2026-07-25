# 8765 协同引擎 Agent 功能优化详细方案

> 基于 2026-07-25 全量代码审查，针对每个 agent 的现状、问题、具体改动方案

---

## 目录

1. [opencode（默认 Worker Agent）](#1-opencode默认-worker-agent)
2. [claude-code（备用 Worker Agent）](#2-claude-code备用-worker-agent)
3. [codex（实验性 Worker Agent）](#3-codex实验性-worker-agent)
4. [hermes（Leader / 备用 Worker Agent）](#4-hermesleader--备用-worker-agent)
5. [windsurf / copilot / openclaw（未安装 Agent）](#5-windsurf--copilot--openclaw未安装-agent)
6. [基础架构层优化](#6-基础架构层优化)
7. [执行优先级矩阵](#7-执行优先级矩阵)

---

## 1. opencode（默认 Worker Agent）

### 1.1 现状

```
agents.py:316-392
command       = ["opencode", "run"]
capabilities  = ["file-edit", "git-ops"]          # ← 过少
auto_prefix   = "opencode-go/"
session支持    = ✅ 已注入 (_supports_sessions=True)
```

### 1.2 问题与具体改动

#### 问题 A: capabilities 定义过少（P0）

opencode 实际支持 test-running、mcp-host、search 等能力，但定义只有 `["file-edit", "git-ops"]`。能力路由 `select_best_agent()` 因此不会为 `test-run`/`mcp-host` 等能力分配 opencode。

**改动：`agents.py:311`**
```python
# 当前：
capabilities=["file-edit", "git-ops"],

# 改为：
capabilities=["file-edit", "git-ops", "test-run", "mcp-host", "search"],
```

#### 问题 B: capabilities.py 中 opencode 的 CapabilityProfile 缺失（P0）

`capabilities.py:15-44` 有 opencode 的 `CapabilityProfile`，但能力映射不完整：
```python
# capabilities.py:15-44
"opencode": CapabilityProfile(
    capabilities={
        "file-edit": 9,
        "implementation": 9,
        "general": 8,
        "git-ops": 8,
        "execution": 8,
        "search": 7,
        "test-run": 7,     # ← 已有但不全
        "reasoning": 6,    # ← 加上
        "debug": 6,
    },
    max_concurrency=3,
),
```

这里的 profile 是路由层用的，opencode 的 AgentBackend.capabilities 是 skill_distributor 层用的。**两者需要同步。**

**同步规则：`capabilities.py:106-151` 的 `CAPABILITY_KEYWORDS` 中如果有 `test`→`test-run` 的映射，而 opencode 的 `capabilities` 列表没有 `"test-run"`，则 `select_best_agent()` 永远不会为 `test-run` 能力选 opencode。**

#### 问题 C: `_opencode_create_session()` 文件描述符管理缺陷（P1）

`agents.py:340-360`：
```python
def _opencode_create_session(prompt: str, **kw) -> SessionHandle:
    ...
    stdout_path = kw.get("stdout_path")
    stderr_path = kw.get("stderr_path")
    _stdout = open(stdout_path, "w", encoding="utf-8") if stdout_path else _sp.PIPE
    _stderr = open(stderr_path, "w", encoding="utf-8") if stderr_path else _sp.PIPE
    proc = _sp.Popen(
        cmd,
        stdin=_sp.DEVNULL, stdout=_stdout, stderr=_stderr, text=True,
    )
    if not stdout_path:
        _stdout.close() if hasattr(_stdout, 'close') else None  # ← 条件反了
    return SessionHandle(...)
```

**bug：** stdout_path 存在时（需要 guardian 监控），`open()` 创建的文件对象永远不会被关闭。Python 在 `Popen` 创建子进程后可以安全关闭父进程的文件句柄（子进程通过 fork 继承了 fd）。

**修复：**
```python
    proc = _sp.Popen(...)
    # 父进程关闭文件句柄，子进程持有写权限
    if stdout_path:
        _stdout.close()
    if stderr_path:
        _stderr.close()
    # 注意：if not stdout_path 时 _stdout 是 PIPE，不需要 close()
```

#### 问题 D: `_opencode_ensure_session_id()` 静默吞异常（P1）

`agents.py:378`：
```python
    except Exception:
        pass
```

**修复：** 加 logging：
```python
    except Exception:
        import logging as _log
        _log.getLogger(__name__).warning("opencode session list failed", exc_info=True)
```

#### 问题 E: `_opencode_session_send()` 未跟踪子进程（P1）

`agents.py:390`：
```python
    _sp.Popen(cmd, stdin=_sp.DEVNULL, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
```

`session_send` 启动子进程后立刻返回，不跟踪 pid 也不等完成。连续多次 `session_send` 可能堆叠大量子进程。

**修复：** 
```python
    proc = _sp.Popen(cmd, ...)
    # 存到 handle.meta 中以便 cleanup
    pending = handle.meta.setdefault("_pending_sends", [])
    pending.append(proc)
```

并在 `close_session` 中清理：
```python
def _opencode_session_close(handle: SessionHandle) -> None:
    for proc in handle.meta.get("_pending_sends", []):
        if proc.poll() is None:
            proc.kill()
    if handle.proc and handle.proc.poll() is None:
        handle.proc.kill()
```

#### 问题 F: `session_send` 用 Popen 但 fire-and-forget — 没有验证会话 ID 正确性（P2）

目前 `_opencode_session_send` 每次重新调 `opencode run -s SID message`，但不验证该 session 是否真的接收了消息。

**建议：** 改为 `subprocess.run(timeout=30)` 并日志返回码（对 session_send 的 latency 影响 ~1s，可以接受）。

---

## 2. claude-code（备用 Worker Agent）

### 2.1 现状

```
agents.py:277-315
command             = ["claude"]
prompt_flag         = "-p"
output_format_flags = ["--output-format", "json"]  # ← 隐患
output_parser       = "claude_json"
capabilities        = ["file-edit", "git-ops", "test-run", "mcp-host", "search"]
session支持          = ❌ 无
```

### 2.2 问题与具体改动

#### 问题 A: `--output-format json` 上游不兼容（P0）

**根因链：** claude-code 2.1.202 发送 Messages API（streaming）到上游 proxy。当上游是 OpenAI API（opencode.ai）时，proxy 需要将 Anthropic Messages API 翻译为 Chat Completions API。但 `--output-format json` 告诉 claude-code 输出 JSON 格式，这会**改变 claude 内部的行为逻辑**——工具调用可能不执行，因为 claude-code 在 JSON 模式下认为只需输出 JSON 而非调用工具。

**改动：`agents.py:282`**
```python
# 当前：
output_format_flags=["--output-format", "json"],

# 改为：空列表（去掉 JSON 模式）
output_format_flags=[],

# 同时将 output_parser 改为 "raw_text"（从 stdout 提取代码块）
output_parser="raw_text",
```

**配套改动：** 需要在 `engine.py` 的 `parse_output` 处理中为 claude-code 添加从 stdout 提取 ```代码块``` 的逻辑（类似 opencode 的方式），或者保留 `claude_json` 解析器作为 raw_text 的预处理。

#### 问题 B: 缺少 session 支持（P1）

claude-code 2.1.202 支持 `claude --resume SESSION -p "message"` 续会模式。

**新增代码：`agents.py`（在 claude-code 注册后追加）**
```python
# 为 claude-code 注入 session 能力
_c = _BUILTINS["claude-code"]
_c._supports_sessions = True

def _claude_create_session(prompt: str, **kw) -> SessionHandle:
    import subprocess as _sp
    import uuid
    sid = kw.get("session_id", "worker-" + uuid.uuid4().hex[:8])
    cmd = ["claude", "--resume", sid]
    quiet = kw.get("quiet", True)
    if quiet:
        cmd.append("--quiet")
    proc = _sp.Popen(
        cmd,
        stdin=_sp.PIPE, stdout=_sp.PIPE, stderr=_sp.PIPE,
        text=True, bufsize=1,
    )
    if proc.stdin:
        proc.stdin.write(prompt + "\n")
        proc.stdin.flush()
    return SessionHandle(session_id=sid, proc=proc, stdin=proc.stdin, stdout=proc.stdout,
                         meta={"type": "claude"})

_c.create_session = _claude_create_session

def _claude_session_send(handle: SessionHandle, message: str) -> None:
    if handle.stdin and not handle.stdin.closed:
        handle.stdin.write(message + "\n")
        handle.stdin.flush()

_c.session_send = _claude_session_send
```

#### 问题 C: `output_parser="claude_json"` 验证缺失（P2）

`agents.py:120-137` 的 `parse_output()` 方法根据 `output_parser` 字符串选择解析器。`claude_json` 解析器在 `engine.py` 的 `_parse_claude_json()` 中有四层解析策略，但从未有测试覆盖。

**建议：** 在 `verification.py` 中加一个 test case：
```python
def test_claude_json_parser():
    from engine import _parse_claude_json
    # 测试原始 JSON
    assert _parse_claude_json('{"result": "hello"}') == "hello"
    # 测试 markdown 包裹的 JSON
    assert _parse_claude_json('```json\n{"result": "hello"}\n```') == "hello"
    # 测试非 JSON（降级到原始文本）
    assert _parse_claude_json("plain text") == "plain text"
```

---

## 3. codex（实验性 Worker Agent）

### 3.1 现状

```
agents.py:277-315 (已修正)
command       = ["codex", "exec"]
needs_pty     = True
prompt_flag   = ""  (位置参数)
capabilities  = ["file-edit", "git-ops"]
session支持    = ❌ 无
```

### 3.2 问题与具体改动

#### 问题 A: codex exec 不写文件（P0）

这是 codex 0.142.5 的已知限制。`codex exec` 将生成的代码输出到 stdout/stderr，但**不写入文件系统**（sandbox 限制）。

**解决方案：两段式 dispatch**

在 `no_leader.py` 或 `engine.py` 中新增 codex 专用 dispatch 逻辑：

```python
def _dispatch_codex(agent: str, task: str, cwd: Path, model: str | None = None) -> WorkerResult:
    """Two-stage codex dispatch: generate → extract → write → execute."""
    from .agents import get_backend
    backend = get_backend(agent)
    
    # Stage 1: Generate code (codex exec outputs to stdout)
    gen_task = f"{task}\n\nIMPORTANT: Output ONLY the code, no explanations."
    cmd = backend.build_command(gen_task, cwd=str(cwd), model=model)
    
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           text=True)
    stdout, stderr = proc.communicate(timeout=120)
    
    # Filter codex startup banner (first 15 lines)
    stderr_lines = stderr.split('\n')
    filtered_stderr = '\n'.join(
        line for line in stderr_lines
        if not any(skip in line for skip in [
            "OpenAI Codex", "Reading additional", "--------",
            "workdir:", "model:", "provider:"
        ])
    )
    
    # Extract code blocks from stdout
    import re
    code_blocks = re.findall(r'```(?:\w+)?\n(.*?)```', stdout, re.DOTALL)
    if not code_blocks:
        # Fallback: use full stdout as code
        code_blocks = [stdout]
    
    # Stage 2: Write files extracted from code
    # ... (parse file paths from task or use default output file)
    
    return WorkerResult(ok=proc.returncode == 0, stdout=stdout, stderr=filtered_stderr,
                       result=code_blocks[0] if code_blocks else "",
                       duration=round(time.time() - start, 3))
```

**推荐位置：** `no_leader.py` 的 `NoLeaderDispatcher.dispatch()` 方法中增加 `if agent == "codex"` 分支。

#### 问题 B: PTY 启动 banner 污染输出（P1）

当前 `engine.py` 的 PIPE+select 循环已经能读取 PTY 输出，但 codex 的启动 banner 混入 worker stdout。

**改动：`engine.py` 的 `_run_worker_impl()` 输出处理**
```python
# 在 print 输出行之前，对 codex agent 过滤 banner
if backend and backend.name == "codex":
    _skip_banner = [
        "OpenAI Codex", "Reading additional", "--------",
        "workdir:", "model:", "provider:", 
    ]
    if any(s in line for s in _skip_banner):
        continue  # 不打印也不写入节点输出文件
```

#### 问题 C: `output_parser="codex_json"` 未验证（P2）

codex 0.142.5 的输出格式是 mixed（启动信息 + JSON + 代码）。当前解析器配置为 `codex_json` 但实际未在 `parse_output()` 中注册对应的 handler。

**改动：`agents.py:58` 或 `agents.py:parse_output()`**
```python
# 确认 codex 的 output_parser 配置
# 当前 output_parser="codex_json" 
# 要么实现 codex_json 解析器（提取 stdout 中的 JSON 块）
# 要么改为 "raw_text" 并用上述的代码块提取逻辑
```

**建议：** 改为 `output_parser="raw_text"` 配合 Stage 2 的文件写入逻辑，因为 codex exec 的输出主要是代码文本而非 JSON。

#### 问题 D: 残留进程清理（P2）

codex 失败/超时后留下 CPU 100% 的僵尸进程。

**改动：`engine.py` 的 cleanup 逻辑**
```python
# 在 _run_worker_impl 的 finally 块中
if backend and backend.name == "codex" and proc:
    # 确保所有 codex 子进程被杀
    import signal
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    # 额外清理：pkill 同名进程
    subprocess.run(["pkill", "-9", "-f", "codex.*exec"], 
                   capture_output=True, timeout=5)
```

---

## 4. hermes（Leader / 备用 Worker Agent）

### 4.1 现状

```
agents.py:394-444
command       = ["hermes", "--provider", "opencode-go"]  # ← provider 硬编码
prompt_flag   = "-z"
capabilities  = ["planning", "analysis", "orchestration", "delegation", "file-edit", "git-ops", "search"]
session支持    = ✅ (PIPE 模式)
```

### 4.2 问题与具体改动

#### 问题 A: `--provider opencode-go` 硬编码（P1）

`agents.py:397` 中 `command = ["hermes", "--provider", "opencode-go"]` 限制了 hermes 只能使用 opencode-go provider。当用户配置了其他 provider（如 deepseek、openai）时，这个硬编码会导致错误。

**改动：`agents.py:397`**
```python
# 方案 A：完全去掉 provider（让 hermes 自己检测默认配置）
command=["hermes"],

# 方案 B：从运行时配置读取（如果存在 config_store）
# 在 _run_worker_impl 中 build_command 之前动态注入
```

**推荐方案 A**，因为 hermes 的默认配置（`~/.hermes/config.yaml`）中已经有 provider 设置，不需要在 engine 层强制指定。

#### 问题 B: PIPE 模式兼容性验证（P1）

`_hermes_create_session()` 使用 `subprocess.PIPE`（非 PTY）。hermes 在某些版本/模式下检测到非 TTY stdin 后可能回显 `Warning: Input is not a terminal` 或拒绝处理输入。

**建议：** 在 `_hermes_create_session` 中添加 2 秒的 drain 循环，丢弃启动后 2 秒内的非响应输出（类似 dragon-team 的 `_start_leader_session` 的 header drain 模式）：

```python
def _hermes_create_session(prompt: str, **kw) -> SessionHandle:
    ...
    # Drain initial banner (2s window)
    import time, select as _sel
    _deadline = time.time() + 2
    while time.time() < _deadline:
        r, _, _ = _sel.select([proc.stdout], [], [], 0.2)
        if r:
            _line = proc.stdout.readline()
            # Skip banner lines
            if any(s in _line for s in ["> ", "Tip:", "Welcome", "Warning"]):
                continue
            break  # First real output = response
        else:
            break
    # Send prompt
    if proc.stdin and proc.stdin.writable():
        proc.stdin.write(prompt + "\n")
        proc.stdin.flush()
    ...
```

#### 问题 C: `capabilities` 包含 `delegation` 但 worker 模式下不适用（P2）

hermes 的 `capabilities` 包含 `"delegation"`，但在 engine 调度到 hermes 作为 worker 时，只是 `subprocess.Popen(["hermes", "-z", prompt])` 的 task 模式，没有 delegation 能力。

**改动：`agents.py:409`**
```python
# 当前：
capabilities=["planning", "analysis", "orchestration", "delegation", "file-edit", "git-ops", "search"],

# 改为（去掉 delegation—— worker 角色下不适用）：
capabilities=["planning", "analysis", "orchestration", "file-edit", "git-ops", "search"],
```

---

## 5. windsurf / copilot / openclaw（未安装 Agent）

### 5.1 现状

```
agents.py:447-507
三个 agent 分别注册，但：
$ which windsurf  →  not found
$ which copilot    →  not found
$ which openclaw   →  not found
```

### 5.2 问题与具体改动

#### 问题 A: 已注册但不可用（P1）

`detect_available_backends()` 已经通过 `is_available()` 过滤不可用 agent（`agents.py:522-524`），所以这些 agent 在前端 UI 中不显示。问题在于**运行时**如果用户/配置指定了这些 agent，`get_backend()` 返回的 backend 在 `Popen` 时会抛出 `FileNotFoundError`。

**目前已在 `engine.py:73-76` 处理：**
```python
except FileNotFoundError:
    self.agent_backend = get_backend("opencode")
    self.leader_agent = None
```

但当用户通过配置覆盖 worker_agent 时，这个 fallback 可能不触发。

**改动：`agents.py:527-529`（register_backend 或 get_backend 中添加检查）**
```python
def get_backend(name: str) -> AgentBackend:
    if name not in _BUILTINS:
        raise KeyError(...)
    backend = _BUILTINS[name]
    if not backend.is_available():
        # 可选：自动 fallback 到 opencode
        import logging
        logging.getLogger(__name__).warning(
            f"Agent {name!r} is not available on PATH, falling back to 'opencode'"
        )
        return _BUILTINS.get("opencode", backend)
    return backend
```

**或者更直接的方案——删除三个未注册 agent（P2）：**

```python
# 删除 agents.py:447-507 的以下注册
# - windsurf  (447-465)
# - copilot   (468-486)
# - openclaw  (489-507)
# 节省 ~60 行代码
```

#### 问题 B: `add_agent.py` 中 agent 自动注册包含这三个不可用 agent（P2）

`add_agent.py` 通过 LLM 动态生成 agent 配置，可能生成 windsurf 等配置。生成的配置会写入 `agents.db`。

**建议：** `add_agent.py` 新增 agent 后立即运行 `detect_available_backends()` 验证，如果不可用给出警告。

---

## 6. 基础架构层优化

### 6.1 store.py CURRENT_TIMESTAMP 替换改进（P1）

**位置：`store.py:357-373`**
```python
def _execute(self, sql: str, params: tuple = ()):
    sql = sql.replace("CURRENT_TIMESTAMP", "datetime('now','localtime')")  # 可能误替换
```

**风险场景：** 如果表名、列名或字符串内容包含子串 "CURRENT_TIMESTAMP"（概率极低但存在）。

**修复：**
```python
import re

def _execute(self, sql: str, params: tuple = ()):
    # 仅替换 SQL 关键字级别的 CURRENT_TIMESTAMP（前后为单词边界）
    sql = re.sub(r'\bCURRENT_TIMESTAMP\b', "datetime('now','localtime')", sql)
    ...
```

同样修改 `_query()` 和 `_one()`。

### 6.2 config_store.py 死常量清理（P1）

**位置：`config_store.py:36`**
```python
_DEFAULT_MAX_KEEP = 5  # → 从未被引用，删除
```

所有函数（`backup_config`、`save_with_backup`）都用函数参数的默认值 `max_keep=5`，模块级常量完全冗余。

**改动：** 删除 `config_store.py:36-37`

### 6.3 add_agent.py URL 构建缺陷（P1）

**位置：`add_agent.py:83-88`**
```python
url = base_url.rstrip('/')
if not url.endswith('/chat/completions'):
    if '/v1' not in url:          # ← 子串判断不可靠
        url += '/v1/chat/completions'
    else:
        url += '/chat/completions'
```

**风险：** base_url = `https://api.myv1service.com` → 域名包含 `/v1` → 错误跳过追加。

**修复：**
```python
# 用 URL 解析代替子串判断
from urllib.parse import urlparse, urlunparse

parsed = urlparse(url)
path = parsed.path.rstrip('/')
if not path.endswith('/chat/completions'):
    if '/v1' not in path:  # 只检查 path 部分，不含域名
        path += '/v1/chat/completions'
    else:
        path += '/chat/completions'
    parsed = parsed._replace(path=path)
    url = urlunparse(parsed)
```

### 6.4 engine.py ANTHROPIC_BASE_URL 默认值（P1）

**位置：`engine.py:1710-1711`**
```python
_ANTHROPIC_DEFAULTS: dict[str, str] = {
    "ANTHROPIC_BASE_URL": "https://api.anthropic.com",  # ← 硬编码
}
```

当默认 agent 是 opencode 时，这个值被 mirror 到 `OPENCODE_BASE_URL`，可能导致 opencode 向上游 https://api.anthropic.com 发请求。

**修复：** 删除这个硬编码默认值，或让它从环境变量读取：
```python
_ANTHROPIC_DEFAULTS: dict[str, str] = {}
# 仅当 ANTHROPIC_BASE_URL 已存在于 env 时才保持不动
# 不要主动注入默认值
```

### 6.5 _idle_watchdog 中 _worker_sessions 无锁访问（P1）

**位置：`engine.py:265-277`**
```python
for node_id, handle in list(self._worker_sessions.items()):  # ← 未加锁
    ...
with self._worker_procs_lock:
    self._worker_sessions.clear()  # ← 此处加锁了
```

遍历时未加锁，如果同时有其他线程写入 `_worker_sessions`（如 `_run_worker` 中的赋值 `engine.py:2102`），会导致 `RuntimeError: dictionary changed size during iteration` 或竞态。

**修复：**
```python
with self._worker_procs_lock:
    for node_id, handle in list(self._worker_sessions.items()):
        ...
    self._worker_sessions.clear()
    self._worker_procs.clear()
```

### 6.6 distill/ 模块集成（P1）

**位置:** `distill/*.py`（553 行）零集成。

**方案: cronjob 定期调用**
```python
# server.py main() 启动时注册 cron
def _register_distill_cron():
    """注册每日 22:00 的会话蒸馏 cronjob。"""
    cronjob(action="create", name="daily-distill-8765",
            schedule="0 22 * * *",
            prompt=(
                "Run the 8765 engine daily distill. "
                "Extract lessons from today's runs in collab.sqlite3, "
                "write memory entries and skill drafts."
            ),
            skills=["collab-engine-dev"],
            )
```

如果不想依赖 cronjob，简化方案：在 `server.py main()` 尾部添加调用：
```python
try:
    from hermes_collab_engine.distill.daily_distill import run_daily_distill
    run_daily_distill(store=outer.store)
except ImportError:
    pass  # distill not available
```

---

## 7. 执行优先级矩阵

| 优先级 | 模块 | 改动项 | 文件 | 行数 | 风险 | 预估工时 |
|--------|------|--------|------|------|------|---------|
| **P0** | opencode | 补全 capabilities | `agents.py:311` | +5 行 | 低 | 2 分钟 |
| **P0** | claude-code | 去掉 `--output-format json` | `agents.py:282` | +2 行 | 中（改变了输出格式，需验证） | 10 分钟 |
| **P0** | codex | 两段式 dispatch | `no_leader.py` | +100 行 | 中 | 2 小时 |
| **P1** | opencode | 修复 _opencode_create_session fd 泄漏 | `agents.py:358-359` | ±3 行 | 低 | 5 分钟 |
| **P1** | opencode | 修复 session list 静默吞异常 | `agents.py:378` | +3 行 | 低 | 2 分钟 |
| **P1** | opencode | _session_send 跟踪子进程 | `agents.py:390` | +15 行 | 低 | 10 分钟 |
| **P1** | claude-code | 添加 session 支持 | `agents.py` (追加) | +60 行 | 中（需验证 claude --resume 工作） | 30 分钟 |
| **P1** | hermes | `--provider` 不硬编码 | `agents.py:397` | ±1 行 | 中（依赖默认配置正确） | 5 分钟 |
| **P1** | hermes | PIPE drain 循环 | `agents.py:417-434` | +15 行 | 低 | 10 分钟 |
| **P1** | 未安装 agent | get_backend 自动 fallback | `agents.py:515-519` | +10 行 | 低 | 5 分钟 |
| **P1** | store | CURRENT_TIMESTAMP 正则替换 | `store.py:358,365,370` | ±3 行 | 低 | 5 分钟 |
| **P1** | config_store | 删除 _DEFAULT_MAX_KEEP | `config_store.py:36` | -1 行 | 低 | 1 分钟 |
| **P1** | add_agent | URL 构建用 urlparse | `add_agent.py:83-88` | +10 行 | 低 | 5 分钟 |
| **P1** | engine | ANTHROPIC_BASE_URL 默认值删除 | `engine.py:1710-1711` | ±2 行 | 低（opencode 不需要 default） | 2 分钟 |
| **P1** | engine | _idle_watchdog 加锁 | `engine.py:265-277` | ±5 行 | 低 | 5 分钟 |
| **P1** | distill | cronjob 集成 | `server.py` | +30 行 | 低 | 15 分钟 |
| **P2** | codex | PTY banner 过滤 | `engine.py` | +10 行 | 低 | 5 分钟 |
| **P2** | codex | output_parser 改为 raw_text | `agents.py:58` | ±1 行 | 低 | 1 分钟 |
| **P2** | codex | 残留进程 cleanup | `engine.py` finally 块 | +8 行 | 低 | 5 分钟 |
| **P2** | hermes | 去掉 delegation capability | `agents.py:409` | -1 行 | 低 | 1 分钟 |
| **P2** | 未安装 agent | 删除注册代码 | `agents.py:447-507` | -60 行 | 低（可用 detect_available_backends 保护） | 5 分钟 |
| **P2** | store | 验证 get_model_context_limit 顺序 | `store.py:1108-1124` | +5 行（排序） | 低 | 5 分钟 |

### Phase 1（可立即执行，~20 分钟总工时）

```
1. opencode capabilites 补全              (agents.py:311, +5行, 2min)
2. claude-code output_format_flags 清空    (agents.py:282, +2行, 10min)
3. config_store _DEFAULT_MAX_KEEP 删除     (config_store.py:36, -1行, 1min)
4. ANTHROPIC_BASE_URL 默认值删除            (engine.py:1710-1711, ±2行, 2min)
5. opencode fd 泄漏修复                     (agents.py:358-359, ±3行, 5min)
```

### Phase 2（本周完成，~1 小时）

```
6. CURRENT_TIMESTAMP 正则替换              (store.py 三处, 5min)
7. add_agent URL 修复                       (add_agent.py:83-88, 5min)
8. _worker_sessions 加锁                    (engine.py:265-277, 5min)
9. hermes --provider 不硬编码               (agents.py:397, 5min)
10. hermes PIPE drain                      (agents.py:417-434, 10min)
11. get_backend 自动 fallback               (agents.py:515-519, 5min)
12. opencode session list 加日志            (agents.py:378, 2min)
13. distill cronjob 集成                    (server.py, 15min)
```

### Phase 3（中长期，~3 小时）

```
14. codex 两段式 dispatch                   (no_leader.py, 2h)
15. claude-code session 支持                (agents.py, 30min)
16. 未安装 agent 删除                       (agents.py:447-507, 5min)
17. codex PTY banner 过滤 + cleanup         (engine.py, 10min)
```
