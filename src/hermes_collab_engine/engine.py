from __future__ import annotations

import concurrent.futures
import json
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .agents import get_backend, AgentBackend
from .models import Plan, RiskPolicy, CheckpointDecision, WBSNode, WorkerResult
from .planner import Planner
from .store import CollabStore


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
    ):
        self.cwd = Path(cwd).resolve()
        self.leader_model = leader_model or model
        self.worker_model = worker_model or model
        self.agent_backend: AgentBackend = get_backend(agent)
        self.store = CollabStore(db_path)
        self.planner = Planner(self.cwd, model=self.leader_model, store=self.store)
        self._node_results: dict[str, str] = {}
        self._node_results_struct: dict[str, dict[str, Any] | None] = {}
        self._node_results_lock = threading.Lock()
        self._current_plan: Plan | None = None
        self._risk_assessments: list[dict[str, Any]] = []
        self._checkpoint_paused_nodes: set[str] = set()
        self._paused_runs: set[str] = set()
        self._file_allowlist: set[str] = set()
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
            plan = self.planner.decompose(request)
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
                        pending.pop(node.id, None)
                        if self._should_split_proactively(node, timeout, max_retries, split_count):
                            shards = self._split_node(node, split_count)
                            split_children[node.id] = {shard.id for shard in shards}
                            split_finished[node.id] = set()
                            split_results[node.id] = []
                            self.store.update_node(node.id, "running")
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
                                self.store.update_node(shard.id, "pending")
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
                        try:
                            node_results = fut.result()
                        except Exception as exc:
                            duration = 0.0
                            result = WorkerResult(node.id, node.title, False, f"Worker crashed: {type(exc).__name__}: {exc}", None, duration, 1, "", node.attempt)
                            self.store.update_node(node.id, "failed", result.result, None, duration, result.result)
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
                                    self.store.update_node(parent_id, "completed", "Completed by proactive shards", None, None, None)
                                else:
                                    failed_final.extend(parent_results)
                                    self.store.update_node(parent_id, "failed", None, None, None, "All proactive shards failed")
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
            self._learn(run_id, results)

            ok = not failed_final and (final.ok if final else True)
            self.store.update_run(run_id, "completed" if ok else "failed")
            self.store.log(run_id, "info" if ok else "error", "run finished", {"ok": ok})
            return {
                "run_id": run_id,
                "ok": ok,
                "complexity": score.to_dict(),
                "results": [r.to_dict() for r in results],
                "aggregate": final.to_dict() if final else None,
            }
        except BaseException as exc:
            self.store.fail_stale_run(run_id, f"interrupted: {type(exc).__name__}: {exc}")
            raise
        finally:
            with self._node_results_lock:
                self._current_plan = None
                self._node_results = {}
                self._node_results_struct = {}
            self._risk_assessments = []
            self._checkpoint_paused_nodes.clear()
            self._paused_runs.discard(run_id)

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
        self.store.update_node(node.id, "running")
        parent = self._run_worker(run_id, node, timeout)
        self.store.update_node(node.id, "completed" if parent.ok else "failed", parent.result, parent.session_id, parent.duration_seconds, None if parent.ok else parent.result)
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
                self.store.update_node(shard.id, "pending")
            # Phase 1: run read-only context shards (scope + evidence) in parallel
            phase1 = [s for s in shards if not s.dependencies]
            phase2 = [s for s in shards if s.dependencies]
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(phase1), 4)) as pool:
                futs = {pool.submit(self._run_worker, run_id, s, timeout): s for s in phase1}
                for fut in concurrent.futures.as_completed(futs):
                    shard = futs[fut]
                    res = fut.result()
                    results.append(res)
                    self.store.update_node(res.node_id, "completed" if res.ok else "failed", res.result, res.session_id, res.duration_seconds, None if res.ok else res.result)
                    if res.ok:
                        self._record_node_result(run_id, res)
            # Phase 2: run implementation shards (with phase 1 upstream context)
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(phase2), 4)) as pool:
                futs = {pool.submit(self._run_worker, run_id, s, timeout): s for s in phase2}
                for fut in concurrent.futures.as_completed(futs):
                    shard = futs[fut]
                    res = fut.result()
                    results.append(res)
                    self.store.update_node(res.node_id, "completed" if res.ok else "failed", res.result, res.session_id, res.duration_seconds, None if res.ok else res.result)
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

    def _run_worker(self, run_id: str, node: WBSNode, timeout: int, model_override: str | None = None) -> WorkerResult:
        worker_id = f"worker_{run_id}_{node.id}_{node.attempt}"
        self.store.worker_start(worker_id, run_id, node.id)
        self.store.log(run_id, "info", "worker started", {"node": node.id, "title": node.title, "agent": self.agent_backend.name}, node.id)
        started = time.time()
        upstream_block = self._build_upstream_context(node)
        shared_brief_block = self._shared_brief_for_worker(node)
        brief_block = f"Brief:\n{node.brief}\n\n" if node.brief else ""
        backend = self.agent_backend
        prompt = f"""{backend.prompt_prefix}

WBS node: {node.title}
Capability: {node.capability}
Deliverable: {node.deliverable}

{shared_brief_block}{brief_block}{upstream_block}Task:
{node.description}

Work in cwd: {self.cwd}
Return the deliverable. If you modify files, state exact paths. If read-only, do not modify files.

Output contract:
- First, write the human-readable deliverable for the user.
- On the final line, include exactly one machine-readable JSON object prefixed by {self._RESULT_MARKER}
- Use this JSON shape: {{"status":"ok|blocked|failed","summary":"short result summary","files_modified":["path"],"verification":["command or check"],"notes":["optional note"]}}
{backend.prompt_suffix}"""
        selected_model = model_override or self.worker_model
        cmd = backend.build_command(
            prompt=prompt,
            model=selected_model,
            allowed_tools=backend.default_allowed_tools,
        )
        try:
            proc = subprocess.run(cmd, cwd=self.cwd, text=True, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
            duration = round(time.time() - started, 3)
        except subprocess.TimeoutExpired as exc:
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
        node = WBSNode("aggregate", "Aggregate results", "Aggregate worker outputs into final answer", "aggregation", 5, [], False, "Final answer")
        report = json.dumps([r.to_dict() for r in results], ensure_ascii=False, indent=2)
        node.description = f"Original request:\n{request}\n\nWorker results:\n{report}\n\nProduce final concise report. Mention timeouts and shard coverage honestly."
        return self._run_worker(run_id, node, timeout, model_override=self.leader_model)

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

    def _detect_risks(self, node: WBSNode, result_struct: dict[str, Any] | None, risk_policy: RiskPolicy) -> list[tuple[str, str]]:
        """Detect risk events from a completed node. Returns [(risk_level, description)]."""
        risks: list[tuple[str, str]] = []
        if result_struct:
            blocking = result_struct.get("blocking_issues") or result_struct.get("notes")
            if blocking and isinstance(blocking, list) and len(blocking) > 0:
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
            if action in ("notify", "pause"):
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
            )
            wbs_nodes.append(wbs_node)
            if n.get("shared_brief"):
                shared_brief = n["shared_brief"]
        return Plan(nodes=wbs_nodes, shared_brief=shared_brief)
