# 8765 Hermes Collab Engine — 测试方案

> 版本: 1.0 · 审核后执行  
> 范围: 全功能回归 + 双模式验证 + codex 修复验证

---

## 一、测试范围

| 层级 | 覆盖内容 |
|------|---------|
| 单元测试 | 36 个现有 test_*.py 文件 |
| 集成测试 | 全 4 agent × 2 模式 = 8 场景 |
| 端点测试 | Dashboard API + DB 写入 + peer review 持久化 |
| 异常测试 | codex 模型元数据缺失、PTY 超时、工具调用失败 |

---

## 二、测试用例矩阵

### 2.1 Agent 连通性检测（detector.py）

| # | 用例 | 预期 | 验证方法 |
|---|------|------|---------|
| TC1 | `detect_all_agents()` 返回全部 4 agent | opencode/claude-code/codex/hermes 均检测 |
| TC2 | codex 二进制缺失时 graceful 降级 | health.installed=False + 明确 error msg |
| TC3 | hermes 无 `--provider` 时自动 fallback | 命令含 `--provider opencode-go` |
| TC4 | 超时检测（agent hang） | 30s 超时后标记 unreachable 而非崩溃 |

### 2.2 WBS 分解（planner.py）

| # | 用例 | 预期 | 验证方法 |
|---|------|------|---------|
| TC5 | 简单任务 "1+1=?" → overall ≤3 | direct 路由，不走 WBS |
| TC6 | 中文写任务 "生成一个 Python 计算器" → overall ≥4 | single/wbs 路由，触发 decompose() |
| TC7 | 多步骤任务含 `1)...2)...3)...` → 强制 WBS | 启发式覆盖 LLM 低估，overall≥5 |
| TC8 | 复杂任务 "实现一个配置中心系统" → WBS ≥5 节点 | 节点含 analysis/implementation/verification |
| TC9 | decompose() 返回节点带 capability 标注 | 每个节点有 analysis/file-edit/test-run 等 |

### 2.3 No-Leader 模式

| # | 用例 | 预期 | 验证方法 |
|---|------|------|---------|
| TC10 | 简单任务 direct dispatch → runs 表写入 | DB 查到 run_id + status=completed |
| TC11 | 多步 WBS 任务 → wbs_nodes 表 ≥3 节点 | sqlite3 查询确认节点数和状态 |
| TC12 | 能力路由：实现节点优先选 claude-code | select_best_agent("file-edit") → claude-code |
| TC13 | 分析节点优先选 hermes | select_best_agent("analysis") → hermes |
| TC14 | 依赖解析：前置节点 failed → 后续 skip | wbs_nodes 有 status=skipped |
| TC15 | codex dispatch 输出过滤 | 启动 banner 被滤除，只有纯净输出 |
| TC16 | peer review A 评 B → 写入 peer_reviews 表 | 该 run_id 在 peer_reviews 中有记录 |
| TC17 | peer_reviews 表数据 → API 可查 | GET /api/peer-reviews?run_id=X 返回数据 |

### 2.4 Leader 模式

| # | 用例 | 预期 | 验证方法 |
|---|------|------|---------|
| TC18 | 单 agent 完成多节点 WBS | 全部节点 completed |
| TC19 | Guardian 30s 静默检测 → ATTENTION 提示 | stdout 出现 `[GUARDIAN]` 标记 |
| TC20 | 回复窗口 → `input()` 交互路径 | TTY 模式 input() 出现；非 TTY 自动继续 |
| TC21 | 中断 worker 后节点标记 failed | DB status=failed + 下游 skip |
| TC22 | Leader 总结写入 DB | meta_json + aggregate log 有内容 |

### 2.5 codex 特殊场景

| # | 用例 | 预期 | 验证方法 |
|---|------|------|---------|
| TC23 | 模型元数据加载 | `~/.codex/model_catalog.json` 存在且 codex 不报 warning |
| TC24 | codex exec + PTY 模式 | Popen 通过 pty.openpty()，无 EIO 崩溃 |
| TC25 | PTY 输出过滤 15 行 banner | stdout 无 "OpenAI Codex" "Reading additional" 行 |
| TC26 | 两段式 dispatch（先 generate 再 execute） | 文件实际写入磁盘 + python3 运行成功 |
| TC27 | 工具调用翻译：codex `tools` → Chat API `functions` | proxy 返回含 function_call → 翻译为 tool_call |
| TC28 | 非流式 codex（SSE bug 绕过） | `stream=False` 返回 JSON，不走 SSE handler |
| TC29 | codex 超时后清理遗留进程 | `pgrep codex` 返回空 |

### 2.6 Dashboard API

| # | 用例 | 预期 | 验证方法 |
|---|------|------|---------|
| TC30 | GET /api/runs 返回列表 | JSON 数组，最新 run 在最前 |
| TC31 | GET /api/runs/{id}?full=1 含 peer_reviews | peer_reviews 字段非空 |
| TC32 | GET /api/peer-reviews?run_id=X 返回数据 | 记录含 reviewer/target/verdict/score |
| TC33 | 前端页面加载 | HTTP 200，HTML 含 Run History 表格 |

### 2.7 存储与持久化

| # | 用例 | 预期 | 验证方法 |
|---|------|------|---------|
| TC34 | runs 表创建 run | 写入后 SELECT 可见 |
| TC35 | wbs_nodes 创建/更新 status | completed/failed/skipped 正确更新 |
| TC36 | peer_reviews 写入 + 查询 | insert + get_run_peer_reviews 返回一致 |
| TC37 | server 重启后数据不丢失 | 重启后 GET /api/runs 数据仍在 |
| TC38 | dashboard 指向正确 DB | `ps aux|grep server` --db 参数一致 |

---

## 三、测试工具

```bash
# DB 查询验证
sqlite3 data/collab.sqlite3 "SELECT id, agent, status FROM runs ORDER BY rowid DESC LIMIT 5"
sqlite3 data/collab.sqlite3 "SELECT COUNT(*), status FROM wbs_nodes WHERE run_id='<id>' GROUP BY status"
sqlite3 data/collab.sqlite3 "SELECT * FROM peer_reviews WHERE run_id='<id>'"

# API 验证
curl -s http://localhost:8765/api/runs | python3 -m json.tool | head -20
curl -s "http://localhost:8765/api/peer-reviews?run_id=<id>"

# 仪表盘
浏览器打开 http://localhost:8765/

# agent 独立测试
KEY=$(cat /tmp/upstream_key.txt) timeout 30 codex exec \
  --model deepseek-v4-flash --skip-git-repo-check \
  "Output the number 42 and nothing else." 2>&1 | head -20
```

---

## 四、执行顺序

```
Phase 1: 验证基础环境
  TC1 (检测) → TC23 (模型元数据) → 确认全部 agent 可用

Phase 2: No-Leader 模式
  TC5 → TC6 → TC10 → TC11 → TC12 → TC14 → TC16 → TC17

Phase 3: Leader 模式
  TC18 → TC19 → TC20 → TC21 → TC22

Phase 4: codex 专项
  TC24 → TC25 → TC26 → TC27 → TC28 → TC29

Phase 5: Dashboard + 持久化
  TC30 → TC31 → TC32 → TC33 → TC34 → TC36 → TC37 → TC38
```

---

## 五、通过标准

- [ ] **P0**: TC1, TC5, TC10, TC23 全部绿色 → 引擎可用
- [ ] **P1**: TC6-9, TC11-15, TC18-22 → 双模式完整
- [ ] **P2**: TC16-17, TC24-29, TC30-38 → 专项覆盖
- [ ] 无未归因的 test_*.py 失败
- [ ] 每个声称通过场景附 run_id + DB 查询 + dashboard 截图

---

## 六、codex 已知问题总结（附测试建议）

| 问题 | 根因 | 当前状态 | 测试影响 |
|------|------|---------|---------|
| 模型元数据 warning | codex 0.142.5 要求 model_catalog.json | ✅ 已消除（文件存在） | TC23 验证无 warning |
| PTY 模式下 banner 混入 stdout | codex 启动打印 15 行 banner | ✅ 已过滤 | TC25 验证过滤 |
| exec 不写文件 | codex exec 子命令设计如此 | ⚠️ 需两段式 | TC26 验证 |
| SSE state machine bug | codex 0.142.5 GitHub #2875 | ⚠️ 非流式 JSON 绕过 | TC28 验证 |
| 工具调用翻译 | upstream Go 端点不支持 function_call | ⚠️ 待验证上游兼容性 | TC27 验证 |
| 遗留进程不清理 | codex 超时后子进程存活 | ⚠️ 需测试后 pgrep 验证 | TC29 验证 |
