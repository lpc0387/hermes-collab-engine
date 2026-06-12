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
from .server import DashboardServer

LESSON_SCOPES = ("global", "project", "run", "node", "wbs-family")
RISK_POLICY_ACTIONS = {"auto", "notify", "pause"}


def _model_options(args):
    model = args.model or os.environ.get("HERMES_COLLAB_MODEL") or os.environ.get("ANTHROPIC_MODEL")
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
    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="Run a collaboration task")
    run.add_argument("request", nargs="*", help="Task request text")
    run.add_argument("--request-file", help="Read request from file")
    run.add_argument("--title")
    run.add_argument("--cwd", default=".")
    run.add_argument("--db", default="data/collab.sqlite3")
    run.add_argument("--model", help="Use the same model for leader and workers")
    run.add_argument("--leader-model", help="Leader brain model for planning and aggregation")
    run.add_argument("--worker-model", help="Worker brain model for coding workers")
    run.add_argument("--agent", default="claude-code", help="Agent backend: claude-code (default), codex, opencode, or custom")
    run.add_argument("--concurrency", type=int, default=4)
    run.add_argument("--timeout", type=int, default=900)
    run.add_argument("--max-retries", type=int, default=2)
    run.add_argument("--split-count", type=int, default=4)
    run.add_argument("--no-aggregate", action="store_true")
    run.add_argument("--json", action="store_true")

    server = sub.add_parser("server", help="Run management dashboard")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8765)
    server.add_argument("--cwd", default=".")
    server.add_argument("--db", default="data/collab.sqlite3")
    server.add_argument("--model", help="Use the same model for leader and workers")
    server.add_argument("--leader-model", help="Leader brain model for planning and aggregation")
    server.add_argument("--worker-model", help="Worker brain model for coding workers")
    server.add_argument("--agent", default="claude-code", help="Agent backend: claude-code (default), codex, opencode, or custom")

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

    verify_v45 = sub.add_parser("verify-v45", help="Run local checks for v4.5 skill/tool/dashboard features")
    verify_v45.add_argument("--json", action="store_true")

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

    args = parser.parse_args()
    if args.cmd == "run":
        request = Path(args.request_file).read_text(encoding="utf-8") if args.request_file else " ".join(args.request)
        model, leader_model, worker_model = _model_options(args)
        engine = CollabEngine(args.db, args.cwd, model, leader_model=leader_model, worker_model=worker_model, agent=args.agent)
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
        DashboardServer(args.host, args.port, args.db, args.cwd, model, leader_model=leader_model, worker_model=worker_model, agent=args.agent).serve()
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

    if args.cmd == "verify-v45":
        from .verification import verify_v45_capabilities
        report = verify_v45_capabilities()
        data = report.to_dict()
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            print(f"v4.5 verification: {report.status}")
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
            # Determine which agent was used for this run
            agent_name = row.get("agent") if "agent" in row.keys() else "claude-code"
            try:
                backend = get_backend(agent_name)
            except KeyError:
                backend = get_backend("claude-code")
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

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
