# 8765 Hermes Collab Engine — 反向优化方案

> 基于 dragon-team (8766) 的实战修复经验，对 8765 进行架构对齐和 Bug 修复。
> 8765 定位：**代码开发 CLI 工具**（非平台）。8766 定位：**ToC 多 Agent 协同平台**。

---

## 已修复（2026-07-09）

| # | 问题 | 文件 | 改动 |
|---|------|------|------|
| P0-1 | dragon-team store `_one(...)[0]` 残留（dict 转换后 [0] 返回 key 名） | `dragon-team/src/engine/store.py` | `create_session()`、`session_chains()` 共 3 处 |
| P0-2 | 8765 cli kill-node 硬编码回退 `claude-code`（实际可能是 opencode） | `8765/cli.py` | 改为从 config_store 读取 `worker_agent` |

---

## 待修复（按优先级）

### 🔴 P1 — 默认值不一致

| 项 | 位置 | 当前 | 应改为 |
|----|------|------|--------|
| P1-1 | `8765/server.py:17` | `agent: str = "claude-code"` | `agent: str = "opencode"` |
| P1-2 | `8765/store.py` `_query/_one` | 返回 raw `sqlite3.Row` | 返回 `dict(r)`（与 dragon-team 对齐） |

### 🟡 P2 — Config-driven 改造

| 项 | 位置 | 问题 | 修复思路 |
|----|------|------|---------|
| P2-1 | `cli.py` run/server | CLI 忽略 `config set worker-agent` | `agent = args.agent or cfg.get("worker_agent", "opencode")` |
| P2-2 | `cli.py` | 不传 `worker_agent`/`leader_agent` 给引擎 | 增加 `--leader-agent`，默认从 config 读取 |
| P2-3 | `engine.py` init | 不加载 DB 的 `agent_config` | `_agent_cfg = self.store.get_setting("agent_config") or {}` |
| P2-4 | `cli.py` provider | `--provider` 不回落 config_store | 优先从 config 加载 |
| P2-5 | `cli.py` agent | 无 `--auto-agent` fallback | 自动选 PATH 上第一个可用 agent |

### 🔵 P3 — Engine 修复同步

| 项 | 位置 | 内容 |
|----|------|------|
| P3-1 | `engine.py` | shard 级 recovery 4 步协议 + stuck_loop 过滤（~200 行） |
| P3-2 | `engine.py` | `_env_for_role()` 按 agent 类型差异化 env 注入 |
| P3-3 | `engine.py` | `_run_direct` PTY I/O + session context 注入增强 |

### 🟢 P4 — API 路由增强（可选）

| 特性 | 行数 | 价值 |
|------|------|------|
| CORS 支持 | ~30 | 外部前端调用 |
| `/api/health` | ~15 | 运维监控 |
| Guardian Feedback API | ~150 | 用户反馈暂停 worker |
| 执行日志 API | ~100 | 日志查询/过滤 |

---

## 差异总结

| 维度 | 8765（代码开发工具） | 8766（协同平台） |
|------|---------------------|-----------------|
| 交互方式 | CLI `hermes-collab run` | Web SPA（admin-app + user-app） |
| 用户认证 | 无 | auth_db + JWT + Guest 模式 |
| 前端 | 单页 dashboard | 12 页面管理端 + 用户聊天端 |
| 模型配置 | CLI `--model` + config_store | Admin UI → DB settings |
| Agent 切换 | CLI `--agent` + config_store | Admin UI Worker 管理页 |
| 代理层 | 无（直连 upstream） | unified-proxy.py（:18080 三大协议） |
| Worker 隔离 | jail.py（namespace） | jail.py + proxy + 资源限制 |
| 代码量 engine.py | 3,464 行 | 4,439 行 |
| 代码量 server.py | 422 行 | 2,635 行 |
