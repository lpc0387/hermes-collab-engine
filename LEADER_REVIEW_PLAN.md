# Leader 审查报告 — 完整实施方案

## 一、需求概述

engine.run() 所有节点执行完毕后，由 LLM 作为 leader agent 对节点产出进行逐项审查，对照原始任务的要求项逐一核实，生成结构化审查报告，持久化到 DB 供 📓 按钮渲染。

## 二、数据流

```
用户请求 → runs.request (已有)
             ↓
Planner.decompose() → 提取 task_requirements
             ↓
engine.run() → 写入 runs.meta_json.task_requirements
             ↓
    ... WBS 节点执行 ...
             ↓
_leader_review() → 从 DB 读 task_requirements + 节点结果
             ↓
         构建 prompt → agent_backend 调 LLM
             ↓
     LLM 返回 markdown 审查报告
             ↓
     写入 runs.meta_json.leader_review + logs aggregate
```

## 三、存储设计

### 3.1 Plan 新增字段（models.py）

```python
# Plan 数据类追加
task_requirements: list[dict] = field(default_factory=list)
# 每项格式:
# {"id": "req-1", "description": "创建 hello_world.py", 
#  "node_id": "wbs-1", "deliverable": "可运行的 hello_world.py"}
```

### 3.2 runs.meta_json 结构

```json
{
  "task_requirements": [
    {"id": "req-1", "description": "创建 hello_world.py", 
     "node_id": "wbs-1", "deliverable": "可运行的 hello_world.py"},
    {"id": "req-2", "description": "输出 'Hello from opencode'",
     "node_id": "wbs-1", "deliverable": "标准输出验证"}
  ],
  "leader_review": "# Leader 审查报告...（完整 markdown）"
}
```

### 3.3 logs 表 aggregate 条目

```json
{
  "node_id": "aggregate",
  "message": "worker finished",
  "data_json": {
    "result": "# Leader 审查报告...（完整 markdown）"
  }
}
```

## 四、Planner 改动（planner.py）

### 4.1 task_requirements 生成逻辑

在 `decompose()` 返回 Plan 之前，遍历 nodes 列表构建 requirements：

```python
plan.task_requirements = [
    {
        "id": f"req-{i+1}",
        "description": n.deliverable or n.description,
        "node_id": n.id,
        "deliverable": n.deliverable,
    }
    for i, n in enumerate(nodes)
]
```

每个 requirement 包含：
- `id`: 唯一标识（req-1, req-2, ...）
- `description`: 该要求的具体描述
- `node_id`: 负责执行的节点
- `deliverable`: 预期交付物

## 五、Engine 改动（engine.py）

### 5.1 run() 中持久化 task_requirements

在 `plan = self.planner.decompose(request, score=score)` 之后：

```python
# 持久化 task_requirements 到 runs.meta_json
if plan.task_requirements:
    self.store.set_run_meta(run_id, {"task_requirements": plan.task_requirements})
```

### 5.2 新增 _leader_review() 方法

在 `run()` 末尾、节点全部完成后调用。

**输入：**
- `run_id`
- `request`（原始任务）
- `plan`（含 task_requirements）
- `results[]`（WorkerResult 列表）
- `backend`（self.agent_backend）

**构建 prompt 给 LLM：**

```python
prompt = f"""你是 Hermes 协作引擎的 Leader Agent，负责审查 WBS 节点执行结果。

## 原始任务
{request}

## 任务要求逐项对照
以下是该任务分解出的具体要求和对应执行节点：

{task_requirements 表格}

## 节点执行结果
{每个节点的: title / agent / 耗时 / 状态 / 关键产出摘要 / 验证情况}

## 审查要求
请按以下步骤输出审查报告：

### 第一步：任务对照
逐项检查每个 requirement 是否被满足：
- ✅ 完全满足：说明证据
- ⚠️ 部分满足：说明差异
- ❌ 未满足：说明原因

### 第二步：节点质量评估
对每个节点评估：
- 执行效果是否符合 deliverable
- 代码/产出物质量（简洁性、正确性、可读性）
- 验证是否充分

### 第三步：整体评估
- 最终交付物是否达成原始任务目标
- 是否有遗漏或偏差
- 改进建议（如有）

## 输出格式
以全中文 markdown 格式输出，不要包含代码块包裹。
输出结构为：（见下方格式要求）
"""
```

**LLM 调用：** 使用 `self.agent_backend.build_command(prompt=prompt, model=self.leader_model)` → `subprocess.run(cmd, ...)` → 解析输出

**写入 DB：**
```python
self.store.set_run_meta(run_id, {"leader_review": review_text})
self.store.log(run_id, "info", "worker finished", {
    "node_id": "aggregate",
    "title": "Leader 审查报告",
    "result": review_text,
}, node_id="aggregate")
```

### 5.3 错误兜底

LLM 调用失败时（超时/解析错误/进程被杀）：
- 写一条 warning log 说明审查失败
- 不影响 run 的正常 completed 状态
- meta_json 不写 leader_review 字段

```python
try:
    review_text = self._leader_review(run_id, request, plan, results)
except Exception as e:
    self.store.log(run_id, "warning", "leader review failed", {"error": str(e)[:200]})
    review_text = None
```

## 六、调用位置

在 `engine.py:run()` 中的准确位置：

```
line ~825:  ok = not failed_final...
line ~826:  self.store.update_run(...)
            ↓
            # 新增: _leader_review() ← 在这里插入
            ↓
line ~829:  self.store.log("run finished", ...)
```

## 七、改动汇总

| 文件 | 改动类型 | 行数 | 说明 |
|------|---------|------|------|
| `models.py` | 新增字段 | +2 | Plan 加 task_requirements |
| `planner.py` | 新增逻辑 | +8 | decompose() 末尾构建 requirements |
| `engine.py` | 新增方法+调用 | +80~100 | _leader_review() + run() 中两处插入 |

## 八、验证方法

1. 执行 `hermes-collab run "写一个hw.py输出Hello" --agent opencode`
2. 检查 `runs.meta_json` 含 `task_requirements` + `leader_review`
3. 检查 `logs` 表有 `node_id='aggregate', message='worker finished'` 条目
4. 📓 按钮渲染正常
