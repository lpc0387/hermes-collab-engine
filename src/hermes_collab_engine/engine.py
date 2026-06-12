from __future__ import annotations

import concurrent.futures
import json
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .models import Plan, WBSNode, WorkerResult
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
    ):
        self.cwd = Path(cwd).resolve()
        self.leader_model = leader_model or model
        self.worker_model = worker_model or model
        self.store = CollabStore(db_path)
        self.planner = Planner(self.cwd, model=self.leader_model, store=self.store)
        self._node_results: dict[str, str] = {}
        self._node_results_struct: dict[str, dict[str, Any]] = {}
        self._node_results_lock = threading.Lock()
        self._current_plan: Plan | None = None

    def run(self, request: str, *, title: str | None = None, concurrency: int = 4, timeout: int = 900, max_retries: int = 2, split_count: int = 4, aggregate: bool = True) -> dict:
        run_id = "run_" + uuid.uuid4().hex[:12]
        score = self.planner.assess(request)
        self.store.create_run(run_id, title or request[:80], request, score.to_dict())
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
        for node in nodes:
            self.store.insert_wbs_node(run_id, node.to_dict())
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
                        ready = [n for n in pending.values() if all(dep in completed for dep in n.dependencies)]
                        if not ready:
                            if running:
                                break
                            # Break dependency deadlocks by running the first pending node and logging it.
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
            self._record_node_result(parent)
        results = [parent]
        if parent.ok:
            return results
        if parent.returncode == 124 and max_retries > 0:
            self.store.log(run_id, "warning", "node timed out; splitting", {"node": node.id, "split_count": split_count}, node.id)
            shards = self._split_node(node, split_count)
            for shard in shards:
                self.store.insert_wbs_node(run_id, shard.to_dict())
                self.store.update_node(shard.id, "pending")
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(split_count, 4)) as pool:
                futs = [pool.submit(self._run_worker, run_id, shard, timeout) for shard in shards]
                for fut in concurrent.futures.as_completed(futs):
                    res = fut.result()
                    results.append(res)
                    self.store.update_node(res.node_id, "completed" if res.ok else "failed", res.result, res.session_id, res.duration_seconds, None if res.ok else res.result)
                    if res.ok:
                        self._record_node_result(res)
        return results

    def _record_node_result(self, result: WorkerResult) -> None:
        with self._node_results_lock:
            self._node_results[result.node_id] = result.result or ''
            if result.result_struct is not None:
                self._node_results_struct[result.node_id] = result.result_struct

    def _split_node(self, node: WBSNode, split_count: int) -> list[WBSNode]:
        focuses = [
            ("scope", "Find the smallest relevant scope and entrypoints only."),
            ("evidence", "Collect exact file paths, commands, symbols, and evidence only."),
            ("implementation", "Produce a minimal implementation plan or patch strategy only."),
            ("risks", "Identify blockers, unknowns, and verification needs only."),
        ]
        shards = []
        for i in range(split_count):
            key, guidance = focuses[i % len(focuses)]
            shard = WBSNode(
                id=f"{node.id}-{key}-{i+1}",
                title=f"{node.title} / {key}",
                description=f"Shard from timed-out or over-budget parent. {guidance}\n\nOriginal task:\n{node.description}",
                capability=node.capability,
                complexity=max(1, node.complexity - 2),
                dependencies=[],
                parallelizable=True,
                deliverable=f"Focused {key} shard result",
                parent_id=node.id,
                attempt=node.attempt + 1,
                brief=node.brief,
            )
            shards.append(shard)
        return shards

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
        self.store.log(run_id, "info", "worker started", {"node": node.id, "title": node.title}, node.id)
        started = time.time()
        upstream_block = self._build_upstream_context(node)
        shared_brief_block = self._shared_brief_for_worker(node)
        brief_block = f"Brief:\n{node.brief}\n\n" if node.brief else ""
        prompt = f"""You are a Claude Code worker in a Hermes collaboration engine.

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
- Use this JSON shape: {{"status":"ok|blocked|failed","summary":"short result summary","files_modified":["path"],"verification":["command or check"],"notes":["optional note"]}}"""
        allowed_tools = ",".join([
            "Read",
            "Edit",
            "Write",
            "MultiEdit",
            "Bash(git diff*)",
            "Bash(git status*)",
            "Bash(git ls-files*)",
            "Bash(git add*)",
            "Bash(git commit*)",
            "Bash(git push*)",
            "Bash(python3 -m unittest*)",
            "Bash(python3 -m py_compile*)",
            "Bash(bash -n*)",
        ])
        cmd = [
            "claude",
            "-p",
            prompt,
            "--output-format",
            "json",
            "--permission-mode",
            "acceptEdits",
            "--allowedTools",
            allowed_tools,
        ]
        selected_model = model_override or self.worker_model
        if selected_model:
            cmd.extend(["--model", selected_model])
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
        session_id = None
        ok = proc.returncode == 0
        try:
            parsed = json.loads(text)
            text = str(parsed.get("result", text))
            session_id = parsed.get("session_id")
            ok = ok and not bool(parsed.get("is_error"))
        except Exception:
            pass
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
