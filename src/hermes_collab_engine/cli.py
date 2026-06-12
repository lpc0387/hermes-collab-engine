from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
from pathlib import Path

from .engine import CollabEngine
from .models import WBSNode
from .server import DashboardServer

LESSON_SCOPES = ("global", "project", "run", "node", "wbs-family")


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
    run.add_argument("--worker-model", help="Worker brain model for Claude Code workers")
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
    server.add_argument("--worker-model", help="Worker brain model for Claude Code workers")

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

    args = parser.parse_args()
    if args.cmd == "run":
        request = Path(args.request_file).read_text(encoding="utf-8") if args.request_file else " ".join(args.request)
        model, leader_model, worker_model = _model_options(args)
        engine = CollabEngine(args.db, args.cwd, model, leader_model=leader_model, worker_model=worker_model)
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
        DashboardServer(args.host, args.port, args.db, args.cwd, model, leader_model=leader_model, worker_model=worker_model).serve()
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

    if args.cmd in {"kill-node", "split-node", "skip-node"}:
        from .store import CollabStore
        store = CollabStore(args.db)
        row = store._one("SELECT * FROM wbs_nodes WHERE id=?", (args.node_id,))
        if row is None:
            print(json.dumps({"ok": False, "error": f"node not found: {args.node_id}"}, ensure_ascii=False))
            return 1
        run_id = args.run_id or row["run_id"]

        if args.cmd == "kill-node":
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
                    if "claude" in cmdline and "--output-format" in cmdline:
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
