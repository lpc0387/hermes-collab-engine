#!/usr/bin/env python3
"""
delegate_task → 8765 DB 记录器

让 Hermes 的 delegate_task 执行结果写入 collab.sqlite3，
使 dashboard 统一可见所有开发活动。

两种用法:
  1. execute_code 中调用 record_delegate_run()
  2. 命令行: python3 record_delegate.py --task "修复bug" ... --nodes '[...]'
"""

import sqlite3
import json
import uuid
import argparse
from datetime import datetime
from pathlib import Path

# 8765 collab DB 路径（硬编码为已知位置）
COLLAB_DB = Path("/root/hermes-collab-engine/data/collab.sqlite3")


def record_delegate_run(
    task: str,
    summary: str = "",
    status: str = "completed",
    agent: str = "delegate_task",
    nodes: list[dict] | None = None,
) -> str:
    """把 delegate_task 的执行记录写入 8765 DB。

    参数:
        task:    任务描述（必填）
        summary: 执行摘要
        status:  completed / failed / partial
        agent:   worker 名称，默认 "delegate_task"
        nodes:   WBS 子节点列表，每个含 {id, title, status, deliverable?}

    返回:  run_id
    """
    if not COLLAB_DB.exists():
        raise FileNotFoundError(
            f"8765 collab DB 不存在: {COLLAB_DB}\n"
            f"请确认 8765 engine 已初始化或调整 COLLAB_DB 路径"
        )

    run_id = f"dt_{uuid.uuid4().hex[:12]}"
    now = datetime.now().isoformat()

    db = sqlite3.connect(str(COLLAB_DB))
    try:
        # 写入 runs 表
        db.execute(
            """INSERT INTO runs(id, title, request, status, agent, created_at, updated_at, complexity_json, meta_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (run_id, task[:200], task, status, agent, now, now,
             json.dumps({"source": "delegate_task"}),
             json.dumps({"summary": summary, "nodes": len(nodes) if nodes else 0})),
        )

        # 写入 wbs_nodes 表（可选子节点）
        if nodes:
            for n in nodes:
                db.execute(
                    """INSERT INTO wbs_nodes(
                           id, run_id, title, description, capability, complexity,
                           dependencies_json, parallelizable, deliverable, status,
                           created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (n.get("id"), run_id, n.get("title", ""),
                     n.get("description", n.get("title", "")),
                     n.get("capability", "implementation"), 1,
                     "[]", 1, n.get("deliverable", ""),
                     n.get("status", "completed"), now, now),
                )

        # 写入 logs 表（摘要日志）
        db.execute(
            """INSERT INTO logs(run_id, level, message, data_json, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (run_id, "info", "delegate_task completed",
             json.dumps({"agent": agent, "nodes": len(nodes) if nodes else 0}), now),
        )

        db.commit()
    finally:
        db.close()

    return run_id


def cli():
    """命令行入口"""
    parser = argparse.ArgumentParser(description="记录 delegate_task 到 8765 DB")
    parser.add_argument("--task", required=True, help="任务描述")
    parser.add_argument("--summary", default="", help="执行摘要")
    parser.add_argument("--status", default="completed",
                        choices=["completed", "failed", "partial"])
    parser.add_argument("--agent", default="delegate_task", help="worker 名称")
    parser.add_argument("--nodes", default=None,
                        help='子节点 JSON: [{"id":"n1","title":"x","status":"completed"}]')
    args = parser.parse_args()

    nodes = json.loads(args.nodes) if args.nodes else None
    run_id = record_delegate_run(
        task=args.task,
        summary=args.summary,
        status=args.status,
        agent=args.agent,
        nodes=nodes,
    )
    print(run_id)


if __name__ == "__main__":
    cli()
