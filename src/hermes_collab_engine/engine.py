from __future__ import annotations

import concurrent.futures
import hashlib
import json
import fnmatch
import queue
import shutil
import sys
import logging
import os
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .agents import get_backend, AgentBackend, SessionHandle
from .models import Plan, RiskPolicy, CheckpointDecision, WBSNode, WorkerResult
from .planner import Planner
from .store import CollabStore
from .registry import get_unified_registry, SkillEntry as USkillEntry, ToolEntry as UToolEntry, MCPEntry as UMCPEntry
from .skill_distributor import SkillDistributor


class CollabEngine:
    _UPSTREAM_PER_CAP = 1500
    _UPSTREAM_PARENT_CAP = 1500
    _UPSTREAM_GRANDPARENT_CAP = 300
    _UPSTREAM_ANCESTOR_CAP = 100
    _UPSTREAM_TOTAL_CAP = 3000
    _RESULT_MARKER = "HERMES-COLLAB-RESULT:"
    # Anti-avalanche: cap how deep shard nesting can go.
    _MAX_SHARD_DEPTH = max(
        1, int(os.environ.get("HERMES_COLLAB_MAX_SHARD_DEPTH", "1"))
    )
    _SHARD_DEPTH_WARN_LOG = True

    def __init__(
        self,
        db_path: str | Path = "data/collab.sqlite3",
        cwd: str | Path = ".",
        model: str | None = None,
        leader_model: str | None = None,
        worker_model: str | None = None,
        agent: str = "opencode",
        leader_agent: str | None = None,
        worker_agent: str | None = None,

        skill_registry: Any = None,
        tool_registry: Any = None,
        provider: Any = None,
        global_max_concurrent: int = 4,
    ):
        self.cwd = Path(cwd).resolve()
        env_model = os.environ.get("HERMES_COLLAB_MODEL") or os.environ.get("ANTHROPIC_MODEL")
        self.leader_model = leader_model or model or os.environ.get("HERMES_COLLAB_LEADER_MODEL") or env_model
        self.worker_model = worker_model or model or os.environ.get("HERMES_COLLAB_WORKER_MODEL") or env_model
        self.agent_backend: AgentBackend = get_backend(agent)
        self.leader_agent: AgentBackend | None = None
        if leader_agent:
            try:
                _lead = get_backend(leader_agent)
                # Verify the binary exists on PATH
                
                _binary = _lead.command[0]
                if shutil.which(_binary) is None:
                    print(f"warning: leader agent '{_binary}' not found on PATH, falling back to worker agent")
                    self.leader_agent = None
                else:
                    self.leader_agent = _lead
            except KeyError:
                print(f"warning: unknown leader agent '{leader_agent}', falling back to worker agent", file=sys.stderr)
                self.leader_agent = None
        self.worker_agent: AgentBackend | None = get_backend(worker_agent) if worker_agent else None
        self.provider = provider
        # Initialize unified registry (loads skills + tools + MCP)
        from .registry import UnifiedRegistry
        self.unified_registry = get_unified_registry()
        self._skill_distributor = SkillDistributor(
            unified_registry=self.unified_registry,
        )
        self.store = CollabStore(db_path, skip_cleanup=True)
        # Load agent profiles for leader/worker model overrides
        # Read stored model_config from DB and override
        try:
            _mc = self.store.get_setting("model_config") or {}
            if _mc.get("leader_model"):
                self.leader_model = _mc["leader_model"]
            if _mc.get("worker_model"):
                self.worker_model = _mc["worker_model"]
            # Provider env vars (base_url / api_key)
            if _mc.get("leader_base_url") or _mc.get("leader_api_key"):
                from .provider import apply_provider_to_env, ProviderProfile
                _p = ProviderProfile(
                    name="leader-override",
                    protocol="openai",
                    base_url=_mc.get("leader_base_url") or "",
                    api_key=_mc.get("leader_api_key") or "",
                )
                apply_provider_to_env(os.environ, _p)
        except Exception:
            pass
        self.planner = Planner(self.cwd, model=self.leader_model, store=self.store, skill_registry=self.skill_registry, tool_registry=self.tool_registry, leader_agent=self.leader_agent)
        self._node_results: dict[str, str] = {}
        self._node_results_struct: dict[str, dict[str, Any] | None] = {}
        self._node_results_lock = threading.Lock()
        self._current_plan: Plan | None = None
        self._risk_assessments: list[dict[str, Any]] = []
        self._checkpoint_paused_nodes: set[str] = set()
        self._paused_runs: set[str] = set()
        self._direct_mode: bool = False  # set by run() when score.routing=="direct"
        self._file_allowlist: set[str] = set()
        self._active_write_targets: dict[str, set[str]] = {}
        self._write_targets_lock = threading.Lock()
        self._active_fingerprints: dict[str, str] = {}
        self._fingerprint_lock = threading.Lock()
        # Worker process tracking for resource-pressure killing
        self._worker_procs: dict[str, subprocess.Popen] = {}
        self._worker_procs_lock = threading.Lock()
        self._worker_sessions: dict[str, SessionHandle] = {}
        self._recovery_attempts: dict[str, int] = {}
        self._resource_timeout_nodes: set[str] = set()
        # Leader persistent session (proc/stdin/stdout) and worker message queues
        self._leader_session: dict | None = None
        self._session_queue: dict = {}
        self._event_queue: queue.Queue = queue.Queue()
        self._leader_replies: dict[tuple[str, str], str] = {}
        self._running = False
        # Idle timeout watchdog: 10 min inactivity → auto-pause + save agent session
        self._idle_timeout = 600  # 10 minutes in seconds
        self._session_last_active: dict[str, float] = {}  # session_id -> last active timestamp
        # Leader watchdog / auto-restart
        self._leader_available: bool = True
        self._leader_max_restarts: int = 3
        self._leader_restart_count: int = 0
        self._leader_watchdog_stop: threading.Event = threading.Event()
        # Global worker semaphore: caps opencode worker processes across ALL runs
        self._global_max_concurrent = max(
            1, int(os.environ.get("HERMES_COLLAB_GLOBAL_MAX_CONCURRENT", str(global_max_concurrent)))
        )
        self._global_worker_sem = threading.Semaphore(self._global_max_concurrent)
        profiles = self._load_agent_profiles()
        self._leader_profile: dict[str, str] | None = profiles["leader"]
        self._worker_profile: dict[str, str] | None = profiles["worker"]
        self._restore_all_run_states()

    @property
    def skill_registry(self):
        return self.unified_registry

    @property
    def tool_registry(self):
        return self.unified_registry

    def _load_agent_profiles(self) -> dict[str, dict[str, str] | None]:
        """Load leader and worker profiles from data/agents.db.

        Returns:
            Dict with 'leader' and 'worker' keys. Each value is a dict with
            base_url, api_key, model fields, or None if not found.
        """
        db_path = self.cwd / "data" / "agents.db"
        try:
            import sqlite3

            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()

            leader: dict[str, str] | None = None
            cur.execute(
                "SELECT base_url, api_key, model FROM agents_profiles WHERE name=? AND is_active=1",
                ("leader",),
            )
            row = cur.fetchone()
            if row:
                leader = {
                    "base_url": row["base_url"],
                    "api_key": row["api_key"],
                    "model": row["model"],
                }

            worker: dict[str, str] | None = None
            cur.execute(
                "SELECT base_url, api_key, model FROM agents_profiles WHERE name!=? AND is_active=1 LIMIT 1",
                ("leader",),
            )
            row = cur.fetchone()
            if row:
                worker = {
                    "base_url": row["base_url"],
                    "api_key": row["api_key"],
                    "model": row["model"],
                }

            conn.close()
            return {"leader": leader, "worker": worker}
        except Exception:
            logging.getLogger(__name__).warning(
                "Failed to load agent profiles from %s", db_path, exc_info=True
            )
            return {"leader": None, "worker": None}

    def _persist_run_state(self, run_id: str) -> None:
        self.store.save_run_state(run_id, run_id in self._paused_runs, self._checkpoint_paused_nodes)

    def _restore_all_run_states(self) -> None:
        states = self.store.load_run_state()
        if not isinstance(states, list):
            return
        self._paused_runs = {state["run_id"] for state in states if state["paused"]}
        self._checkpoint_paused_nodes = {
            node_id
            for state in states
            for node_id in state["checkpoint_paused_nodes"]
        }
        # 清理重启后残留的僵死 run 移至 server.py 启动时执行（引擎每请求创建一次，不能放这里）

    def _touch_session(self, session_id: str | None) -> None:
        """Update the last-active timestamp for a conversation session.

        This prevents the idle watchdog from pausing the session's runs.
        Also persists the touch to the DB.
        """
        if not session_id:
            return
        self._session_last_active[session_id] = time.time()
        try:
            self.store.touch_session(session_id)
        except Exception:
            pass  # best-effort

    def _idle_watchdog(self) -> None:
        """Background thread: every 30s check for idle sessions.

        When a session has been idle for more than ``_idle_timeout`` seconds,
        the watchdog closes all worker sessions, saves the agent_session_id
        to the DB, marks the run as paused, and cleans up worker tracking.
        """
        while self._running:
            time.sleep(30)
            now = time.time()
            expired = [
                sid for sid, t in list(self._session_last_active.items())
                if now - t > self._idle_timeout
            ]
            if not expired:
                continue
            for session_id in expired:
                self._session_last_active.pop(session_id, None)
                # Find all running runs for this session
                try:
                    rows = self.store._query(
                        "SELECT id FROM runs WHERE session_id=? AND status='running'",
                        (session_id,),
                    )
                except Exception:
                    rows = []
                for row in rows:
                    run_id = row["id"]
                    # Close all worker sessions and save their agent session IDs
                    with self._worker_procs_lock:
                        for node_id, handle in list(self._worker_sessions.items()):
                            try:
                                self.agent_backend.close_session(handle)
                            except Exception:
                                pass
                            if handle.session_id:
                                try:
                                    self.store.save_agent_session(session_id, handle.session_id)
                                except Exception:
                                    pass
                        # Clean up worker tracking
                        self._worker_sessions.clear()
                        self._worker_procs.clear()
                    # Mark run as paused
                    self._paused_runs.add(run_id)
                    try:
                        self.store._execute(
                            "UPDATE wbs_nodes SET status='paused' WHERE run_id=? AND status IN ('running','pending')",
                            (run_id,),
                        )
                        self.store.update_run(run_id, "paused")
                        self.store.log(
                            run_id, "warning",
                            "run paused by idle watchdog",
                            {"session_id": session_id, "idle_timeout": self._idle_timeout},
                        )
                    except Exception:
                        pass

    def _restore_run_state(self, run_id: str) -> None:
        state = self.store.load_run_state(run_id)
        if not state:
            return
        if state["paused"]:
            self._paused_runs.add(run_id)
        else:
            self._paused_runs.discard(run_id)
        self._checkpoint_paused_nodes = set(state["checkpoint_paused_nodes"])

    def restore_run_state(self, run_id: str) -> dict:
        self._restore_run_state(run_id)
        return {
            "ok": True,
            "run_id": run_id,
            "paused": run_id in self._paused_runs,
            "checkpoint_paused_nodes": sorted(self._checkpoint_paused_nodes),
        }

    def run(self, request: str, *, title: str | None = None, concurrency: int = 4, timeout: int = 86400, max_retries: int = 2, split_count: int = 4, aggregate: bool = True, session_id: str | None = None, prev_session_id: str | None = None, run_id: str | None = None) -> dict:
        run_id = run_id or ("run_" + uuid.uuid4().hex[:12])
        score = self.planner.assess(request)
        self.store.create_run(run_id, title or request[:80], request, score.to_dict(), agent=self.agent_backend.name, session_id=session_id)
        self.store.log(run_id, "info", "complexity assessed", score.to_dict())
        # Start leader persistent session (must succeed before any routing)
        self._leader_restart_count = 0
        self._leader_available = True
        self._start_leader_session(run_id)
        # Start event loop thread to process events -> leader routing
        self._running = True
        _evt_thread = threading.Thread(target=self._event_loop, daemon=True)
        _evt_thread.start()
        # Start idle watchdog thread
        _watchdog_thread = threading.Thread(target=self._idle_watchdog, daemon=True)
        _watchdog_thread.start()
        # Touch the session so watchdog knows this session is active
        self._touch_session(session_id)
        plan = self.planner.decompose(request, score=score)
        if isinstance(plan, list):
            plan = Plan(nodes=plan)
        nodes = plan.nodes
        # Build task_requirements from plan nodes for leader review
        plan.task_requirements = [
            {"id": f"req-{i+1}", "description": n.deliverable or n.description,
             "node_id": n.id, "deliverable": n.deliverable}
            for i, n in enumerate(nodes)
        ]
        self.store.set_run_meta(run_id, {"task_requirements": plan.task_requirements})
        if plan.shared_brief:
            self.store.log(run_id, "info", "shared plan brief created", {"shared_brief": plan.shared_brief})
            for node in nodes:
                if node.capability == "implementation":
                    node.brief = f"Shared brief:\n{plan.shared_brief}\n\nNode brief:\n{node.brief}" if node.brief else plan.shared_brief
        # Inject past session turns as context for continuous conversation
        if session_id:
            past_turns = self.store.get_session_turns(session_id, limit=5)
            if past_turns:
                turn_lines = []
                for t in past_turns:
                    req_preview = t["user_request"][:200]
                    agg_preview = (t["aggregate"] or "")[:200]
                    turn_lines.append(f"  [{t['turn_index']}] User: {req_preview}")
                    if agg_preview:
                        turn_lines.append(f"       Result: {agg_preview}")
                session_ctx = (
                    f"Session context — previous turns in session {session_id[:12]}...:\n"
                    + "\n".join(turn_lines)
                    + "\n\n"
                )
                plan.shared_brief = (plan.shared_brief or "") + session_ctx
        # Inject previous-session context for conversation continuation (会话链)
        if prev_session_id:
            from .store import get_model_context_limit
            # Get current model context limit
            model_name = self.leader_model or ""
            ctx_limit = get_model_context_limit(model_name)
            # 80% threshold for safe injection
            max_inject_tokens = int(ctx_limit * 0.8)
            prev_turns = self.store.get_session_turns(prev_session_id, limit=999)
            if prev_turns:
                turn_lines = []
                total_est_tokens = 0
                # Estimate existing tokens from shared_brief + request
                existing_est = len(plan.shared_brief or "") * 0.25 + len(request) * 0.25
                budget = max_inject_tokens - existing_est
                # Build context lines from the last turns, stopping if budget exceeded
                for t in reversed(prev_turns):
                    req_preview = t["user_request"][:300]
                    agg_preview = (t["aggregate"] or "")[:300]
                    line = f"  [{t['turn_index']}] User: {req_preview}"
                    if agg_preview:
                        line += f"\n       Result: {agg_preview}"
                    est_tokens = len(line) * 0.25
                    if total_est_tokens + est_tokens > budget and len(turn_lines) >= 3:
                        # Truncate: keep only what fits within budget; keep minimum 3 turns
                        break
                    turn_lines.append(line)
                    total_est_tokens += est_tokens
                # Ensure at least last 3 turns if available
                min_turns = min(3, len(prev_turns))
                if len(turn_lines) < min_turns:
                    turn_lines = [f"  [{t['turn_index']}] User: {t['user_request'][:300]}" + (f"\n       Result: {(t['aggregate'] or '')[:300]}" if t.get('aggregate') else "") for t in prev_turns[-min_turns:]]
                # Sort turns chronologically for context coherence
                turn_lines.sort(key=lambda x: int(x.split("[")[1].split("]")[0]) if "[" in x else 0)
                prev_ctx = (
                    f"Previous session context — conversation history from {prev_session_id[:12]}...:\n"
                    + "\n".join(turn_lines)
                    + "\n\n"
                )
                plan.shared_brief = (plan.shared_brief or "") + prev_ctx
                self.store.log(run_id, "info", "prev_session context injected", {
                    "prev_session_id": prev_session_id,
                    "turns_used": len(turn_lines),
                    "total_prev_turns": len(prev_turns),
                    "estimated_tokens": int(total_est_tokens),
                    "model": model_name,
                    "model_ctx_limit": ctx_limit,
                })
        with self._node_results_lock:
            self._current_plan = plan
            self._node_results = {}
            self._node_results_struct = {}
        self._risk_assessments = []
        # Mode B fix: each node insertion is its own try/except
        inserted_node_ids: list[str] = []
        for node in nodes:
            try:
                node_data = node.to_dict()
                node_data["shared_brief"] = plan.shared_brief
                self.store.insert_wbs_node(run_id, node_data)
                inserted_node_ids.append(node.id)
            except Exception as exc:
                self.store.log(
                    run_id, "error",
                    "wbs node insert failed; skipping this node",
                    {"node": getattr(node, "id", "?"), "error": f"{type(exc).__name__}: {exc}"},
                )
                self.store.add_lesson(
                    "wbs-persistence",
                    f"Failed to insert WBS node {getattr(node, 'id', '?')} into SQLite; engine continued with remaining nodes. ({type(exc).__name__}: {exc})",
                    {"run_id": run_id, "node": getattr(node, "id", "?")},
                )
        # If we intended N nodes but only wrote fewer, fail loud now
        if len(inserted_node_ids) < len(nodes):
            missing = [n.id for n in nodes if n.id not in inserted_node_ids]
            self.store.log(
                run_id, "error",
                "wbs persistence incomplete; aborting run before dispatch",
                {"expected": len(nodes), "inserted": len(inserted_node_ids), "missing": missing},
            )
            self.store.update_run(run_id, "failed")
            self.store.log(run_id, "error", "run aborted; wbs persistence incomplete", {"missing": missing})
            return {
                "run_id": run_id,
                "ok": False,
                "complexity": score.to_dict(),
                "results": [],
                "aggregate": None,
                "lessons_learned": [],
                "abort_reason": f"wbs_persistence_incomplete: missing {missing}",
            }
        self._preallocate_skills_tools(run_id, nodes, task_type=getattr(plan, 'task_type', 'development'))
        self._restore_run_state(run_id)
        # Override with this run's own checkpoint_paused state
        run_state = self.store.load_run_state(run_id)
        if isinstance(run_state, dict):
            self._checkpoint_paused_nodes = set(run_state.get("checkpoint_paused_nodes") or [])
        else:
            self._checkpoint_paused_nodes = set()
        self.store.update_run(run_id, "running")
        started_at = time.time()  # total run budget clock

        try:
            results: list[WorkerResult] = []
            pending = {n.id: n for n in nodes}
            completed: set[str] = set()
            failed_final: list[WorkerResult] = []
            max_workers = max(1, concurrency)
            split_children: dict[str, set[str]] = {}
            split_finished: dict[str, set[str]] = {}
            split_results: dict[str, list[WorkerResult]] = {}
            deferred_queue: list[str] = []  # nodes killed by watchdog, waiting for resources

            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
                running: dict[concurrent.futures.Future[list[WorkerResult]], WBSNode] = {}
                running_started: dict[concurrent.futures.Future, float] = {}  # future -> start time
                _STALL_CHECK_INTERVAL = 60  # seconds between stale-worker checks
                while pending or running:
                    while pending and len(running) < max_workers:
                        if run_id in self._paused_runs:
                            break  # Don't schedule new nodes while paused
                        ready = [n for n in pending.values() if all(dep in completed for dep in n.dependencies)]
                        # Also skip nodes whose dependencies are checkpoint-paused
                        ready = [n for n in ready if not any(dep in self._checkpoint_paused_nodes for dep in n.dependencies)]
                        if not ready:
                            if running:
                                break
                            # Break dependency deadlocks, but not if we're checkpoint-paused
                            if self._checkpoint_paused_nodes:
                                break
                            ready = [next(iter(pending.values()))]
                            self.store.log(run_id, "warning", "dependency deadlock avoided", {"node": ready[0].id})

                        node = ready[0]
                        duplicate_of = self._duplicate_running_node(node)
                        if duplicate_of:
                            pending.pop(node.id, None)
                            reason = f"duplicate of active node {duplicate_of}"
                            result = WorkerResult(node.id, node.title, True, f"Skipped duplicate worker: {reason}", None, 0.0, 0, "", node.attempt, {"status": "ok", "summary": reason})
                            results.append(result)
                            completed.add(node.id)
                            self.store.update_node(node.id, "completed", result.result, None, 0.0, None, run_id=run_id)
                            self.store.log(run_id, "warning", "duplicate worker killed before launch", {"node": node.id, "duplicate_of": duplicate_of, "fingerprint": self._node_fingerprint(node)}, node.id)
                            self._record_node_result(run_id, result)
                            continue
                        blocked_by = self._blocked_by_active_write(node)
                        if blocked_by:
                            if len(ready) > 1:
                                ready = [candidate for candidate in ready if not self._blocked_by_active_write(candidate)]
                                if not ready:
                                    break
                                node = ready[0]
                            else:
                                break
                        pending.pop(node.id, None)
                        self._claim_fingerprint(node)
                        write_targets = self._claim_write_targets(node)
                        if write_targets:
                            self.store.log(run_id, "info", "worker write targets claimed", {"node": node.id, "write_targets": sorted(write_targets)}, node.id)
                        if self._should_split_proactively(node, timeout, max_retries, split_count, started_at):
                            self._release_fingerprint(node.id)
                            self._release_write_targets(node.id)
                            if self._shard_too_deep(node, run_id=run_id):
                                self.store.log(
                                    run_id,
                                    "warning",
                                    "anti-avalanche: refused to split shard, running as-is",
                                    {
                                        "node": node.id,
                                        "estimated_duration": node.estimated_duration,
                                        "effective_timeout": self._effective_timeout(node, timeout, started_at),
                                        "timeout": timeout,
                                        "max_shard_depth": self._MAX_SHARD_DEPTH,
                                    },
                                    node.id,
                                )
                            else:
                                shards = self._split_node(node, split_count)
                                split_children[node.id] = {shard.id for shard in shards}
                                split_finished[node.id] = set()
                                split_results[node.id] = []
                                self.store.update_node(node.id, "running", run_id=run_id)
                                self.store.log(
                                    run_id,
                                    "warning",
                                    "node estimated to exceed timeout; splitting proactively",
                                    {
                                        "node": node.id,
                                        "estimated_duration": node.estimated_duration,
                                        "effective_timeout": self._effective_timeout(node, timeout, started_at),
                                        "timeout": timeout,
                                        "split_count": len(shards),
                                    },
                                    node.id,
                                )
                                for shard in shards:
                                    self.store.insert_wbs_node(run_id, shard.to_dict())
                                    self.store.update_node(shard.id, "pending", run_id=run_id)
                                    pending[shard.id] = shard
                            continue

                        # ── Total run budget check ──────────────────────────
                        remaining_budget = max(0, timeout - (time.time() - started_at))
                        if remaining_budget < 30:
                            self.store.log(
                                run_id, "warning",
                                "run budget running low",
                                {"remaining_budget": round(remaining_budget, 1),
                                 "node": node.id},
                                node.id,
                            )
                            self.store.update_node(node.id, "failed",
                                "budget exhausted",
                                None, 0.0, None, run_id=run_id)
                            result = WorkerResult(node.id, node.title, False,
                                "budget exhausted", None, 0.0, 1, "", node.attempt)
                            results.append(result)
                            completed.add(node.id)
                            self._record_node_result(run_id, result)
                            self.store.log(run_id, "warning",
                                "node skipped: budget exhausted",
                                {"node": node.id,
                                 "remaining_budget": round(remaining_budget, 1)},
                                node.id)
                            # If this skipped node is a proactive shard, record it
                            # in split_finished so the parent gets reconciled.
                            pending.pop(node.id, None)
                            _parent_of_skipped = node.parent_id if node.parent_id in split_children else None
                            if _parent_of_skipped:
                                split_finished[_parent_of_skipped].add(node.id)
                                split_results[_parent_of_skipped].extend([result])
                                if split_children[_parent_of_skipped] <= split_finished[_parent_of_skipped]:
                                    _p_results = split_results[_parent_of_skipped]
                                    if any(r.ok for r in _p_results):
                                        _shard_ids = sorted(split_children[_parent_of_skipped])
                                        _combined_text, _combined_struct = self._combine_shard_results(
                                            run_id, _parent_of_skipped, _shard_ids,
                                        )
                                        self.store.update_node(
                                            _parent_of_skipped, "completed",
                                            _combined_text,
                                            None, None, None, run_id=run_id,
                                        )
                                        with self._node_results_lock:
                                            self._node_results[_parent_of_skipped] = _combined_text
                                            self._node_results_struct[_parent_of_skipped] = _combined_struct
                                    else:
                                        self.store.update_node(_parent_of_skipped, "failed",
                                            None, None, None,
                                            "All proactive shards exhausted budget",
                                            run_id=run_id)
                            continue

                        # 2026-06-20: load-aware dispatch — if system is
                        # under pressure, don't submit more workers; let the
                        # watchdog kill a worker first to free resources.
                        _cpu_now = self._get_cpu_percent() or 0
                        _mem_now = self._get_mem_percent() or 0
                        if _cpu_now > 85 or _mem_now > 90:
                            self.store.log(run_id, "warning",
                                "dispatch paused: system under pressure",
                                {"cpu": f"{_cpu_now:.0f}%", "mem": f"{_mem_now:.0f}%",
                                 "node": node.id, "running": len(running)},
                                node.id)
                            break
                        future = pool.submit(self._run_node_with_retries, run_id, node, self._effective_timeout(node, timeout, started_at), max_retries, split_count)
                        running[future] = node
                        running_started[future] = time.time()

                    if not running:
                        if self._checkpoint_paused_nodes:
                            break
                        # Tight-spin guard: if pending nodes exist but nothing can
                        # dispatch, back off to prevent 100% CPU ghost loop.
                        if pending:
                            time.sleep(5)
                        # Deferred recovery: if resources available, re-dispatch killed nodes
                        if deferred_queue:
                            cpu_now = self._get_cpu_percent() or 0
                            mem_now = self._get_mem_percent() or 0
                            if cpu_now < 70 and mem_now < 70:
                                deferred_queue.sort(key=lambda nid: self.store._one("SELECT created_at FROM wbs_nodes WHERE id=?", (nid,))["created_at"] if self.store._one("SELECT created_at FROM wbs_nodes WHERE id=?", (nid,)) else "")
                                for nid in list(deferred_queue):
                                    if len(running) >= max_workers:
                                        break
                                    node_obj = pending.get(nid)
                                    if not node_obj:
                                        node_obj = self.store._one("SELECT * FROM wbs_nodes WHERE id=? AND run_id=?", (nid, run_id))
                                        if node_obj:
                                            import json as _json
                                            deps = _json.loads(node_obj.get("dependencies_json", "[]"))
                                            node_obj2 = WBSNode(nid, node_obj["title"], node_obj["description"],
                                                node_obj["capability"], node_obj["complexity"], deps,
                                                node_obj.get("parallelizable", 1), node_obj["deliverable"])
                                            pending[nid] = node_obj2
                                    if nid in pending:
                                        deferred_queue.remove(nid)
                            elif any(nid not in deferred_queue for nid in deferred_queue):
                                _now2 = time.time()
                                for nid in list(deferred_queue):
                                    row = self.store._one("SELECT created_at FROM wbs_nodes WHERE id=?", (nid,))
                                    if row:
                                        try:
                                            from datetime import datetime as _dt
                                            created = _dt.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S")
                                            if (_now2 - created.timestamp()) > 600:
                                                self.store.update_node(nid, "failed", "deferred timeout: resources not available", None, None, None, run_id=run_id)
                                                deferred_queue.remove(nid)
                                        except Exception:
                                            pass
                        continue

                    done, _ = concurrent.futures.wait(running.keys(), timeout=_STALL_CHECK_INTERVAL, return_when=concurrent.futures.FIRST_COMPLETED)
                    # Resource-pressure watchdog: kill the oldest running worker
                    # when CPU or memory exceeds thresholds.
                    if not done and running:
                        cpu_pct = self._get_cpu_percent() or 0
                        mem_pct = self._get_mem_percent() or 0
                        if self._system_under_pressure(cpu_pct, mem_pct):
                            oldest_fut = min(running.keys(), key=lambda f: running_started.get(f, float('inf')))
                            oldest_node = running[oldest_fut]
                            with self._worker_procs_lock:
                                proc = self._worker_procs.get(oldest_node.id)
                                if proc:
                                    import os, signal
                                    try:
                                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                                    except (ProcessLookupError, PermissionError):
                                        proc.kill()
                            deferred_queue.append(oldest_node.id)
                            self._resource_timeout_nodes.add(oldest_node.id)
                            self.store.log(run_id, "warning",
                                "worker killed by resource-pressure watchdog",
                                {"node": oldest_node.id, "cpu": f"{cpu_pct:.0f}%",
                                 "mem": f"{mem_pct:.0f}%", "elapsed": round(time.time() - running_started.get(oldest_fut, 0), 1)},
                                oldest_node.id)
                            oldest_fut.cancel()
                            done.add(oldest_fut)
                    for fut in done:
                        node = running.pop(fut)
                        running_started.pop(fut, None)
                        self._release_fingerprint(node.id)
                        self._release_write_targets(node.id)
                        try:
                            node_results = fut.result()
                        except Exception as exc:
                            duration = 0.0
                            result = WorkerResult(node.id, node.title, False, f"Worker crashed: {type(exc).__name__}: {exc}", None, duration, 1, "", node.attempt)
                            self.store.update_node(node.id, "failed", result.result, None, duration, result.result, run_id=run_id)
                            self.store.log(run_id, "error", "worker future failed", result.to_dict(), node.id)
                            node_results = [result]
                        except BaseException as exc:
                            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                                raise  # must propagate; outer handler does cleanup
                            # Handles CancelledError from stale-worker watchdog
                            duration = 0.0
                            result = WorkerResult(node.id, node.title, False, f"Worker cancelled: {type(exc).__name__}: {exc}", None, duration, 1, "", node.attempt)
                            self.store.update_node(node.id, "failed", result.result, None, duration, result.result, run_id=run_id)
                            self.store.log(run_id, "warning", "worker future cancelled", result.to_dict(), node.id)
                            node_results = [result]

                        results.extend(node_results)
                        parent_id = node.parent_id if node.parent_id in split_children else None
                        if parent_id:
                            split_finished[parent_id].add(node.id)
                            split_results[parent_id].extend(node_results)
                            if any(r.ok for r in node_results):
                                completed.add(node.id)
                            if split_children[parent_id] <= split_finished[parent_id]:
                                parent_results = split_results[parent_id]
                                if any(r.ok for r in parent_results):
                                    completed.add(parent_id)
                                    self.store.update_node(parent_id, "completed", "Completed by proactive shards", None, None, None, run_id=run_id)
                                else:
                                    failed_final.extend(parent_results)
                                    self.store.update_node(parent_id, "failed", None, None, None, "All proactive shards failed", run_id=run_id)
                            continue

                        if any(r.ok for r in node_results):
                            # Mark the original node as covered if parent or any shard succeeded.
                            completed.add(node.id)
                            # --- v3: checkpoint & risk detection ---
                            risk_policy = self.store.load_risk_policy()
                            result_struct = None
                            with self._node_results_lock:
                                result_struct = self._node_results_struct.get(node.id)
                            risks = self._detect_risks(node, result_struct, risk_policy)
                            self._apply_risk_policy(run_id, risks, risk_policy)
                            # If this node is checkpoint-paused, remove from completed
                            # so downstream dependencies are not considered satisfied
                            if node.id in self._checkpoint_paused_nodes:
                                completed.discard(node.id)
                        else:
                            failed_final.extend(node_results)

                    # If a checkpoint node failed and recovery exhausted, abort the run
                    if failed_final and pending:
                        checkpoint_failed = any(
                            hasattr(n, 'checkpoint') and n.checkpoint
                            for n in failed_final
                        )
                        if checkpoint_failed:
                            self.store.log(run_id, "warning",
                                "run aborted — checkpoint node failed after recovery",
                                {"failed_nodes": [r.node_id for r in failed_final]})
                            for nid in list(pending.keys()):
                                self.store.update_node(nid, "failed",
                                    error="checkpoint node failed after recovery",
                                    run_id=run_id)
                            pending.clear()
                            break

            # Post-execution file conflict detection
            conflicts = self._detect_file_conflicts(run_id, results)
            if conflicts:
                self.store.log(run_id, "warn",
                    f"File conflicts found: {len(conflicts)} file(s) written by multiple workers. "
                    "The last worker's changes may overwrite earlier work.",
                    {"conflicts": conflicts})

            final = None
            if aggregate:
                if score.routing == "direct" and results:
                    # direct 路由：worker 结果就是最终答案，跳过 aggregate 节点。
                    final = results[0]
                    if final and final.node_id:
                        self.store.update_node(
                            final.node_id,
                            "completed" if final.ok else "failed",
                            final.result,
                            final.session_id,
                            final.duration_seconds,
                            None if final.ok else final.result,
                            run_id=run_id,
                        )
                else:
                    final = self._aggregate(run_id, request, results, timeout)
                    self.store.update_node(
                        final.node_id,
                        "completed" if final.ok else "failed",
                        final.result,
                        final.session_id,
                        final.duration_seconds,
                        None if final.ok else final.result,
                        run_id=run_id,
                    )
                    if final.ok:
                        self._record_node_result(run_id, final)
                    # Merge all worker files_modified into aggregate result_struct
                    merged_files = set()
                    for r in results:
                        if r.result_struct:
                            files = r.result_struct.get("files_modified", []) or []
                            if isinstance(files, list):
                                for f in files:
                                    if isinstance(f, str) and f.strip():
                                        merged_files.add(f.strip())
                    if merged_files and final.result_struct:
                        final.result_struct["files_modified"] = sorted(merged_files)
                        self.store.save_node_result(run_id, final.node_id,
                            final.result or "", final.result_struct)
            self._learn(run_id, results)

            if session_id:
                aggregate_text = final.result if final else ""
                self.store.add_session_turn(session_id, run_id, request, aggregate_text)

            ok = not failed_final and (final.ok if final else True)
            self.store.update_run(run_id, "completed" if ok else "failed")

            # Leader review: LLM evaluates each node's output against task_requirements
            review_text = None
            try:
                review_text = self._leader_review(run_id, request, plan, results)
            except Exception as _rev_e:
                self.store.log(run_id, "warning", "leader review failed, using fallback summary",
                               {"error": str(_rev_e)[:200]})
            if review_text:
                self.store.set_run_meta(run_id, {"leader_review": review_text})
                self.store.log(run_id, "info", "worker finished", {
                    "result": review_text,
                }, node_id="aggregate")
            else:
                # Fallback: simple summary if LLM review fails
                _simple = "\n".join([
                    f"Run {run_id} — {len(results)} nodes",
                    *[f"  {'✅' if _r.ok else '❌'} {_r.node_id}: {(_r.result or '')[:100]}"
                      for _r in results]
                ])
                self.store.log(run_id, "info", "worker finished", {
                    "result": _simple,
                }, node_id="aggregate")

            self.store.log(run_id, "info" if ok else "error", "run finished", {"ok": ok})
            # Scan output/ for files generated during this run
            if ok:
                try:
                    output_dir = self.cwd / "output"
                    if output_dir and output_dir.exists():
                        import time as _time
                        started_row = self.store._one(
                            "SELECT created_at FROM runs WHERE id=?", (run_id,)
                        )
                        if started_row:
                            from datetime import datetime as _dt
                            start_ts = _dt.strptime(started_row[0], "%Y-%m-%d %H:%M:%S").timestamp()
                            new_files = []
                            for fpath in sorted(output_dir.iterdir()):
                                if fpath.is_file() and fpath.name != '.gitkeep':
                                    mtime = fpath.stat().st_mtime
                                    if mtime >= start_ts - 120:
                                        new_files.append({
                                            "path": f"output/{fpath.name}",
                                            "size": fpath.stat().st_size,
                                            "mtime": _time.strftime(
                                                "%Y-%m-%d %H:%M:%S", _time.localtime(mtime)),
                                        })
                            if new_files:
                                self.store.save_run_files(run_id, new_files)
                except Exception:
                    pass  # file scan non-blocking

            # Collect high-value lessons for parent (Hermes) memory mapping
            _EXCLUDED_CATEGORIES = {"planning", "worker-contract"}
            lessons_learned: list[dict[str, Any]] = []
            for scope in ("engine", "parent"):
                for lesson in self.store.lessons(limit=100, scope=scope):
                    if lesson.get("category") not in _EXCLUDED_CATEGORIES:
                        lessons_learned.append(lesson)

            return {
                "run_id": run_id,
                "ok": ok,
                "complexity": score.to_dict(),
                "results": [r.to_dict() for r in results],
                "aggregate": final.to_dict() if final else None,
                "lessons_learned": lessons_learned,
            }
        except BaseException as exc:
            self.store.fail_stale_run(run_id, f"interrupted: {type(exc).__name__}: {exc}")
            raise
        finally:
            with self._node_results_lock:
                self._current_plan = None
                self._node_results = {}
                self._node_results_struct = {}
            with self._write_targets_lock:
                self._active_write_targets = {}
            with self._fingerprint_lock:
                self._active_fingerprints = {}
            self._risk_assessments = []
            self._checkpoint_paused_nodes.clear()
            self._paused_runs.discard(run_id)
            self._resource_timeout_nodes.clear()

    def _node_fingerprint(self, node: WBSNode) -> str:
        if node.fingerprint:
            return node.fingerprint
        text = " ".join([
            node.title,
            node.description,
            node.capability,
        ]).lower()
        words = re.findall(r"[\w/.-]+", text)
        stop_words = {
            "the", "and", "for", "with", "from", "that", "this", "task", "node", "phase",
            "implementation", "analysis", "planning", "verification", "实现", "分析", "规划", "验证",
        }
        normalized = " ".join(word for word in words if len(word) > 2 and word not in stop_words)
        if not normalized:
            normalized = f"{node.capability}:{node.title.lower()}"
        node.fingerprint = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]
        return node.fingerprint

    def _duplicate_running_node(self, node: WBSNode) -> str | None:
        fingerprint = self._node_fingerprint(node)
        with self._fingerprint_lock:
            return self._active_fingerprints.get(fingerprint)

    def _claim_fingerprint(self, node: WBSNode) -> str:
        fingerprint = self._node_fingerprint(node)
        with self._fingerprint_lock:
            self._active_fingerprints[fingerprint] = node.id
        return fingerprint

    def _release_fingerprint(self, node_id: str) -> None:
        with self._fingerprint_lock:
            for fingerprint, active_node_id in list(self._active_fingerprints.items()):
                if active_node_id == node_id:
                    self._active_fingerprints.pop(fingerprint, None)

    def _node_write_targets(self, node: WBSNode) -> set[str]:
        if node.capability not in {"implementation", "coding", "debugging", "docs"}:
            return set()
        targets = {str(target).strip().strip("/") for target in node.write_targets if str(target).strip()}
        if targets:
            return targets
        return {"."}

    def _targets_overlap(self, left: set[str], right: set[str]) -> bool:
        if not left or not right:
            return False
        for a in left:
            for b in right:
                if a == "." or b == ".":
                    return True
                if a == b or a.startswith(b.rstrip("/") + "/") or b.startswith(a.rstrip("/") + "/"):
                    return True
                if fnmatch.fnmatch(a, b) or fnmatch.fnmatch(b, a):
                    return True
        return False

    def _blocked_by_active_write(self, node: WBSNode) -> str | None:
        targets = self._node_write_targets(node)
        if not targets:
            return None
        with self._write_targets_lock:
            for node_id, active in self._active_write_targets.items():
                # Mode E fix: skip self-claims — a claim from a node against itself
                # is not concurrent and cannot deadlock the same node.
                if node_id == node.id:
                    continue
                if self._targets_overlap(targets, active):
                    return node_id
        return None

    def _claim_write_targets(self, node: WBSNode) -> set[str]:
        targets = self._node_write_targets(node)
        if targets:
            with self._write_targets_lock:
                self._active_write_targets[node.id] = targets
        return targets

    def _release_write_targets(self, node_id: str) -> None:
        with self._write_targets_lock:
            self._active_write_targets.pop(node_id, None)

    def _effective_timeout(self, node: WBSNode, timeout: int, started_at: float) -> int:
        # Cap by remaining run budget so sequential nodes don't cumulatively
        # exceed the user's --timeout. Minimum 30s so a worker has a viable window.
        remaining = max(30, int(timeout - (time.time() - started_at)))
        if node.estimated_duration:
            try:
                estimated_floor = max(1, int(node.estimated_duration) * 2)
            except (TypeError, ValueError):
                return min(timeout, remaining)
            return min(timeout, max(estimated_floor, timeout // 2), remaining)
        return min(timeout, remaining)

    def _should_split_proactively(self, node: WBSNode, timeout: int, max_retries: int, split_count: int, started_at: float) -> bool:
        """Resource-driven proactive split decision.

        Splits based on:
        1. Task estimated duration (bigger = more shards)
        2. System load factor (busy = fewer shards)
        3. WBS minimum granularity (each shard >= 2 min of work)
        4. Available concurrency slots
        5. User --split-count CLI cap
        """
        if split_count <= 1 or not node.estimated_duration:
            return False
        try:
            est = int(node.estimated_duration)
        except (TypeError, ValueError):
            return False

        # 1. Base shard count by task size + min granularity
        # Each shard must produce at least MIN_SHARD_WORK seconds of work.
        _MIN_SHARD_WORK = 180  # 3 min per shard minimum
        if est <= _MIN_SHARD_WORK:
            return False
        if est > 1200:
            target = 4
        elif est > 600:
            target = 3
        elif est > 300:
            target = 2
        else:
            return False  # < 5 min, no split (already handled above)

        # 2. Load factor: system busy → fewer shards
        cpu = self._get_cpu_percent() or 0
        mem = self._get_mem_percent() or 0
        if cpu > 85 or mem > 90:
            return False  # under pressure, no split at all
        elif cpu > 70 or mem > 80:
            factor = 0.25
        elif cpu > 50 or mem > 60:
            factor = 0.5
        else:
            factor = 1.0
        import math
        target = max(2, int(math.ceil(target * factor)))

        # 3. WBS min granularity: each shard >= MIN_SHARD_WORK sec of work
        target = min(target, max(1, est // _MIN_SHARD_WORK))

        # 4. Available concurrency slots
        running_count = len(self._worker_procs)
        available = max(1, running_count)  # at least 1
        # Cap: don't split more than available slots x 2
        slot_limit = max(1, running_count) * 2
        target = min(target, slot_limit)

        # 5. User --split-count hard cap
        target = min(target, split_count)

        return target > 1

    # ── Resource monitoring ────────────────────────────────────────────
    @staticmethod
    def _get_cpu_percent() -> float | None:
        """CPU usage % from /proc/stat (user+system vs idle)."""
        try:
            with open("/proc/stat") as f:
                parts = f.readline().split()
            if parts[0] != "cpu" or len(parts) < 5:
                return None
            vals = [int(v) for v in parts[1:9]]
            total = sum(vals)
            idle = vals[3]
            return 100.0 * (1.0 - idle / total) if total else None
        except Exception:
            return None

    @staticmethod
    def _get_mem_percent() -> float | None:
        try:
            with open("/proc/meminfo") as f:
                data = {}
                for line in f:
                    kv = line.split(":", 1)
                    if len(kv) == 2:
                        data[kv[0].strip()] = int(kv[1].strip().split()[0])
            total = data.get("MemTotal")
            avail = data.get("MemAvailable")
            if total and avail:
                return 100.0 * (1.0 - avail / total)
            return None
        except Exception:
            return None

    def _system_under_pressure(self, cpu_pct: float = 0, mem_pct: float = 0,
                               cpu_threshold: float = 90, mem_threshold: float = 90) -> bool:
        try:
            if not cpu_pct:
                cpu_pct = self._get_cpu_percent() or 0
            if not mem_pct:
                mem_pct = self._get_mem_percent() or 0
        except Exception:
            return False
        return cpu_pct > cpu_threshold or mem_pct > mem_threshold

    def _run_node_with_retries(self, run_id: str, node: WBSNode, timeout: int, max_retries: int, split_count: int) -> list[WorkerResult]:
        # Global semaphore: cap concurrent worker processes across all runs.
        self._global_worker_sem.acquire()
        try:
            return self._run_node_with_retries_inner(run_id, node, timeout, max_retries, split_count)
        finally:
            self._global_worker_sem.release()

    def _run_node_with_retries_inner(self, run_id: str, node: WBSNode, timeout: int, max_retries: int, split_count: int) -> list[WorkerResult]:
        self.store.update_node(node.id, "running", run_id=run_id)
        try:
            parent = self._run_worker(run_id, node, timeout)
        except Exception as exc:
            duration = 0.0
            parent = WorkerResult(node.id, node.title, False,
                f"Worker crashed unexpectedly: {type(exc).__name__}: {exc}",
                None, duration, 1, str(exc), node.attempt)
            self.store.worker_finish(f"worker_{run_id}_{node.id}_{node.attempt}", "failed", duration, None, str(exc))
            self.store.log(run_id, "error", "worker crashed in _run_node_with_retries",
                {"node": node.id, "error": str(exc)}, node.id)
        self.store.update_node(node.id,
            "completed" if parent.ok else
            "timeout" if parent.returncode == 124 else "failed",
            parent.result, parent.session_id, parent.duration_seconds,
            None if parent.ok else parent.result, run_id=run_id)
        if parent.ok:
            self._record_node_result(run_id, parent)
        results = [parent]
        if parent.ok:
            return results
        if parent.returncode == 124 and split_count > 1:
            if self._shard_too_deep(node, run_id=run_id):
                self.store.log(
                    run_id, "warning",
                    "anti-avalanche: shard at max depth timed out, not re-splitting",
                    {"node": node.id, "max_shard_depth": self._MAX_SHARD_DEPTH},
                    node.id,
                )
                return results
            self.store.log(run_id, "warning", "node timed out; splitting", {"node": node.id, "split_count": split_count}, node.id)
            shards = self._split_node(node, split_count)
            for shard in shards:
                self.store.insert_wbs_node(run_id, shard.to_dict())
                self.store.update_node(shard.id, "pending", run_id=run_id)
            # Phase 1: run read-only context shards (scope + evidence) in parallel
            phase1 = [s for s in shards if not s.dependencies]
            phase2 = [s for s in shards if s.dependencies]
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(phase1), 4)) as pool:
                futs = {pool.submit(self._run_worker, run_id, s, timeout): s for s in phase1}
                for fut in concurrent.futures.as_completed(futs):
                    shard = futs[fut]
                    try:
                        res = fut.result()
                    except Exception as exc:
                        duration = 0.0
                        res = WorkerResult(shard.id, shard.title, False, f"Shard crashed: {type(exc).__name__}: {exc}", None, duration, 1, "", shard.attempt)
                        self.store.update_node(shard.id, "failed", res.result, None, duration, res.result, run_id=run_id)
                        self.store.log(run_id, "error", "shard future failed", res.to_dict(), shard.id)
                    results.append(res)
                    self.store.update_node(res.node_id, "completed" if res.ok else "failed", res.result, res.session_id, res.duration_seconds, None if res.ok else res.result, run_id=run_id)
                    if res.ok:
                        self._record_node_result(run_id, res)
            # Phase 2: run implementation shards (with phase 1 upstream context)
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(phase2), 4)) as pool:
                futs = {pool.submit(self._run_worker, run_id, s, timeout): s for s in phase2}
                for fut in concurrent.futures.as_completed(futs):
                    shard = futs[fut]
                    try:
                        res = fut.result()
                    except Exception as exc:
                        duration = 0.0
                        res = WorkerResult(shard.id, shard.title, False, f"Shard crashed: {type(exc).__name__}: {exc}", None, duration, 1, "", shard.attempt)
                        self.store.update_node(shard.id, "failed", res.result, None, duration, res.result, run_id=run_id)
                        self.store.log(run_id, "error", "shard future failed", res.to_dict(), shard.id)
                    results.append(res)
                    self.store.update_node(res.node_id, "completed" if res.ok else "failed", res.result, res.session_id, res.duration_seconds, None if res.ok else res.result, run_id=run_id)
                    if res.ok:
                        self._record_node_result(run_id, res)
        # Tier 2: Leader guard retry — if all attempts failed, ask the leader
        # whether to retry with a modified approach before giving up.
        if not any(r.ok for r in results):
            leader_node = self._leader_guard_retry(run_id, node, results[0], node.attempt)
            if leader_node is not None:
                self.store.insert_wbs_node(run_id, leader_node.to_dict())
                self.store.update_node(leader_node.id, "pending", run_id=run_id)
                leader_result = self._run_worker(run_id, leader_node,
                    max(30, timeout // 2))
                self.store.update_node(leader_node.id,
                    "completed" if leader_result.ok else "failed",
                    leader_result.result, leader_result.session_id,
                    leader_result.duration_seconds,
                    None if leader_result.ok else leader_result.result,
                    run_id=run_id)
                if leader_result.ok:
                    self._record_node_result(run_id, leader_result)
                    results.append(leader_result)
                else:
                    results.append(leader_result)
        return results

    def _leader_guard_retry(self, run_id: str, node: WBSNode, result: WorkerResult,
                             attempt: int) -> WBSNode | None:
        """Tier 2: Ask the leader whether to retry a failed node with a modified approach."""
        prompt = (
            f"You are the Leader supervising a multi-agent run.\n\n"
            f"A worker node failed after attempt {attempt}.\n\n"
            f"Node title: {node.title}\n"
            f"Capability: {node.capability}\n"
            f"Original description: {node.description}\n\n"
            f"Worker output:\n{(result.result or '')[:2000]}\n\n"
            f"Error: {result.returncode}\n\n"
            f"Decide whether to retry with a modified approach or give up.\n"
            f"Return ONLY a JSON object on the final line, prefixed by "
            f"HERMES-COLLAB-RESULT:\n"
            f'{{"action":"retry|give_up",'
            f'"description":"modified node description if retrying; empty if give_up"}}'
        )
        try:
            wr = self._run_worker(run_id, node, 60,
                                   model_override=self.leader_model, role="leader")
        except Exception as exc:
            self.store.log(run_id, "error",
                           "leader guard crashed", {"node": node.id, "error": str(exc)},
                           node.id)
            return None
        text = wr.result or ""
        _, parsed = self._parse_result_contract(text)
        if not isinstance(parsed, dict):
            return None
        action = parsed.get("action", "give_up")
        if action != "retry":
            return None
        new_desc = parsed.get("description", "").strip()
        if not new_desc:
            return None
        new_node = WBSNode(
            id=f"{node.id}-retry-{attempt+1}",
            title=node.title + " (retry)",
            description=new_desc,
            capability=node.capability,
            complexity=max(1, node.complexity // 2),
            dependencies=node.dependencies,
            parallelizable=node.parallelizable,
            deliverable=node.deliverable,
        )
        new_node.estimated_duration = max(30, (node.estimated_duration or 120) // 2)
        self.store.log(run_id, "warning", "leader decided to retry node",
                       {"node": node.id, "retry_id": new_node.id,
                        "description": new_desc[:200]}, node.id)
        return new_node

    MAX_RECOVERIES = 3

    def _record_node_result(self, run_id: str, result: WorkerResult) -> None:
        result_text = result.result or ''
        with self._node_results_lock:
            self._node_results[result.node_id] = result_text
            self._node_results_struct[result.node_id] = result.result_struct
            for node in self._current_plan.nodes if self._current_plan else []:
                if node.id == result.node_id:
                    node.status = "completed" if result.ok else "failed"
                    break
        self.store.save_node_result(run_id, result.node_id, result_text, result.result_struct)
        self._save_context_snapshot(run_id, "node_completed", result.node_id)

    def build_context_snapshot(self, run_id: str) -> dict[str, Any]:
        """Public API for building a context snapshot (used by CLI save-snapshot)."""
        return self._build_context_snapshot(run_id)

    def _save_context_snapshot(self, run_id: str, snapshot_type: str, node_id: str | None = None) -> None:
        self.store.save_context_snapshot(run_id, snapshot_type, self._build_context_snapshot(run_id), node_id)

    def _build_context_snapshot(self, run_id: str) -> dict[str, Any]:
        plan = self._current_plan
        nodes: dict[str, dict[str, Any]] = {}
        with self._node_results_lock:
            result_structs = dict(self._node_results_struct)
            result_texts = dict(self._node_results)
        for row in self.store.get_nodes(run_id):
            node_id = row["id"]
            result_struct = result_structs.get(node_id)
            node_snapshot: dict[str, Any] = {"status": row.get("status", "pending")}
            if result_struct:
                quality = result_struct.get("status")
                if node_snapshot["status"] == "completed" and quality is not None:
                    node_snapshot["quality"] = quality
                key_facts = result_struct.get("key_facts") or result_struct.get("summary")
                if key_facts is not None:
                    node_snapshot["key_facts"] = key_facts
            elif node_id in result_texts:
                node_snapshot["key_facts"] = result_texts[node_id]
            nodes[node_id] = node_snapshot
        return {
            "plan_summary": plan.shared_brief if plan else "",
            "nodes": nodes,
            "decisions": [],
            "risk_assessments": list(self._risk_assessments),
            "user_instructions": [],
            "pending_actions": sorted(self._checkpoint_paused_nodes),
        }

    def _shard_depth_of(self, node: WBSNode, run_id: str | None = None) -> int:
        """Compute how deep a node sits in the shard nesting tree.
        Root nodes (no parent_id) are depth 0. A direct shard of a root is
        depth 1. A shard of a shard is depth 2. etc.
        """
        depth = 0
        current_id: str | None = node.id
        seen: set[str] = set()
        by_id = self._plan_nodes_by_id()
        while current_id and current_id not in seen:
            seen.add(current_id)
            current = by_id.get(current_id)
            if current is None:
                row = None
                if run_id is not None:
                    try:
                        row = self.store.get_node(run_id, current_id)
                    except Exception:
                        row = None
                if row is not None:
                    parent_id_val = row.get("parent_id") or None
                    depth += 1 if parent_id_val else 0
                    if not parent_id_val:
                        return depth
                    current_id = parent_id_val
                    continue
                break
            parent_id = getattr(current, "parent_id", None) or None
            if not parent_id:
                return depth
            depth += 1
            current_id = parent_id
            if depth > self._MAX_SHARD_DEPTH + 5:
                break
        return depth

    def _shard_too_deep(self, node: WBSNode, run_id: str | None = None) -> bool:
        depth = self._shard_depth_of(node, run_id=run_id)
        if depth >= self._MAX_SHARD_DEPTH:
            if self._SHARD_DEPTH_WARN_LOG:
                import sys
                print(
                    f"[anti-avalanche] refusing to split node {node.id} "
                    f"(depth={depth}, max={self._MAX_SHARD_DEPTH}); "
                    f"node will return failure as-is",
                    file=sys.stderr,
                    flush=True,
                )
            return True
        return False

    def _split_node(self, node: WBSNode, split_count: int) -> list[WBSNode]:
        """Split an over-budget node into shards.

        For development tasks: Phase 1 (parallel, read-only) scope + evidence,
        Phase 2 (depends on phase 1) implementation.
        For operation/execution tasks: skip research phase, go straight to execution.
        """
        parent_fingerprint = self._node_fingerprint(node)
        # Detect operation tasks — skip research phases
        _is_operation = False
        try:
            tt = self._current_plan.task_type if self._current_plan else "development"
            _is_operation = tt == "operation"
        except Exception:
            pass
        if _is_operation:
            # Operation tasks: skip scope/evidence, only create execution shards
            impl_shards = []
            for i in range(max(1, split_count)):
                impl_shard = WBSNode(
                    id=f"{node.id}-impl-{i+1}",
                    title=f"{node.title} / execution-{i+1}",
                    description=(
                        f"Shard from over-budget operation parent — EXECUTION phase.\n"
                        f"You MUST execute the task using available tools "
                        f"(browser, search, commands). Do NOT analyze or research.\n"
                        f"Execute directly.\n\n"
                        f"Original task:\n{node.description}"
                    ),
                    capability="implementation",
                    complexity=max(1, node.complexity - 2),
                    dependencies=[],
                    parallelizable=True,
                    deliverable=f"Operation executed successfully",
                    parent_id=node.id,
                    attempt=node.attempt + 1,
                    brief=node.brief,
                    write_targets=list(node.write_targets),
                    fingerprint=f"{parent_fingerprint}:impl:{i}",
                )
                impl_shards.append(impl_shard)
            return impl_shards

        # Phase 1: read-only context shards (development tasks only)
        scope_shard = WBSNode(
            id=f"{node.id}-scope-1",
            title=f"{node.title} / scope",
            description=(
                f"Shard from over-budget parent — SCOPE phase (read-only).\n"
                f"Find the smallest relevant scope, entrypoints, and file locations.\n"
                f"Read files but do NOT modify anything.\n\n"
                f"Original task:\n{node.description}"
            ),
            capability="analysis",
            complexity=max(1, node.complexity - 3),
            dependencies=[],
            parallelizable=True,
            deliverable="Scope summary: files, symbols, entrypoints to change",
            parent_id=node.id,
            attempt=node.attempt + 1,
            brief=node.brief,
            write_targets=[],
            fingerprint=f"{parent_fingerprint}:scope",
        )
        evidence_shard = WBSNode(
            id=f"{node.id}-evidence-2",
            title=f"{node.title} / evidence",
            description=(
                f"Shard from over-budget parent — EVIDENCE phase (read-only).\n"
                f"Collect exact file paths, commands, symbols, and evidence.\n"
                f"Read files but do NOT modify anything.\n\n"
                f"Original task:\n{node.description}"
            ),
            capability="analysis",
            complexity=max(1, node.complexity - 3),
            dependencies=[],
            parallelizable=True,
            deliverable="Evidence: exact paths, line numbers, symbols found",
            parent_id=node.id,
            attempt=node.attempt + 1,
            brief=node.brief,
            write_targets=[],
            fingerprint=f"{parent_fingerprint}:evidence",
        )
        # Phase 2: implementation shards that depend on phase 1
        impl_shards = []
        for i in range(max(1, split_count - 2)):
            impl_shard = WBSNode(
                id=f"{node.id}-impl-{i+3}",
                title=f"{node.title} / implementation-{i+1}",
                description=(
                    f"Shard from over-budget parent — "
                    f"{'EXECUTION' if 'implement' not in (node.capability or '').lower() else 'IMPLEMENTATION'} phase.\n"
                    f"You MUST "
                    f"{'execute the task using available tools (browser, search, commands)' if 'implement' not in (node.capability or '').lower() else 'write actual code changes to files'}"
                    f". Do NOT just produce a plan.\n"
                    f"Use the upstream context from scope and evidence shards to guide your actions.\n"
                    f"Focus on a distinct subset of the original task.\n\n"
                    f"Original task:\n{node.description}"
                ),
                capability="implementation",
                complexity=max(1, node.complexity - 2),
                dependencies=[scope_shard.id, evidence_shard.id],
                parallelizable=True,
                deliverable=f"Working implementation (files modified)",
                parent_id=node.id,
                attempt=node.attempt + 1,
                brief=node.brief,
                write_targets=list(node.write_targets),
                fingerprint=f"{parent_fingerprint}:impl:{i}",
            )
            impl_shards.append(impl_shard)
        return [scope_shard, evidence_shard] + impl_shards

    def _plan_nodes_by_id(self) -> dict[str, WBSNode]:
        plan = self._current_plan
        if plan is None:
            return {}
        return {node.id: node for node in plan.nodes}

    def _ancestor_tiers(self, node: WBSNode) -> list[tuple[int, str]]:
        by_id = self._plan_nodes_by_id()
        tiers: list[tuple[int, str]] = []
        seen: set[str] = set()
        frontier = [(dep_id, 1) for dep_id in node.dependencies]
        while frontier:
            dep_id, depth = frontier.pop(0)
            if dep_id in seen:
                continue
            seen.add(dep_id)
            tiers.append((depth, dep_id))
            dep_node = by_id.get(dep_id)
            if dep_node is not None:
                frontier.extend((parent_id, depth + 1) for parent_id in dep_node.dependencies)
        return tiers

    def _cap_for_tier(self, depth: int) -> int:
        if depth <= 1:
            return min(self._UPSTREAM_PARENT_CAP, self._UPSTREAM_PER_CAP)
        if depth == 2:
            return self._UPSTREAM_GRANDPARENT_CAP
        return self._UPSTREAM_ANCESTOR_CAP

    def _context_text_for_node(self, node_id: str, text: str | None, struct: dict[str, Any] | None) -> str:
        if not struct:
            return text or ''
        parts = []
        status = struct.get("status")
        summary = struct.get("summary")
        if status:
            parts.append(f"status: {status}")
        if summary:
            parts.append(f"summary: {summary}")
        files = struct.get("files_modified") or struct.get("files")
        if files:
            parts.append(f"files: {files}")
        verification = struct.get("verification")
        if verification:
            parts.append(f"verification: {verification}")
        if parts:
            return "\n".join(parts)
        return text or ''

    def _build_upstream_context(self, node) -> str:
        if not node.dependencies:
            return ''
        tiers = self._ancestor_tiers(node) or [(1, dep_id) for dep_id in node.dependencies]
        with self._node_results_lock:
            text_snapshot = {dep_id: self._node_results.get(dep_id) for _depth, dep_id in tiers}
            struct_snapshot = {dep_id: self._node_results_struct.get(dep_id) for _depth, dep_id in tiers}
        # Step 1: collect all texts with per-tier caps applied (no total-cap truncation)
        dep_texts: dict[str, tuple[int, str]] = {}
        for depth, dep_id in tiers:
            text = self._context_text_for_node(dep_id, text_snapshot.get(dep_id), struct_snapshot.get(dep_id))
            if not text:
                continue
            cap = self._cap_for_tier(depth)
            if len(text) > cap:
                text = '[truncated]\n' + text[-(cap - len('[truncated]\n')):]
            dep_texts[dep_id] = (depth, text)
        if not dep_texts:
            return ''
        # Step 2: build labeled entries
        kept: list[str] = []
        for dep_id, (depth, text) in dep_texts.items():
            label = "parent" if depth == 1 else "grandparent" if depth == 2 else f"ancestor depth {depth}"
            kept.append(f"--- from {dep_id} ({label}) ---\n{text}")
        # Step 3: return full context if it fits, otherwise summarize via leader LLM
        total_size = sum(len(e) for e in kept) + (len(kept) - 1) * 2  # \n\n separators between entries
        if total_size <= self._UPSTREAM_TOTAL_CAP:
            return 'Upstream context (from completed dependency nodes):\n' + '\n\n'.join(kept) + '\n\n'
        summary = self._summarize_context_for_worker({dep_id: text for dep_id, (_, text) in dep_texts.items()})
        return (
            'Upstream context (from completed dependency nodes):\n'
            '--- summarized (full context exceeded capacity) ---\n'
            f'{summary}\n\n'
        )

    def _summarize_context_for_worker(self, dep_texts: dict[str, str]) -> str:
        """Call the leader LLM to produce a ~200-token summary of upstream node results.

        Falls back to head truncation if the LLM call fails or times out.
        """
        if not dep_texts:
            return ''
        parts = []
        for dep_id, text in dep_texts.items():
            parts.append(f"--- {dep_id} ---\n{text[:2000]}")
        full_text = "\n\n".join(parts)
        prompt = (
            "You are summarizing upstream worker results for a downstream worker. "
            "Produce a concise 200-token summary preserving:\n"
            "1. Key status and outcomes\n"
            "2. Files modified or analyzed\n"
            "3. Verification commands or checks needed\n"
            "4. Important findings the downstream worker must know\n\n"
            "Upstream results:\n\n"
            f"{full_text[:8000]}\n\n"
            "Summary (200 tokens max, plain text, no JSON):"
        )
        cmd = ["claude", "-p", prompt, "--output-format", "json"]
        if self.leader_model:
            cmd.extend(["--model", self.leader_model])
        try:
            proc = subprocess.run(
                cmd, cwd=self.cwd, text=True, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
            )
            if proc.returncode == 0:
                outer = json.loads(proc.stdout)
                summary = outer.get("result", "").strip()
                if summary:
                    return summary
        except Exception:
            pass
        kept_lines: list[str] = []
        remaining = 2000
        for dep_id, text in dep_texts.items():
            snippet = text
            if len(snippet) > remaining:
                snippet = snippet[:remaining]
            kept_lines.append(f"--- {dep_id} ---\n{snippet}")
            remaining -= len(snippet)
            if remaining <= 0:
                break
        return "Fallback summary (LLM summarization failed):\n" + "\n\n".join(kept_lines)

    def _shared_brief_for_worker(self, node: WBSNode) -> str:
        plan = self._current_plan
        if plan is None or not plan.shared_brief:
            return ""
        if node.capability != "implementation":
            return ""
        if plan.shared_brief in (node.brief or ""):
            return ""
        return f"Completed work summary (upstream workers):\n{plan.shared_brief}\n\n"

    def _task_text_for_worker(self, node: WBSNode) -> str:
        return "\n".join(part for part in (node.title, node.deliverable, node.brief, node.description) if part)

    def _preallocate_skills_tools(self, run_id: str, nodes: list[WBSNode], task_type: str = "development") -> None:
        """Pre-compute skills/tools for all nodes before workers start.

        Called once after WBS decomposition, before worker dispatch.
        Stores results in node.skills_json and node.tools_json so workers
        skip per-worker registry traversal.
        Respects leader-assigned skills_json/tools_json when present.
        Filters out built-in (source="hermes") entries that overlap with
        the agent's native capabilities.  Non-built-in entries (web-ui, mcp,
        etc.) are always preserved regardless of capability overlap.

        If the run was triggered via a package (Leader selected a scenario
        bundle), the package's Skill collection is treated as the canonical
        skill set for *every* node unless a node already has skills_json.
        """
        native_caps = set(self.agent_backend.capabilities)
        for node in nodes:
            try:
                # Respect leader-assigned values; only fill gaps via distributor
                if not node.skills_json or not node.tools_json:
                    # Determine leader_skills for the distributor:
                    #   1. Leader-assigned skills_json on the node
                    #   2. Package node_skill_map → only this node's skill
                    #   3. Package-level skills (scenario override, fallback)
                    #   4. None → fall back to capability default
                    if node.skills_json:
                        leader_skills = json.loads(node.skills_json)
                    else:
                        leader_skills = None

                    skill_names, tool_profile_names = self._skill_distributor.resolve_for_node(
                        node_capability=node.capability,
                        leader_skills=leader_skills,
                        agent_backend=self.agent_backend,
                    )
                    if not node.skills_json:
                        node.skills_json = json.dumps(skill_names)
                    if not node.tools_json:
                        node.tools_json = json.dumps(tool_profile_names)

                # Native capability overlap filtering is intentionally disabled.
                # Even when an agent has a native capability (e.g. file-edit),
                # the skill content provides task-specific guidance that is
                # still useful to the worker (e.g. "make the smallest change").
                self.store.update_node_skills_tools(node.id, node.skills_json, node.tools_json)
            except Exception:
                # Pre-allocation failure is non-fatal; worker falls back to per-worker selection
                self.store.log(run_id, "warning", "skill/tool pre-allocation failed", {"node": node.id}, node.id)

    def _env_for_role(self, role: str) -> dict[str, str]:
        prefix = f"HERMES_COLLAB_{role.upper()}_"
        env = os.environ.copy()
        provider = getattr(self, 'provider', None)

        if provider is not None:
            from .provider import env_targets_for_protocol, apply_provider_to_env  # type: ignore[import-not-found]
            targets = env_targets_for_protocol(provider.protocol)
            # 1. Role-prefixed overrides (HERMES_COLLAB_WORKER_*) from base env
            for source_suffix, target_keys in targets.items():
                value = os.environ.get(prefix + source_suffix.upper())
                if value:
                    for target in target_keys:
                        env[target] = value
            for src_key, dst_key in provider.env_aliases.items():
                value = os.environ.get(prefix + src_key.upper()) or os.environ.get(src_key)
                if value:
                    env[dst_key] = value
            # 2. Provider profile values win over everything for protocol-specific vars
            apply_provider_to_env(env, provider, model_override=None)
            # 3. Model override: instance attrs (from CLI args) win
            model_val = (self.worker_model if role == "worker"
                         else self.leader_model if role == "leader"
                         else None)
            if model_val:
                model_targets = targets.get("model")
                if model_targets:
                    env[model_targets[0]] = model_val
                else:
                    env["ANTHROPIC_MODEL"] = model_val
        else:
            # 1. Role-prefixed env var overrides (no provider)
            value_map = {
                "API_KEY": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
                "AUTH_TOKEN": ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"),
                "BASE_URL": ("ANTHROPIC_BASE_URL",),
                "MODEL": ("ANTHROPIC_MODEL",),
            }
            for source_suffix, targets in value_map.items():
                value = os.environ.get(prefix + source_suffix)
                if not value:
                    continue
                for target in targets:
                    env[target] = value
            # Always override ANTHROPIC_MODEL with the instance attribute
            if role == "worker" and self.worker_model:
                env["ANTHROPIC_MODEL"] = self.worker_model
            elif role == "leader" and self.leader_model:
                env["ANTHROPIC_MODEL"] = self.leader_model
        # 2. Profile-based values with fallback chain
        profile = self._leader_profile if role == "leader" else self._worker_profile
        if profile is not None:
            runtime_config: dict[str, Any] | None = None
            runtime_config_path = self.cwd / ".runtime-config.json"
            if runtime_config_path.is_file():
                try:
                    with open(runtime_config_path) as f:
                        runtime_config = json.load(f)
                except (json.JSONDecodeError, OSError):
                    pass
            _ANTHROPIC_DEFAULTS: dict[str, str] = {}
            field_map: dict[str, str] = {
                "base_url": "ANTHROPIC_BASE_URL",
                "api_key": "ANTHROPIC_API_KEY",
                "model": "ANTHROPIC_MODEL",
            }
            for profile_key, env_key in field_map.items():
                profile_val = profile.get(profile_key)
                if profile_val:
                    env[env_key] = profile_val
                elif runtime_config and env_key in runtime_config:
                    env[env_key] = runtime_config[env_key]
                elif env_key not in env:
                    default = _ANTHROPIC_DEFAULTS.get(env_key)
                    if default:
                        env[env_key] = default
        # When using opencode backend, mirror ANTHROPIC_* env vars as
        # OPENCODE_* so the subprocess can pick them up correctly.
        if self.agent_backend.name == "opencode":
            _opc_src_map = {
                "ANTHROPIC_BASE_URL": "OPENCODE_BASE_URL",
                "ANTHROPIC_API_KEY": "OPENCODE_API_KEY",
            }
            for _src, _dst in _opc_src_map.items():
                if _src in env:
                    env[_dst] = env[_src]
        # Apply reasoning env vars from the agent backend
        for _k, _v in self.agent_backend.reasoning_env.items():
            if _v:
                env[_k] = _v
        # 3. Git credentials
        git_value_map = {
            "GIT_TOKEN": "HERMES_COLLAB_GIT_TOKEN",
            "GIT_USERNAME": "HERMES_COLLAB_GIT_USERNAME",
            "GIT_ALLOWED_HOSTS": "HERMES_COLLAB_GIT_ALLOWED_HOSTS",
            "GIT_CREDENTIAL_HELPER": "HERMES_COLLAB_GIT_CREDENTIAL_HELPER",
        }
        for source_suffix, target in git_value_map.items():
            value = os.environ.get(prefix + source_suffix)
            if value:
                env[target] = value
        self._configure_git_credentials(env)
        # Safety net: ensure PATH is never empty when spawning subprocesses.
        # systemd's default PATH is minimal; this prevents cryptic ENOENT errors
        # for tools like `hermes` (leader aggregator) installed in standard dirs.
        if not env.get("PATH", "").strip():
            env["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin"
        return env

    def _append_git_config(self, env: dict[str, str], key: str, value: str) -> None:
        try:
            index = int(env.get("GIT_CONFIG_COUNT", "0"))
        except ValueError:
            index = 0
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
        env["GIT_CONFIG_COUNT"] = str(index + 1)

    def _configure_git_credentials(self, env: dict[str, str]) -> None:
        env.setdefault("GIT_TERMINAL_PROMPT", "0")
        helper = env.get("HERMES_COLLAB_GIT_CREDENTIAL_HELPER")
        token = env.get("HERMES_COLLAB_GIT_TOKEN")
        if helper:
            self._append_git_config(env, "credential.helper", helper)
            return
        if not token:
            return
        env.setdefault("HERMES_COLLAB_GIT_USERNAME", "x-access-token")
        env.setdefault("HERMES_COLLAB_GIT_ALLOWED_HOSTS", "github.com")
        self._append_git_config(
            env,
            "credential.helper",
            "!f() { "
            'test "$1" = get || exit 0; '
            "protocol=; host=; "
            'while IFS= read -r line; do '
            'case "$line" in protocol=*) protocol=${line#protocol=};; host=*) host=${line#host=};; esac; '
            "done; "
            'test "$protocol" = https || exit 0; '
            'case ",${HERMES_COLLAB_GIT_ALLOWED_HOSTS}," in *,"$host",*) ;; *) exit 0;; esac; '
            'test -n "$HERMES_COLLAB_GIT_TOKEN" || exit 0; '
            "printf 'username=%s\\npassword=%s\\n' \"$HERMES_COLLAB_GIT_USERNAME\" \"$HERMES_COLLAB_GIT_TOKEN\"; "
            "}; f",
        )

    def _force_kill_subprocess_tree(self, exc: "subprocess.TimeoutExpired | None", tmp_path: str | None, *, worker_role: str = "worker") -> None:
        """Kill the whole process group of a timed-out subprocess and clean up tmp files.

        Uses subprocess.Popen + communicate() for worker execution so the
        process pid is available for killpg when a timeout fires.
        """
        import os
        import signal
        import gc

        pgid_pid = None
        if exc is not None and hasattr(exc, "__subprocess_pid"):
            pgid_pid = exc.__subprocess_pid  # type: ignore[attr-defined]
        if pgid_pid is None:
            # Fallback: use psutil to find children
            try:
                import psutil  # type: ignore
            except ImportError:
                psutil = None
            if psutil is not None:
                try:
                    parent = psutil.Process(os.getpid())
                    for child in parent.children(recursive=True):
                        try:
                            if child.pid != child.ppid:
                                os.killpg(child.pid, signal.SIGKILL)
                        except (ProcessLookupError, PermissionError, OSError):
                            pass
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        else:
            try:
                os.killpg(pgid_pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    os.kill(pgid_pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass

        if exc is not None:
            for stream in (getattr(exc, "stdout", None), getattr(exc, "stderr", None)):
                if stream is None:
                    continue
                try:
                    stream.close() if hasattr(stream, "close") else None
                except Exception:
                    pass

        gc.collect()

        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def _parse_result_contract(self, text: str) -> tuple[dict[str, Any] | None, str | None]:
        """Parse the HERMES-COLLAB-RESULT contract line from worker output.

        Lenient parser with soft fallback — when the contract is missing or
        invalid, returns a synthetic struct so downstream code can continue.
        """
        scan_text = text[-8192:] if len(text) > 8192 else text
        marker_idx = scan_text.rfind(self._RESULT_MARKER)
        if marker_idx < 0:
            for variant in ("HERMES-COLLAB-RESULT", "Hermes-Result:",
                           "HERMES_RESULT:", "RESULT:", "H-C-RESULT:"):
                idx = scan_text.rfind(variant)
                if idx >= 0:
                    marker_idx = idx
                    break
        if marker_idx >= 0:
            raw = scan_text[marker_idx:].split(":", 1)[-1].strip()
            if raw.startswith("```"):
                raw = raw.strip("`").lstrip("json").strip()
            for line in raw.splitlines():
                line = line.strip().rstrip(",")
                if not line:
                    continue
                if line.startswith("{"):
                    parsed = self._try_parse_json(line)
                    if parsed is not None:
                        return parsed, None
            parsed = self._try_parse_json(raw)
            if parsed is not None:
                return parsed, None
        parsed = self._extract_first_json_object(scan_text)
        if parsed is not None:
            return parsed, None
        summary = text.strip()[:240] if text else ""
        soft = {
            "status": "ok",
            "summary": summary,
            "files_modified": [],
            "verification": [],
            "notes": [
                "contract missing or invalid — engine synthesised a soft "
                "result_struct from raw worker text"
            ],
        }
        return soft, None

    @staticmethod
    def _try_parse_json(text: str) -> dict[str, Any] | None:
        """Try json.loads with progressively looser escape repair."""
        try:
            v = json.loads(text)
            return v if isinstance(v, dict) else None
        except json.JSONDecodeError:
            pass
        for transform in (
            lambda s: s.replace("\n", "\\n").replace("\t", "\\t"),
            lambda s: s.replace("\\", "\\\\"),
        ):
            try:
                v = json.loads(transform(text))
                return v if isinstance(v, dict) else None
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _extract_first_json_object(text: str) -> dict[str, Any] | None:
        """Find the first balanced ``{...}`` in `text` and parse it."""
        depth = 0
        start: int | None = None
        for i, ch in enumerate(text):
            if ch == "{":
                if start is None:
                    start = i
                depth += 1
            elif ch == "}":
                if depth > 0:
                    depth -= 1
                    if depth == 0 and start is not None:
                        candidate = text[start : i + 1]
                        try:
                            v = json.loads(candidate)
                            if isinstance(v, dict):
                                return v
                        except json.JSONDecodeError:
                            pass
                        start = None
        return None

    def _run_worker(self, run_id: str, node: WBSNode, timeout: int, model_override: str | None = None, role: str = "worker") -> WorkerResult:
        # Resolve per-role agent without mutating self.agent_backend (thread-safe)
        backend = self.agent_backend
        if role == "leader" and self.leader_agent:
            backend = self.leader_agent
        elif role == "worker" and self.worker_agent:
            backend = self.worker_agent
        return self._run_worker_impl(run_id, node, timeout, model_override, role, backend=backend)

    def _run_worker_impl(self, run_id: str, node: WBSNode, timeout: int, model_override: str | None = None, role: str = "worker", backend: AgentBackend | None = None) -> WorkerResult:
        if backend is None:
            backend = self.agent_backend
        worker_id = f"worker_{run_id}_{node.id}_{node.attempt}"
        self.store.worker_start(worker_id, run_id, node.id)
        self.store.log(run_id, "info", "worker started", {"node": node.id, "title": node.title, "agent": backend.name}, node.id)
        started = time.time()
        upstream_block = self._build_upstream_context(node)
        shared_brief_block = self._shared_brief_for_worker(node)
        brief_block = f"Brief:\n{node.brief}\n\n" if node.brief else ""
        if node.skills_json and node.tools_json:
            skill_names = json.loads(node.skills_json)
            tool_profile_names = json.loads(node.tools_json)
        else:
            # Fallback: resolve both via SkillDistributor when either is missing
            skill_names, tool_profile_names = self._skill_distributor.resolve_for_node(
                node_capability=node.capability,
                leader_skills=None,
                agent_backend=backend,
            )
            node.skills_json = json.dumps(skill_names)
            node.tools_json = json.dumps(tool_profile_names)
        mcp_servers = self._skill_distributor.resolve_mcp(skill_names, backend.name)
        skills_block, tools_block, mcp_block = self._skill_distributor.render_for_prompt(
            skill_names, tool_profile_names, mcp_servers,
        )
        # Resolve allowed_tools for the tool whitelist
        tool_allowed = tool_profile_names
        # Tool manager acts as whitelist: if profiles matched, use only their tools;
        # if no profiles matched, fall back to backend defaults
        if tool_allowed:
            final_allowed = tool_allowed
        else:
            final_allowed = list(backend.default_allowed_tools)
        # Skills/tools are pre-allocated by Leader at WBS time; no per-worker log needed
        # Persist skills/tools to node for dashboard display
        import json as _json
        self.store.update_node_skills_tools(node.id, _json.dumps(skill_names), _json.dumps(tool_profile_names))
        write_targets = self._node_write_targets(node)
        write_block = ""
        if write_targets:
            write_block = "Write targets reserved for this worker: " + ", ".join(sorted(write_targets)) + "\nOnly modify files under these repository-relative targets.\n\n"
        prompt = f"""{backend.prompt_prefix}

WBS node: {node.title}
Capability: {node.capability}
Deliverable: {node.deliverable}

{skills_block}{tools_block}{mcp_block}{write_block}{shared_brief_block}{brief_block}{upstream_block}Task:
{node.description}

Work in cwd: {self.cwd}
Return the deliverable. If you modify files, state exact paths. If read-only, do not modify files.

Output contract:
- First, write the human-readable deliverable for the user.
- On the final line, include exactly one machine-readable JSON object prefixed by {self._RESULT_MARKER}
- Use this JSON shape: {{"status":"ok|blocked|failed","summary":"short result summary","files_modified":["path"],"verification":["command or check"],"notes":["optional note"]}}
{backend.prompt_suffix}"""
        # 2026-06-20 fix: hard cap prompt size to prevent ARG_MAX overflow.
        # Truncate by BYTES (not chars) because CJK UTF-8 is 3 bytes/char.
        _PROMPT_SAFE_MAX_BYTES = 500_000  # 500 KB, well under 2 MB ARG_MAX
        _prompt_bytes = prompt.encode("utf-8", errors="replace")
        _pb_len = len(_prompt_bytes)
        if _pb_len > _PROMPT_SAFE_MAX_BYTES:
            _half = _PROMPT_SAFE_MAX_BYTES // 2
            _msg = "\n\n...[PROMPT TRUNCATED: " + str(_pb_len) + " bytes > " + str(_PROMPT_SAFE_MAX_BYTES) + " limit]...\n\n"
            _msg_b = _msg.encode("utf-8")
            # Take first half bytes and last half bytes, decode back safely
            prompt = _prompt_bytes[:_half].decode("utf-8", errors="ignore") \
                   + _msg \
                   + _prompt_bytes[-_half:].decode("utf-8", errors="ignore")
            self.store.log(run_id, "warning", "prompt truncated for ARG_MAX",
                {"node": node.id, "original_bytes": _pb_len,
                 "truncated_to": _PROMPT_SAFE_MAX_BYTES}, node.id)
        # 2026-06-21: second pass — hard cap at 900KB for safety
        _pb2 = prompt.encode("utf-8", errors="replace")
        if len(_pb2) > 900_000:
            _half2 = 450_000
            prompt = _pb2[:_half2].decode("utf-8", errors="ignore") \
                   + "\n...[FINAL ARG_MAX SAFETY TRUNCATION: " + str(len(_pb2)) + " bytes > 900KB limit]...\n" \
                   + _pb2[-_half2:].decode("utf-8", errors="ignore")
        selected_model = model_override or self.worker_model
        # Apply backend auto_prefix (e.g. "opencode-go/") for both session and one-shot paths
        if backend.auto_prefix and selected_model and not selected_model.startswith(backend.auto_prefix):
            selected_model = backend.auto_prefix + selected_model

        # If prompt is too long for command-line args, use stdin via temp file.
        # Only applies to backends with a prompt_flag (e.g. claude -p "...");
        # positional-arg backends (opencode run "...") take prompt directly
        # as a positional subprocess argument, which is safe up to ARG_MAX (~2MB).
        _PROMPT_ARG_MAX = 100_000  # conservative limit for -p argument
        use_stdin = bool(backend.prompt_flag) and len(prompt.encode("utf-8", errors="replace")) > _PROMPT_ARG_MAX
        tmp_path: str | None = None
        if use_stdin:
            import tempfile
            tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8")
            tmp.write(prompt)
            tmp.close()
            tmp_path = tmp.name
            cmd = backend.build_command(
                prompt="",  # empty -p, actual content via stdin
                model=selected_model,
                allowed_tools=final_allowed,
                provider=self.provider,
                reasoning=True,
            )
        else:
            cmd = backend.build_command(
                prompt=prompt,
                model=selected_model,
                allowed_tools=final_allowed,
                provider=self.provider,
                reasoning=True,
            )
        # ── Persistent session or one-shot Popen ──
        if backend.supports_sessions():
            # Use persistent session (backend manages subprocess lifecycle)
            import tempfile as _tf
            _tmp_stdout = _tf.NamedTemporaryFile(mode="w+", suffix=".out", delete=False, encoding="utf-8")
            _tmp_stderr = _tf.NamedTemporaryFile(mode="w+", suffix=".err", delete=False, encoding="utf-8")
            _tmp_stdout_path = _tmp_stdout.name
            _tmp_stderr_path = _tmp_stderr.name
            _tmp_stdout.close()
            _tmp_stderr.close()
            # ── Check for saved agent session ID for resume ──
            _saved_agent_sid: str | None = None
            try:
                _conv_row = self.store._one("SELECT session_id FROM runs WHERE id=?", (run_id,))
                _conv_sid = _conv_row["session_id"] if _conv_row else None
                if _conv_sid:
                    _saved_agent_sid = self.store.get_agent_session(_conv_sid)
            except Exception:
                pass
            if _saved_agent_sid:
                handle = backend.create_session(
                    prompt,
                    cwd=str(self.cwd),
                    model=selected_model,
                    stdout_path=_tmp_stdout_path,
                    stderr_path=_tmp_stderr_path,
                    session_id=_saved_agent_sid,
                )
            else:
                handle = backend.create_session(
                    prompt,
                    cwd=str(self.cwd),
                    model=selected_model,
                    stdout_path=_tmp_stdout_path,
                    stderr_path=_tmp_stderr_path,
                )
            self._worker_sessions[node.id] = handle
            proc = handle.proc
            with self._worker_procs_lock:
                self._worker_procs[node.id] = proc
        else:
            stdin_data = open(tmp_path, "r").read() if tmp_path else None
            # 2026-06-19 fix: Use temp files instead of PIPE for stdout/stderr.
            # PIPE-based proc.communicate() hangs forever if the subprocess forks
            # a daemon that inherits the pipe FD — EOF never arrives. Temp files
            # eliminate the dependency on pipe EOF entirely. The subprocess writes
            # to files, so even if it forks daemon children, the files close properly
            # when the subprocess exits. proc.communicate(timeout=T) then reliably
            # raises TimeoutExpired.
            import tempfile as _tf
            _tmp_stdout = _tf.NamedTemporaryFile(mode="w+", suffix=".out", delete=False, encoding="utf-8")
            _tmp_stderr = _tf.NamedTemporaryFile(mode="w+", suffix=".err", delete=False, encoding="utf-8")
            _tmp_stdout_path = _tmp_stdout.name
            _tmp_stderr_path = _tmp_stderr.name
            run_kwargs = {
                "cwd": self.cwd,
                "env": self._env_for_role(role),
                "text": True,
                "stdout": _tmp_stdout,
                "stderr": _tmp_stderr,
                "start_new_session": True,
            }
            if use_stdin:
                run_kwargs["input"] = stdin_data
            else:
                run_kwargs["stdin"] = subprocess.DEVNULL
            # Use Popen so external code can kill the process tree
            proc = subprocess.Popen(cmd, **run_kwargs)
            # Close the temp file handles in the parent so the subprocess is sole writer.
            _tmp_stdout.close()
            _tmp_stderr.close()
            with self._worker_procs_lock:
                self._worker_procs[node.id] = proc
        try:
            child_stdout, child_stderr = proc.communicate(timeout=timeout)
            duration = round(time.time() - started, 3)
            # Read output from temp files
            try:
                with open(_tmp_stdout_path, "r", encoding="utf-8") as _f:
                    _stdout_text = _f.read()
                with open(_tmp_stderr_path, "r", encoding="utf-8") as _f:
                    _stderr_text = _f.read()
            except (FileNotFoundError, OSError):
                _stdout_text = ""
                _stderr_text = ""
            # Filter codex PTY startup banner from stderr
            if backend and backend.name == "codex" and _stderr_text:
                _stderr_lines = _stderr_text.split("\n")
                _filtered = []
                _skip_count = 0
                for _line in _stderr_lines:
                    if any(_s in _line for _s in [
                        "OpenAI Codex", "Reading additional", "--------",
                        "workdir:", "model:", "provider:", "sandbox:",
                    ]):
                        _skip_count += 1
                        continue
                    _filtered.append(_line)
                _stderr_text = "\n".join(_filtered)
            proc.stdout = _stdout_text
            proc.stderr = _stderr_text
        except subprocess.TimeoutExpired as exc:
            self._force_kill_subprocess_tree(exc, tmp_path, worker_role=role)
            duration = round(time.time() - started, 3)
            try:
                with open(_tmp_stdout_path, "r", encoding="utf-8") as _f:
                    _stdo = _f.read()
            except (FileNotFoundError, OSError):
                _stdo = ""
            try:
                with open(_tmp_stderr_path, "r", encoding="utf-8") as _f:
                    _stde = _f.read()
            except (FileNotFoundError, OSError):
                _stde = ""
            result = WorkerResult(node.id, node.title, False, f"Timed out after {timeout}s", None, duration, 124, _stde, node.attempt)
            self.store.worker_finish(worker_id, "timeout", duration, None, result.result)
            self.store.log(run_id, "warning", "worker timeout", result.to_dict(), node.id)
            for _p in (_tmp_stdout_path, _tmp_stderr_path):
                try: os.unlink(_p)
                except OSError: pass
            return result
        finally:
            with self._worker_procs_lock:
                self._worker_procs.pop(node.id, None)
            # Cleanup worker session if one was created
            _handle = self._worker_sessions.pop(node.id, None)
            if _handle is not None:
                try:
                    backend.close_session(_handle)
                except Exception:
                    pass
            # Codex cleanup: kill orphaned codex processes
            if backend and backend.name == "codex" and proc:
                try:
                    _pgid = os.getpgid(proc.pid)
                    os.killpg(_pgid, 9)  # SIGKILL
                except (ProcessLookupError, PermissionError, AttributeError):
                    pass
                import subprocess as _sp_codex
                _sp_codex.run(["pkill", "-9", "-f", "codex.*exec"],
                             capture_output=True, timeout=5)
            for _p in (_tmp_stdout_path, _tmp_stderr_path):
                try: os.unlink(_p)
                except OSError: pass

        # Check if this worker was killed by the resource-pressure watchdog
        if node.id in self._resource_timeout_nodes:
            self._resource_timeout_nodes.discard(node.id)
            result = WorkerResult(node.id, node.title, False,
                "timeout: system resource pressure", None, duration, 124, "", node.attempt)
            self.store.worker_finish(worker_id, "timeout", duration, None, result.result)
            self.store.log(run_id, "warning", "worker killed by resource-pressure watchdog",
                {"node": node.id, "duration": round(duration, 1)}, node.id)
            return result

        # Use agent backend to parse output
        parsed = backend.parse_output(
            stdout=proc.stdout,
            stderr=proc.stderr,
            returncode=proc.returncode,
            node_id=node.id,
            node_title=node.title,
            duration=duration,
            attempt=node.attempt,
        )
        ok = parsed["ok"]
        text = parsed["result"]
        session_id = parsed["session_id"]
        result_struct, contract_error = self._parse_result_contract(text)
        # On 2026-06-17 the parser was made lenient: it almost always
        # returns a usable struct (possibly a soft fallback) and
        # contract_error is usually None. The lesson is recorded only
        # when the parser actually had to fall back, which we can
        # detect from the soft-fallback note in the struct.
        used_soft_fallback = bool(
            result_struct
            and result_struct.get("notes")
            and any("soft" in n.lower() for n in result_struct["notes"])
        )
        if ok and (contract_error or used_soft_fallback):
            reason = contract_error or "soft fallback used (no contract found)"
            self.store.log(run_id, "warning", "worker result contract missing or invalid", {"node": node.id, "error": reason}, node.id)
            self.store.add_lesson(
                "worker-contract",
                "Workers should end successful output with a valid HERMES-COLLAB-RESULT JSON line so downstream context can use structured summaries.",
                {"run_id": run_id, "node": node.id, "error": reason},
            )
        result = WorkerResult(node.id, node.title, ok, text, session_id, duration, proc.returncode, proc.stderr.strip(), node.attempt, result_struct)
        self.store.worker_finish(worker_id, "completed" if ok else "failed", duration, session_id, None if ok else text)
        # CRITICAL: also update the wbs_node status to match the worker status.
        # Without this, wbs_nodes stays 'running' forever after a successful worker,
        # causing aggregate nodes (and parent runs) to appear stuck.
        self.store.update_node(
            node.id,
            "completed" if ok else "failed",
            text,
            session_id,
            duration,
            None if ok else text,
            run_id=run_id,
        )
        self.store.log(run_id, "info" if ok else "error", "worker finished", result.to_dict(), node.id)
        # Auto-populate shared_brief from successful worker result_struct
        if ok and result_struct and result_struct.get("summary"):
            plan = self._current_plan
            if plan is not None:
                _entry = f"- {node.title}: {result_struct['summary']}"
                _files = result_struct.get("files_modified", [])
                if _files:
                    _entry += f" (改: {', '.join(_files)})"
                if plan.shared_brief:
                    plan.shared_brief += "\n" + _entry
                else:
                    plan.shared_brief = _entry
                self.store.log(run_id, "info", "shared brief updated",
                    {"node": node.id, "brief_entry": _entry}, node.id)
        if tmp_path:
            try: os.unlink(tmp_path)
            except OSError: pass
        return result

    def _combine_shard_results(self, run_id: str, parent_id: str, shard_ids: list[str]) -> tuple[str, dict]:
        """Combine shard results under a parent node into a single text+struct.
        
        Reads each shard's result from the store, concatenates them ordered
        by shard ID, and merges their structs.
        """
        texts: list[str] = []
        structs: list[dict] = []
        for sid in shard_ids:
            node_data = self.store.get_node(run_id, sid)
            if node_data and node_data.get("result"):
                texts.append(str(node_data["result"]))
            if node_data and node_data.get("result_struct_json"):
                try:
                    structs.append(json.loads(node_data["result_struct_json"]))
                except (json.JSONDecodeError, TypeError):
                    pass
        combined_text = "\n\n---\n\n".join(texts) if texts else ""
        combined_struct = {}
        for s in structs:
            combined_struct.update(s)
        return combined_text, combined_struct

    def _aggregate(self, run_id: str, request: str, results: list[WorkerResult], timeout: int) -> WorkerResult:
        node = WBSNode(f"{run_id}-aggregate", "Aggregate results", "Aggregate worker outputs into final answer", "aggregation", 5, [], False, "Final answer")
        self.store.insert_wbs_node(run_id, node.to_dict())
        self.store.update_node(node.id, "running", run_id=run_id)

        # ★ Short-circuit: single successful result → pass through, no LLM call
        if len(results) == 1 and results[0].ok:
            w = results[0]
            result_text = re.sub(r'HERMES-COLLAB-RESULT[\s\S]*', '', w.result).strip() or w.result
            return WorkerResult(
                node.id, node.title, True, result_text,
                w.session_id, w.duration_seconds, 0, "", 1,
                result_struct=w.result_struct,
            )

        # 2026-06-21: Use result_struct.summary instead of full result text
        # to prevent ARG_MAX overflow. Each worker's summary is short (~100 chars).
        summaries = []
        for r in results:
            summary = (r.result_struct or {}).get("summary", "") if r.result_struct else ""
            files = (r.result_struct or {}).get("files_modified", []) if r.result_struct else []
            status = "✅" if r.ok else "❌"
            entry = f"{status} {r.title}: {summary[:200]}"
            if files:
                entry += f" (改: {', '.join(files)})"
            summaries.append(entry)
        report = "\n".join(summaries)
        _AGGREGATE_REQUEST_MAX_CHARS = 500
        if len(request) > _AGGREGATE_REQUEST_MAX_CHARS:
            request = request[:_AGGREGATE_REQUEST_MAX_CHARS] + f"\n... [truncated {len(request)} chars]"
        node.description = (
            f"Original request:\n{request}\n\nWorker results:\n{report}\n\n"
            f"Produce final concise report. Mention timeouts and shard coverage honestly."
            f"\n\nWhen generating your HERMES-COLLAB-RESULT JSON, include ALL files_modified from the upstream workers listed above."
            f" Use paths relative to the project root."
        )
        return self._run_worker(run_id, node, timeout, model_override=self.leader_model, role="leader")

    def _learn(self, run_id: str, results: list[WorkerResult]) -> None:
        timeouts = [r for r in results if r.returncode == 124]
        if timeouts:
            self.store.add_lesson(
                "watchdog",
                f"Run {run_id}: {len(timeouts)} worker(s) timed out; split large WBS nodes earlier or reduce scope.",
                {"run_id": run_id, "nodes": [r.node_id for r in timeouts]},
                scope="L1",
                tags=["timeout", "wbs-scope"],
            )
        slow = [r for r in results if r.duration_seconds > 120 and r.ok]
        if slow:
            self.store.add_lesson(
                "planning",
                f"Run {run_id}: {len(slow)} slow successful worker(s); consider smaller WBS nodes for similar tasks.",
                {"run_id": run_id, "nodes": [r.node_id for r in slow]},
                scope="L1",
                tags=["slow-worker", "wbs-granularity"],
            )
        argmax = [
            r for r in results
            if r.returncode == 7 or (r.stderr and "Argument list too long" in r.stderr)
        ]
        if argmax:
            self.store.add_lesson(
                "watchdog",
                f"Run {run_id}: {len(argmax)} worker(s) hit ARG_MAX limit; reduce WBS scope or split node input into smaller chunks.",
                {"run_id": run_id, "nodes": [r.node_id for r in argmax]},
                scope="L1",
                tags=["argmax", "wbs-scope"],
            )

    # ── Post-execution file conflict detection ──────────────────────
    def _detect_file_conflicts(self, run_id: str, results: list[WorkerResult]) -> list[dict]:
        file_to_nodes: dict[str, list[str]] = {}
        for r in results:
            if not r.ok or not r.result:
                continue
            try:
                contract, _ = self._parse_result_contract(r.result)
                if contract and isinstance(contract.get("files_modified"), list):
                    for f in contract["files_modified"]:
                        if isinstance(f, str) and f.strip():
                            file_to_nodes.setdefault(f.strip(), []).append(r.node_id)
            except Exception:
                pass
        conflicts = []
        for file_path, node_ids in file_to_nodes.items():
            if len(node_ids) > 1:
                conflicts.append({"file": file_path, "nodes": node_ids})
                self.store.log(run_id, "warn",
                    f"FILE CONFLICT: '{file_path}' modified by {len(node_ids)} workers: {', '.join(node_ids)}",
                    {"file": file_path, "nodes": node_ids})
        return conflicts

    # ------------------------------------------------------------------
    # v3: Leader persistent session, direct mode, event loop
    # ------------------------------------------------------------------

    def _event_loop(self):
        """Background thread: process events from _event_queue and handle leader health checks."""
        _log = logging.getLogger(__name__)
        while self._running:
            try:
                event = self._event_queue.get(timeout=1)
                self._route_event(event)
            except queue.Empty:
                pass
            except Exception as exc:
                _log.warning("[EventLoop] Error processing event: %s", exc)

            # ── Leader health check (every cycle) ──
            try:
                session = self._leader_session
                if session:
                    proc = session.get("proc")
                    if proc is not None and proc.poll() is not None:
                        _log.warning(
                            "[EventLoop] leader session died (pid=%d, rc=%s), triggering restart",
                            proc.pid,
                            proc.returncode,
                        )
                        run_id = session.get("run_id", "unknown")
                        self.store.log(
                            run_id, "warning",
                            "leader session died, restarting",
                            {
                                "pid": proc.pid,
                                "returncode": proc.returncode,
                                "restart_count": self._leader_restart_count,
                                "max_restarts": self._leader_max_restarts,
                            },
                        )
                        self._start_leader_session(run_id)
            except Exception as exc:
                _log.warning("[EventLoop] Leader health check error: %s", exc)

    def _route_event(self, event: dict):
        """Route an event from the event queue to the appropriate handler."""
        from logging import getLogger as _log
        _log = _log(__name__)
        event_type = event.get("type", "")
        detail = event.get("detail", "")
        node_id = event.get("node_id", "")
        run_id = event.get("run_id", "")

        if event_type == "leader_replied":
            # Leader has replied — write reply to workspace files for worker to read
            _reply_text = self._leader_replies.pop((run_id, node_id), detail)
            _log.info("[EventLoop] Leader replied for node %s: %s", node_id, _reply_text[:80])

            # ── Write leader_reply.txt ──
            _workspace_dir = self.cwd / ".workspace" / run_id[:8]
            _reply_path = _workspace_dir / "leader_reply.txt"
            try:
                _workspace_dir.mkdir(parents=True, exist_ok=True)
                _reply_path.write_text(_reply_text, encoding="utf-8")
            except Exception as exc:
                _log.warning("[EventLoop] Failed to write leader reply to %s: %s", _reply_path, exc)

            # ── Write node-level leader_reply.txt ──
            if node_id:
                _node_dir = _workspace_dir / node_id[:10]
                _node_reply_path = _node_dir / "leader_reply.txt"
                try:
                    _node_dir.mkdir(parents=True, exist_ok=True)
                    _node_reply_path.write_text(_reply_text, encoding="utf-8")
                except Exception as exc:
                    _log.warning("[EventLoop] Failed to write node leader reply to %s: %s", _node_reply_path, exc)

            # Log the write operation
            try:
                self.store.log(run_id, "info", "leader reply written", {"reply": _reply_text[:200]})
            except Exception as exc:
                _log.warning("[EventLoop] Failed to log leader reply write: %s", exc)

            # Update node status to running
            try:
                self.store.update_node(node_id, "running", run_id=run_id)
                _log.info("[EventLoop] Node %s → running (leader replied)", node_id)
            except Exception as exc:
                _log.warning("[EventLoop] Failed to update node %s on leader_replied: %s", node_id, exc)

        elif event_type == "guardian:stream":
            # Worker output — log to DB
            if run_id:
                try:
                    self.store.log(run_id, "info", detail[:500], {"node_id": node_id, "type": "stream"})
                except Exception:
                    pass
            _log.info("[EventLoop] [%s] %s", node_id, detail[:200])

        elif event_type == "guardian:completed":
            dur = event.get("duration", "")
            _log.info("[EventLoop] Node %s completed (%ss)", node_id, dur)

        elif event_type == "guardian:worker_error":
            error = event.get("error", detail)
            _log.warning("[EventLoop] Node %s failed: %s", node_id, error[:200])

        elif event_type == "guardian:need_input":
            _log.info("[EventLoop] Node %s needs input: %s", node_id, detail[:200])

        else:
            _log.debug("[EventLoop] Unhandled event type: %s (detail: %s)", event_type, detail[:80])

    def _leader_review(self, run_id: str, request: str, plan: Any, results: list) -> str | None:
        """Use LLM to review each node's output against task requirements.

        Builds a structured prompt with task_requirements + node results,
        calls the agent LLM, and returns the review as markdown text.
        Returns None on any failure (caller falls back to simple summary).
        """
        import subprocess as _sp, json as _json
        reqs = plan.task_requirements or []
        if not reqs or not results:
            return None

        # Build node result summary (no raw stdout)
        _node_lines = []
        for _r in results:
            _status = "通过" if _r.ok else "失败"
            # Extract key info from result_struct if available
            _files = ""
            _summary = ""
            if _r.result_struct:
                _files = ", ".join(_r.result_struct.get("files_modified", []))
                _summary = _r.result_struct.get("summary", "")
            _node_lines.append(
                f"### {_r.node_id}\n"
                f"- 状态: {_status}\n"
                f"- 耗时: {_r.duration_seconds}s\n"
                f"- 关键产出: {_summary}\n"
                f"- 涉及文件: {_files}\n"
            )

        _nodes_text = "\n".join(_node_lines)

        # Build requirement table
        _req_lines = []
        for _req in reqs:
            _req_lines.append(f"- {_req['id']}: {_req['description']} (节点: {_req['node_id']})")
        _reqs_text = "\n".join(_req_lines)

        prompt = (
            f"你是 Hermes 协作引擎的 Leader Agent，负责对 WBS 节点执行结果进行审查并输出报告。\n\n"
            f"## 原始任务\n{request}\n\n"
            f"## 任务要求逐项对照\n{_reqs_text}\n\n"
            f"## 节点执行结果\n{_nodes_text}\n\n"
            f"## 审查要求\n"
            f"1. 逐项检查每个 requirement 是否被满足：✅ 完全满足 / ⚠️ 部分满足 / ❌ 未满足\n"
            f"2. 对每个节点评估执行效果、代码质量、验证充分性\n"
            f"3. 整体判断交付物是否达成原始任务目标\n\n"
            f"## 输出格式\n"
            f"以全中文 Markdown 输出，不要用代码块包裹。结构如下：\n\n"
            f"# Leader 审查报告 · {run_id[:12]}...\n\n"
            f"## 任务对照\n"
            f"- 原始任务: {request[:100]}...\n"
            f"逐项说明...\n\n"
            f"## 节点审查\n"
            f"各节点评估...\n\n"
            f"## 整体评估\n"
            f"综合评价...\n"
        )

        # Call LLM via agent_backend
        _review_model = self.leader_model or self.worker_model
        # If no model configured, use auto_prefix with a default
        if not _review_model and self.agent_backend.auto_prefix:
            _review_model = f"{self.agent_backend.auto_prefix}deepseek-v4-flash"
        cmd = self.agent_backend.build_command(
            prompt=prompt, model=_review_model,
            allowed_tools=[], provider=None, reasoning=False,
        )
        proc = _sp.run(cmd, cwd=str(self.cwd), text=True,
                       stdin=_sp.DEVNULL, stdout=_sp.PIPE, stderr=_sp.PIPE,
                       timeout=60)
        if proc.returncode != 0:
            return None

        # Extract markdown from LLM output
        text = (proc.stdout or "").strip()
        # Strip code fences if LLM wrapped output
        import re as _re
        _m = _re.search(r"```(?:markdown)?\s*([\s\S]*?)```", text)
        if _m:
            text = _m.group(1).strip()
        return text if len(text) > 50 else None

    def _start_leader_session(self, run_id: str) -> bool:
        """Start a persistent leader session via ``hermes chat -z --resume dt-leader-{run_id}``.

        The leader session is a long-running subprocess that the engine
        can use to communicate with the leader LLM throughout the run's
        lifecycle.  The proc handle is stored in ``self._leader_session``.
        This method also tracks restart count and starts a watchdog
        daemon thread that auto-restarts the leader if it dies.

        If the leader has already been restarted ``_leader_max_restarts``
        times, it sets ``_leader_available = False`` and does not retry.

        Returns:
            True if the session was started successfully, False otherwise.
        """
        # ── Check restart budget ──
        if self._leader_restart_count >= self._leader_max_restarts:
            self._leader_available = False
            _log = logging.getLogger(__name__)
            _log.warning(
                "[LeaderWatchdog] leader restart limit (%d) reached, marking unavailable",
                self._leader_max_restarts,
            )
            self.store.log(
                run_id, "warning",
                "leader restart limit reached, leader unavailable",
                {"restart_count": self._leader_restart_count, "max": self._leader_max_restarts},
            )
            return False

        # ── Kill any existing leader proc ──
        old = self._leader_session
        if old and old.get("proc"):
            old_proc = old["proc"]
            _log = logging.getLogger(__name__)
            if old_proc.poll() is None:
                _log.info(
                    "[LeaderWatchdog] killing old leader proc pid=%d",
                    old_proc.pid,
                )
                try:
                    old_proc.terminate()
                    old_proc.wait(timeout=5)
                except Exception:
                    try:
                        old_proc.kill()
                        old_proc.wait(timeout=3)
                    except Exception:
                        pass
            # Close old pipes
            for pipe_name in ("stdin", "stdout", "stderr"):
                pipe = old.get(pipe_name)
                if pipe and not pipe.closed:
                    try:
                        pipe.close()
                    except Exception:
                        pass

        session_name = f"dt-leader-{run_id}"
        _lead_cmd = self.leader_agent.command[0] if self.leader_agent else None
        if _lead_cmd is None:
            # No leader agent configured — user is the leader (v7.0 mode)
            import logging as _logging
            _logging.getLogger(__name__).info("[LeaderSession] No leader agent configured, skipping leader session (user-as-leader mode)")
            return False
        cmd = [_lead_cmd, "chat", "-q", "", "--resume", session_name, "--quiet"]
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            self._leader_session = {
                "proc": proc,
                "stdin": proc.stdin,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
                "session_name": session_name,
                "run_id": run_id,
                "event_queue": self._event_queue,
            }
            self._leader_available = True
            self._leader_restart_count += 1
            self.store.log(
                run_id, "info",
                "leader persistent session started",
                {
                    "session_name": session_name,
                    "pid": proc.pid,
                    "restart_count": self._leader_restart_count,
                },
            )
            # ── Start a watchdog daemon for this leader ──
            self._leader_watchdog_stop.clear()
            _wt = threading.Thread(
                target=self._leader_watchdog,
                args=(run_id,),
                daemon=True,
            )
            _wt.start()
            return True
        except (FileNotFoundError, OSError) as exc:
            _log = logging.getLogger(__name__)
            _log.warning(
                "[LeaderSession] failed to start leader session: %s", exc,
            )
            self.store.log(
                run_id, "warning",
                f"failed to start leader session: {exc}",
                {"session_name": session_name},
            )
            self._leader_session = None
            return False

    def _leader_watchdog(self, run_id: str) -> None:
        """Daemon thread: monitor leader process and auto-restart if it dies.

        Checks every 5 seconds.  If the leader proc has exited, logs the
        event and calls ``_start_leader_session`` to restart it.  Stops
        checking when ``_leader_watchdog_stop`` is set (engine shutdown)
        or when ``_leader_available`` is False (restart budget exhausted).
        """
        _log = logging.getLogger(__name__)
        while self._running and not self._leader_watchdog_stop.is_set():
            time.sleep(5)

            if self._leader_watchdog_stop.is_set() or not self._running:
                break

            if not self._leader_available:
                # Restart budget exhausted — stop watchdog
                _log.warning(
                    "[LeaderWatchdog] leader unavailable, watchdog stopping",
                )
                break

            session = self._leader_session
            if session is None:
                continue

            proc = session.get("proc")
            if proc is None:
                continue

            # Check if leader proc has exited
            if proc.poll() is not None:
                _log.warning(
                    "[LeaderWatchdog] leader session died (pid=%d, rc=%s), restarting",
                    proc.pid,
                    proc.returncode,
                )
                self.store.log(
                    run_id, "warning",
                    "leader session died, restarting",
                    {
                        "pid": proc.pid,
                        "returncode": proc.returncode,
                        "restart_count": self._leader_restart_count,
                        "max_restarts": self._leader_max_restarts,
                    },
                )
                # Attempt restart (increments _leader_restart_count inside)
                self._start_leader_session(run_id)

    # ------------------------------------------------------------------
    # v3: Checkpoint, risk detection, pause/resume, redo-node
    # ------------------------------------------------------------------

    def pause_run(self, run_id: str, *, reason: str | None = None) -> dict:
        """Stop dispatching new nodes. Running workers continue to completion."""
        self._paused_runs.add(run_id)
        self._persist_run_state(run_id)
        self.store.log(run_id, "pause", f"Run paused by parent{': '+reason if reason else ''}")
        return {"ok": True, "run_id": run_id, "action": "paused"}

    def resume_run(self, run_id: str, *, reason: str | None = None) -> dict:
        """Resume dispatching nodes after a pause."""
        self._paused_runs.discard(run_id)
        self._checkpoint_paused_nodes.clear()
        self._persist_run_state(run_id)
        self.store.log(run_id, "resume", f"Run resumed by parent{': '+reason if reason else ''}")
        return {"ok": True, "run_id": run_id, "action": "resumed"}

    _READ_ONLY_CAPABILITIES = frozenset({"analysis", "planning", "verification"})

    def _detect_risks(self, node: WBSNode, result_struct: dict[str, Any] | None, risk_policy: RiskPolicy) -> list[tuple[str, str]]:
        """Detect risk events from a completed node. Returns [(risk_level, description)]."""
        risks: list[tuple[str, str]] = []
        if result_struct:
            blocking_issues = result_struct.get("blocking_issues")
            notes = result_struct.get("notes")
            blocking = blocking_issues or notes
            if blocking and isinstance(blocking, list) and len(blocking) > 0:
                # Read-only nodes (analysis/planning/verification) reporting
                # notes without explicit blocking_issues are expected — no-edit
                # output is normal for these capabilities.
                if not blocking_issues and node.capability in self._READ_ONLY_CAPABILITIES:
                    risks.append(("low", f"Node {node.id} read-only notes (no edits expected): {blocking}"))
                else:
                    risks.append(("medium", f"Node {node.id} reports blocking issues: {blocking}"))
            files = result_struct.get("files_modified") or result_struct.get("files_touched") or []
        if node.checkpoint:
            risks.append(("high", f"Checkpoint node {node.id} ({node.title}) completed"))
        return risks

    def _apply_risk_policy(self, run_id: str, risks: list[tuple[str, str]], risk_policy: RiskPolicy) -> None:
        """Apply the configured risk policy to detected risks."""
        for risk_level, desc in risks:
            action = getattr(risk_policy, risk_level, "auto")
            assessment = {"risk_level": risk_level, "description": desc, "action": action}
            self._risk_assessments.append(assessment)
            self.store.log(run_id, "risk", f"[{risk_level}] {desc} (action={action})")
            if action in ("notify", "pause", "checkpoint"):
                # Find which node this risk is about (extract from desc)
                node_id = ""
                for n in self._current_plan.nodes if self._current_plan else []:
                    if n.id in desc:
                        node_id = n.id
                        break
                if node_id:
                    self._checkpoint_paused_nodes.add(node_id)
                    self.store.log(run_id, "checkpoint", f"Paused at {node_id}: {desc}", node_id=node_id)
                if action == "notify":
                    # Auto-resume after timeout
                    threading.Timer(
                        risk_policy.checkpoint_timeout,
                        self._auto_resume_checkpoint,
                        args=(run_id, node_id),
                    ).start()
                self._persist_run_state(run_id)
                self._save_context_snapshot(run_id, "checkpoint", node_id or None)
                # action == "pause" requires explicit resume

    def _auto_resume_checkpoint(self, run_id: str, node_id: str) -> None:
        """Auto-resume a checkpoint after timeout if still paused."""
        if node_id in self._checkpoint_paused_nodes:
            self._checkpoint_paused_nodes.discard(node_id)
            self._persist_run_state(run_id)
            self.store.log(run_id, "checkpoint", f"Auto-resumed {node_id} after timeout", node_id=node_id)
            risk_policy = self.store.load_risk_policy()
            self.store.add_lesson(
                "checkpoint-timeout",
                f"Checkpoint at {node_id} auto-resumed after {risk_policy.checkpoint_timeout}s",
                scope="engine",
            )

    def redo_node(self, run_id: str, node_id: str, *, cascade: bool = False, worker_model: str | None = None, reason: str | None = None, description_delta: str | None = None) -> dict:
        """Re-execute a single node from a completed (or paused) run.

        If cascade=True, also redo all downstream nodes that depend on this node.
        """
        plan = self._load_plan_from_db(run_id)
        with self._node_results_lock:
            self._current_plan = plan
        node = next((n for n in plan.nodes if n.id == node_id), None)
        if not node:
            raise ValueError(f"Node {node_id} not found in run {run_id}")

        # Increment attempt
        node.attempt += 1
        self.store.update_node_attempt(run_id, node_id, node.attempt)

        # Re-run the worker with same prompt
        result = self._run_worker(run_id, node, 900, model_override=worker_model)
        self._record_node_result(run_id, result)
        self.store.update_node_result(run_id, node_id, result.result or "")

        # If cascade, find and redo all downstream nodes
        if cascade:
            downstream = self._find_downstream_nodes(run_id, node_id)
            for ds_node_id in downstream:
                ds_node = next((n for n in plan.nodes if n.id == ds_node_id), None)
                if ds_node:
                    ds_node.attempt += 1
                    self.store.update_node_attempt(run_id, ds_node_id, ds_node.attempt)
                    ds_result = self._run_worker(run_id, ds_node, 900, model_override=worker_model)
                    self._record_node_result(run_id, ds_result)
                    self.store.update_node_result(run_id, ds_node_id, ds_result.result or "")

        # Write lesson
        self.store.add_lesson(
            "redo-node",
            f"Redid {node_id} (attempt {node.attempt}), cascade={cascade}",
            scope="parent",
            evidence={"node_id": node_id, "attempt": node.attempt, "cascade": cascade},
        )
        return {"node_id": node_id, "attempt": node.attempt, "status": "completed" if result.ok else "failed"}

    def _find_downstream_nodes(self, run_id: str, node_id: str) -> list[str]:
        """BFS to find all nodes that directly or indirectly depend on node_id."""
        plan = self._load_plan_from_db(run_id)
        downstream: set[str] = set()
        queue = [node_id]
        while queue:
            current = queue.pop(0)
            for node in plan.nodes:
                if current in node.dependencies and node.id not in downstream:
                    downstream.add(node.id)
                    queue.append(node.id)
        return list(downstream)

    def _load_plan_from_db(self, run_id: str) -> Plan:
        """Reconstruct a Plan object from stored WBS nodes."""
        nodes_data = self.store.get_nodes(run_id)
        loaded_results: dict[str, str] = {}
        loaded_structs: dict[str, dict[str, Any] | None] = {}
        for row in self.store.load_node_results(run_id):
            loaded_results[row["node_id"]] = row.get("result_text") or ""
            raw_struct = row.get("result_struct_json")
            loaded_structs[row["node_id"]] = json.loads(raw_struct) if raw_struct else None
        with self._node_results_lock:
            self._node_results = loaded_results
            self._node_results_struct = loaded_structs
        wbs_nodes: list[WBSNode] = []
        shared_brief = ""
        for n in nodes_data:
            deps = json.loads(n.get("dependencies_json", "[]"))
            try:
                write_targets = json.loads(n.get("write_targets_json") or "[]")
            except json.JSONDecodeError:
                write_targets = []
            if not isinstance(write_targets, list):
                write_targets = []
            wbs_node = WBSNode(
                id=n.get("id", ""),
                title=n.get("title", ""),
                brief=str(n.get("brief") or ""),
                description=n.get("description", ""),
                capability=n.get("capability", "implementation"),
                complexity=n.get("complexity", 5),
                dependencies=deps,
                parallelizable=bool(n.get("parallelizable", 1)),
                deliverable=n.get("deliverable", ""),
                parent_id=n.get("parent_id"),
                attempt=n.get("attempt", 1),
                checkpoint=bool(n.get("checkpoint", 0)),
                estimated_duration=n.get("estimated_duration"),
                write_targets=[str(target) for target in write_targets if str(target).strip()],
                fingerprint=str(n.get("fingerprint") or ""),
                skills_json=str(n.get("skills_json") or ""),
                tools_json=str(n.get("tools_json") or ""),
            )
            wbs_nodes.append(wbs_node)
            if n.get("shared_brief"):
                shared_brief = n["shared_brief"]
        return Plan(nodes=wbs_nodes, shared_brief=shared_brief)
