#!/usr/bin/env python3
"""Standalone sandbox dashboard server with mock Hermes data."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
INDEX_HTML = ROOT / "index.html"

DATA_SOURCE = "memory"
LESSON_COUNT = 3
DEFAULT_TTL_SECONDS = 2 * 60 * 60
SANDBOX_MARKER_FILENAME = ".hermes-collab-sandbox-workspace"
SANDBOX_CONFIG: dict = {}
REAL_EXECUTION = False
REAL_RUNS_USED = 0
REAL_RUN_LOCK = threading.Lock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


RUNS: list[dict] = [
    {
        "id": "sandbox-demo-001",
        "title": "沙盒演示：页面克隆与隔离验证",
        "status": "running",
        "created_at": "2026-06-12T09:00:00+00:00",
    },
    {
        "id": "sandbox-demo-000",
        "title": "沙盒演示：历史运行样例",
        "status": "completed",
        "created_at": "2026-06-12T08:30:00+00:00",
    },
]

RUN_DETAILS: dict[str, dict] = {
    "sandbox-demo-001": {
        "id": "sandbox-demo-001",
        "title": "沙盒演示：页面克隆与隔离验证",
        "status": "running",
        "created_at": "2026-06-12T09:00:00+00:00",
        "nodes": [
            {
                "id": "wbs-sandbox-1",
                "title": "分析现有页面结构",
                "capability": "analysis",
                "status": "completed",
                "deliverable": "页面结构说明",
                "brief": "读取生产页面，仅复制演示需要的最小集合。",
                "skills": ["debug-root-cause"],
                "tools": ["file-edit"],
                "result": "识别 dashboard HTML、API 列表、运行详情和 SSE 更新链路。",
            },
            {
                "id": "wbs-sandbox-2",
                "title": "复制前端到沙盒目录",
                "capability": "implementation",
                "status": "completed",
                "deliverable": "sandbox/index.html",
                "brief": "保留页面视觉效果，替换品牌文案为沙盒演示。",
                "skills": ["implementation-focus"],
                "tools": ["file-edit"],
                "result": "沙盒页面使用本地 mock API，不读取生产数据库。",
            },
            {
                "id": "wbs-sandbox-3",
                "title": "连接 Mock 后端入口",
                "capability": "implementation",
                "status": "running",
                "deliverable": "sandbox/server.py",
                "brief": "提供演示所需的最小 API：overview、runs、skills、tools、events。",
                "skills": ["implementation-focus", "test-verify"],
                "tools": ["python-tests"],
                "result": "POST /api/runs 仅创建内存演示记录，不启动 worker。",
            },
            {
                "id": "wbs-sandbox-4",
                "title": "验证本地构建运行",
                "capability": "verification",
                "status": "planning",
                "deliverable": "本地检查结果",
                "brief": "执行语法检查并说明启动方式。",
                "skills": ["test-verify"],
                "tools": ["python-tests"],
            },
        ],
        "logs": [
            {"timestamp": "2026-06-12T09:00:01+00:00", "level": "info", "node_id": "wbs-sandbox-1", "message": "sandbox analysis started", "data": {"source": "mock"}},
            {"timestamp": "2026-06-12T09:01:12+00:00", "level": "info", "node_id": "wbs-sandbox-2", "message": "frontend cloned into isolated directory", "data": {"path": "sandbox/index.html"}},
            {"timestamp": "2026-06-12T09:02:20+00:00", "level": "warning", "node_id": "wbs-sandbox-3", "message": "production task execution disabled in sandbox", "data": {"post_runs": "mock-only"}},
            {"timestamp": "2026-06-12T09:03:05+00:00", "level": "info", "node_id": "wbs-sandbox-3", "message": "worker skills selected", "data": {"skills": ["implementation-focus", "test-verify"]}},
            {"timestamp": "2026-06-12T09:03:06+00:00", "level": "info", "node_id": "wbs-sandbox-3", "message": "worker tool profiles selected", "data": {"profiles": ["file-edit", "python-tests"]}},
        ],
    },
    "sandbox-demo-000": {
        "id": "sandbox-demo-000",
        "title": "沙盒演示：历史运行样例",
        "status": "completed",
        "created_at": "2026-06-12T08:30:00+00:00",
        "nodes": [
            {"id": "wbs-history-1", "title": "准备演示数据", "capability": "implementation", "status": "completed", "deliverable": "Mock 数据", "skills": ["implementation-focus"], "tools": ["file-edit"]},
            {"id": "wbs-history-2", "title": "检查隔离策略", "capability": "verification", "status": "completed", "deliverable": "隔离检查", "skills": ["test-verify"], "tools": ["python-tests"]},
        ],
        "logs": [
            {"timestamp": "2026-06-12T08:30:00+00:00", "level": "info", "node_id": "wbs-history-1", "message": "demo data loaded", "data": {"source": "mock"}},
            {"timestamp": "2026-06-12T08:31:00+00:00", "level": "info", "node_id": "wbs-history-2", "message": "sandbox isolation verified", "data": {"db": "not used"}},
        ],
    },
}

def skills_payload(node_type: str = "", task: str = "") -> list[dict]:
    from hermes_collab_engine.skills import get_default_registry

    registry = get_default_registry()
    skills = registry.select_for_node(node_type, task) if node_type else registry.list_all()
    return [skill.to_dict() for skill in skills]



def tools_payload(node_type: str = "", task: str = "") -> list[dict]:
    from hermes_collab_engine.tools import get_default_tool_registry

    registry = get_default_tool_registry()
    profiles = registry.select_for_node(node_type, task) if node_type else registry.list_all()
    return [profile.to_dict() for profile in profiles]


def normalize_base_path(value: str | None) -> str:
    if not value or value == "/":
        return ""
    return "/" + value.strip("/")


def public_url(host: str, port: int, base_path: str, configured: str | None) -> str:
    if configured:
        return configured.rstrip("/") + (base_path or "/")
    return f"http://{host}:{port}{base_path or '/'}"


def _loads(value: str | None, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def load_db_snapshot(db_path: Path) -> bool:
    global DATA_SOURCE, LESSON_COUNT
    if not db_path.exists():
        return False

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        runs = [dict(row) for row in conn.execute("SELECT id,title,status,created_at FROM runs ORDER BY created_at DESC").fetchall()]
        details: dict[str, dict] = {}
        for run in runs:
            nodes = []
            for row in conn.execute(
                """SELECT id,title,description,capability,deliverable,status,result,brief,skills_json,tools_json
                FROM wbs_nodes WHERE run_id=? ORDER BY created_at,id""",
                (run["id"],),
            ).fetchall():
                node = dict(row)
                node["skills"] = _loads(node.pop("skills_json", None), [])
                node["tools"] = _loads(node.pop("tools_json", None), [])
                nodes.append(node)
            logs = []
            for row in conn.execute(
                "SELECT created_at AS timestamp,level,node_id,message,data_json FROM logs WHERE run_id=? ORDER BY created_at,id",
                (run["id"],),
            ).fetchall():
                log = dict(row)
                log["data"] = _loads(log.pop("data_json", None), {})
                logs.append(log)
            details[run["id"]] = {**run, "nodes": nodes, "logs": logs}

        if not runs:
            return False
        RUNS[:] = runs
        RUN_DETAILS.clear()
        RUN_DETAILS.update(details)
        LESSON_COUNT = conn.execute("SELECT COUNT(*) FROM lessons").fetchone()[0]
        DATA_SOURCE = f"sqlite:{db_path}"
        return True
    finally:
        conn.close()


def load_mock_config(path: Path | None) -> dict:
    if not path or not path.exists():
        return {"services": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    services = []
    for service in data.get("services", []):
        services.append({"name": service.get("name"), "kind": service.get("kind"), "base_url": service.get("base_url")})
    return {"path": str(path), "services": services, "egress_policy": data.get("egress_policy", {})}


def overview() -> dict:
    running = sum(1 for run in RUNS if run["status"] == "running")
    latest_nodes = RUN_DETAILS.get(RUNS[0]["id"], {}).get("nodes", []) if RUNS else []
    workers = sum(1 for node in latest_nodes if node.get("status") == "running")
    return {"runs": len(RUNS), "running": running, "workers_running": workers, "lessons": LESSON_COUNT, "sandbox": True, "data_source": DATA_SOURCE}



def compact_title(text: str, limit: int = 80) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text or "沙盒任务"
    return text[: max(1, limit - 1)].rstrip() + "…"



def config_payload() -> dict:
    env_model = os.environ.get("HERMES_SANDBOX_MODEL") or os.environ.get("HERMES_COLLAB_MODEL") or os.environ.get("ANTHROPIC_MODEL")
    leader_model = os.environ.get("HERMES_SANDBOX_LEADER_MODEL") or os.environ.get("HERMES_COLLAB_LEADER_MODEL") or env_model
    worker_model = os.environ.get("HERMES_SANDBOX_WORKER_MODEL") or os.environ.get("HERMES_COLLAB_WORKER_MODEL") or env_model
    return {
        "model": env_model,
        "leader_model": leader_model,
        "worker_model": worker_model,
        "effective_leader_model": leader_model,
        "effective_worker_model": worker_model,
        "model_overrides_readonly": True,
        "agent": os.environ.get("HERMES_SANDBOX_AGENT", "claude-code"),
        "sandbox": True,
        "real_execution": REAL_EXECUTION,
    }



def _token_totals(logs: list[dict]) -> dict:
    totals = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    for log in logs:
        data = log.get("data") or _loads(log.get("data_json"), {})
        usage = data.get("usage") if isinstance(data, dict) and isinstance(data.get("usage"), dict) else data
        if not isinstance(usage, dict):
            continue
        for key in totals:
            try:
                totals[key] += int(usage.get(key, 0) or 0)
            except (TypeError, ValueError):
                pass
    return totals



def task_set_for_run(run_id: str) -> dict:
    detail = RUN_DETAILS.get(run_id, {})
    nodes = detail.get("nodes", [])
    logs = detail.get("logs", [])
    counts = {"total": 0, "pending": 0, "planning": 0, "running": 0, "completed": 0, "failed": 0, "skipped": 0}
    for node in nodes:
        status = str(node.get("status") or "pending")
        counts["total"] += 1
        counts[status] = counts.get(status, 0) + 1
    kills = []
    for log in logs:
        message = str(log.get("message") or "")
        if "duplicate" in message.lower() or "kill" in message.lower() or "killed" in message.lower():
            kills.append({"created_at": log.get("timestamp") or log.get("created_at"), "node_id": log.get("node_id"), "level": log.get("level"), "message": message, "data": log.get("data") or {}})
    return {
        "counts": counts,
        "workers": {"total": len(nodes), "running": sum(1 for node in nodes if node.get("status") == "running")},
        "tokens": {**_token_totals(logs), "source": "sandbox event logs"},
        "dedup_kills": kills[:20],
        "sandbox": True,
    }



def task_sets(limit: int = 20) -> list[dict]:
    return [{"run": run, **task_set_for_run(run["id"])} for run in RUNS[:limit]]



def resume_context(run_id: str | None = None, *, node_limit: int = 4, log_limit: int = 8) -> dict | None:
    if not RUNS:
        return None
    run = next((item for item in RUNS if item.get("id") == run_id), RUNS[0] if run_id is None else None)
    if not run:
        return None
    detail = RUN_DETAILS.get(run["id"], {})
    nodes = []
    for node in detail.get("nodes", [])[-node_limit:]:
        result = str(node.get("result") or node.get("brief") or "")
        nodes.append({
            "id": node.get("id"),
            "title": node.get("title"),
            "status": node.get("status"),
            "result_excerpt": result[:800],
        })
    logs = detail.get("logs", [])[-log_limit:]
    summary_lines = [
        f"Previous sandbox run {run['id']} ({run.get('status', 'unknown')}): {run.get('title', '')}",
    ]
    for node in nodes:
        summary_lines.append(f"- {node['id']} {node['status']}: {node['title']} — {node['result_excerpt'][:300]}")
    summary = "\n".join(summary_lines)
    return {
        "run": run,
        "summary": summary,
        "recent_interactions": logs,
        "estimated_tokens": max(1, (len(summary) + sum(len(str(item)) for item in logs)) // 4),
        "limits": {"nodes": node_limit, "logs": log_limit, "result_excerpt_chars": 800},
        "sandbox": True,
    }



def resume_prompt(request: str, run_id: str | None = None) -> tuple[str, dict | None]:
    context = resume_context(run_id)
    if not context:
        return request, None
    interactions = "\n".join(
        f"- {item.get('timestamp') or item.get('created_at', '')} {item.get('level', '')} {item.get('node_id') or ''}: {item.get('message', '')}"
        for item in context["recent_interactions"][-8:]
    )
    prompt = (
        "Sandbox session resume context (bounded summary only; production context is not loaded):\n"
        f"{context['summary']}\n\nRecent interactions:\n{interactions}\n\n"
        f"New user request:\n{request}"
    )
    return prompt, context


def add_demo_run(request: str) -> dict:
    run_id = f"sandbox-demo-{len(RUNS) + 1:03d}"
    created = now_iso()
    run = {"id": run_id, "title": compact_title(request), "status": "running", "created_at": created}
    detail = {
        **run,
        "nodes": [
            {"id": f"{run_id}-1", "title": "接收沙盒任务", "capability": "implementation", "status": "completed", "deliverable": "内存演示记录", "brief": request, "skills": ["implementation-focus"], "tools": ["file-edit"]},
            {"id": f"{run_id}-2", "title": "模拟协同执行", "capability": "verification", "status": "running", "deliverable": "Mock 运行状态", "brief": "沙盒不会启动真实 worker。", "skills": ["test-verify"], "tools": ["python-tests"]},
            {"id": f"{run_id}-3", "title": "输出演示结果", "capability": "verification", "status": "planning", "deliverable": "演示页面更新", "brief": "用于展示页面交互，不写入生产数据。", "skills": ["test-verify"], "tools": ["python-tests"]},
        ],
        "logs": [
            {"timestamp": created, "level": "info", "node_id": f"{run_id}-1", "message": "sandbox run accepted", "data": {"mock_only": True}},
            {"timestamp": created, "level": "warning", "node_id": f"{run_id}-2", "message": "real worker execution is disabled", "data": {"isolation": "sandbox"}},
        ],
    }
    RUNS.insert(0, run)
    RUN_DETAILS[run_id] = detail
    return {"accepted": True, "run_id": run_id, "sandbox": True}


def _sandbox_marker_path(workspace: Path) -> Path:
    return workspace / SANDBOX_MARKER_FILENAME


def _write_sandbox_marker(workspace: Path) -> None:
    marker = {
        "kind": "hermes-collab-sandbox-workspace",
        "repo_root": str(REPO_ROOT.resolve()),
        "workspace": str(workspace.resolve()),
        "pid": os.getpid(),
        "created_at": now_iso(),
    }
    _sandbox_marker_path(workspace).write_text(json.dumps(marker, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_sandbox_marker(workspace: Path) -> dict | None:
    marker_path = _sandbox_marker_path(workspace)
    if not marker_path.is_file():
        return None
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(marker, dict):
        return None
    return marker


def _sandbox_marker_matches_current_process(workspace: Path) -> bool:
    marker = _read_sandbox_marker(workspace)
    return bool(
        marker
        and marker.get("kind") == "hermes-collab-sandbox-workspace"
        and marker.get("repo_root") == str(REPO_ROOT.resolve())
        and marker.get("workspace") == str(workspace.resolve())
        and marker.get("pid") == os.getpid()
    )


def _is_protected_workspace_path(workspace: Path) -> bool:
    workspace = workspace.resolve()
    protected = {Path("/").resolve(), Path.home().resolve(), REPO_ROOT.resolve()}
    return workspace in protected


def _safe_copytree(src: Path, dst: Path) -> bool:
    dst = dst.resolve()
    if _is_protected_workspace_path(dst):
        raise RuntimeError(f"refusing protected sandbox workspace path: {dst}")
    if dst.exists():
        raise RuntimeError(f"refusing existing sandbox workspace; choose an empty path: {dst}")
    ignore = shutil.ignore_patterns(".git", "data", "logs", "__pycache__", ".pytest_cache", "*.pyc")
    shutil.copytree(src, dst, ignore=ignore)
    _write_sandbox_marker(dst)
    return True


def _cleanup_sandbox_workspace(workspace: Path) -> None:
    workspace = workspace.resolve()
    marker = _sandbox_marker_path(workspace)
    if not _sandbox_marker_matches_current_process(workspace):
        print(f"Hermes sandbox workspace cleanup skipped; current-run marker missing: {marker}", flush=True)
        return
    if _is_protected_workspace_path(workspace):
        print(f"Hermes sandbox workspace cleanup skipped; protected path: {workspace}", flush=True)
        return
    if not workspace.is_dir() or not any(workspace.iterdir()):
        print(f"Hermes sandbox workspace cleanup skipped; empty or missing path: {workspace}", flush=True)
        return
    shutil.rmtree(workspace)
    print(f"Hermes sandbox workspace removed: {workspace}", flush=True)


def _install_shutdown_handlers(httpd: ThreadingHTTPServer) -> None:
    def handle_shutdown(signum, _frame) -> None:
        print(f"Hermes sandbox received signal {signum}; shutting down", flush=True)
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)


def _refresh_from_store(store, run_id: str, alias_run_id: str | None = None) -> None:
    """Refresh in-memory sandbox API state from the real engine store.

    `alias_run_id` keeps the browser-facing placeholder ID alive while the
    underlying CollabEngine run has a different real run_id. This lets the
    sandbox UI show real WBS nodes/logs during execution instead of a static
    "启动隔离真实执行" placeholder until the run fully completes.
    """
    global LESSON_COUNT
    detail = store.run_detail(run_id)
    run = detail.get("run") or {}
    if not run:
        return
    nodes = []
    for node in detail.get("nodes", []):
        n = dict(node)
        n["skills"] = _loads(n.pop("skills_json", None), [])
        n["tools"] = _loads(n.pop("tools_json", None), [])
        nodes.append(n)
    logs = []
    for log in detail.get("logs", []):
        l = dict(log)
        l["timestamp"] = l.get("created_at") or l.get("timestamp")
        l["data"] = _loads(l.pop("data_json", None), {})
        logs.append(l)

    rendered = {**run, "nodes": nodes, "logs": logs, "real_run_id": run_id}
    RUN_DETAILS[run_id] = rendered

    summary = {k: run.get(k) for k in ("id", "title", "status", "created_at")}
    summary["real_run_id"] = run_id
    if not alias_run_id:
        existing = next((r for r in RUNS if r.get("id") == run_id), None)
        if existing:
            existing.update(summary)
        else:
            RUNS.insert(0, summary)

    if alias_run_id and alias_run_id != run_id:
        alias_detail = {**rendered, "id": alias_run_id, "real_run_id": run_id}
        RUN_DETAILS[alias_run_id] = alias_detail
        alias_summary = {**summary, "id": alias_run_id, "real_run_id": run_id}
        alias_existing = next((r for r in RUNS if r.get("id") == alias_run_id), None)
        if alias_existing:
            alias_existing.update(alias_summary)
        else:
            RUNS.insert(0, alias_summary)

    RUNS.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    try:
        LESSON_COUNT = len(store.lessons())
    except Exception:
        pass


def _latest_real_run_id(db_path: Path, title: str) -> str | None:
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1)
        try:
            row = conn.execute(
                "SELECT id FROM runs WHERE title=? ORDER BY created_at DESC LIMIT 1",
                (title,),
            ).fetchone()
            return row[0] if row else None
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def _run_real_sandbox_task(run_id: str, request: str, db_path: Path, cwd: Path, display_request: str | None = None) -> None:
    try:
        from hermes_collab_engine.engine import CollabEngine
        engine = CollabEngine(
            db_path=db_path,
            cwd=cwd,
            leader_model=os.environ.get("HERMES_SANDBOX_LEADER_MODEL") or os.environ.get("HERMES_COLLAB_LEADER_MODEL"),
            worker_model=os.environ.get("HERMES_SANDBOX_WORKER_MODEL") or os.environ.get("HERMES_COLLAB_WORKER_MODEL"),
            agent=os.environ.get("HERMES_SANDBOX_AGENT", "claude-code"),
        )
        display_title = (display_request or request).strip()
        title = "沙盒真实任务：" + display_title[:60]
        outcome: dict = {"result": None, "error": None}

        def execute() -> None:
            try:
                outcome["result"] = engine.run(
                    request,
                    title=title,
                    concurrency=max(1, env_int("HERMES_SANDBOX_CONCURRENCY", 1)),
                    timeout=max(30, env_int("HERMES_SANDBOX_TIMEOUT", 240)),
                    max_retries=max(0, env_int("HERMES_SANDBOX_MAX_RETRIES", 0)),
                    split_count=max(2, env_int("HERMES_SANDBOX_SPLIT_COUNT", 2)),
                    aggregate=os.environ.get("HERMES_SANDBOX_AGGREGATE", "1") == "1",
                )
            except Exception as exc:  # pragma: no cover - surfaced after join
                outcome["error"] = exc

        worker = threading.Thread(target=execute, daemon=True)
        worker.start()
        real_run_id: str | None = None
        while worker.is_alive():
            if not real_run_id:
                real_run_id = _latest_real_run_id(db_path, title)
            if real_run_id:
                _refresh_from_store(engine.store, real_run_id, alias_run_id=run_id)
            worker.join(timeout=2)

        if outcome["error"]:
            raise outcome["error"]
        result = outcome["result"] or {}
        real_run_id = result.get("run_id") or real_run_id
        if real_run_id:
            _refresh_from_store(engine.store, real_run_id, alias_run_id=run_id)
    except Exception as exc:
        created = now_iso()
        detail = RUN_DETAILS.get(run_id, {})
        detail["status"] = "failed"
        detail.setdefault("logs", []).append({"timestamp": created, "level": "error", "node_id": "sandbox-real", "message": "sandbox real execution failed", "data": {"error": f"{type(exc).__name__}: {exc}"}})
        RUN_DETAILS[run_id] = detail
        for run in RUNS:
            if run.get("id") == run_id:
                run["status"] = "failed"
                break


def _reset_sandbox_state(db_path: Path) -> None:
    """On startup, wipe all run data from the sandbox DB.

    Skills, MCP, and agents come from code registries (read-only) and are
    not affected.  This ensures each sandbox session starts completely fresh:
    no stale runs, no resume context, no orphan workers.
    """
    global REAL_RUNS_USED
    REAL_RUNS_USED = 0
    RUNS.clear()
    RUN_DETAILS.clear()
    try:
        conn = sqlite3.connect(str(db_path), timeout=1)
        for table in ("runs", "wbs_nodes", "workers", "logs",
                       "node_results", "run_state", "context_snapshots", "metrics"):
            conn.execute(f"DELETE FROM [{table}]")
        conn.commit()
        conn.close()
    except Exception:
        pass


def add_real_sandbox_run(request: str) -> dict:
    global REAL_RUNS_USED
    max_runs = max(0, env_int("HERMES_SANDBOX_REAL_RUN_LIMIT", 5))
    with REAL_RUN_LOCK:
        if REAL_RUNS_USED >= max_runs:
            return {"accepted": False, "sandbox": True, "error": f"沙盒真实任务额度已用完（{REAL_RUNS_USED}/{max_runs}）"}
        REAL_RUNS_USED += 1
        used = REAL_RUNS_USED
        SANDBOX_CONFIG.setdefault("quota", {})["used"] = used

    created = now_iso()
    placeholder_id = f"sandbox-real-pending-{used:03d}"
    db_path = Path(SANDBOX_CONFIG["database"]["path"]).resolve()
    cwd = Path(SANDBOX_CONFIG["workspace"]["path"]).resolve()
    run = {"id": placeholder_id, "title": "沙盒真实任务：" + request[:60], "status": "running", "created_at": created}
    detail = {
        **run,
        "nodes": [{"id": "sandbox-real", "title": "启动隔离真实执行", "capability": "implementation", "status": "running", "deliverable": str(cwd), "brief": request, "skills": ["implementation-focus"], "tools": ["file-edit", "python-tests"]}],
        "logs": [{"timestamp": created, "level": "warning", "node_id": "sandbox-real", "message": "real sandbox execution accepted", "data": {"isolated_db": str(db_path), "isolated_cwd": str(cwd), "quota_used": used, "quota_limit": max_runs}}],
    }
    RUNS.insert(0, run)
    RUN_DETAILS[placeholder_id] = detail
    isolated_request = (
        "[SANDBOX REAL EXECUTION — ISOLATION REQUIRED]\n"
        f"Work only inside this isolated sandbox workspace: {cwd}\n"
        "Do not read from or write to the production repository at /root/hermes-collab-engine, "
        "except for this sandbox workspace path if it is nested under it. "
        "Do not touch production DB data/collab.sqlite3. "
        "All outputs must stay in the sandbox DB/workspace.\n\n"
        f"User task:\n{request}"
    )
    threading.Thread(target=_run_real_sandbox_task, args=(placeholder_id, isolated_request, db_path, cwd, request), daemon=True).start()
    return {"accepted": True, "run_id": placeholder_id, "sandbox": True, "real_execution": True, "quota_used": used, "quota_limit": max_runs, "aggregate": os.environ.get("HERMES_SANDBOX_AGGREGATE", "1") == "1"}


class SandboxHandler(BaseHTTPRequestHandler):
    server: ThreadingHTTPServer
    protocol_version = "HTTP/1.1"

    def _json(self, data, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _path_under_base(self, raw_path: str) -> str | None:
        base_path = self.server.sandbox_base_path  # type: ignore[attr-defined]
        if not base_path:
            return raw_path
        if raw_path == base_path:
            return "/"
        if raw_path.startswith(base_path + "/"):
            return raw_path[len(base_path):]
        return None

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def _index(self) -> None:
        config = {"apiBase": self.server.sandbox_base_path, **SANDBOX_CONFIG}  # type: ignore[attr-defined]
        marker = "</head>"
        html = INDEX_HTML.read_text(encoding="utf-8")
        inject = f"<script>window.__HERMES_SANDBOX_CONFIG__={json.dumps(config, ensure_ascii=False)};</script>\n"
        body = html.replace(marker, inject + marker, 1).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        raw_path = urlparse(self.path).path
        if raw_path == "/" and self.server.sandbox_base_path:  # type: ignore[attr-defined]
            return self._redirect(self.server.sandbox_base_path + "/")  # type: ignore[attr-defined]
        path = self._path_under_base(raw_path)
        if path is None:
            return self._json({"error": "not found", "sandbox_base_path": self.server.sandbox_base_path}, 404)  # type: ignore[attr-defined]
        if path in {"/", "/index.html"}:
            self._index()
        elif path == "/api/overview":
            self._json(overview())
        elif path == "/api/runs":
            self._json(RUNS)
        elif path == "/api/resume-context":
            query = parse_qs(urlparse(self.path).query)
            run_id = (query.get("run_id") or [None])[0]
            context = resume_context(run_id)
            self._json(context or {"error": "no previous sandbox run"}, 200 if context else 404)
        elif path == "/api/task-sets":
            self._json(task_sets())
        elif path.startswith("/api/runs/"):
            run_id = path.rsplit("/", 1)[-1]
            detail = RUN_DETAILS.get(run_id)
            if detail:
                self._json({**detail, "task_set": task_set_for_run(run_id)})
            else:
                self._json({"error": "not found"}, 404)
        elif path == "/api/logs":
            self._json(RUN_DETAILS[RUNS[0]["id"]]["logs"] if RUNS else [])
        elif path == "/api/config":
            self._json(config_payload())
        elif path == "/api/skills":
            query = parse_qs(urlparse(self.path).query)
            node_type = (query.get("node_type") or [""])[0]
            task = (query.get("task") or [""])[0]
            self._json(skills_payload(node_type, task))
        elif path == "/api/tools":
            query = parse_qs(urlparse(self.path).query)
            node_type = (query.get("node_type") or [""])[0]
            task = (query.get("task") or [""])[0]
            self._json(tools_payload(node_type, task))
        elif path == "/api/session-chains":
            self._json([])
        elif path == "/api/registry":
            from hermes_collab_engine.registry import get_unified_registry
            registry = get_unified_registry()
            entries = registry.list_all()
            skills = [e.to_dict() for e in entries if e.__class__.__name__ == "SkillEntry"]
            tools = [e.to_dict() for e in entries if e.__class__.__name__ == "ToolEntry"]
            mcp = [e.to_dict() for e in entries if e.__class__.__name__ == "MCPEntry"]
            self._json({"skills": skills, "tools": tools, "mcp": mcp, "total": len(entries)})
        elif path == "/api/agents":
            from hermes_collab_engine.agents import detect_available_backends
            self._json([b.to_dict() for b in detect_available_backends()])
        elif path == "/api/sandbox/config":
            self._json(SANDBOX_CONFIG)
        elif path == "/api/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                heartbeat_counter = 0
                while True:
                    latest_logs = RUN_DETAILS[RUNS[0]["id"]]["logs"][-10:] if RUNS else []
                    payload = json.dumps({"type": "sandbox", "overview": overview(), "logs": latest_logs}, ensure_ascii=False)
                    self.wfile.write(f"data: {payload}\n\n".encode())
                    self.wfile.flush()
                    heartbeat_counter += 1
                    # 每 15 次数据推送（约 30 秒）发送一次 keepalive 注释，防止代理/防火墙断开空闲连接
                    if heartbeat_counter >= 15:
                        self.wfile.write(b":keepalive\n\n")
                        self.wfile.flush()
                        heartbeat_counter = 0
                    time.sleep(2)
            except Exception:
                pass
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        raw_path = urlparse(self.path).path
        path = self._path_under_base(raw_path)
        if path is None:
            return self._json({"error": "not found", "sandbox_base_path": self.server.sandbox_base_path}, 404)  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode() if length else "{}"
        try:
            data = json.loads(body)
        except Exception:
            return self._json({"error": "invalid json"}, 400)
        if path == "/api/runs":
            request = str(data.get("request") or "").strip()
            if not request:
                return self._json({"error": "request is required"}, 400)
            if data.get("resume"):
                request, context = resume_prompt(request, data.get("resume_run_id"))
            else:
                context = None
            if REAL_EXECUTION:
                result = add_real_sandbox_run(request)
                if context and result.get("accepted"):
                    result["resume"] = {"source_run_id": context["run"]["id"], "estimated_tokens": context["estimated_tokens"]}
                return self._json(result, 201 if result.get("accepted") else 429)
            result = add_demo_run(request)
            if context:
                result["resume"] = {"source_run_id": context["run"]["id"], "estimated_tokens": context["estimated_tokens"]}
            return self._json(result, 201)
        elif path == "/api/registry":
            entry_type = data.get("type", "skill")
            name = str(data.get("name", "")).strip()
            if not name:
                return self._json({"error": "name is required"}, 400)
            from hermes_collab_engine.registry import get_unified_registry, SkillEntry, MCPEntry
            registry = get_unified_registry()
            if entry_type == "skill":
                entry = SkillEntry(name=name, display_name=data.get("display_name", name), category=data.get("category", "custom"), description=data.get("description", ""), capabilities=data.get("capabilities", ["*"]), source="web-ui", priority=int(data.get("priority", 2)), content=data.get("content", ""))
            elif entry_type == "mcp":
                entry = MCPEntry(name=name, display_name=data.get("display_name", name), category=data.get("category", "mcp"), description=data.get("description", ""), capabilities=data.get("capabilities", ["*"]), source="web-ui", priority=int(data.get("priority", 2)), server_name=data.get("server_name", ""), tool_name=data.get("tool_name", ""), endpoint=data.get("endpoint", ""), allowed_tools=data.get("allowed_tools", []))
            else:
                return self._json({"error": f"unknown type: {entry_type}"}, 400)
            registry.register(entry)
            return self._json({"ok": True, "name": name, "type": entry_type})
        self._json({"error": "not found"}, 404)

    def log_message(self, fmt, *args) -> None:
        return


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _shutdown_after(httpd: ThreadingHTTPServer, seconds: int) -> None:
    time.sleep(seconds)
    print(f"Hermes sandbox TTL reached ({seconds}s); shutting down", flush=True)
    httpd.shutdown()


def _register_shutdown_timer(httpd: ThreadingHTTPServer, seconds: int) -> None:
    timer = threading.Thread(
        target=_shutdown_after,
        args=(httpd, seconds),
        daemon=True,
    )
    timer.start()
    print(f"Hermes sandbox TTL: {seconds}s (auto-shutdown timer registered)", flush=True)


def main() -> int:
    global SANDBOX_CONFIG, REAL_EXECUTION, DATA_SOURCE
    parser = argparse.ArgumentParser(description="Run the Hermes sandbox dashboard with mock data")
    parser.add_argument("--host", default=os.environ.get("HERMES_SANDBOX_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=env_int("HERMES_SANDBOX_PORT", 8876))
    parser.add_argument("--base-path", default=os.environ.get("HERMES_SANDBOX_BASE_PATH", "/"))
    parser.add_argument("--public-url", default=os.environ.get("HERMES_SANDBOX_PUBLIC_URL"))
    parser.add_argument("--db", default=os.environ.get("HERMES_SANDBOX_DB", ""))
    parser.add_argument("--mock-config", default=os.environ.get("HERMES_SANDBOX_MOCK_CONFIG", str(REPO_ROOT / "config" / "sandbox-mocks.json")))
    parser.add_argument("--real", action="store_true", default=os.environ.get("HERMES_SANDBOX_REAL_EXECUTION", "0") == "1", help="enable real CollabEngine runs against isolated sandbox db/workspace")
    parser.add_argument("--workspace", default=os.environ.get("HERMES_SANDBOX_WORKSPACE", str(REPO_ROOT / "data" / "sandbox_workspace")))
    parser.add_argument("--ttl-seconds", type=int, default=env_int("HERMES_SANDBOX_TTL_SECONDS", DEFAULT_TTL_SECONDS), help="seconds before the sandbox server stops itself; use 0 to disable")
    args = parser.parse_args()

    ttl_seconds = max(0, args.ttl_seconds)
    base_path = normalize_base_path(args.base_path)
    db_path = Path(args.db).expanduser().resolve() if args.db else None
    workspace_path = Path(args.workspace).expanduser().resolve()
    cleanup_workspace: Path | None = None
    if args.real:
        REAL_EXECUTION = True
        workspace_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            _safe_copytree(REPO_ROOT, workspace_path)
        except RuntimeError as exc:
            print(f"Hermes sandbox real execution refused; {exc}", file=sys.stderr, flush=True)
            return 2
        cleanup_workspace = workspace_path
        if db_path is None:
            db_path = (REPO_ROOT / "data" / "sandbox_real.sqlite3").resolve()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _reset_sandbox_state(db_path)
        DATA_SOURCE = f"sandbox-real:{db_path}"
    elif db_path:
        load_db_snapshot(db_path)
    mock_config = load_mock_config(Path(args.mock_config).expanduser().resolve() if args.mock_config else None)
    SANDBOX_CONFIG = {
        "mode": "sandbox-real" if REAL_EXECUTION else "sandbox-demo",
        "publicUrl": public_url(args.host, args.port, base_path, args.public_url),
        "basePath": base_path or "/",
        "database": {"path": str(db_path) if db_path else None, "source": DATA_SOURCE, "readOnly": not REAL_EXECUTION},
        "workspace": {"path": str(workspace_path), "isolated": REAL_EXECUTION},
        "mockConfig": mock_config,
        "productionExecution": "disabled",
        "realExecution": REAL_EXECUTION,
        "quota": {"used": REAL_RUNS_USED, "limit": env_int("HERMES_SANDBOX_REAL_RUN_LIMIT", 5), "concurrency": env_int("HERMES_SANDBOX_CONCURRENCY", 1), "timeout": env_int("HERMES_SANDBOX_TIMEOUT", 240), "max_retries": env_int("HERMES_SANDBOX_MAX_RETRIES", 0)},
        "aggregate": env_bool("HERMES_SANDBOX_AGGREGATE", True),
        "ttlSeconds": ttl_seconds,
    }

    httpd = ThreadingHTTPServer((args.host, args.port), SandboxHandler)
    httpd.sandbox_base_path = base_path  # type: ignore[attr-defined]
    httpd.sandbox_config = SANDBOX_CONFIG  # type: ignore[attr-defined]
    _install_shutdown_handlers(httpd)
    print(f"Hermes sandbox dashboard: {SANDBOX_CONFIG['publicUrl']}")
    print(f"Hermes sandbox API: {base_path}/api/sandbox/config" if base_path else "Hermes sandbox API: /api/sandbox/config")
    if ttl_seconds:
        _register_shutdown_timer(httpd, ttl_seconds)
    else:
        print("Hermes sandbox TTL: disabled", flush=True)
    try:
        httpd.serve_forever()
    finally:
        if hasattr(httpd, "server_close"):
            httpd.server_close()
        if cleanup_workspace is not None:
            _cleanup_sandbox_workspace(cleanup_workspace)
        print("Hermes sandbox server stopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
