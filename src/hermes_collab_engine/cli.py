from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
from pathlib import Path
from typing import Any

from .engine import CollabEngine
from .models import RiskPolicy, WBSNode
from .provider import ProviderProfile
from .server import DashboardServer

# ── Dialog mode: default CLI entry point ───────────────────────────
def _run_dialog(args: argparse.Namespace) -> int:
    """Run the CLI dialog mode — detect agents, choose mode, execute tasks interactively."""
    from .detector import detect_all_agents, format_health_report
    from .cli_protocol import CLIGuardianProtocol

    cwd = Path(getattr(args, 'cwd', '.')).resolve()
    cwd.mkdir(parents=True, exist_ok=True)
    quiet = getattr(args, 'quiet', False)
    protocol = CLIGuardianProtocol(quiet=quiet)

    # Step 1: Detect agents
    if not quiet:
        print("扫描已安装的 Agent...")

    model = getattr(args, 'model', None) or os.environ.get("HERMES_COLLAB_MODEL")
    # Use shorter detection timeout for CLI startup (5s per agent, 30s total)
    health_results = detect_all_agents(model=model, timeout=10)

    # Step 2: Print detection report
    report = format_health_report(health_results)
    print(report)

    # Step 3: Choose mode
    try:
        mode_choice = input("").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n退出")
        return 0

    if mode_choice == "2":
        mode = "leader"
        # Leader mode: select worker agents
        available = [h for h in health_results if h.installed]
        if not available:
            print("没有可用的 agent，请先安装至少一个")
            return 1

        print("\nWorker Agent 选择（可用 agent 前有 ✅）:")
        for i, h in enumerate(available, 1):
            status = "✅" if h.reachable else "⚠️"
            model_str = h.model[:20] if h.model else "默认"
            print(f"  [{i}] {h.display_name:<15} {status}  {model_str} ({h.latency_ms:.0f}ms)")

        print("\n输入编号（可多选: 1,2 或 1-3）:")
        try:
            choices = input("").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n退出")
            return 0

        selected = _parse_agent_choices(choices, available)
        if not selected:
            print("未选择有效 agent，默认使用 opencode")
            selected = ["opencode"]

        worker_agents = ", ".join(selected)
        print(f"  Worker agent: {worker_agents}")
        print(f"  Leader mode 已就绪，输入任务开始 WBS 调度")
    else:
        mode = "no-leader"
        print("  无领袖模式 — 你分配任务，各 agent 平权协作")
        selected = [h.name for h in health_results if h.installed]

    # Step 4: Dialog loop
    print('')
    print('─' * 55)
    print('  请输入任务描述（或 /help 查看命令，Ctrl+C 退出）')
    print('─' * 55)

    while True:
        try:
            task = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n退出")
            break

        if not task:
            continue
        if task == "/quit" or task == "/exit":
            break
        if task == "/help":
            _print_dialog_help()
            continue
        if task == "/agents":
            print(format_health_report(health_results))
            continue
        if task.startswith("/mode"):
            parts = task.split()
            if len(parts) > 1:
                mode = "leader" if parts[1] == "leader" else "no-leader"
                print(f"  切换到 {mode} 模式")
            continue

        # Execute task
        if mode == "leader":
            _run_leader_task(task, cwd, model, selected, protocol)
        else:
            _run_no_leader_task(task, cwd, model, selected, protocol)

    return 0


def _parse_agent_choices(choice_str: str, available: list) -> list[str]:
    """Parse user agent selection like '1,2' or '1-3' or '1'."""
    import re
    result = []
    choice_str = choice_str.strip()
    if not choice_str:
        return [available[0].name] if available else []

    for part in re.split(r'[,，\s]+', choice_str):
        part = part.strip()
        if not part:
            continue
        if '-' in part:
            try:
                a, b = part.split('-', 1)
                for i in range(int(a), int(b) + 1):
                    if 1 <= i <= len(available):
                        result.append(available[i - 1].name)
            except ValueError:
                pass
        else:
            try:
                i = int(part)
                if 1 <= i <= len(available):
                    result.append(available[i - 1].name)
            except ValueError:
                pass

    return result or [available[0].name]


def _run_no_leader_task(task: str, cwd: Path, model: str | None,
                        agents: list[str], protocol: object) -> None:
    """No-Leader mode: WBS decomposition + agent dispatch + DB tracking."""
    from .engine import CollabEngine
    from .capabilities import infer_capability, select_best_agent
    from .cli_protocol import CLIGuardianProtocol as _CP
    proto = protocol if isinstance(protocol, _CP) else _CP()

    # Create engine for DB + planner
    db_path = str(cwd / "data" / "collab.sqlite3")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    eng = CollabEngine(db_path, str(cwd), model,
        agent=agents[0] if agents else "opencode",
        leader_agent=None, worker_agent=agents[0] if agents else None)

    # Planner: decompose task into WBS nodes
    score = eng.planner.assess(task)
    overall = score.overall if hasattr(score, 'overall') else (score.get('overall', 5) if isinstance(score, dict) else 5)
    if overall <= 3:
        nodes = []
    else:
        plan = eng.planner.decompose(task, max_nodes=overall)
        nodes = plan.nodes if plan and hasattr(plan, 'nodes') else []

    if not nodes or len(nodes) <= 1:
        # Simple task: single dispatch, but still write to DB
        run_id = "run_" + __import__('uuid').uuid4().hex[:12]
        eng.store.create_run(run_id, task[:100], task, {"overall": overall}, agent=agents[0] if agents else "opencode")
        for name in agents:
            if not _dispatch_single(name, task, cwd, model):
                break
        eng.store._execute("UPDATE runs SET status='completed', completed_at=CURRENT_TIMESTAMP WHERE id=?", (run_id,))
        return

    # Multi-node WBS: create run + dispatch each node
    run_id = "run_" + __import__('uuid').uuid4().hex[:12]
    eng.store.create_run(run_id, task[:100], task, {"overall": overall}, agent=agents[0] if agents else "opencode")

    print(f"  📋 WBS: {len(nodes)} nodes")
    for node in nodes:
        cap = node.capability
        best = select_best_agent(cap, agents)
        if not best:
            best = agents[0]
        print(f"    [{best}] {node.title} ({cap})")

    # Dispatch results collected for peer review
    dispatch_results: list[dict] = []

    for node in nodes:
        cap = node.capability
        best = select_best_agent(cap, agents)
        if not best:
            best = agents[0]

        # Insert WBS node to DB
        eng.store.insert_wbs_node(run_id, node.to_dict())
        eng.store.update_node(node.id, "running", run_id=run_id)

        proto.emit_stream(node.id, f"dispatch to {best}...")
        result = _dispatch_single(best, node.description or node.title, cwd, model, prefix=f"    [{node.id}] ")

        if result:
            eng.store.update_node(node.id, "completed",
                result.get('stdout', ''), run_id=run_id)
            proto.emit_completed(node.id, result.get('dur', 0))
        else:
            eng.store.update_node(node.id, "failed",
                error="dispatch failed", run_id=run_id)
            proto.emit_error(node.id, "dispatch failed")

        dispatch_results.append({
            "node_id": node.id,
            "node_title": node.title,
            "agent": best,
            "ok": result.get("ok", False) if result else False,
            "stdout": (result.get("stdout", "") if result else "")[:2000],
            "stderr": result.get("stderr", "") if result else "no result",
        })

    # ── Peer Review: each successful node reviewed by a different agent ──
    print(f"\n  📋 互评开始...")
    from .no_leader import NoLeaderDispatcher as _NLD
    reviewer = _NLD(cwd=cwd, model=model)

    for dr in dispatch_results:
        if not dr["ok"]:
            print(f"    ⏭ [{dr['node_id']}] 跳过（未完成）")
            continue

        # Pick a reviewer different from the original agent
        reviewer_agent = None
        for ag in agents:
            if ag != dr["agent"]:
                # Check if this reviewer is installed
                import shutil as _su
                from .agents import get_backend as _gb
                try:
                    _bin = _gb(ag).command[0]
                    if isinstance(_bin, (list, tuple)):
                        _bin = _bin[0]
                    if _su.which(_bin):
                        reviewer_agent = ag
                        break
                except Exception:
                    continue

        if reviewer_agent is None:
            print(f"    ⏭ [{dr['node_id']}] 无可用的评审 agent")
            continue

        print(f"    [{dr['node_id']}] {reviewer_agent} 评审 {dr['agent']} 产出...", end=" ", flush=True)
        from .no_leader import WorkerResult as _WR
        review = reviewer.peer_review(
            target_result=_WR(
                agent=dr["agent"], ok=dr["ok"], stdout=dr["stdout"],
                stderr=dr["stderr"], duration_s=0, node_id=dr["node_id"],
                task=dr["node_title"],
            ),
            target_agent=dr["agent"],
            reviewer_agent=reviewer_agent,
            task_description=dr["node_title"],
        )

        verdict_icon = "✅" if review.verdict == "accept" else ("⚠️" if review.verdict == "needs_improvement" else "❌")
        print(f"{verdict_icon} 评分={review.score}  verdict={review.verdict}")

        # Store review result
        eng.store._execute(
            "INSERT INTO logs(run_id,node_id,level,message,data_json,created_at) VALUES(?,?,?,?,?,CURRENT_TIMESTAMP)",
            (run_id, dr["node_id"], "review",
             f"peer_review: {reviewer_agent} -> {dr['agent']}: {review.verdict} ({review.score}/10)",
             __import__('json').dumps({"reviewer": reviewer_agent, "target": dr["agent"],
                "verdict": review.verdict, "score": review.score,
                "suggestions": review.suggestions}, ensure_ascii=False))
        )

        if review.verdict == "reject" and dr["ok"]:
            print(f"      → 标记节点 {dr['node_id']} 为 needs_rework")

    eng.store._execute("UPDATE runs SET status=?, completed_at=CURRENT_TIMESTAMP WHERE id=?",
        ("completed", run_id))
    print(f"  ✅ Run {run_id} completed")


def _dispatch_single(agent: str, task: str, cwd: Path, model: str | None,
                     prefix: str = "  ", timeout: int = 300) -> dict | None:
    """Dispatch a single task to an agent. Returns result dict or None on failure."""
    import subprocess as _sp, time as _time, shutil as _shutil
    from .agents import get_backend
    try:
        backend = get_backend(agent)
    except KeyError:
        print(f"{prefix}[{agent}] unknown agent")
        return None
    binary = backend.command[0] if isinstance(backend.command, (list, tuple)) else backend.command
    if not _shutil.which(binary):
        print(f"{prefix}[{agent}] not installed")
        return None
    cmd = backend.build_command(prompt=task, model=model, allowed_tools=[], provider=None, reasoning=False)
    print(f"{prefix}[{agent}] executing...", end=" ", flush=True)
    start = _time.time()
    try:
        proc = _sp.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=str(cwd))
        elapsed = _time.time() - start
        ok = proc.returncode == 0
        out = (proc.stdout or "").strip()[:300]
        if ok:
            print(f"✅ ({elapsed:.0f}s)")
            return {"ok": True, "stdout": out, "dur": round(elapsed, 1)}
        else:
            err = (proc.stderr or proc.stdout or "").strip()[:100]
            print(f"❌ {err}")
            return {"ok": False, "stderr": err, "dur": round(elapsed, 1)}
    except _sp.TimeoutExpired:
        print(f"❌ timeout")
        return None
    except Exception as e:
        print(f"❌ {e}")
        return None


def _run_leader_task(task: str, cwd: Path, model: str | None,
                     agents: list[str], protocol: object) -> None:
    """Leader mode: WBS + capability routing + guardian CLI + aggregator.

    Uses engine's planner for decomposition and store for DB tracking,
    but dispatches workers via NoLeaderDispatcher (bypasses engine.run's
    broken hermes leader session).
    """
    from .engine import CollabEngine
    from .no_leader import NoLeaderDispatcher
    from .capabilities import select_best_agent, infer_capability
    from .cli_protocol import CLIGuardianProtocol as _CP
    import uuid as _uuid

    proto = protocol if isinstance(protocol, _CP) else _CP()
    db_path = str(cwd / "data" / "collab.sqlite3")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    # Create engine for planner + store
    eng = CollabEngine(db_path, str(cwd), model,
        agent=agents[0] if agents else "opencode",
        leader_agent=None, worker_agent=agents[0] if agents else None)

    # 1. Assess complexity + decompose
    score = eng.planner.assess(task)
    overall = score.overall if hasattr(score, 'overall') else (score.get('overall', 5) if isinstance(score, dict) else 5)
    print(f"  📊 复杂度评估: overall={overall}")

    run_id = "run_" + _uuid.uuid4().hex[:12]
    eng.store.create_run(run_id, task[:100], task, {"overall": overall},
                         agent=agents[0] if agents else "opencode")

    if overall > 3:
        plan = eng.planner.decompose(task, max_nodes=overall)
        nodes = plan.nodes if plan and hasattr(plan, 'nodes') else []
    else:
        nodes = []

    if not nodes or len(nodes) <= 1:
        # Simple: single dispatch with guardian output
        print(f"  → 单节点执行")
        for ag in agents:
            eng.store._execute("UPDATE runs SET agent=? WHERE id=?", (ag, run_id))
            proto.emit_stream(run_id, f"dispatch to {ag}")
            result = _dispatch_single(ag, task, cwd, model, prefix="    ")
            if result and result.get("ok"):
                proto.emit_completed(run_id, result.get("dur", 0))
                eng.store._execute("UPDATE runs SET status='completed', completed_at=CURRENT_TIMESTAMP WHERE id=?", (run_id,))
                print(f"  ✅ Run {run_id} completed")
                return
            else:
                err = result.get("stderr", "failed") if result else "no result"
                proto.emit_error(run_id, err)
        eng.store._execute("UPDATE runs SET status='failed', completed_at=CURRENT_TIMESTAMP WHERE id=?", (run_id,))
        print(f"  ❌ Run {run_id} failed")
        return

    # 2. Multi-node WBS: show plan
    print(f"  📋 WBS 分解: {len(nodes)} 个节点")
    d = NoLeaderDispatcher(cwd=cwd, model=model)

    for n in nodes:
        cap = infer_capability(n.title, n.description or "")
        best = select_best_agent(cap, agents)
        print(f"    [{n.id:15s}] {best:15s} ({cap:15s}) {n.title[:40]}")

    # 3. Execute nodes with guardian output
    for n in nodes:
        cap = infer_capability(n.title, n.description or "")
        best = select_best_agent(cap, agents)

        # DB tracking
        node_dict = n.__dict__ if hasattr(n, '__dict__') else n.to_dict()
        eng.store.insert_wbs_node(run_id, node_dict)
        eng.store.update_node(n.id, "running", run_id=run_id)

        # Guardian stream output
        proto.emit_stream(n.id, f"→ {best}: {n.title}")

        # Dispatch
        result = _dispatch_single(best, n.description or n.title, cwd, model,
                                  prefix=f"    [{n.id[:10]}] ")

        if result and result.get("ok"):
            eng.store.update_node(n.id, "completed", result.get("stdout", ""),
                                  run_id=run_id, duration_seconds=result.get("dur", 0))
            proto.emit_completed(n.id, result.get("dur", 0))
        else:
            err = result.get("stderr", "dispatch failed") if result else "no result"
            eng.store.update_node(n.id, "failed", error=err, run_id=run_id)
            proto.emit_error(n.id, err)

    # 4. Aggregate results
    all_nodes = eng.store._query(
        "SELECT id, status FROM wbs_nodes WHERE run_id=?", (run_id,))
    done = sum(1 for r in all_nodes if r.get("status") in ("completed", "failed"))
    ok_count = sum(1 for r in all_nodes if r.get("status") == "completed")

    if done == len(all_nodes) and ok_count > 0:
        eng.store._execute("UPDATE runs SET status='completed', completed_at=CURRENT_TIMESTAMP WHERE id=?", (run_id,))
        proto.emit_aggregate(f"Run {run_id}: {ok_count}/{len(all_nodes)} nodes completed")
        print(f"  ✅ Run {run_id} completed ({ok_count}/{len(all_nodes)} nodes OK)")
    else:
        eng.store._execute("UPDATE runs SET status='failed', completed_at=CURRENT_TIMESTAMP WHERE id=?", (run_id,))
        print(f"  ❌ Run {run_id} failed ({ok_count}/{len(all_nodes)} OK)")


def _print_dialog_help() -> None:
    print("")
    print("  命令:")
    print("    <task>     执行任务")
    print("    /help      显示此帮助")
    print("    /quit      退出")
    print("    /exit      退出")
    print("    /agents    重新显示 agent 连通性")
    print("    /mode no-leader|leader  切换模式")
    print("")


LESSON_SCOPES = ("global", "project", "run", "node", "wbs-family")
RISK_POLICY_ACTIONS = {"auto", "notify", "pause"}


def _model_options(args):
    model = args.model or os.environ.get("HERMES_COLLAB_MODEL") or os.environ.get("ANTHROPIC_MODEL")
    if args.model:
        leader_model = args.leader_model
        worker_model = args.worker_model
    else:
        leader_model = args.leader_model or os.environ.get("HERMES_COLLAB_LEADER_MODEL")
        worker_model = args.worker_model or os.environ.get("HERMES_COLLAB_WORKER_MODEL")
    return model, leader_model, worker_model


def _json_arg(value: str, flag: str) -> dict:
    try:
        data = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid {flag}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"invalid {flag}: expected object, got {type(data).__name__}")
    return data


def _setting_value(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _json_print(data: dict | list, pretty: bool = True) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2 if pretty else None))


def _policy_action(value: str) -> str:
    if value not in RISK_POLICY_ACTIONS:
        raise argparse.ArgumentTypeError(f"must be one of {sorted(RISK_POLICY_ACTIONS)}")
    return value


def _node_from_row(row) -> WBSNode:
    deps = json.loads(row["dependencies_json"] or "[]")
    return WBSNode(
        id=row["id"],
        title=row["title"],
        description=row["description"],
        capability=row["capability"],
        complexity=row["complexity"],
        dependencies=deps,
        parallelizable=bool(row["parallelizable"]),
        deliverable=row["deliverable"],
        status=row["status"],
        parent_id=row["parent_id"],
        attempt=row["attempt"],
    )


def main() -> int:
    parser = argparse.ArgumentParser(prog="hermes-collab", description="Standalone Hermes-Claude collaboration engine")
    sub = parser.add_subparsers(dest="cmd")

    # ── Dialog mode (default when no subcommand) ──────────────
    dialog = sub.add_parser("dialog", help="[默认] 对话模式 — 连通性检测 + 双模式选择 + 交互式任务执行")
    dialog.add_argument("--cwd", default=".", help="Working directory")
    dialog.add_argument("--db", default="data/collab.sqlite3")
    dialog.add_argument("--model", help="Default model for all agents")
    dialog.add_argument("--quiet", action="store_true", help="减少 CLI 输出")

    run = sub.add_parser("run", help="Run a collaboration task")
    run.add_argument("request", nargs="*", help="Task request text")
    run.add_argument("--request-file", help="Read request from file")
    run.add_argument("--title")
    run.add_argument("--cwd", default=".")
    run.add_argument("--db", default="data/collab.sqlite3")
    run.add_argument("--model", help="Use the same model for leader and workers")
    run.add_argument("--leader-model", help="Leader brain model for planning and aggregation")
    run.add_argument("--worker-model", help="Worker brain model for coding workers")
    run.add_argument("--agent", default="opencode", help="Agent backend: opencode (default), claude-code, codex, hermes, or custom")
    run.add_argument("--leader-agent", default=None, help="Leader agent (default: hermes)")
    run.add_argument("--worker-agent", default=None, help="Worker agent (default: same as --agent)")
    run.add_argument("--concurrency", type=int, default=2, help="Per-run in-flight workers (threads in run's pool)")
    run.add_argument("--global-max-concurrent", type=int, default=4, help="Global cap on opencode worker processes across ALL runs. Prevents 4-run storm (4GB RAM death spiral).")
    run.add_argument("--timeout", type=int, default=86400)
    run.add_argument("--max-retries", type=int, default=2)
    run.add_argument("--split-count", type=int, default=4)
    run.add_argument("--no-aggregate", action="store_true")
    run.add_argument("--json", action="store_true")
    run.add_argument("--provider", help="Provider name (enables multi-provider env var mapping)")
    run.add_argument("--provider-base-url", help="Provider API base URL (required with --provider)")
    run.add_argument("--provider-api-key", help="Provider API key (required with --provider)")
    run.add_argument("--provider-model", help="Provider default model (falls back to --model then env var)")

    server = sub.add_parser("server", help="Run management dashboard")
    server.add_argument("--host", default="0.0.0.0")
    server.add_argument("--port", type=int, default=8765)
    server.add_argument("--cwd", default=".")
    server.add_argument("--db", default="data/collab.sqlite3")
    server.add_argument("--model", help="Use the same model for leader and workers")
    server.add_argument("--leader-model", help="Leader brain model for planning and aggregation")
    server.add_argument("--worker-model", help="Worker brain model for coding workers")
    server.add_argument("--agent", default="opencode", help="Agent backend: opencode (default), claude-code, codex, hermes, or custom")

    status = sub.add_parser("status", help="Show engine status")
    status.add_argument("--db", default="data/collab.sqlite3")
    status.add_argument("--json", action="store_true")

    lesson = sub.add_parser("lesson", help="Manage lessons learned")
    lesson_sub = lesson.add_subparsers(dest="lesson_cmd", required=True)

    lesson_add = lesson_sub.add_parser("add", help="Add a lesson")
    lesson_add.add_argument("--db", default="data/collab.sqlite3")
    lesson_add.add_argument("--scope", choices=LESSON_SCOPES, default="global")
    lesson_add.add_argument("--category", required=True)
    lesson_add.add_argument("--lesson", required=True)
    lesson_add.add_argument("--source", default="preflight")
    lesson_add.add_argument("--evidence-json", default="{}")

    lesson_list = lesson_sub.add_parser("list", help="List lessons")
    lesson_list.add_argument("--db", default="data/collab.sqlite3")
    lesson_list.add_argument("--limit", type=int, default=20)
    lesson_list.add_argument("--category")
    lesson_list.add_argument("--scope", choices=LESSON_SCOPES)
    lesson_list.add_argument("--json", action="store_true")

    parent_log = sub.add_parser("parent-log", help="Write a parent/operator log entry")
    parent_log.add_argument("--db", default="data/collab.sqlite3")
    parent_log.add_argument("--run-id")
    parent_log.add_argument("--node-id")
    parent_log.add_argument("--level", default="info", choices=("debug", "info", "warning", "error"))
    parent_log.add_argument("--message", required=True)
    parent_log.add_argument("--data-json", default="{}")
    parent_log.add_argument("--json", action="store_true")

    kill_node = sub.add_parser("kill-node", help="Kill a running worker process for a node and mark it failed")
    kill_node.add_argument("--db", default="data/collab.sqlite3")
    kill_node.add_argument("--node-id", required=True)
    kill_node.add_argument("--run-id")
    kill_node.add_argument("--reason", default="killed by parent/operator intervention")
    kill_node.add_argument("--signal", default="TERM", choices=("TERM", "KILL", "INT"))
    kill_node.add_argument("--json", action="store_true")

    split_node = sub.add_parser("split-node", help="Proactively split a WBS node into focused shards")
    split_node.add_argument("--db", default="data/collab.sqlite3")
    split_node.add_argument("--node-id", required=True)
    split_node.add_argument("--run-id")
    split_node.add_argument("--split-count", type=int, default=4)
    split_node.add_argument("--reason", default="split by parent/operator intervention")
    split_node.add_argument("--json", action="store_true")

    skip_node = sub.add_parser("skip-node", help="Mark a node failed so the parent can continue with degraded context")
    skip_node.add_argument("--db", default="data/collab.sqlite3")
    skip_node.add_argument("--node-id", required=True)
    skip_node.add_argument("--run-id")
    skip_node.add_argument("--reason", required=True)
    skip_node.add_argument("--json", action="store_true")

    pause_run = sub.add_parser("pause-run", help="Pause new worker dispatch for a run")
    pause_run.add_argument("--db", default="data/collab.sqlite3")
    pause_run.add_argument("--cwd", default=".")
    pause_run.add_argument("--run-id", required=True)
    pause_run.add_argument("--reason", default="paused by parent/operator intervention")
    pause_run.add_argument("--json", action="store_true")

    resume_run = sub.add_parser("resume-run", help="Resume new worker dispatch for a paused run")
    resume_run.add_argument("--db", default="data/collab.sqlite3")
    resume_run.add_argument("--cwd", default=".")
    resume_run.add_argument("--run-id", required=True)
    resume_run.add_argument("--reason", default="resumed by parent/operator intervention")
    resume_run.add_argument("--json", action="store_true")

    snapshot = sub.add_parser("snapshot", help="Show persisted run pause/checkpoint state")
    snapshot.add_argument("--db", default="data/collab.sqlite3")
    snapshot.add_argument("--run-id")
    snapshot.add_argument("--json", action="store_true")

    context_snapshot = sub.add_parser("context-snapshot", help="Show persisted context snapshots for a run")
    context_snapshot.add_argument("--db", default="data/collab.sqlite3")
    context_snapshot.add_argument("run_id", nargs="?")
    context_snapshot.add_argument("--latest", action="store_true")
    context_snapshot.add_argument("--type", choices=("pre_compaction", "node_completed", "checkpoint"), dest="snapshot_type")

    save_snapshot = sub.add_parser("save-snapshot", help="Manually save a context snapshot (e.g. before compaction)")
    save_snapshot.add_argument("--db", default="data/collab.sqlite3")
    save_snapshot.add_argument("--cwd", default=".")
    save_snapshot.add_argument("run_id")
    save_snapshot.add_argument("--type", choices=("pre_compaction", "node_completed", "checkpoint"), dest="snapshot_type", default="pre_compaction")
    save_snapshot.add_argument("--node-id", default=None)
    save_snapshot.add_argument("--decisions", default=None, help="JSON array of decision strings")
    save_snapshot.add_argument("--user-instructions", default=None, help="JSON array of user instruction strings")
    save_snapshot.add_argument("--json", action="store_true")

    agents_cmd = sub.add_parser("agents", help="List available agent backends")
    agents_cmd.add_argument("--db", default="data/collab.sqlite3")
    agents_cmd.add_argument("--available", action="store_true", help="Only show agents on PATH")
    agents_cmd.add_argument("--json", action="store_true")

    skills_cmd = sub.add_parser("skills", help="List worker prompt skills")
    skills_cmd.add_argument("--node-type", help="Preview skills selected for a node capability")
    skills_cmd.add_argument("--task", default="", help="Task text used with --node-type selection")
    skills_cmd.add_argument("--json", action="store_true")

    tools_cmd = sub.add_parser("tools", help="List worker tool and MCP profiles")
    tools_cmd.add_argument("--node-type", help="Preview tool profiles selected for a node capability")
    tools_cmd.add_argument("--task", default="", help="Task text used with --node-type selection")
    tools_cmd.add_argument("--json", action="store_true")

    mcp_server = sub.add_parser("mcp-server", help="Manage registered MCP servers")
    mcp_server_sub = mcp_server.add_subparsers(dest="mcp_server_cmd", required=True)
    mcp_list = mcp_server_sub.add_parser("list", help="List registered MCP servers")
    mcp_list.add_argument("--db", default="data/collab.sqlite3")
    mcp_list.add_argument("--json", action="store_true")
    mcp_add = mcp_server_sub.add_parser("add", help="Register a new MCP server")
    mcp_add.add_argument("--db", default="data/collab.sqlite3")
    mcp_add.add_argument("--name", required=True, help="Server name [a-zA-Z0-9_-]")
    mcp_add.add_argument("--command", required=True, help="Executable command")
    mcp_add.add_argument("--args", default="", help="Space-separated command arguments")
    mcp_add.add_argument("--env", default="{}", help="JSON object of environment variables")
    mcp_add.add_argument("--tools", default="", help="Comma-separated tool names")
    mcp_add.add_argument("--description", default="", help="Server description")
    mcp_add.add_argument("--display-name", default="", help="Human-readable display name")
    mcp_add.add_argument("--capabilities", default='["*"]', help="JSON array of capability tags")
    mcp_add.add_argument("--json", action="store_true")
    mcp_remove = mcp_server_sub.add_parser("remove", help="Remove a registered MCP server")
    mcp_remove.add_argument("--db", default="data/collab.sqlite3")
    mcp_remove.add_argument("--name", required=True, help="Server name to remove")
    mcp_remove.add_argument("--json", action="store_true")

    verify_v45 = sub.add_parser("verify-v45", help="Legacy alias for verify-release")
    verify_v45.add_argument("--json", action="store_true")

    verify_release = sub.add_parser("verify-release", help="Run current release verification checks")
    verify_release.add_argument("--json", action="store_true")

    redo_node = sub.add_parser("redo-node", help="Create a redo node while keeping the source node for audit")
    redo_node.add_argument("--db", default="data/collab.sqlite3")
    redo_node.add_argument("--cwd", default=".")
    redo_node.add_argument("--run-id")
    redo_node.add_argument("--node-id", required=True)
    redo_node.add_argument("--reason", default="manual")
    redo_node.add_argument("--description-delta", default="Redo requested by parent/operator intervention")
    redo_node.add_argument("--cascade", action="store_true")
    redo_node.add_argument("--worker-model")
    redo_node.add_argument("--json", action="store_true")

    # doctor — one-shot health check for the runtime config
    doctor = sub.add_parser("doctor", help="Diagnose .runtime-config.json (path, perms, providers, token masking, backup count)")
    doctor.add_argument("--config", default=".runtime-config.json", help="Path to runtime config (default: ./.runtime-config.json)")
    doctor.add_argument("--json", action="store_true", help="Emit raw JSON instead of human-readable report")

    # config show / set / add-provider — thin wrappers over config_store
    config = sub.add_parser("config", help="Inspect and mutate .runtime-config.json")
    config_sub = config.add_subparsers(dest="config_cmd", required=True)

    config_show = config_sub.add_parser("show", help="Show current config (uses config_store.load_with_migration + diagnose)")
    config_show.add_argument("--config", default=".runtime-config.json")
    config_show.add_argument("--json", action="store_true")

    config_set = config_sub.add_parser("set", help="Set a top-level config field (worker-model / leader-model / active_provider / worker_agent)")
    config_set.add_argument("--config", default=".runtime-config.json")
    config_set.add_argument("field", choices=["worker-model", "leader-model", "active-provider", "worker-agent"])
    config_set.add_argument("value")
    config_set.add_argument("--json", action="store_true")

    config_add_provider = config_sub.add_parser("add-provider", help="Add a provider entry to the providers list")
    config_add_provider.add_argument("--config", default=".runtime-config.json")
    config_add_provider.add_argument("name")
    config_add_provider.add_argument("--base-url", required=True)
    config_add_provider.add_argument("--api-key", required=True)
    config_add_provider.add_argument("--protocol", default="anthropic", choices=["anthropic", "openai", "gemini", "custom"])
    config_add_provider.add_argument("--default-model", default="")
    config_add_provider.add_argument("--json", action="store_true")

    setting = sub.add_parser("setting", help="Manage persistent engine settings")
    setting_sub = setting.add_subparsers(dest="setting_cmd", required=True)
    setting_get = setting_sub.add_parser("get", help="Get a setting value")
    setting_get.add_argument("--db", default="data/collab.sqlite3")
    setting_get.add_argument("key")
    setting_set = setting_sub.add_parser("set", help="Set a setting value")
    setting_set.add_argument("--db", default="data/collab.sqlite3")
    setting_set.add_argument("key")
    setting_set.add_argument("value")
    setting_list = setting_sub.add_parser("list", help="List all settings")
    setting_list.add_argument("--db", default="data/collab.sqlite3")

    risk_policy = sub.add_parser("risk-policy", help="Show or update risk policy")
    risk_policy_sub = risk_policy.add_subparsers(dest="risk_policy_cmd", required=True)
    risk_policy_show = risk_policy_sub.add_parser("show", help="Show current risk policy")
    risk_policy_show.add_argument("--db", default="data/collab.sqlite3")
    risk_policy_set = risk_policy_sub.add_parser("set", help="Update risk policy fields")
    risk_policy_set.add_argument("--db", default="data/collab.sqlite3")
    risk_policy_set.add_argument("--low", type=_policy_action)
    risk_policy_set.add_argument("--medium", type=_policy_action)
    risk_policy_set.add_argument("--high", type=_policy_action)
    risk_policy_set.add_argument("--checkpoint-timeout", type=int)

    python_compat = sub.add_parser("python-compat", help="Check Python version feature compatibility (3.13+ feature detection)")
    python_compat.add_argument("--json", action="store_true")

    args = parser.parse_args()

    # ── Default: dialog mode (no subcommand given) ──────────
    if args.cmd is None:
        return _run_dialog(args)

    if args.cmd == "dialog":
        return _run_dialog(args)

    if args.cmd == "run":
        request = Path(args.request_file).read_text(encoding="utf-8") if args.request_file else " ".join(args.request)
        model, leader_model, worker_model = _model_options(args)
        # Load config store for agent/provider defaults (CLI args override config)
        _cfg_path = Path(args.cwd) / ".runtime-config.json"
        _cfg = {}
        if _cfg_path.exists():
            try:
                import json as _json
                _cfg = _json.loads(_cfg_path.read_text())
            except Exception:
                pass
        _cfg_agent = _cfg.get("worker_agent", "opencode")
        agent = args.agent or _cfg_agent
        provider = None
        if args.provider:
            provider = ProviderProfile(
                name=args.provider,
                protocol="custom",  # CLI-specified providers default to custom
                base_url=args.provider_base_url or "",
                api_key=args.provider_api_key or "",
                default_model=args.provider_model or model or "",
            )
        elif _cfg.get("active_provider"):
            # Load provider from config
            _providers = _cfg.get("providers", {})
            _act = _cfg["active_provider"]
            if _act in _providers:
                provider = ProviderProfile.from_dict({"name": _act, **_providers[_act]})
        engine = CollabEngine(
            args.db, args.cwd, model,
            leader_model=leader_model, worker_model=worker_model,
            agent=agent, provider=provider,
            leader_agent=args.leader_agent or _cfg.get("leader_agent", "hermes"),
            worker_agent=args.worker_agent or _cfg.get("worker_agent", agent),
            global_max_concurrent=getattr(args, 'global_max_concurrent', 4),
        )
        result = engine.run(
            request,
            title=args.title,
            concurrency=args.concurrency,
            timeout=args.timeout,
            max_retries=args.max_retries,
            split_count=args.split_count,
            aggregate=not args.no_aggregate,
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"Run: {result['run_id']} ok={result['ok']}")
            if result.get("aggregate"):
                print(result["aggregate"]["result"])
        return 0 if result["ok"] else 1

    if args.cmd == "server":
        model, leader_model, worker_model = _model_options(args)
        _cfg_path = Path(args.cwd) / ".runtime-config.json"
        _cfg = {}
        if _cfg_path.exists():
            try:
                import json as _json
                _cfg = _json.loads(_cfg_path.read_text())
            except Exception:
                pass
        _agent = args.agent or _cfg.get("worker_agent", "opencode")
        DashboardServer(args.host, args.port, args.db, args.cwd, model, leader_model=leader_model, worker_model=worker_model, agent=_agent).serve()
        return 0

    if args.cmd == "status":
        from .store import CollabStore
        store = CollabStore(args.db)
        data = {"overview": store.overview(), "runs": store.list_runs(10), "lessons": store.lessons(10)}
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "agents":
        from .agents import list_backends, detect_available_backends
        backends = detect_available_backends() if args.available else list_backends()
        if args.json:
            print(json.dumps([b.to_dict() for b in backends], ensure_ascii=False, indent=2))
        else:
            for b in backends:
                avail = "✓" if b.is_available() else "✗"
                print(f"  {avail} {b.name:16s} {b.display_name:20s} parser={b.output_parser}")
        return 0

    if args.cmd == "skills":
        from .skills import get_default_registry
        registry = get_default_registry()
        if args.node_type:
            skills = registry.select_for_node(args.node_type, args.task)
        else:
            skills = registry.list_all()
        if args.json:
            print(json.dumps([skill.to_dict() for skill in skills], ensure_ascii=False, indent=2))
        else:
            for skill in skills:
                node_types = ",".join(skill.applicable_node_types)
                print(f"  {skill.name:22s} p{skill.priority} {skill.category:12s} [{node_types}] {skill.display_name}")
        return 0

    if args.cmd == "tools":
        from .tools import get_default_tool_registry
        registry = get_default_tool_registry()
        if args.node_type:
            profiles = registry.select_for_node(args.node_type, args.task)
        else:
            profiles = registry.list_all()
        if args.json:
            print(json.dumps([profile.to_dict() for profile in profiles], ensure_ascii=False, indent=2))
        else:
            for profile in profiles:
                node_types = ",".join(profile.applicable_node_types)
                mcp = " mcp" if profile.mcp_tools else ""
                print(f"  {profile.name:22s} p{profile.priority} {profile.category:12s}{mcp:4s} [{node_types}] {profile.display_name}")
        return 0

    if args.cmd == "mcp-server":
        from .store import CollabStore
        from .registry import get_unified_registry
        store = CollabStore(args.db)
        registry = get_unified_registry(store=store)

        if args.mcp_server_cmd == "list":
            servers = registry.list_mcp_servers()
            if args.json:
                print(json.dumps(servers, ensure_ascii=False, indent=2))
            else:
                if not servers:
                    print("No MCP servers registered.")
                else:
                    print(f"MCP servers ({len(servers)}):")
                    for srv in servers:
                        tools_str = ", ".join(t["tool_name"] for t in srv["tools"])
                        print(f"  {srv['server_name']:24s} tools=[{tools_str}] endpoint={srv['endpoint']}")
            return 0

        if args.mcp_server_cmd == "add":
            import json as _json
            name = args.name
            command = args.command
            args_list = [a for a in args.args.split() if a] if args.args else []
            try:
                env = _json.loads(args.env) if isinstance(args.env, str) else args.env
                if not isinstance(env, dict):
                    env = {}
            except _json.JSONDecodeError:
                print(json.dumps({"ok": False, "error": "invalid --env JSON"}, ensure_ascii=False))
                return 2
            tools_list = [t.strip() for t in args.tools.split(",") if t.strip()] if args.tools else []
            try:
                capabilities = _json.loads(args.capabilities) if isinstance(args.capabilities, str) else args.capabilities
                if not isinstance(capabilities, list):
                    capabilities = ["*"]
            except _json.JSONDecodeError:
                print(json.dumps({"ok": False, "error": "invalid --capabilities JSON"}, ensure_ascii=False))
                return 2
            display_name = args.display_name or name
            existing = registry.list_mcp_servers()
            if any(s["server_name"] == name for s in existing):
                print(json.dumps({"ok": False, "error": f"MCP server {name!r} already exists"}, ensure_ascii=False))
                return 1
            created = registry.register_mcp_server(
                server_name=name,
                command=command,
                args=args_list,
                env=env,
                tools=tools_list,
                description=args.description,
                display_name=display_name,
                capabilities=capabilities,
                source="cli",
            )
            result = {"ok": True, "server_name": name, "tools": tools_list, "entries_created": len(created)}
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print(f"Registered MCP server {name!r} with {len(created)} tool entries")
            return 0

        if args.mcp_server_cmd == "remove":
            removed = registry.remove_mcp_server(args.name)
            if removed > 0:
                result = {"ok": True, "server_name": args.name, "entries_removed": removed}
                if args.json:
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                else:
                    print(f"Removed MCP server {args.name!r} ({removed} entries)")
                return 0
            else:
                result = {"ok": False, "error": f"MCP server {args.name!r} not found"}
                print(json.dumps(result, ensure_ascii=False, indent=2 if args.json else None))
                return 1

    if args.cmd in {"verify-v45", "verify-release"}:
        from .verification import verify_v45_capabilities
        report = verify_v45_capabilities()
        data = report.to_dict()
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            label = "release" if args.cmd == "verify-release" else "release (legacy verify-v45 alias)"
            print(f"{label} verification: {report.status}")
            for check in report.checks:
                marker = "✓" if check.status == "passed" else "✗"
                print(f"  {marker} {check.name}: {check.detail}")
            if report.skipped:
                print("Skipped:")
                for item in report.skipped:
                    print(f"  - {item}")
        return 0 if report.status == "ok" else 1

    if args.cmd == "lesson":
        from .store import CollabStore
        if args.lesson_cmd == "add":
            try:
                extra = _json_arg(args.evidence_json, "--evidence-json")
            except ValueError as exc:
                print(str(exc))
                return 2
            evidence = {"source": args.source, **extra, "scope": args.scope}
            store = CollabStore(args.db)
            store.add_lesson(args.category, args.lesson, evidence, scope=args.scope)
            print(json.dumps({"ok": True, "category": args.category, "scope": args.scope, "source": args.source}, ensure_ascii=False, separators=(",", ":")))
            return 0
        if args.lesson_cmd == "list":
            store = CollabStore(args.db)
            rows = store.lessons(args.limit, scope=args.scope)
            if args.category:
                rows = [r for r in rows if r["category"] == args.category]
            if args.json:
                print(json.dumps(rows, ensure_ascii=False, indent=2))
            else:
                for r in rows:
                    print(f"[{r['id']}] {r.get('scope', 'global')} {r['category']}: {r['lesson']}  ({r['created_at']})")
            return 0

    if args.cmd == "parent-log":
        from .store import CollabStore
        try:
            data = _json_arg(args.data_json, "--data-json")
        except ValueError as exc:
            print(str(exc))
            return 2
        store = CollabStore(args.db)
        store.log(args.run_id, args.level, args.message, {"source": "parent-log", **data}, args.node_id)
        result = {"ok": True, "run_id": args.run_id, "node_id": args.node_id, "level": args.level, "message": args.message}
        print(json.dumps(result, ensure_ascii=False, indent=2 if args.json else None))
        return 0

    if args.cmd == "snapshot":
        from .store import CollabStore
        store = CollabStore(args.db)
        result = store.load_run_state(args.run_id)
        _json_print({"ok": True, "snapshot": result}, pretty=True)
        return 0

    if args.cmd == "context-snapshot":
        from .store import CollabStore
        if not args.run_id:
            print("error: missing run_id")
            return 1
        try:
            store = CollabStore(args.db)
            result = store.load_context_snapshots(args.run_id, snapshot_type=args.snapshot_type)
            if args.latest:
                result = result[-1:] if result else []
            _json_print(result, pretty=True)
            return 0
        except Exception as exc:
            print(f"error: {exc}")
            return 1

    if args.cmd == "save-snapshot":
        engine = CollabEngine(args.db, args.cwd)
        try:
            snapshot = engine.build_context_snapshot(args.run_id)
            # Inject externally-provided decisions and user instructions
            if args.decisions:
                import json as _json
                snapshot["decisions"] = _json.loads(args.decisions)
            if args.user_instructions:
                import json as _json
                snapshot["user_instructions"] = _json.loads(args.user_instructions)
            engine.store.save_context_snapshot(args.run_id, args.snapshot_type, snapshot, args.node_id)
            result = {"ok": True, "run_id": args.run_id, "snapshot_type": args.snapshot_type, "node_id": args.node_id}
            _json_print(result, pretty=getattr(args, "json", False))
            return 0
        except Exception as exc:
            print(f"error: {exc}")
            return 1

    if args.cmd in {"pause-run", "resume-run", "redo-node"}:
        engine = CollabEngine(args.db, args.cwd, worker_model=getattr(args, "worker_model", None))
        try:
            if args.cmd == "pause-run":
                result = engine.pause_run(args.run_id, reason=args.reason)
            elif args.cmd == "resume-run":
                result = engine.resume_run(args.run_id, reason=args.reason)
            else:
                result = engine.redo_node(
                    run_id=args.run_id,
                    node_id=args.node_id,
                    reason=args.reason,
                    description_delta=args.description_delta,
                    cascade=args.cascade,
                    worker_model=args.worker_model,
                )
        except AttributeError as exc:
            result = {"ok": False, "error": str(exc), "command": args.cmd}
        except TypeError:
            if args.cmd == "pause-run":
                result = engine.pause_run(args.run_id)
            elif args.cmd == "resume-run":
                result = engine.resume_run(args.run_id)
            else:
                result = engine.redo_node(args.run_id, args.node_id, cascade=args.cascade, worker_model=args.worker_model)
        _json_print(result, pretty=True)
        return 0 if result.get("ok", True) else 1

    if args.cmd == "setting":
        from .store import CollabStore
        store = CollabStore(args.db)
        if args.setting_cmd == "get":
            value = store.get_setting(args.key)
            _json_print({"ok": True, "key": args.key, "value": value}, pretty=True)
            return 0
        if args.setting_cmd == "set":
            value = _setting_value(args.value)
            store.set_setting(args.key, value)
            _json_print({"ok": True, "key": args.key, "value": value}, pretty=True)
            return 0
        if args.setting_cmd == "list":
            _json_print({"ok": True, "settings": store.list_settings()}, pretty=True)
            return 0

    if args.cmd == "risk-policy":
        from .store import CollabStore
        store = CollabStore(args.db)
        if args.risk_policy_cmd == "show":
            _json_print({"ok": True, "risk_policy": store.load_risk_policy().to_dict()}, pretty=True)
            return 0
        if args.risk_policy_cmd == "set":
            existing = store.load_risk_policy().to_dict()
            updates = {key: value for key, value in {"low": args.low, "medium": args.medium, "high": args.high}.items() if value is not None}
            if args.checkpoint_timeout is not None:
                if args.checkpoint_timeout < 1:
                    _json_print({"ok": False, "error": "--checkpoint-timeout must be >= 1"}, pretty=True)
                    return 2
                updates["checkpoint_timeout"] = args.checkpoint_timeout
            policy = RiskPolicy.from_dict({**existing, **updates})
            store.set_setting("risk_policy", policy.to_dict())
            _json_print({"ok": True, "risk_policy": policy.to_dict()}, pretty=True)
            return 0

    if args.cmd in {"kill-node", "split-node", "skip-node"}:
        from .store import CollabStore
        store = CollabStore(args.db)
        row = store._one("SELECT * FROM wbs_nodes WHERE id=?", (args.node_id,))
        if row is None:
            print(json.dumps({"ok": False, "error": f"node not found: {args.node_id}"}, ensure_ascii=False))
            return 1
        run_id = args.run_id or row["run_id"]

        if args.cmd == "kill-node":
            from .agents import get_backend
            from .config_store import load_with_migration
            # Determine which agent was used for this run
            _cfg = load_with_migration(".runtime-config.json") if os.path.exists(".runtime-config.json") else {}
            _fallback_agent = _cfg.get("worker_agent", "opencode")
            agent_name = row.get("agent") if "agent" in row.keys() else _fallback_agent
            try:
                backend = get_backend(agent_name)
            except KeyError:
                backend = get_backend(_fallback_agent)
            patterns = [args.node_id, f"WBS node: {row['title']}"]
            pid_map: dict[int, str] = {}
            for pattern in patterns:
                proc = subprocess.run(["pgrep", "-af", pattern], text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
                for line in proc.stdout.splitlines():
                    try:
                        pid_text, cmdline = line.split(" ", 1)
                        pid = int(pid_text)
                    except ValueError:
                        continue
                    if pid == os.getpid() or "pgrep" in cmdline:
                        continue
                    if backend.command[0] in cmdline:
                        pid_map[pid] = cmdline
            sig = {"TERM": signal.SIGTERM, "KILL": signal.SIGKILL, "INT": signal.SIGINT}[args.signal]
            killed = []
            for pid in sorted(pid_map):
                try:
                    os.kill(pid, sig)
                    killed.append(pid)
                except ProcessLookupError:
                    pass
            store._execute("UPDATE workers SET status='failed', error=COALESCE(error, ?), updated_at=CURRENT_TIMESTAMP WHERE node_id=? AND status='running'", (args.reason, args.node_id))
            store.update_node(args.node_id, "failed", error=args.reason)
            store.log(run_id, "warning", "node killed by intervention", {"node": args.node_id, "reason": args.reason, "signal": args.signal, "pids": killed}, args.node_id)
            result = {"ok": bool(killed), "node_id": args.node_id, "run_id": run_id, "pids": killed}
            print(json.dumps(result, ensure_ascii=False, indent=2 if args.json else None))
            return 0 if killed else 1

        if args.cmd == "split-node":
            if args.split_count < 1:
                print(json.dumps({"ok": False, "error": "--split-count must be >= 1"}, ensure_ascii=False))
                return 2
            node = _node_from_row(row)
            engine = CollabEngine(args.db, ".")
            shards = engine._split_node(node, args.split_count)
            for shard in shards:
                engine.store.insert_wbs_node(run_id, shard.to_dict())
                engine.store.update_node(shard.id, "pending")
            engine.store.update_node(args.node_id, "split", result=f"Split into shards: {', '.join(s.id for s in shards)}")
            engine.store.log(run_id, "warning", "node split by intervention", {"node": args.node_id, "reason": args.reason, "shards": [s.id for s in shards]}, args.node_id)
            result = {"ok": True, "node_id": args.node_id, "run_id": run_id, "shards": [s.to_dict() for s in shards]}
            print(json.dumps(result, ensure_ascii=False, indent=2 if args.json else None))
            return 0

        if args.cmd == "skip-node":
            store.update_node(args.node_id, "failed", error=args.reason)
            store.log(run_id, "warning", "node skipped by intervention", {"node": args.node_id, "reason": args.reason}, args.node_id)
            result = {"ok": True, "node_id": args.node_id, "run_id": run_id, "status": "failed", "reason": args.reason}
            print(json.dumps(result, ensure_ascii=False, indent=2 if args.json else None))
            return 0

    # ------------------------------------------------------------------
    # python-compat — Python 3.13+ feature detection
    # ------------------------------------------------------------------
    if args.cmd == "python-compat":
        from .pycompat import check_all, summary_lines

        report = check_all()
        if args.json:
            print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        else:
            for line in summary_lines(report):
                print(line)
        return 0

    # ------------------------------------------------------------------
    # doctor + config — config_store driven diagnostics and mutation
    # ------------------------------------------------------------------
    if args.cmd == "doctor":
        from .config_store import diagnose, mask_token, load_with_migration
        cfg_path = Path(args.config)
        if not cfg_path.is_absolute():
            cfg_path = Path.cwd() / cfg_path
        diag = diagnose(cfg_path)
        loaded = load_with_migration(cfg_path) if diag["valid_json"] else {}
        # Augment with token-masked api_key (so user can audit without leaking)
        masked = {}
        for prov in loaded.get("providers", []) if isinstance(loaded.get("providers"), list) else []:
            if isinstance(prov, dict) and "api_key" in prov:
                pname = prov.get("name", "?")
                masked[pname] = mask_token(prov.get("api_key", ""))
        # Backup count
        backup_dir = cfg_path.parent / ".backups"
        backup_count = 0
        if backup_dir.is_dir():
            backup_count = sum(1 for _ in backup_dir.glob(f"{cfg_path.name}.*.bak"))
        report = {
            **diag,
            "loaded_keys": sorted(list(loaded.keys())) if isinstance(loaded, dict) else [],
            "worker_model": loaded.get("worker_model"),
            "leader_model": loaded.get("leader_model"),
            "active_provider": loaded.get("active_provider"),
            "worker_agent": loaded.get("worker_agent"),
            "fallback_chain": loaded.get("fallback_chain", []),
            "provider_count": diag.get("provider_count", 0),
            "provider_keys_masked": masked,
            "backup_count": backup_count,
        }
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            ok = "✓" if diag["valid_json"] and not diag["errors"] else "✗"
            print(f"{ok} {cfg_path}")
            print(f"  exists       : {diag['exists']}")
            print(f"  permissions  : {diag['permissions'] or '-'}")
            print(f"  size         : {diag['size_bytes']} bytes")
            print(f"  valid_json   : {diag['valid_json']}")
            if diag["errors"]:
                print(f"  errors       : {diag['errors']}")
            print(f"  worker_model : {loaded.get('worker_model', '-')}")
            print(f"  leader_model : {loaded.get('leader_model', '-')}")
            print(f"  worker_agent : {loaded.get('worker_agent', '-')}")
            print(f"  active       : {loaded.get('active_provider', '-')}")
            print(f"  fallback     : {loaded.get('fallback_chain', [])}")
            print(f"  providers    : {diag['provider_count']} (keys masked: {len(masked)})")
            if masked:
                for n, m in masked.items():
                    print(f"    {n}: {m}")
            print(f"  backups      : {backup_count}")
        return 0 if diag["valid_json"] and not diag["errors"] else 1

    if args.cmd == "config":
        from .config_store import load_with_migration, save_with_backup, mask_token
        cfg_path = Path(args.config)
        if not cfg_path.is_absolute():
            cfg_path = Path.cwd() / cfg_path

        if args.config_cmd == "show":
            data = load_with_migration(cfg_path)
            # Mask any api_key fields we find at the top level or inside providers
            if isinstance(data, dict):
                if "api_key" in data and isinstance(data["api_key"], str):
                    data["api_key_masked"] = mask_token(data["api_key"])
                providers = data.get("providers")
                if isinstance(providers, list):
                    masked_list = []
                    for p in providers:
                        if isinstance(p, dict) and isinstance(p.get("api_key"), str):
                            p = dict(p)
                            p["api_key_masked"] = mask_token(p["api_key"])
                        masked_list.append(p)
                    data["providers"] = masked_list
            if args.json:
                print(json.dumps(data, ensure_ascii=False, indent=2))
            else:
                print(json.dumps(data, ensure_ascii=False, indent=2))
            return 0

        if args.config_cmd == "set":
            data = load_with_migration(cfg_path)
            key_map = {
                "worker-model": "worker_model",
                "leader-model": "leader_model",
                "active-provider": "active_provider",
                "worker-agent": "worker_agent",
            }
            target = key_map[args.field]
            old = data.get(target)
            data[target] = args.value
            backup = save_with_backup(cfg_path, data)
            result = {"ok": True, "field": target, "old": old, "new": args.value, "backup": str(backup) if backup else None}
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print(f"set {target}: {old!r} -> {args.value!r}")
                if backup:
                    print(f"backup saved: {backup}")
            return 0

        if args.config_cmd == "add-provider":
            data = load_with_migration(cfg_path)
            providers = data.get("providers")
            if not isinstance(providers, list):
                providers = []
            # Remove any existing entry with same name (overwrite semantics)
            providers = [p for p in providers if not (isinstance(p, dict) and p.get("name") == args.name)]
            new_prov = {
                "name": args.name,
                "protocol": args.protocol,
                "base_url": args.base_url,
                "api_key": args.api_key,
            }
            if args.default_model:
                new_prov["default_model"] = args.default_model
            providers.append(new_prov)
            data["providers"] = providers
            if not data.get("active_provider"):
                data["active_provider"] = args.name
            backup = save_with_backup(cfg_path, data)
            masked_view = dict(new_prov)
            masked_view["api_key"] = mask_token(args.api_key)
            result = {"ok": True, "provider": masked_view, "total_providers": len(providers), "active_provider": data.get("active_provider"), "backup": str(backup) if backup else None}
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print(f"added provider {args.name!r} ({args.protocol})")
                print(f"  base_url     : {args.base_url}")
                print(f"  api_key      : {mask_token(args.api_key)}")
                print(f"  total        : {len(providers)}")
                if backup:
                    print(f"  backup       : {backup}")
            return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
