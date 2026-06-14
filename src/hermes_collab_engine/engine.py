from __future__ import annotations

import concurrent.futures
import hashlib
import json
import fnmatch
import os
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .agents import get_backend, AgentBackend
from .models import Plan, RiskPolicy, CheckpointDecision, WBSNode, WorkerResult
from .planner import Planner
from .skills import SkillRegistry, get_default_registry
from .store import CollabStore
from .tools import ToolRegistry, get_default_tool_registry
from .registry import get_unified_registry, SkillEntry as USkillEntry, ToolEntry as UToolEntry, MCPEntry as UMCPEntry


class CollabEngine:
    _UPSTREAM_PER_CAP = 1500
    _UPSTREAM_PARENT_CAP = 1500
    _UPSTREAM_GRANDPARENT_CAP = 300
    _UPSTREAM_ANCESTOR_CAP = 100
    _UPSTREAM_TOTAL_CAP = 3000
    _RESULT_MARKER = "HERMES-COLLAB-RESULT:"

    def __init__(
        self,
        db_path: str | Path = "data/collab.sqlite3",
        cwd: str | Path = ".",
        model: str | None = None,
        leader_model: str | None = None,
        worker_model: str | None = None,
        agent: str = "claude-code",
        skill_registry: SkillRegistry | None = None,
        tool_registry: ToolRegistry | None = None,
    ):
        self.cwd = Path(cwd).resolve()
        env_model = os.environ.get("HERMES_COLLAB_MODEL") or os.environ.get("ANTHROPIC_MODEL")
        self.leader_model = leader_model or model or os.environ.get("HERMES_COLLAB_LEADER_MODEL") or env_model
        self.worker_model = worker_model or model or os.environ.get("HERMES_COLLAB_WORKER_MODEL") or env_model
        self.agent_backend: AgentBackend = get_backend(agent)
        self.skill_registry = skill_registry or get_default_registry()
        self.tool_registry = tool_registry or get_default_tool_registry()
        self.store = CollabStore(db_path)
        # Initialize unified registry with store for persistence
        get_unified_registry(store=self.store)
        self.planner = Planner(self.cwd, model=self.leader_model, store=self.store)
        self._node_results: dict[str, str] = {}
        self._node_results_struct: dict[str, dict[str, Any] | None] = {}
        self._node_results_lock = threading.Lock()
        self._current_plan: Plan | None = None
        self._risk_assessments: list[dict[str, Any]] = []
        self._checkpoint_paused_nodes: set[str] = set()
        self._paused_runs: set[str] = set()
        self._file_allowlist: set[str] = set()
        self._active_write_targets: dict[str, set[str]] = {}
        self._write_targets_lock = threading.Lock()
        self._active_fingerprints: dict[str, str] = {}
        self._fingerprint_lock = threading.Lock()
        self._restore_all_run_states()

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

    def run(self, request: str, *, title: str | None = None, concurrency: int = 4, timeout: int = 900, max_retries: int = 2, split_count: int = 4, aggregate: bool = True) -> dict:
        run_id = "run_" + uuid.uuid4().hex[:12]
        score = self.planner.assess(request)
        self.store.create_run(run_id, title or request[:80], request, score.to_dict(), agent=self.agent_backend.name)
        self.store.update_run(run_id, "planning")
        self.store.log(run_id, "info", "complexity assessed", score.to_dict())

        if score.routing == "direct":
            plan = Plan(nodes=[WBSNode("wbs-1", "Direct execution", request, "general", score.overall, [], True, "Direct answer")])
        else:
            plan = self.planner.decompose(request, capabilities=self.agent_backend.capabilities)
        if isinstance(plan, list):
            plan = Plan(nodes=plan)
        nodes = plan.nodes
        if plan.shared_brief:
            self.store.log(run_id, "info", "shared plan brief created", {"shared_brief": plan.shared_brief})
            for node in nodes:
                if node.capability == "implementation":
                    node.brief = f"Shared brief:\n{plan.shared_brief}\n\nNode brief:\n{node.brief}" if node.brief else plan.shared_brief
        with self._node_results_lock:
            self._current_plan = plan
            self._node_results = {}
            self._node_results_struct = {}
        self._risk_assessments = []
        for node in nodes:
            node_data = node.to_dict()
            node_data["shared_brief"] = plan.shared_brief
            self.store.insert_wbs_node(run_id, node_data)
        self._preallocate_skills_tools(run_id, nodes)
        self._restore_run_state(run_id)
        self.store.update_run(run_id, "running")

        try:
            results: list[WorkerResult] = []
            pending = {n.id: n for n in nodes}
            completed: set[str] = set()
            failed_final: list[WorkerResult] = []
            max_workers = max(1, concurrency)
            split_children: dict[str, set[str]] = {}
            split_finished: dict[str, set[str]] = {}
            split_results: dict[str, list[WorkerResult]] = {}

            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
                running: dict[concurrent.futures.Future[list[WorkerResult]], WBSNode] = {}
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
                        if self._should_split_proactively(node, timeout, max_retries, split_count):
                            self._release_fingerprint(node.id)
                            self._release_write_targets(node.id)
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
                                    "effective_timeout": self._effective_timeout(node, timeout),
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

                        future = pool.submit(self._run_node_with_retries, run_id, node, self._effective_timeout(node, timeout), max_retries, split_count)
                        running[future] = node

                    if not running:
                        if self._checkpoint_paused_nodes:
                            # Checkpoint paused, no workers running — stop scheduling
                            break
                        continue

                    done, _ = concurrent.futures.wait(running.keys(), return_when=concurrent.futures.FIRST_COMPLETED)
                    for fut in done:
                        node = running.pop(fut)
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

            final = None
            if aggregate:
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
            self._learn(run_id, results)

            ok = not failed_final and (final.ok if final else True)
            self.store.update_run(run_id, "completed" if ok else "failed")
            self.store.log(run_id, "info" if ok else "error", "run finished", {"ok": ok})

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

    def _effective_timeout(self, node: WBSNode, timeout: int) -> int:
        if node.estimated_duration:
            try:
                estimated_timeout = max(1, int(node.estimated_duration) * 2)
            except (TypeError, ValueError):
                return timeout
            return min(timeout, estimated_timeout)
        return timeout

    def _should_split_proactively(self, node: WBSNode, timeout: int, max_retries: int, split_count: int) -> bool:
        if max_retries <= 0 or split_count <= 1 or not node.estimated_duration:
            return False
        try:
            estimated_timeout = int(node.estimated_duration) * 2
        except (TypeError, ValueError):
            return False
        return estimated_timeout > timeout

    def _run_node_with_retries(self, run_id: str, node: WBSNode, timeout: int, max_retries: int, split_count: int) -> list[WorkerResult]:
        self.store.update_node(node.id, "running", run_id=run_id)
        parent = self._run_worker(run_id, node, timeout)
        self.store.update_node(node.id, "completed" if parent.ok else "failed", parent.result, parent.session_id, parent.duration_seconds, None if parent.ok else parent.result, run_id=run_id)
        if parent.ok:
            self._record_node_result(run_id, parent)
        results = [parent]
        if parent.ok:
            return results
        if parent.returncode == 124 and max_retries > 0:
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
                    res = fut.result()
                    results.append(res)
                    self.store.update_node(res.node_id, "completed" if res.ok else "failed", res.result, res.session_id, res.duration_seconds, None if res.ok else res.result, run_id=run_id)
                    if res.ok:
                        self._record_node_result(run_id, res)
            # Phase 2: run implementation shards (with phase 1 upstream context)
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(phase2), 4)) as pool:
                futs = {pool.submit(self._run_worker, run_id, s, timeout): s for s in phase2}
                for fut in concurrent.futures.as_completed(futs):
                    shard = futs[fut]
                    res = fut.result()
                    results.append(res)
                    self.store.update_node(res.node_id, "completed" if res.ok else "failed", res.result, res.session_id, res.duration_seconds, None if res.ok else res.result, run_id=run_id)
                    if res.ok:
                        self._record_node_result(run_id, res)
        return results

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

    def _split_node(self, node: WBSNode, split_count: int) -> list[WBSNode]:
        """Split an over-budget node into shards.

        Phase 1 (parallel, read-only): scope + evidence — collect context.
        Phase 2 (depends on phase 1): implementation — actually write files.
        """
        parent_fingerprint = self._node_fingerprint(node)
        # Phase 1: read-only context shards
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
                    f"Shard from over-budget parent — IMPLEMENTATION phase.\n"
                    f"You MUST write actual code changes to files. Do NOT just produce a plan.\n"
                    f"Use the upstream context from scope and evidence shards to guide your changes.\n"
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
        kept: list[str] = []
        remaining = self._UPSTREAM_TOTAL_CAP
        for depth, dep_id in tiers:
            text = self._context_text_for_node(dep_id, text_snapshot.get(dep_id), struct_snapshot.get(dep_id))
            if not text:
                continue
            cap = self._cap_for_tier(depth)
            snippet = text
            if len(snippet) > cap:
                snippet = '[truncated]\n' + snippet[-(cap - len('[truncated]\n')):]
            if remaining < len(snippet):
                if remaining <= len('[truncated]\n'):
                    break
                snippet = '[truncated]\n' + snippet[-(remaining - len('[truncated]\n')):]
            label = "parent" if depth == 1 else "grandparent" if depth == 2 else f"ancestor depth {depth}"
            kept.append(f"--- from {dep_id} ({label}) ---\n{snippet}")
            remaining -= len(snippet)
            if remaining <= 0:
                break
        if not kept:
            return ''
        return 'Upstream context (from completed dependency nodes):\n' + '\n\n'.join(kept) + '\n\n'

    def _shared_brief_for_worker(self, node: WBSNode) -> str:
        plan = self._current_plan
        if plan is None or not plan.shared_brief:
            return ""
        if node.capability != "implementation":
            return ""
        if plan.shared_brief in (node.brief or ""):
            return ""
        return f"Shared brief:\n{plan.shared_brief}\n\n"

    def _task_text_for_worker(self, node: WBSNode) -> str:
        return "\n".join(part for part in (node.title, node.deliverable, node.brief, node.description) if part)

    def _skills_for_worker(self, node: WBSNode) -> tuple[list[str], str]:
        task_text = self._task_text_for_worker(node)
        # Legacy registry selection
        selected = self.skill_registry.select_for_node(node.capability, task_text)
        names = {skill.name for skill in selected}
        # Bridge: pull web-added entries from UnifiedRegistry (skip built-in "hermes" source)
        unified = get_unified_registry()
        for us in unified.select_skills(node.capability, task_text, max_skills=16):
            if us.name not in names and us.source != "hermes":
                names.add(us.name)
                selected.append(us)
        return list(names), self.skill_registry.render_for_prompt(selected)

    def _tools_for_worker(self, node: WBSNode) -> tuple[list[str], list[str], str]:
        task_text = self._task_text_for_worker(node)
        # Legacy registry selection
        profiles = self.tool_registry.select_for_node(node.capability, task_text)
        names = {profile.name for profile in profiles}
        allowed = self.tool_registry.allowed_tools_for_profiles(profiles)
        # Bridge: pull web-added entries from UnifiedRegistry (skip built-in "hermes" source)
        unified = get_unified_registry()
        for ut in unified.select_tools(node.capability, task_text, max_tools=16):
            if ut.name not in names and ut.source != "hermes":
                names.add(ut.name)
                profiles.append(ut)
                for t in ut.allowed_tools:
                    if t not in allowed:
                        allowed.append(t)
        # Bridge MCP entries: their allowed_tools flow into the tool whitelist
        for mcp in unified.select_mcp(node.capability, max_entries=8):
            if mcp.name not in names and mcp.source != "hermes":
                names.add(mcp.name)
                profiles.append(mcp)
                for t in mcp.allowed_tools:
                    if t not in allowed:
                        allowed.append(t)
        return (list(names), allowed, self.tool_registry.render_for_prompt(profiles))

    def _render_skills_from_names(self, names: list[str]) -> str:
        """Reconstruct the skills prompt block from a list of skill names."""
        unified = get_unified_registry()
        skills = []
        for name in names:
            entry = self.skill_registry.get(name)
            if entry is None:
                # Check unified registry for web-added skills
                for us in unified.list_by_type(USkillEntry):
                    if us.name == name:
                        entry = us
                        break
            if entry is not None:
                skills.append(entry)
        return self.skill_registry.render_for_prompt(skills)

    def _render_tools_from_names(self, names: list[str]) -> tuple[list[str], str]:
        """Reconstruct the tools prompt block and allowed_tools from profile names."""
        unified = get_unified_registry()
        profiles = []
        allowed: list[str] = []
        for name in names:
            entry = self.tool_registry.get(name)
            if entry is None:
                for ut in unified.list_by_type(UToolEntry) + unified.list_by_type(UMCPEntry):
                    if ut.name == name:
                        entry = ut
                        break
            if entry is not None:
                profiles.append(entry)
                for t in getattr(entry, 'allowed_tools', []):
                    if t not in allowed:
                        allowed.append(t)
        return allowed, self.tool_registry.render_for_prompt(profiles)

    def _preallocate_skills_tools(self, run_id: str, nodes: list[WBSNode]) -> None:
        """Pre-compute skills/tools for all nodes before workers start.

        Called once after WBS decomposition, before worker dispatch.
        Stores results in node.skills_json and node.tools_json so workers
        skip per-worker registry traversal.
        Respects leader-assigned skills_json/tools_json when present.
        Filters out built-in (source="hermes") entries that overlap with
        the agent's native capabilities.  Non-built-in entries (web-ui, mcp,
        etc.) are always preserved regardless of capability overlap.
        """
        native_caps = set(self.agent_backend.capabilities)
        for node in nodes:
            try:
                # Respect leader-assigned values; only fill gaps via registry
                if not node.skills_json:
                    skill_names, _skills_block = self._skills_for_worker(node)
                    node.skills_json = json.dumps(skill_names)
                if not node.tools_json:
                    tool_profile_names, _tool_allowed, _tools_block = self._tools_for_worker(node)
                    node.tools_json = json.dumps(tool_profile_names)
                # Filter out built-in SKILLS already covered by native capabilities.
                # Do NOT filter tool profiles — they are permission whitelists,
                # not capability indicators. Removing them would strip the worker
                # of necessary permissions (e.g., Read/Edit/Write).
                if native_caps:
                    if node.skills_json:
                        skill_names = json.loads(node.skills_json)
                        skill_names = [
                            n for n in skill_names
                            if not (n in native_caps and self._is_hermes_builtin_skill(n))
                        ]
                        node.skills_json = json.dumps(skill_names)
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
            "test \"$1\" = get || exit 0; "
            "protocol=; host=; "
            "while IFS= read -r line; do "
            "case \"$line\" in protocol=*) protocol=${line#protocol=};; host=*) host=${line#host=};; esac; "
            "done; "
            "test \"$protocol\" = https || exit 0; "
            "case \",${HERMES_COLLAB_GIT_ALLOWED_HOSTS},\" in *,\"$host\",*) ;; *) exit 0;; esac; "
            "test -n \"$HERMES_COLLAB_GIT_TOKEN\" || exit 0; "
            "printf 'username=%s\\npassword=%s\\n' \"$HERMES_COLLAB_GIT_USERNAME\" \"$HERMES_COLLAB_GIT_TOKEN\"; "
            "}; f",
        )

    def _run_worker(self, run_id: str, node: WBSNode, timeout: int, model_override: str | None = None, role: str = "worker") -> WorkerResult:
        worker_id = f"worker_{run_id}_{node.id}_{node.attempt}"
        self.store.worker_start(worker_id, run_id, node.id)
        self.store.log(run_id, "info", "worker started", {"node": node.id, "title": node.title, "agent": self.agent_backend.name}, node.id)
        started = time.time()
        upstream_block = self._build_upstream_context(node)
        shared_brief_block = self._shared_brief_for_worker(node)
        brief_block = f"Brief:\n{node.brief}\n\n" if node.brief else ""
        if node.skills_json:
            skill_names = json.loads(node.skills_json)
            skills_block = self._render_skills_from_names(skill_names)
        else:
            skill_names, skills_block = self._skills_for_worker(node)
        if node.tools_json:
            tool_profile_names = json.loads(node.tools_json)
            tool_allowed, tools_block = self._render_tools_from_names(tool_profile_names)
        else:
            tool_profile_names, tool_allowed, tools_block = self._tools_for_worker(node)
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

{skills_block}{tools_block}{write_block}{shared_brief_block}{brief_block}{upstream_block}Task:
{node.description}

Work in cwd: {self.cwd}
Return the deliverable. If you modify files, state exact paths. If read-only, do not modify files.

Output contract:
- First, write the human-readable deliverable for the user.
- On the final line, include exactly one machine-readable JSON object prefixed by {self._RESULT_MARKER}
- Use this JSON shape: {{"status":"ok|blocked|failed","summary":"short result summary","files_modified":["path"],"verification":["command or check"],"notes":["optional note"]}}
{backend.prompt_suffix}"""
        selected_model = model_override or self.worker_model
        # If prompt is too long for command-line args, use stdin via temp file
        _PROMPT_ARG_MAX = 100_000  # conservative limit for -p argument
        use_stdin = len(prompt.encode("utf-8", errors="replace")) > _PROMPT_ARG_MAX
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
            )
        else:
            cmd = backend.build_command(
                prompt=prompt,
                model=selected_model,
                allowed_tools=final_allowed,
            )
        try:
            stdin_data = open(tmp_path, "r").read() if tmp_path else None
            run_kwargs = {
                "cwd": self.cwd,
                "env": self._env_for_role(role),
                "text": True,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "timeout": timeout,
            }
            if use_stdin:
                run_kwargs["input"] = stdin_data
            else:
                run_kwargs["stdin"] = subprocess.DEVNULL
            proc = subprocess.run(cmd, **run_kwargs)
            duration = round(time.time() - started, 3)
        except subprocess.TimeoutExpired as exc:
            if tmp_path:
                import os
                try: os.unlink(tmp_path)
                except OSError: pass
            duration = round(time.time() - started, 3)
            result = WorkerResult(node.id, node.title, False, f"Timed out after {timeout}s", None, duration, 124, (exc.stderr or "") if isinstance(exc.stderr, str) else "", node.attempt)
            self.store.worker_finish(worker_id, "timeout", duration, None, result.result)
            self.store.log(run_id, "warning", "worker timeout", result.to_dict(), node.id)
            return result

        text = proc.stdout.strip()
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
        if ok and contract_error:
            self.store.log(run_id, "warning", "worker result contract missing or invalid", {"node": node.id, "error": contract_error}, node.id)
            self.store.add_lesson(
                "worker-contract",
                "Workers must end successful output with a valid HERMES-COLLAB-RESULT JSON line so downstream context can use structured summaries.",
                {"run_id": run_id, "node": node.id, "error": contract_error},
            )
        result = WorkerResult(node.id, node.title, ok, text, session_id, duration, proc.returncode, proc.stderr.strip(), node.attempt, result_struct)
        self.store.worker_finish(worker_id, "completed" if ok else "failed", duration, session_id, None if ok else text)
        self.store.log(run_id, "info" if ok else "error", "worker finished", result.to_dict(), node.id)
        if tmp_path:
            import os
            try: os.unlink(tmp_path)
            except OSError: pass
        return result

    def _parse_result_contract(self, text: str) -> tuple[dict[str, Any] | None, str | None]:
        marker_index = text.rfind(self._RESULT_MARKER)
        if marker_index < 0:
            return None, "missing HERMES-COLLAB-RESULT marker"
        raw = text[marker_index + len(self._RESULT_MARKER):].strip()
        if not raw:
            return None, "empty HERMES-COLLAB-RESULT payload"
        line = raw.splitlines()[0].strip()
        if line.startswith("```") and len(raw.splitlines()) > 1:
            line = raw.splitlines()[1].strip()
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as exc:
            return None, f"invalid HERMES-COLLAB-RESULT JSON: {exc}"
        if not isinstance(parsed, dict):
            return None, "HERMES-COLLAB-RESULT payload is not an object"
        return parsed, None

    def _aggregate(self, run_id: str, request: str, results: list[WorkerResult], timeout: int) -> WorkerResult:
        node = WBSNode(f"{run_id}-aggregate", "Aggregate results", "Aggregate worker outputs into final answer", "aggregation", 5, [], False, "Final answer")
        self.store.insert_wbs_node(run_id, node.to_dict())
        self.store.update_node(node.id, "running", run_id=run_id)
        report = json.dumps([r.to_dict() for r in results], ensure_ascii=False, indent=2)
        node.description = f"Original request:\n{request}\n\nWorker results:\n{report}\n\nProduce final concise report. Mention timeouts and shard coverage honestly."
        return self._run_worker(run_id, node, timeout, model_override=self.leader_model, role="leader")

    def _learn(self, run_id: str, results: list[WorkerResult]) -> None:
        timeouts = [r for r in results if r.returncode == 124]
        if timeouts:
            self.store.add_lesson("watchdog", f"Run {run_id}: {len(timeouts)} worker(s) timed out; split large WBS nodes earlier or reduce scope.", {"run_id": run_id})
        slow = [r for r in results if r.duration_seconds > 120 and r.ok]
        if slow:
            self.store.add_lesson("planning", f"Run {run_id}: {len(slow)} slow successful worker(s); consider smaller WBS nodes for similar tasks.", {"run_id": run_id})

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
