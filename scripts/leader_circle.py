#!/usr/bin/env python3
"""
8765 Leader 调度循环 — 在 background 运行，通过 ctl_dir 跟 Hermes Agent 对话。

用法:
  python3 scripts/leader_circle.py --task "生成一个计算器" \\
      --agents opencode,claude-code --cwd /tmp --timeout 300

Hermes Agent 管理方式:
  process(action='poll', session_id=X)  看到实时输出
  read_file(CTL_DIR/status.json)        看完整状态
  write_file(CTL_DIR/ctl.json, ...)     下决策
"""
from __future__ import annotations

import os
import json
import re
import sys
import uuid
from pathlib import Path

_HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE / "src"))

from hermes_collab_engine.engine import CollabEngine
from hermes_collab_engine.agents import get_backend
from hermes_collab_engine.capabilities import infer_capability, select_best_agent


def run_leader_circle(
    task: str,
    agents: list[str],
    db_path: str,
    cwd: str,
    model: str | None = None,
    timeout: int = 300,
) -> int:
    run_id = "run_" + uuid.uuid4().hex[:12]
    ctl_dir = f"/tmp/leader_ctl/{run_id}"
    os.makedirs(f"{ctl_dir}/nodes", exist_ok=True)

    eng = CollabEngine(db_path, cwd, model,
        agent=agents[0], leader_agent=None, worker_agent=agents[0])
    eng.leader_ctl_dir = ctl_dir

    eng.store.create_run(run_id, task[:100], task, {}, agent=agents[0])

    # 打印连接信息 → 我 process(poll) 看到后可以 read_file/write_file
    print(f"RUN_ID={run_id}", flush=True)
    print(f"CTL_DIR={ctl_dir}", flush=True)
    print(f"  DB: {db_path}", flush=True)
    print(f"  Agents: {', '.join(agents)}", flush=True)

    # ── 评估 + 分解 ──
    score = eng.planner.assess(task)
    overall = score.overall if hasattr(score, 'overall') else (score.get('overall', 5) if isinstance(score, dict) else 5)
    numbered = len(re.findall(r'(?:^|\s)\d+[).]\s?', task))
    if numbered >= 3:
        overall = max(overall, 5)
        # 强制标记为 wbs 路由，否则 decompose 内部重新 assess 又拿 direct
        if hasattr(score, 'routing'):
            score.routing = 'wbs'
            score.overall = overall

    if overall > 3:
        plan = eng.planner.decompose(task, max_nodes=overall, score=score)
        nodes = plan.nodes if plan and hasattr(plan, 'nodes') else []
    else:
        nodes = []

    print(f"  Overall: {overall}, Nodes: {len(nodes) if nodes else 1}", flush=True)

    if not nodes or len(nodes) <= 1:
        # ── 简单任务 ──
        simple_ok = False
        for ag in agents:
            node = type('Node', (), {
                'id': 'direct', 'title': task, 'description': task,
                'capability': 'general', 'complexity': 1,
                'dependencies': [], 'parallelizable': True,
                'deliverable': task, 'brief': '',
                'skills_json': '', 'tools_json': '',
                'checkpoint': False, 'attempt': 1, 'fingerprint': '',
                'write_targets': [], 'estimated_duration': None, 'parent_id': None,
            })()
            eng.agent_backend = get_backend(ag)
            print(f"[NODE] direct -> {ag}", flush=True)
            wr = eng._run_worker(run_id, node, timeout, model_override=model)
            print(f"[NODE] direct/{ag}: ok={wr.ok} dur={wr.duration_seconds:.1f}s rc={wr.returncode}", flush=True)
            if wr.ok:
                simple_ok = True
                break
        run_status = "completed" if simple_ok else "failed"
        eng.store._execute(
            "UPDATE runs SET status=?, completed_at=CURRENT_TIMESTAMP WHERE id=?",
            (run_status, run_id),
        )
        # ── 生成工作报告 ──
        _gen_report(eng, run_id, run_status, task, [{
            "node_id": "direct", "title": task, "agent": agents[0] if simple_ok else agents[-1],
            "ok": simple_ok, "dur": getattr(wr, 'duration_seconds', 0),
            "error": None if simple_ok else (getattr(wr, 'stderr', '') or 'failed'),
        }], ctl_dir=ctl_dir, model=model, agents=agents, cwd=cwd)
        print(f"[DONE] {run_id} status={run_status}", flush=True)
        return 0 if simple_ok else 1

    # ── 多节点 WBS ──
    node_results: list[dict] = []
    node_status: dict[str, str] = {}

    for n in nodes:
        cap = infer_capability(n.title, n.description or "")
        best = select_best_agent(cap, agents) or agents[0]

        # 依赖跳过
        if n.dependencies:
            failed = [d for d in n.dependencies if node_status.get(d) == "failed"]
            if failed:
                print(f"  ⏭ [{n.id}] 依赖 {failed[0]} 失败，跳过", flush=True)
                node_status[n.id] = "skipped"
                node_results.append({"id": n.id, "ok": False, "skipped": True})
                continue

        eng.store.insert_wbs_node(run_id, n.__dict__ if hasattr(n, '__dict__') else n.to_dict())
        eng.store.update_node(n.id, "running", run_id=run_id)

        print(f"[NODE] {n.id}: {best} → {n.title}", flush=True)

        eng.agent_backend = get_backend(best)
        # 支持 retry：重复 dispatch 直到成功或 leader 选择 kill
        max_attempts = 5
        for attempt in range(1, max_attempts + 1):
            wr = eng._run_worker(run_id, n, timeout, model_override=model)
            # 检查是否为 retry（通过 status.json 的最后一条记录）
            is_retry = False
            try:
                with open(f"{ctl_dir}/status.json") as _sf:
                    _last = json.loads(_sf.read())
                    if isinstance(_last, dict) and _last.get("type") == "retry":
                        is_retry = True
            except (OSError, json.JSONDecodeError):
                pass
            if is_retry and attempt < max_attempts:
                print(f"  ⟳ [{n.id}] retry #{attempt}...", flush=True)
                eng.store.update_node(n.id, "running", run_id=run_id)
                continue
            elif is_retry:
                print(f"  ✗ [{n.id}] 重试次数耗尽 ({max_attempts})", flush=True)
            break

        ok = wr.ok
        eng.store.update_node(n.id, "completed" if ok else "failed",
            wr.result or "", run_id=run_id,
            duration_seconds=wr.duration_seconds,
            error=None if ok else (wr.stderr or "failed"))

        print(f"[NODE] {n.id}: {'✅' if ok else '❌'} {wr.duration_seconds:.1f}s", flush=True)
        node_status[n.id] = "completed" if ok else "failed"
        node_results.append({
            "id": n.id, "title": n.title, "agent": best, "ok": ok,
            "dur": wr.duration_seconds, "error": None if ok else (wr.stderr or "failed"),
        })

    # ── 最终状态 + 工作报告 ──
    all_ok = all(r["ok"] for r in node_results)
    run_status = "completed" if all_ok else "failed"
    eng.store._execute(
        "UPDATE runs SET status=?, completed_at=CURRENT_TIMESTAMP WHERE id=?",
        (run_status, run_id),
    )
    _gen_report(eng, run_id, run_status, task, node_results,
                ctl_dir=ctl_dir, model=model, agents=agents, cwd=cwd)
    print(f"[DONE] {run_id} status={run_status}", flush=True)
    return 0 if all_ok else 1


def _gen_report(eng, run_id: str, status: str, task: str, nodes: list[dict],
                ctl_dir: str = "", model: str | None = None,
                agents: list[str] | None = None, cwd: str = "") -> None:
    """生成工作报告：列出各节点产出（不调 agent 评估，由 leader 亲自评审）。"""
    total_dur = sum(n.get("dur", 0) or 0 for n in nodes)
    ok_count = sum(1 for n in nodes if n.get("ok"))
    fail_count = sum(1 for n in nodes if not n.get("ok"))

    lines: list[str] = []
    lines.append("=" * 60)
    lines.append(f"  📋 Worker 工作报告 · {run_id}")
    lines.append("=" * 60)
    lines.append(f"  任务: {task[:80]}")
    lines.append(f"  状态: {'✅ 完成' if status == 'completed' else '❌ 失败'}")
    lines.append(f"  节点: {ok_count} 完成 / {fail_count} 失败 / {len(nodes)} 总计")
    lines.append(f"  耗时: {total_dur:.1f}s")
    lines.append("")
    lines.append(f"  {'节点':<20} {'Agent':<14} {'耗时':>8} {'结果':<8}")
    lines.append(f"  {'─'*20} {'─'*14} {'─'*8} {'─'*8}")
    for n in nodes:
        icon = "✅" if n.get("ok") else "❌"
        err = n.get("error") or ""
        if err:
            err = err[:30].replace("\n", " ")
        dur_s = f"{n.get('dur', 0):.1f}s" if n.get("dur") else "-"
        lines.append(f"  {n.get('title','?')[:20]:<20} {n.get('agent','?'):<14} {dur_s:>8} {icon} {err}")
    lines.append("")

    # 列出各节点原始产出（leader 亲自评估用）
    lines.append("─" * 60)
    lines.append("  📄 节点原始产出（供 leader 评估）")
    lines.append("─" * 60)
    for n in nodes:
        nid = n.get("node_id", n.get("id", ""))
        title = n.get("title", "?")
        agent_name = n.get("agent", "?")
        icon = "✅" if n.get("ok") else "❌"
        lines.append(f"\n  {icon} [{agent_name}] {title}")
        if n.get("ok") and ctl_dir:
            out_file = f"{ctl_dir}/nodes/{nid}.out"
            try:
                with open(out_file) as f:
                    content = f.read().strip()
                if content:
                    _clines = content.split("\n")
                    for _cl in _clines[:30]:
                        lines.append(f"    | {_cl}")
                    if len(_clines) > 30:
                        lines.append(f"    | ...（共 {len(_clines)} 行）")
                else:
                    lines.append("    (空)")
            except OSError:
                lines.append("    (无输出文件)")
        elif not n.get("ok"):
            err = (n.get("error") or "")[:200]
            lines.append(f"    ❌ {err}")
    lines.append("")
    lines.append("─" * 60)
    lines.append("  💡 评估指引")
    lines.append("─" * 60)
    lines.append("  leader 可通过以下方式查看产出并提交评估：")
    lines.append(f"    read_file(path=\"{ctl_dir}/nodes/<id>.out\")  查看节点产出")
    lines.append(f"    评估完成后，使用 terminal() 执行：")
    _sql_cmd = f'sqlite3 {eng.store.db_path} "UPDATE runs SET meta_json=json_set(meta_json,\'$.leader_eval\',\'your evaluation here\') WHERE id=\'{run_id}\'"'
    lines.append(f"    {_sql_cmd[:120]}...")
    lines.append("")

    report = "\n".join(lines)
    print(report, flush=True)

    # 写入 DB
    now = __import__("time").strftime("%Y-%m-%d %H:%M:%S")
    meta = {"summary": report, "created_at": now,
            "nodes_total": len(nodes), "nodes_ok": ok_count, "nodes_failed": fail_count,
            "total_duration_s": round(total_dur, 1),
            "eval_status": "awaiting_leader"}
    eng.store._execute(
        "UPDATE runs SET meta_json=? WHERE id=?",
        (json.dumps(meta, ensure_ascii=False), run_id),
    )
    eng.store._execute(
        "INSERT INTO logs(run_id,node_id,level,message,data_json,created_at) VALUES(?,?,?,?,?,CURRENT_TIMESTAMP)",
        (run_id, "aggregate", "info", "worker finished",
         json.dumps(meta, ensure_ascii=False)),
    )


def main():
    import argparse
    ap = argparse.ArgumentParser(description="8765 Leader Circle")
    ap.add_argument("--task", required=True)
    ap.add_argument("--agents", default="opencode")
    ap.add_argument("--db", default="data/collab.sqlite3")
    ap.add_argument("--cwd", default=".")
    ap.add_argument("--model", default=None)
    ap.add_argument("--timeout", type=int, default=300)
    args = ap.parse_args()
    agents = [a.strip() for a in args.agents.split(",") if a.strip()]
    sys.exit(run_leader_circle(args.task, agents, args.db, args.cwd, args.model, args.timeout))


if __name__ == "__main__":
    main()
