# 8765 协同引擎 — 全量改动验收报告

日期: 2026-07-25
范围: 7 个源文件, 23 项改动

---

## 一、改动清单

### agents.py（12 项）

| # | 改动 | 文件:行 | 旧值 | 新值 |
|---|------|---------|------|------|
| 1 | opencode capabilities 补全 | :331 | `["file-edit","git-ops"]` | `+test-run, +mcp-host, +search` |
| 2 | claude-code output-format 清空 | :282 | `["--output-format","json"]` | `[]` |
| 3 | opencode fd 泄漏修复 | :358-361 | `if not stdout_path: _stdout.close()` | `if stdout_path: _stdout.close()` |
| 4 | opencode session list 加日志 | :380-382 | `except Exception: pass` | `except Exception: _log.warning(exc_info=True)` |
| 5 | hermes `--provider` 不硬编码 | :400 | `["hermes","--provider","opencode-go"]` | `["hermes"]` |
| 6 | hermes delegation 去掉 | :423 | `["...orchestration","delegation"...]` | 去掉 `"delegation"` |
| 7 | hermes PIPE drain 循环 | :444-456 | `Popen → 直接写 stdin` | `Popen → 2s drain → 写 stdin` |
| 8 | `_opencode_session_send` 跟踪子进程 | :393 | `_sp.Popen(...)` fire-and-forget | `_proc = Popen(...); meta["_pending_sends"].append(_proc)` |
| 9 | `_opencode_session_close` 新增 | :396 | 不存在 | 遍历 `_pending_sends` → kill 残留 |
| 10 | `get_backend()` 自动 fallback | :534-540 | `return _BUILTINS[name]` | `if not is_available(): return opencode` |
| 11 | claude-code session 支持 | :296-328 | 无 session | `_claude_create_session` + `_claude_session_send` |
| 12 | 删除未安装 agent | :474-534 | windsurf/copilot/openclaw 注册 | 删除 3 个 block (-67 行) |

### engine.py（4 项）

| # | 改动 | 位置 | 旧 | 新 |
|---|------|------|----|----|
| 13 | ANTHROPIC_BASE_URL 默认值删除 | :1710 | `{"ANTHROPIC_BASE_URL":"https://api.anthropic.com"}` | `{}` |
| 14 | `_idle_watchdog` `_worker_sessions` 加锁 | :265-278 | for 循环在 lock 外，clear 在 lock 内 | 整个 for+clear 在 `with _worker_procs_lock:` 内 |
| 15 | codex PTY banner 过滤 | :2149-2163 | 无过滤 | 过滤 "OpenAI Codex"/"workdir:"/"model:" 等 7 种 banner 行 |
| 16 | codex 残留进程 cleanup | :2195-2203 | 无 | `killpg(9)` + `pkill -9 -f codex` |

### no_leader.py（1 项）

| # | 改动 | 位置 | 旧 | 新 |
|---|------|------|----|----|
| 17 | codex 两段式 dispatch | :88-89 + :132-210 | 通用 subprocess.run | codex 专用：gen→extract→write 三段式 |

### server.py（1 项）

| # | 改动 | 位置 | 旧 | 新 |
|---|------|------|----|----|
| 18 | distill 模块集成 | :437-444 | 无 | `try: from .distill.daily_distill import run` |

### store.py（3 项）

| # | 改动 | 位置 | 旧 | 新 |
|---|------|------|----|----|
| 19 | `import re` 新增 | :4 | 无 | `import re` |
| 20 | CURRENT_TIMESTAMP regex | :358,365,370 | `str.replace(...)` | `re.sub(r'\bCURRENT_TIMESTAMP\b', ...)` |
| 21 | model_context_limit 排序 | :1085 | `for key in limits.items()` | `for key in sorted(limits.items(), key=-len)` |

### config_store.py（1 项）

| # | 改动 | 位置 | 旧 | 新 |
|---|------|------|----|----|
| 22 | `_DEFAULT_MAX_KEEP` 删除 | :36-37 | 定义但未使用的常量 | 删除 |

### add_agent.py（1 项）

| # | 改动 | 位置 | 旧 | 新 |
|---|------|------|----|----|
| 23 | URL 构建用 urlparse | :83-95 | `if '/v1' not in url:`（域名也会匹配） | `if '/v1' not in path:`（只检查路径） |

---

## 二、验证结果

| TC | 测试 | 方法 | 结果 | 证据 |
|----|------|------|------|------|
| **TC1** | 模块导入 (14 模块) | `from ... import ...` 全部 14 个 | ✅ PASS | 全部无错误 |
| **TC2** | Agent 注册 | 属性 assert（capabilities/flags/command/fallback） | ✅ PASS | 7 backend 注册，4 可用，fallback 有效 |
| **TC3** | Planner WBS | `assess()` + `decompose()` | ⚠️ 已知限制 | 简单 chat→direct 正确。短中文实现任务评分偏低（overall=3→direct）是已知问题 |
| **TC4** | 引擎简单 run | `hermes-collab run "echo test"` | ✅ PASS | `run_1fdf0397aa8a` completed (opencode) |
| **TC5** | WBS 节点 | DB 查询 wbs_nodes 表 | ✅ PASS | 1 节点 wbs-1(completed) |
| **TC6** | Dashboard API | `GET /api/overview` | ✅ PASS | port 8765, 237 runs, 0 running |
| **TC7** | 代码级验证 | 23 项 assert 自动化脚本 | ✅ PASS | 23/23 checks across 7 files |
| **TC8** | 资源清理 | 进程/Zombie/DB 检查 | ✅ PASS | 0 残留引擎进程, 1 僵尸(dragon-team chrome,无害) |

---

## 三、diff 统计

```
src/hermes_collab_engine/agents.py         +95 -79   (12 changes)
src/hermes_collab_engine/engine.py          +29 -4    (4 changes)
src/hermes_collab_engine/no_leader.py       +80 -1    (1 change)
src/hermes_collab_engine/server.py          +8 -0     (1 change)
src/hermes_collab_engine/store.py           +7 -3     (3 changes)
src/hermes_collab_engine/config_store.py    +0 -2     (1 change)
src/hermes_collab_engine/add_agent.py       +9 -5     (1 change)
-------------------------------------------------------
总计:                                       +228 -94   (23 changes)
```

---

## 四、剩余未处理事项

| 项 | 优先级 | 类型 | 说明 |
|----|--------|------|------|
| Planner 短中文评分偏低 | P2 | 已知 bug | `_local_assess` 返回 overall=3/direct，绕过 `_prefer_direct_for_simple`。需改 `assess()` 方法让写动词任务即使评分 low 也走 LLM 评估 |
| dragon-team 僵尸进程 | — | 已标记 | `server.py` Popen 后未 `proc.wait()`。已记入 memory，下次修 DT 时做 |
