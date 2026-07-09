from __future__ import annotations

import concurrent.futures
import hashlib
import json
import fnmatch
import queue
import shutil
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
from .skills import SkillRegistry, get_default_registry
from .store import CollabStore
from .tools import ToolRegistry, get_default_tool_registry
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
        leader_agent: str | None = "hermes",
        worker_agent: str | None = None,

        skill_registry: SkillRegistry | None = None,
        tool_registry: ToolRegistry | None = None,
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
                import shutil as _shutil
                _binary = _lead.command[0]
                if _shutil.which(_binary) is None:
                    print(f"warning: leader agent '{_binary}' not found on PATH, falling back to worker agent")
                    self.leader_agent = None
                else:
                    self.leader_agent = _lead
            except KeyError:
                print(f"warning: unknown leader agent '{leader_agent}', falling back to worker agent", file=sys.stderr)
                self.leader_agent = None
        self.worker_agent: AgentBackend | None = get_backend(worker_agent) if worker_agent else None
        self.provider = provider
        self.skill_registry = skill_registry or get_default_registry()
        self.tool_registry = tool_registry or get_default_tool_registry()
        self._skill_distributor = SkillDistributor(
            skill_registry=self.skill_registry,
            tool_registry=self.tool_registry,
        )
        self.store = CollabStore(db_path, skip_cleanup=True)
        # Load agent profiles for leader/worker model overrides
        # Initialize unified registry with store for persistence
        get_unified_registry(store=self.store)
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

    def _summarize_package_plan(self, package: str) -> str:
        """Render a one-line plan summary for a package.

        Reads the graph definition from packages.sqlite3 via
        ``backend.graph_interpreter`` and returns ``summarize_text(graph)``
        — or "" if the package has no graph yet or the interpreter
        is unavailable.
        """
        try:
            import sys
            # Ensure backend/ is importable regardless of how the
            # engine was launched (cron, CLI, in-process test).
            backend_src = str(self.cwd / "src")
            if backend_src not in sys.path:
                sys.path.insert(0, backend_src)
            from backend.graph_interpreter import summarize_text
            from backend.skill_graph import SkillGraph
            from backend.scenario_packages import get_package
            db_path = self.cwd / "data" / "packages.sqlite3"
            if not db_path.exists():
                return ""
            pkg = get_package(package, db_path=str(db_path))
            if not pkg:
                return ""
            graph_def = pkg.get("graph_definition") or {}
            if not graph_def:
                return ""
            graph = SkillGraph.from_dict(graph_def)
            return summarize_text(graph)
        except Exception as exc:
            logging.getLogger(__name__).debug(
                "_summarize_package_plan failed for %s: %s", package, exc
            )
            return ""

    def _render_package_plan(self, package_name: str, graph_def: dict) -> str:
        """Render a detailed human-readable execution plan for a package.

        Walks the topological steps of *graph_def*, describing each step's
        type, associated skill, input mapping, condition branches, parallel
        forks, etc.  The result is appended to ``plan.shared_brief`` so
        downstream workers understand the intended execution flow.

        Args:
            package_name: Name of the scenario package.
            graph_def: Dict representation of the SkillGraph (as stored in
                       ``packages.sqlite3.packages.graph_definition``).

        Returns:
            Multi-line string describing the plan, or an empty string if
            *graph_def* has no steps.
        """
        from backend.skill_graph import SkillGraph, SkillEdge

        if not graph_def or not isinstance(graph_def, dict):
            return ""
        try:
            graph = SkillGraph.from_dict(graph_def)
        except (KeyError, ValueError, TypeError) as exc:
            logging.getLogger(__name__).debug(
                "_render_package_plan: from_dict failed for %s: %s",
                package_name, exc,
            )
            return ""
        if not graph.steps:
            return ""

        lines: list[str] = []
        lines.append(f"## Package Plan: {graph.display_name} ({package_name})")
        lines.append(f"Description: {graph.description}")
        lines.append(f"Entry point: {graph.entry_point}")
        lines.append(f"Exit points: {', '.join(graph.exit_points) if graph.exit_points else 'N/A'}")

        # Topological order
        try:
            topo_order = graph.topological_sort()
        except ValueError:
            lines.append("⚠️  Graph contains cycles — cannot determine execution order.")
            topo_order = list(graph.steps.keys())

        lines.append(f"Steps ({len(topo_order)} total):")
        for idx, step_id in enumerate(topo_order, 1):
            step = graph.steps.get(step_id)
            if step is None:
                lines.append(f"  {idx}. **{step_id}** — (unknown)")
                continue
            deps = self._step_dependencies(step_id, graph)
            dep_str = f" (depends on: {', '.join(deps)})" if deps else ""
            lines.append(f"  {idx}. **{step.name}** [{step.step_type.value}]{dep_str}")
            lines.append(f"     {self._describe_step(step)}")

        lines.append("")
        return "\n".join(lines)

    def _describe_step(self, step: "Any") -> str:
        """Return a human-readable one-line description of a skill step.

        Args:
            step: A ``SkillStep`` instance.

        Returns:
            Single-line description string.
        """
        from backend.skill_graph import StepType

        st = step.step_type
        if st == StepType.SKILL:
            skill_name = step.skill_name or step.name
            entry = self.skill_registry.get(skill_name)
            desc = entry.description if entry else ""
            im = step.input_mapping or {}
            parts = [f"Skill: **{skill_name}**"]
            if desc:
                parts.append(f"({desc[:120]})")
            if im:
                mapping_str = ", ".join(f"{k} ← {v}" for k, v in im.items())
                parts.append(f"inputs: [{mapping_str}]")
            return " ".join(parts)
        elif st == StepType.CONDITION:
            parts = [f"Condition: `{step.condition or '(empty)'}`"]
            if step.true_branch:
                parts.append(f"→ true: **{step.true_branch}**")
            if step.false_branch:
                parts.append(f"→ false: **{step.false_branch}**")
            return " | ".join(parts)
        elif st == StepType.LOOP:
            body = ", ".join(step.loop_body) if step.loop_body else "(empty)"
            return (
                f"Loop: max {step.max_iterations} iterations | "
                f"body: [{body}] | "
                f"condition: `{step.loop_condition or 'none'}`"
            )
        elif st == StepType.PARALLEL:
            children = ", ".join(step.parallel_steps) if step.parallel_steps else "(empty)"
            return f"Parallel: fork into [{children}]"
        elif st == StepType.HUMAN:
            return "Human-in-the-loop: awaiting manual input"
        else:
            return f"Step type: {st}"

    def _step_dependencies(self, step_id: str, graph: "Any") -> list[str]:
        """Return IDs of steps that *step_id* depends on (incoming edges).

        Args:
            step_id: The step to find dependencies for.
            graph: A ``SkillGraph`` instance.

        Returns:
            Sorted list of dependency step IDs.
        """
        return sorted(
            edge.from_step for edge in graph.edges if edge.to_step == step_id
        )

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
                    with self._worker_procs_lock:
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

    def _build_package_plan(self, graph, request: str, run_id: str, package_name: str, context_vars: dict) -> list:
        """Convert SkillGraph steps into WBSNode list for the run() loop.

        Each step in the graph becomes a WBS implementation node.
        Dependencies (edges) are encoded as node.depends_on.
        The Leader run loop in run() dispatches them in topological order.

        When a step references a skill_name, loads the prompt_template
        from the engine's SkillRegistry instead of using inline params.
        """
        from engine.models import WBSNode, Plan
        import logging

        topo_order = []
        try:
            topo_order = graph.topological_sort()
        except Exception:
            topo_order = list(graph.steps.keys())

        nodes = []
        prev_step_id = None
        for step_id in topo_order:
            step = graph.steps.get(step_id)
            if not step:
                continue

            step_name = step.name or step_id
            skill_name = getattr(step, 'skill_name', "")

            # Load prompt from SkillRegistry if step references a registered skill
            prompt_template = None
            if skill_name:
                skill_entry = self.skill_registry.get(skill_name)
                if skill_entry and getattr(skill_entry, 'content', None):
                    prompt_template = skill_entry.content
                else:
                    logging.getLogger(__name__).debug(
                        "Skill %s not found in registry for step %s, fallback to inline",
                        skill_name, step_id)

            # Fallback to inline params
            if not prompt_template:
                skill_params = getattr(step, 'skill_params', {}) or getattr(step, 'params', {})
                prompt_template = skill_params.get("prompt", skill_params.get("prompt_template",
                    f"执行步骤: {step_name}"))

            # Render template with context_vars
            from string import Template
            try:
                filled_prompt = Template(prompt_template).safe_substitute(context_vars)
            except Exception:
                filled_prompt = prompt_template

            # Create WBS node
            from engine.models import WBSNode
            node = WBSNode(
                id=step_id,
                title=step_name,
                capability="implementation",
                description=filled_prompt,
                complexity=3,
                dependencies=[],
                parallelizable=False,
                deliverable="",
            )
            # Set node-specific brief: only this step's task, not the full plan
            node.brief = f"步骤: {step_name}" + (f"\n{step.description[:1000]}" if getattr(step, 'description', None) else "")
            # Set dependency if not first step
            if prev_step_id:
                node.dependencies = [prev_step_id]
            prev_step_id = step_id

            nodes.append(node)

        return nodes

    def run(self, request: str, *, title: str | None = None, concurrency: int = 4, timeout: int = 86400, max_retries: int = 2, split_count: int = 4, aggregate: bool = True, package: str | None = None, package_skills: list[str] | None = None, session_id: str | None = None, prev_session_id: str | None = None, run_id: str | None = None) -> dict:
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
        # Persist package + the Skill collection it triggered so the dashboard
        # and downstream workers can see which scenario Leader picked.
        # If a package is set, also try to read its graph definition and
        # render a one-line plan summary via graph_interpreter.summarize_text
        # so the chat bubble can show "📋 Run 4 steps (3 skill, ...)".
        package_plan: str = ""
        package_graph_def: dict | None = None
        if package:
            package_plan = self._summarize_package_plan(package)
            # Read the full graph_def for detailed plan rendering
            try:
                from backend.scenario_packages import get_package
                pkg = get_package(package, db_path=str(self.cwd / "data" / "packages.sqlite3"))
                if pkg:
                    package_graph_def = pkg.get("graph_definition")
            except Exception as exc:
                logging.getLogger(__name__).warning(
                    "Failed to read graph_def for package %s: %s", package, exc,
                )
        if package or package_skills:
            # Build per-node skill mapping from graph definition steps
            node_skill_map = {}
            if package_graph_def and isinstance(package_graph_def, dict):
                steps = package_graph_def.get("steps", {})
                if isinstance(steps, dict):
                    for sid, sdef in steps.items():
                        if isinstance(sdef, dict):
                            sk = sdef.get("skill_name", "")
                            if sk:
                                node_skill_map[sid] = sk
            self.store.set_run_meta(run_id, {
                "package": package,
                "package_skills": list(package_skills or []),
                "node_skill_map": node_skill_map,
                "package_plan": package_plan,
            })

        # ── SkillGraph → WBS plan ──
        # When a package has a SkillGraph, convert its steps into WBS nodes
        plan_from_graph = None
        self._graph_node_ids: set[str] | None = None
        if package:
            logging.getLogger(__name__).info(
                "SkillGraph check: package=%s, graph_def_type=%s",
                package, type(package_graph_def).__name__ if package_graph_def is not None else "None")
            if package_graph_def and isinstance(package_graph_def, dict):
                try:
                    from backend.skill_graph import SkillGraph
                    graph = SkillGraph.from_dict(package_graph_def)
                    if graph and graph.steps:
                        # Get context variables from site_map
                        context_vars = {"target_url": "https://mail.qq.com",
                                        "site_name": "", "user_request": request}
                        try:
                            from backend import site_map
                            match_result = site_map.match(request)
                            if match_result.get("target_url"):
                                context_vars["target_url"] = match_result["target_url"]
                            if match_result.get("site_name"):
                                context_vars["site_name"] = match_result["site_name"]
                        except Exception:
                            pass
                        # Build nodes from graph
                        graph_nodes = self._build_package_plan(
                            graph, request, run_id, package, context_vars)
                        if graph_nodes:
                            from engine.models import Plan
                            plan_from_graph = Plan(nodes=graph_nodes, shared_brief=(
                                f"SkillGraph plan: {len(graph_nodes)} steps"
                            ))
                            self._graph_node_ids = {n.id for n in graph_nodes}
                            self.store.log(run_id, "info",
                                           f"skillgraph plan: {len(graph_nodes)} nodes from {package}")
                except Exception as exc:
                    logging.getLogger(__name__).warning(
                        "SkillGraph plan failed, falling back to WBS: %s", exc)
            else:
                logging.getLogger(__name__).info(
                    "SkillGraph check: graph_def not a dict, type=%s",
                    type(package_graph_def).__name__)

        if plan_from_graph:
            plan = plan_from_graph
        elif score.routing == "direct":
            self._direct_mode = True
            return self._run_direct(run_id, request, score, package, session_id, timeout)
        elif score.routing == "single":
            plan = self.planner.fallback_wbs(request, score=score)
        else:
            plan = self.planner.decompose(
                request,
                capabilities=self.agent_backend.capabilities,
                agent_backend=self.agent_backend,
                package=package,
                package_graph_def=package_plan or None,
                score=score,
            )
        if isinstance(plan, list):
            plan = Plan(nodes=plan)
        # Append detailed package plan to shared_brief so downstream
        # workers understand the intended execution flow.
        if package_graph_def:
            rendered = self._render_package_plan(package or "", package_graph_def)
            if rendered:
                if plan.shared_brief:
                    plan.shared_brief += "\n" + rendered
                else:
                    plan.shared_brief = rendered
        nodes = plan.nodes
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
        self._preallocate_skills_tools(run_id, nodes, task_type=plan.task_type)
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

            # ── Suggested skills from WBS-generated nodes ──
            suggested = []
            if ok and self._graph_node_ids:
                for r in results:
                    node_id = getattr(r, 'node_id', '')
                    if node_id and node_id not in self._graph_node_ids:
                        if hasattr(r, 'title') and r.title:
                            title = r.title or node_id
                            desc = (r.result or "")[:200]
                            suggested.append({
                                "name": node_id.replace("-", "_"),
                                "display_name": title,
                                "description": desc[:100],
                                "prompt_template": f"执行以下任务：{title}\n\n详细说明：{desc}",
                                "category": "generated",
                            })
                if suggested:
                    self.store.set_run_meta(run_id, {
                        "suggested_skills": suggested,
                    })
                    self.store.log(run_id, "info",
                                   f"suggested {len(suggested)} skill(s) from WBS overflow",
                                   {"suggested": suggested})

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

        # Store the computed shard count for _split_node
        self._proactive_shard_count = target
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

    def _leader_guard_run(self, run_id: str, request: str,
                          failed_node_ids: list[str], remaining_node_ids: list[str],
                          elapsed: float, timeout: int) -> str:
        """Tier 3: Ask the leader whether the entire run should continue."""
        prompt = (
            f"You are the Leader orchestrating a multi-worker run.\n\n"
            f"Run request: {request[:500]}\n"
            f"Elapsed time: {elapsed:.0f}s / Budget: {timeout}s\n\n"
            f"Failed nodes: {failed_node_ids or 'none'}\n"
            f"Remaining nodes: {remaining_node_ids or 'none'}\n\n"
            f"Decide whether to continue with remaining work or abort the run.\n"
            f"Return ONLY a JSON object on the final line, prefixed by "
            f"HERMES-COLLAB-RESULT:\n"
            f'{{"action":"continue|abort","reason":"brief justification"}}'
        )
        mock_node = WBSNode(f"{run_id}-guard", "Run guard assessment",
                            prompt, "planning", 1, [], True, "decision")
        try:
            wr = self._run_worker(run_id, mock_node, 30,
                                   model_override=self.leader_model, role="leader")
        except Exception as exc:
            self.store.log(run_id, "error",
                           "leader run guard crashed", {"error": str(exc)})
            return "continue"
        text = wr.result or ""
        _, parsed = self._parse_result_contract(text)
        if not isinstance(parsed, dict):
            return "continue"
        action = parsed.get("action", "continue")
        reason = parsed.get("reason", "")
        if action == "abort":
            self.store.log(run_id, "warning", "leader aborted the run",
                           {"reason": reason})
        return action

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
        self.skill_registry.refresh()
        native_caps = set(self.agent_backend.capabilities)
        package_meta = self.store.get_run_meta(run_id) or {}
        package_skills: list[str] = list(package_meta.get("package_skills") or [])
        node_skill_map: dict = package_meta.get("node_skill_map") or {}
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
                    elif node.id in node_skill_map:
                        leader_skills = [node_skill_map[node.id]]
                    elif package_skills:
                        leader_skills = list(package_skills)
                    else:
                        leader_skills = None

                    skill_names, tool_profile_names = self._skill_distributor.resolve_for_node(
                        node_capability=node.capability,
                        leader_skills=leader_skills,
                        agent_backend=self.agent_backend,
                        task_type=task_type,
                        task_description=node.description,
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

    def _is_hermes_builtin_skill(self, name: str) -> bool:
        """Check if a skill name refers to a built-in hermes skill."""
        entry = self.skill_registry.get(name)
        if entry is not None:
            return getattr(entry, "source", "hermes") == "hermes"
        unified = get_unified_registry()
        for us in unified.list_by_type(USkillEntry):
            if us.name == name:
                return us.source == "hermes"
        return False

    def _is_hermes_builtin_tool(self, name: str) -> bool:
        """Check if a tool profile name refers to a built-in hermes tool."""
        entry = self.tool_registry.get(name)
        if entry is not None:
            return getattr(entry, "source", "hermes") == "hermes"
        unified = get_unified_registry()
        for ut in unified.list_by_type(UToolEntry) + unified.list_by_type(UMCPEntry):
            if ut.name == name:
                return ut.source == "hermes"
        return False

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
            _ANTHROPIC_DEFAULTS: dict[str, str] = {
                "ANTHROPIC_BASE_URL": "https://api.anthropic.com",
            }
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
        # Resolve per-role agent
        _saved_agent = self.agent_backend
        if role == "leader" and self.leader_agent:
            self.agent_backend = self.leader_agent
        elif role == "worker" and self.worker_agent:
            self.agent_backend = self.worker_agent
        try:
            return self._run_worker_impl(run_id, node, timeout, model_override, role)
        finally:
            self.agent_backend = _saved_agent

    def _run_worker_impl(self, run_id: str, node: WBSNode, timeout: int, model_override: str | None = None, role: str = "worker") -> WorkerResult:
        worker_id = f"worker_{run_id}_{node.id}_{node.attempt}"
        self.store.worker_start(worker_id, run_id, node.id)
        self.store.log(run_id, "info", "worker started", {"node": node.id, "title": node.title, "agent": self.agent_backend.name}, node.id)
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
                agent_backend=self.agent_backend,
            )
            node.skills_json = json.dumps(skill_names)
            node.tools_json = json.dumps(tool_profile_names)
        mcp_servers = self._skill_distributor.resolve_mcp(skill_names, self.agent_backend.name)
        skills_block, tools_block, mcp_block = self._skill_distributor.render_for_prompt(
            skill_names, tool_profile_names, mcp_servers,
        )
        # Resolve allowed_tools for the tool whitelist
        tool_allowed = tool_profile_names
        backend = self.agent_backend
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
        parsed = self.agent_backend.parse_output(
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
        # Surface the Leader-selected package + Skill collection so the
        # aggregator has the full context when writing the final report.
        meta = self.store.get_run_meta(run_id) or {}
        package_name = meta.get("package")
        package_skills = list(meta.get("package_skills") or [])
        package_block = ""
        if package_name or package_skills:
            skills_line = ", ".join(package_skills) if package_skills else "(no skills recorded)"
            package_block = (
                f"\n\nLeader-selected scenario:\n"
                f"- Package: {package_name or '(none)'}\n"
                f"- Triggered Skill collection: {skills_line}\n"
                f"When writing the final answer, mention the scenario bundle that was used so the operator can see which Skill set Leader activated.\n"
            )
        node.description = (
            f"Original request:\n{request}\n\nWorker results:\n{report}\n\n"
            f"Produce final concise report. Mention timeouts and shard coverage honestly."
            f"{package_block}"
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

        else:
            _log.debug("[EventLoop] Unhandled event type: %s", event_type)

    def _wait_leader_reply(self, node_id: str, run_id: str) -> None:
        """Background thread: read leader stdout after a need_input was forwarded.

        Waits up to 120 seconds for the leader to produce output on its stdout.
        After 3 seconds of silence the reply is considered complete, and a
        ``leader_replied`` event is pushed onto the event queue.

        If the leader process dies during the wait, falls back immediately.
        """
        import logging as _logging
        import select as _sel

        _log = _logging.getLogger(__name__)
        _leader = self._leader_session
        if not _leader or not _leader.get("stdout"):
            _log.warning("[LeaderReply] No leader stdout for node %s, falling back to running", node_id)
            self.store.update_node(node_id, "running", run_id=run_id)
            return

        _sfd = _leader["stdout"]
        _proc = _leader.get("proc")
        try:
            _fd = _sfd.fileno()
        except Exception:
            _log.warning("[LeaderReply] Leader stdout fd unavailable for node %s, falling back", node_id)
            self.store.update_node(node_id, "running", run_id=run_id)
            return

        _lines = []
        _deadline = time.time() + 120  # max wait for leader reply
        _idle_limit = 3.0  # seconds of silence = reply complete

        while time.time() < _deadline:
            # ── Check leader health ──
            if _proc is not None and _proc.poll() is not None:
                _log.warning(
                    "[LeaderReply] leader died while waiting for reply on node %s (rc=%s), falling back",
                    node_id,
                    _proc.returncode,
                )
                self.store.update_node(node_id, "running", run_id=run_id)
                return

            try:
                _r, _, _ = _sel.select([_fd], [], [], _idle_limit)
                if _r:
                    _line = _sfd.readline()
                    if not _line:
                        break
                    # Skip prompt lines that hermes may echo
                    stripped = _line.strip()
                    if stripped in ("> ", ">", "") or stripped.startswith("> "):
                        continue
                    _lines.append(_line)
                else:
                    # Idle timeout — response is complete
                    break
            except Exception:
                break

        if _lines:
            _reply = "".join(_lines).strip()
            _log.info("[LeaderReply] Leader replied for node %s (%d chars)", node_id, len(_reply))
            self._leader_replies[(run_id, node_id)] = _reply
            self._event_queue.put({
                "type": "leader_replied",
                "node_id": node_id,
                "run_id": run_id,
                "detail": _reply[:200],
                "timestamp": time.time(),
            })
        else:
            _log.warning("[LeaderReply] No leader reply for node %s within deadline, falling back", node_id)
            self.store.update_node(node_id, "running", run_id=run_id)

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

    def _run_direct(self, run_id: str, request: str, score, package: str | None, session_id: str | None, timeout: int) -> dict:
        """Direct mode: leader answers immediately, no workers/WBS/aggregate.

        For ``routing == "direct"``, the leader (hermes) responds directly.
        This avoids spawning any opencode worker subprocess, creating WBS
        nodes, or running the aggregate summarization step.
        """
        self._direct_mode = True
        self.store.log(run_id, "info", "direct mode: leader answering directly")

        started = time.time()
        ok = False
        stdout = ""
        stderr = ""

        # --- Try leader persistent session first ---
        _leader = self._leader_session
        _used_leader = False
        if _leader is not None and _leader.get("proc") is not None and _leader["proc"].poll() is None:
            try:
                _msg = f"用户问题: {request}\n\n请直接回答:"
                _leader["stdin"].write(_msg + "\n")
                _leader["stdin"].flush()

                import select as _sel
                _sfd = _leader["stdout"]
                _fd = _sfd.fileno()
                _lines = []
                _deadline = time.time() + min(timeout, 60)
                _idle_limit = 2.0

                while time.time() < _deadline:
                    _r, _, _ = _sel.select([_fd], [], [], _idle_limit)
                    if _r:
                        _line = _sfd.readline()
                        if not _line:
                            break
                        # Skip prompt lines that hermes may echo
                        if _line.strip() in ("> ", ">", "") or _line.strip().startswith("> "):
                            continue
                        _lines.append(_line)
                    else:
                        # No more data — response complete
                        break

                _raw = "".join(_lines).strip()
                # If we got a response, trim any trailing prompt artifacts
                if _raw:
                    stdout = _raw
                    ok = True
                    _used_leader = True
            except Exception as _exc:
                self.store.log(
                    run_id, "warning",
                    f"leader session read failed, falling back to one-shot: {_exc}",
                )

        # --- Fallback: one-shot Popen + communicate ---
        if not _used_leader:
            backend = self.leader_agent or self.agent_backend
            # Verify backend binary exists
            import shutil as _shutil
            _binary = backend.command[0]
            if _shutil.which(_binary) is None:
                self.store.log(run_id, "error", f"no available agent binary found (tried '{_binary}'), returning fallback")
                stdout = f"No available agent binary found. Tried: '{_binary}'. Please install the CLI tool."
                ok = False
            else:
                # Build session context from past turns for conversational continuity
                _session_ctx = ""
                if session_id:
                    _past_turns = self.store.get_session_turns(session_id, limit=5)
                    if _past_turns:
                        _turn_lines = []
                        for _t in _past_turns:
                            _req = str(_t.get("user_request", ""))[:200]
                            _agg = str(_t.get("aggregate", ""))[:200]
                            _turn_lines.append(f"  用户：{_req}")
                            if _agg:
                                _turn_lines.append(f"  回答：{_agg}")
                        if _turn_lines:
                            _session_ctx = "历史对话：\n" + "\n".join(_turn_lines) + "\n\n"
                _prompt = (
                    f"{backend.prompt_prefix}\n\n"
                    f"你是一个智能助手，请直接回答用户的问题。\n\n"
                    f"{_session_ctx}"
                    f"用户问题: {request}\n\n"
                    f"回答:"
                )
                cmd = backend.build_command(
                    prompt=_prompt, model=self.leader_model,
                    allowed_tools=[], provider=self.provider, reasoning=False,
                )
                import subprocess as _sp
                try:
                    if getattr(backend, 'needs_pty', False):
                        # PTY mode for agents like codex that require a terminal
                        import pty as _pty, os as _os, errno as _errno
                        _master, _slave = _pty.openpty()
                        proc = _sp.Popen(cmd, stdout=_slave, stderr=_slave,
                                         stdin=_slave, close_fds=True, cwd=self.cwd)
                        _os.close(_slave)
                        _stdout_bytes = b""
                        import select as _sel
                        _deadline = time.time() + min(timeout, 60)
                        while time.time() < _deadline and proc.poll() is None:
                            _r, _, _ = _sel.select([_master], [], [], 0.5)
                            if _r:
                                try:
                                    _data = _os.read(_master, 4096)
                                    if not _data:
                                        break
                                    _stdout_bytes += _data
                                except OSError as _ioe:
                                    if _ioe.errno == _errno.EIO:
                                        break  # PTY closed
                                    raise
                        _os.close(_master)
                        proc.wait(timeout=5)
                        _stdout = _stdout_bytes.decode("utf-8", errors="replace")
                        stdout = _stdout.strip()
                        stderr = ""
                        ok = proc.returncode == 0
                    else:
                        proc = _sp.Popen(cmd, stdout=_sp.PIPE, stderr=_sp.PIPE,
                                         text=True, cwd=self.cwd)
                        _stdout, _stderr = proc.communicate(timeout=min(timeout, 60))
                        stdout = _stdout.strip()
                        stderr = _stderr.strip() if _stderr else ""
                        ok = proc.returncode == 0
                except _sp.TimeoutExpired:
                    proc.kill()
                    _stdout, _stderr = proc.communicate(timeout=5)
                    ok = False
                    stdout = (_stdout or "") + "\n[timeout]"
                    stderr = _stderr.strip() if _stderr else ""
                except FileNotFoundError:
                    ok = False
                    # Distinguish between binary not found and cwd not found
                    import shutil as _shutil2
                    if _shutil2.which(backend.command[0]) is None:
                        stdout = f"leader agent not found: {backend.command[0]}"
                    elif not os.path.isdir(self.cwd):
                        stdout = f"cwd does not exist: {self.cwd}"
                    else:
                        stdout = f"failed to spawn {backend.command[0]} (unknown FileNotFoundError)"
                    stderr = ""

        duration = time.time() - started
        result_text = stdout.strip()

        # Save result and finalize run
        self.store.update_run(run_id, "completed" if ok else "failed")
        self.store.log(run_id, "info" if ok else "error",
                       "direct answer completed",
                       {"ok": ok, "duration": round(duration, 1)})

        # Insert a minimal WBS node so frontend _reconstructFromRun can find the result.
        self.store.insert_wbs_node(run_id, {
            "id": "direct-answer",
            "title": "Direct answer",
            "description": request,
            "capability": "general",
            "complexity": score.overall,
            "dependencies": [],
            "parallelizable": True,
            "deliverable": "Direct answer",
            "status": "completed" if ok else "failed",
            "attempt": 1,
            "checkpoint": False,
        })
        self.store.update_node_result(run_id, "direct-answer", result_text)

        self.store.log(run_id, "info" if ok else "error",
                       "run finished", {"ok": ok})

        if session_id:
            self.store.add_session_turn(session_id, run_id, request, result_text)

        # Build WorkerResult-like dict for return
        return {
            "run_id": run_id,
            "ok": ok,
            "complexity": score.to_dict(),
            "results": [],
            "aggregate": {
                "ok": ok,
                "result": result_text,
                "node_id": "direct-answer",
                "duration_seconds": round(duration, 1),
                "returncode": 0,
                "stderr": stderr.strip() if stderr else "",
            } if result_text else None,
            "lessons_learned": [],
        }

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
            if self._file_allowlist and files:
                for f in files:
                    fpath = f.get("path", f) if isinstance(f, dict) else f
                    if fpath not in self._file_allowlist:
                        risks.append(("medium", f"Node {node.id} touched non-allowlist file: {fpath}"))
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
