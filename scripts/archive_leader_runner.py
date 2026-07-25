#!/usr/bin/env python3
"""
Leader Runner — 让 Hermes Agent 坐稳 leader 位置的 worker 调度器。

原理：
  以 background 进程运行，把每个 WBS 节点的 agent 输出流式写到 stdout 和文件。
  我（Hermes 对话中的 leader）通过读输出文件 + 写决策文件来管理。
  不修改 engine.py 核心，通过 engine.store 做 DB 操作。

通信协议：
  /tmp/leader_ctl/<run_id>/status.json   ← leader_runner 写（状态汇报）
  /tmp/leader_ctl/<run_id>/ctl.json      ← 我写（决策下发）
  /tmp/leader_ctl/<run_id>/nodes/<node_id>.out  ← 节点详细输出
"""
from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import time
import uuid
from pathlib import Path

# ── 把 8765 引擎加到路径 ──
_HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE / "src"))

from hermes_collab_engine.agents import get_backend
from hermes_collab_engine.capabilities import infer_capability, select_best_agent
from hermes_collab_engine.engine import CollabEngine
from hermes_collab_engine.models import WBSNode

# ── 常量 ──
CTL_ROOT = Path("/tmp/leader_ctl")
STALL_SECONDS = 30        # 静默多久触发 GUARDIAN
GUARDIAN_MIN_ELAPSED = 60 # 至少跑多久才触发 GUARDIAN
CTL_POLL_INTERVAL = 2     # 等待决策时每 2s 检查一次 ctl.json
SHORT_READ_TIMEOUT = 3    # worker 退出后读残余数据的最大等待


def _ctl_dir(run_id: str) -> Path:
    return CTL_ROOT / run_id


def _status_path(run_id: str) -> Path:
    return _ctl_dir(run_id) / "status.json"


def _ctl_path(run_id: str) -> Path:
    return _ctl_dir(run_id) / "ctl.json"


def _node_out_path(run_id: str, node_id: str) -> Path:
    return _ctl_dir(run_id) / "nodes" / f"{node_id}.out"


def _node_err_path(run_id: str, node_id: str) -> Path:
    return _ctl_dir(run_id) / "nodes" / f"{node_id}.err"


def write_status(run_id: str, **kwargs):
    """写入状态文件，leader（我）可以通过 read_file 读取。"""
    p = _status_path(run_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {"ts": time.time(), **kwargs}
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    # 也打印到 stdout 让 background process output 可见
    tag = kwargs.get("type", "status")
    print(f"[LEADER:{tag}] {kwargs.get('message', '')}", flush=True)


def write_node_output(run_id: str, node_id: str, text: str, is_stderr: bool = False):
    """把一行 worker 输出追加到节点文件。"""
    p = _node_err_path(run_id, node_id) if is_stderr else _node_out_path(run_id, node_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(text + "\n")


def wait_for_decision(run_id: str, timeout: int = 120) -> str:
    """等待 leader（我）写入 ctl.json。返回 'kill' 或 'continue' 或 'timeout'。"""
    ctl = _ctl_path(run_id)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if ctl.exists():
            try:
                data = json.loads(ctl.read_text())
                decision = data.get("decision", "")
                if decision in ("kill", "continue"):
                    ctl.unlink(missing_ok=True)
                    return decision
            except (json.JSONDecodeError, OSError):
                pass
        time.sleep(CTL_POLL_INTERVAL)
    return "timeout"


def stream_worker(
    run_id: str,
    node: WBSNode,
    cmd: list[str],
    cwd: str,
    env: dict | None,
    timeout: int,
) -> dict:
    """用 PIPE+select 流式运行一个 worker，支持 leader 介入。

    返回 dict: ok, stdout_text, stderr_text, duration, interrupted
    """
    node_id = node.id
    out_path = _node_out_path(run_id, node_id)
    err_path = _node_err_path(run_id, node_id)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # ── 启动 ──
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=cwd,
            env=env,
            start_new_session=True,
        )
    except FileNotFoundError as e:
        write_status(run_id, type="error", node=node_id, message=f"binary not found: {e}")
        return {"ok": False, "stdout_text": "", "stderr_text": str(e), "duration": 0, "interrupted": False}

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    last_output = time.time()
    start = time.time()
    interrupted = False

    write_status(run_id, type="dispatch", node=node_id, message=f"started (pid={proc.pid})")

    # ── 流式读取循环（不依赖 EOF） ──
    while proc.poll() is None:
        now = time.time()
        elapsed = now - start

        # 用 select 读可用数据（0.5s 超时用于做 GUARDIAN 检查）
        reads = [fd for fd in (proc.stdout, proc.stderr) if fd is not None]
        if not reads:
            break
        rlist, _, _ = select.select(reads, [], [], 0.5)

        for fd in rlist:
            line = fd.readline()
            if not line:
                continue
            line = line.rstrip("\n\r")
            if fd == proc.stdout:
                stdout_lines.append(line)
                write_node_output(run_id, node_id, line, is_stderr=False)
                # 打印到脚本 stdout（让 background process poll 可见）
                print(f"  [{node_id[:10]}] {line[:200]}", flush=True)
            else:
                stderr_lines.append(line)
                write_node_output(run_id, node_id, line, is_stderr=True)
            last_output = time.time()

        # ── GUARDIAN 静默检测 ──
        idle = now - last_output
        if idle > STALL_SECONDS and elapsed > GUARDIAN_MIN_ELAPSED:
            write_status(
                run_id, type="guardian", node=node_id,
                message=f"worker 已静默 {idle:.0f}s（总运行 {elapsed:.0f}s）",
                idle_seconds=round(idle), elapsed_seconds=round(elapsed),
            )
            decision = wait_for_decision(run_id, timeout=120)
            if decision == "kill":
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                interrupted = True
                write_status(run_id, type="killed", node=node_id, message="worker 已被 leader 中断")
                break
            elif decision == "timeout":
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                write_status(run_id, type="timeout", node=node_id, message="等待决策超时，已终止")
                return {"ok": False, "stdout_text": "".join(stdout_lines),
                        "stderr_text": "leader decision timeout", "duration": time.time() - start,
                        "interrupted": True}
            # decision == "continue" → 重置静默计时，继续等
            last_output = time.time()

        # 硬超时
        if elapsed > timeout:
            write_status(run_id, type="timeout", node=node_id, message=f"超过超时 {timeout}s")
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            return {"ok": False, "stdout_text": "".join(stdout_lines),
                    "stderr_text": f"timeout after {timeout}s", "duration": time.time() - start,
                    "interrupted": False}

    # ── 进程已退出，读残余数据（短超时防 daemon 卡住） ──
    duration = round(time.time() - start, 3)
    try:
        remaining_out, remaining_err = proc.communicate(timeout=SHORT_READ_TIMEOUT)
        if remaining_out:
            stdout_lines.append(remaining_out.rstrip("\n\r"))
            write_node_output(run_id, node_id, remaining_out.rstrip("\n\r"), is_stderr=False)
        if remaining_err:
            stderr_lines.append(remaining_err.rstrip("\n\r"))
            write_node_output(run_id, node_id, remaining_err.rstrip("\n\r"), is_stderr=True)
    except subprocess.TimeoutExpired:
        # daemon 继承了 pipe，3s 读不完 → 关 fd 放弃
        for fd_name in ("stdout", "stderr"):
            fd = getattr(proc, fd_name, None)
            if fd is not None:
                try:
                    fd.close()
                except OSError:
                    pass
        proc.kill()
        proc.wait(timeout=3)

    stdout_text = "\n".join(stdout_lines)
    stderr_text = "\n".join(stderr_lines)
    ok = proc.returncode == 0

    write_status(run_id, type="done", node=node_id,
                 message=f"{'✅' if ok else '❌'} returncode={proc.returncode}, {duration:.1f}s",
                 ok=ok, returncode=proc.returncode, duration=duration)

    return {"ok": ok, "stdout_text": stdout_text, "stderr_text": stderr_text,
            "duration": duration, "interrupted": interrupted}


def run_leader_cycle(
    task: str,
    agents: list[str],
    db_path: str,
    cwd: str,
    model: str | None = None,
    timeout: int = 300,
) -> int:
    """完整的 leader 调度循环。返回 exit code。"""
    run_id = "run_" + uuid.uuid4().hex[:12]
    ctl_dir = _ctl_dir(run_id)
    ctl_dir.mkdir(parents=True, exist_ok=True)

    # 初始化状态文件
    write_status(run_id, type="init", message=f"run_id={run_id}, agents={agents}")

    # ── 创建引擎（只用于 planner + store） ──
    eng = CollabEngine(
        db_path, cwd, model,
        agent=agents[0] if agents else "opencode",
        leader_agent=None,
        worker_agent=agents[0] if agents else None,
    )

    # ── 评估 + 分解 ──
    score = eng.planner.assess(task)
    overall = score.overall if hasattr(score, 'overall') else (score.get('overall', 5) if isinstance(score, dict) else 5)
    # 强制 WBS 多步骤
    import re
    numbered_steps = len(re.findall(r'(?:^|\n)\s*\d+[).]\s', task))
    if numbered_steps >= 3:
        overall = max(overall, 5)

    eng.store.create_run(run_id, task[:100], task, {"overall": overall},
                         agent=agents[0] if agents else "opencode")

    if overall > 3:
        plan = eng.planner.decompose(task, max_nodes=overall)
        nodes = plan.nodes if plan and hasattr(plan, 'nodes') else []
    else:
        nodes = []

    if not nodes or len(nodes) <= 1:
        # 简单任务
        write_status(run_id, type="info", message=f"simple task (overall={overall})")
        for ag in agents:
            backend = get_backend(ag)
            cmd = backend.build_command(prompt=task, model=model, allowed_tools=[], provider=None, reasoning=False)
            result = stream_worker(run_id, WBSNode(id="direct", title=task, description=task,
                                    capability="general", complexity=1, dependencies=[], parallelizable=True,
                                    deliverable=task), cmd, cwd, None, timeout)
            if result["ok"]:
                eng.store._execute("UPDATE runs SET status='completed', completed_at=CURRENT_TIMESTAMP WHERE id=?", (run_id,))
                write_status(run_id, type="completed", message=f"run completed by {ag}")
                return 0
        eng.store._execute("UPDATE runs SET status='failed', completed_at=CURRENT_TIMESTAMP WHERE id=?", (run_id,))
        write_status(run_id, type="failed", message="all agents failed")
        return 1

    # ── 多节点 WBS ──
    write_status(run_id, type="wbs", message=f"{len(nodes)} nodes", node_count=len(nodes))
    node_results: list[dict] = []
    node_status: dict[str, str] = {}

    for n in nodes:
        cap = infer_capability(n.title, n.description or "")
        best = select_best_agent(cap, agents)
        if not best:
            best = agents[0]

        # 依赖跳过
        if n.dependencies:
            failed = [d for d in n.dependencies if node_status.get(d) == "failed"]
            if failed:
                write_status(run_id, type="skip", node=n.id, message=f"依赖 {failed[0]} 失败，跳过")
                eng.store.insert_wbs_node(run_id, n.__dict__ if hasattr(n, '__dict__') else n.to_dict())
                eng.store.update_node(n.id, "skipped",
                    error=f"dependency: {failed[0]} failed", run_id=run_id)
                node_status[n.id] = "skipped"
                node_results.append({"node_id": n.id, "node_title": n.title, "agent": best, "ok": False})
                continue

        # 写入 DB
        eng.store.insert_wbs_node(run_id, n.__dict__ if hasattr(n, '__dict__') else n.to_dict())
        eng.store.update_node(n.id, "running", run_id=run_id)

        # 构建命令
        backend = get_backend(best)
        cmd = backend.build_command(prompt=n.description or n.title, model=model,
                                    allowed_tools=[], provider=None, reasoning=False)

        write_status(run_id, type="dispatch", node=n.id, message=f"{best} → {n.title}")

        # 流式运行
        result = stream_worker(run_id, n, cmd, cwd, None, timeout)

        ok = result["ok"] and not result["interrupted"]
        eng.store.update_node(n.id, "completed" if ok else "failed",
                              result["stdout_text"], run_id=run_id,
                              duration_seconds=result["duration"],
                              error=None if ok else (result["stderr_text"] or "failed"))

        node_status[n.id] = "completed" if ok else "failed"
        node_results.append({
            "node_id": n.id, "node_title": n.title, "agent": best,
            "ok": ok, "stdout": result["stdout_text"][:2000],
            "stderr": result["stderr_text"],
        })

    # ── Peer Review ──
    write_status(run_id, type="peer_review", message="开始互评...")
    for nr in node_results:
        if not nr["ok"]:
            continue
        # 找不同的 reviewer
        reviewer = None
        for ag in agents:
            if ag != nr["agent"]:
                reviewer = ag
                break
        if reviewer is None:
            continue
        # review 也用 stream_worker
        review_prompt = (
            f"请评审以下结果（1-10分）。原始任务: {nr['node_title']}\n\n"
            f"输出:\n```\n{nr['stdout'][:2000]}\n```\n\n"
            f"返回 JSON: {{\"verdict\":\"accept|reject|needs_improvement\",\"score\":<1-10>}}"
        )
        backend = get_backend(reviewer)
        review_cmd = backend.build_command(prompt=review_prompt, model=model,
                                           allowed_tools=[], provider=None, reasoning=False)
        review_node = WBSNode(
            id=f"review-{nr['node_id']}", title=f"Review {nr['node_id']}",
            description=review_prompt, capability="review", complexity=1,
            dependencies=[], parallelizable=True, deliverable="review result",
        )
        rr = stream_worker(run_id, review_node, review_cmd, cwd, None, 120)
        eng.store.log(run_id, "review",
            f"peer_review: {reviewer} -> {nr['agent']}: {rr['ok']}",
            {"reviewer": reviewer, "target": nr["agent"]}, node_id=nr["node_id"])

    # ── 最终状态 ──
    all_ok = all(nr["ok"] for nr in node_results if nr.get("node_id") not in [
        r["node_id"] for r in node_results if r.get("node_id", "").startswith("review-")
    ])
    run_status = "completed" if all_ok else "failed"
    eng.store._execute(
        "UPDATE runs SET status=?, completed_at=CURRENT_TIMESTAMP WHERE id=?",
        (run_status, run_id),
    )
    write_status(run_id, type="final", message=f"run {run_status}", status=run_status)
    return 0 if all_ok else 1


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Leader Runner — 流式 worker 调度器")
    ap.add_argument("--task", required=True, help="任务描述")
    ap.add_argument("--agents", default="opencode,claude-code,codex,hermes", help="逗号分隔的 agent 列表")
    ap.add_argument("--db", default="data/collab.sqlite3", help="DB 路径")
    ap.add_argument("--cwd", default=".", help="工作目录")
    ap.add_argument("--model", default=None, help="模型名")
    ap.add_argument("--timeout", type=int, default=300, help="每节点超时秒数")
    args = ap.parse_args()
    agents = [a.strip() for a in args.agents.split(",") if a.strip()]
    sys.exit(run_leader_cycle(args.task, agents, args.db, args.cwd, args.model, args.timeout))


if __name__ == "__main__":
    main()
