from __future__ import annotations

import concurrent.futures
import json
import subprocess
import threading
import time
import uuid
from pathlib import Path

from .models import WBSNode, WorkerResult
from .planner import Planner
from .store import CollabStore


class CollabEngine:
    _UPSTREAM_PER_CAP = 800
    _UPSTREAM_TOTAL_CAP = 3000

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
        self.planner = Planner(self.cwd, model=self.leader_model)
        self._node_results: dict[str, str] = {}
        self._node_results_lock = threading.Lock()

    def run(self, request: str, *, title: str | None = None, concurrency: int = 4, timeout: int = 900, max_retries: int = 2, split_count: int = 4, aggregate: bool = True) -> dict:
        run_id = "run_" + uuid.uuid4().hex[:12]
        score = self.planner.assess(request)
        self.store.create_run(run_id, title or request[:80], request, score.to_dict())
        self.store.update_run(run_id, "planning")
        self.store.log(run_id, "info", "complexity assessed", score.to_dict())

        if score.routing == "direct":
            nodes = [WBSNode("wbs-1", "Direct execution", request, "general", score.overall, [], True, "Direct answer")]
        else:
            nodes = self.planner.decompose(request)
        for node in nodes:
            self.store.insert_wbs_node(run_id, node.to_dict())
        self.store.update_run(run_id, "running")

        try:
            results: list[WorkerResult] = []
            pending = {n.id: n for n in nodes}
            completed: set[str] = set()
            failed_final: list[WorkerResult] = []

            while pending:
                ready = [n for n in pending.values() if all(dep in completed for dep in n.dependencies)]
                if not ready:
                    # Break dependency deadlocks by running the first pending node and logging it.
                    ready = [next(iter(pending.values()))]
                    self.store.log(run_id, "warning", "dependency deadlock avoided", {"node": ready[0].id})

                batch = ready[:max(1, concurrency)]
                for node in batch:
                    pending.pop(node.id, None)

                with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
                    futs = [pool.submit(self._run_node_with_retries, run_id, node, timeout, max_retries, split_count) for node in batch]
                    for fut in concurrent.futures.as_completed(futs):
                        node_results = fut.result()
                        results.extend(node_results)
                        if any(r.ok for r in node_results):
                            # Mark the original node as covered if parent or any shard succeeded.
                            completed.add(batch[futs.index(fut)].id)
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

    def _run_node_with_retries(self, run_id: str, node: WBSNode, timeout: int, max_retries: int, split_count: int) -> list[WorkerResult]:
        self.store.update_node(node.id, "running")
        parent = self._run_worker(run_id, node, timeout)
        self.store.update_node(node.id, "completed" if parent.ok else "failed", parent.result, parent.session_id, parent.duration_seconds, None if parent.ok else parent.result)
        if parent.ok:
            with self._node_results_lock:
                self._node_results[node.id] = parent.result or ''
        results = [parent]
        if parent.ok:
            return results
        if parent.returncode == 124 and max_retries > 0:
            self.store.log(run_id, "warning", "node timed out; splitting", {"node": node.id, "split_count": split_count})
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
        return results

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
                description=f"Shard from timed-out parent. {guidance}\n\nOriginal task:\n{node.description}",
                capability=node.capability,
                complexity=max(1, node.complexity - 2),
                dependencies=[],
                parallelizable=True,
                deliverable=f"Focused {key} shard result",
                parent_id=node.id,
                attempt=node.attempt + 1,
            )
            shards.append(shard)
        return shards

    def _build_upstream_context(self, node) -> str:
        if not node.dependencies:
            return ''
        with self._node_results_lock:
            snapshot = {dep: self._node_results.get(dep) for dep in node.dependencies}
        kept: list[str] = []
        remaining = self._UPSTREAM_TOTAL_CAP
        for dep_id in node.dependencies:
            text = snapshot.get(dep_id)
            if not text:
                continue
            snippet = text
            if len(snippet) > self._UPSTREAM_PER_CAP:
                snippet = '[truncated]\n' + snippet[-(self._UPSTREAM_PER_CAP - len('[truncated]\n')):]
            if remaining < len(snippet):
                if remaining <= len('[truncated]\n'):
                    break
                snippet = '[truncated]\n' + snippet[-(remaining - len('[truncated]\n')):]
            kept.append(f"--- from {dep_id} ---\n{snippet}")
            remaining -= len(snippet)
            if remaining <= 0:
                break
        if not kept:
            return ''
        return 'Upstream context (from completed dependency nodes):\n' + '\n\n'.join(kept) + '\n\n'

    def _run_worker(self, run_id: str, node: WBSNode, timeout: int, model_override: str | None = None) -> WorkerResult:
        worker_id = f"worker_{run_id}_{node.id}_{node.attempt}"
        self.store.worker_start(worker_id, run_id, node.id)
        self.store.log(run_id, "info", "worker started", {"node": node.id, "title": node.title}, node.id)
        started = time.time()
        upstream_block = self._build_upstream_context(node)
        prompt = f"""You are a Claude Code worker in a Hermes collaboration engine.

WBS node: {node.title}
Capability: {node.capability}
Deliverable: {node.deliverable}

{upstream_block}Task:
{node.description}

Work in cwd: {self.cwd}
Return the deliverable. If you modify files, state exact paths. If read-only, do not modify files."""
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
        result = WorkerResult(node.id, node.title, ok, text, session_id, duration, proc.returncode, proc.stderr.strip(), node.attempt)
        self.store.worker_finish(worker_id, "completed" if ok else "failed", duration, session_id, None if ok else text)
        self.store.log(run_id, "info" if ok else "error", "worker finished", result.to_dict(), node.id)
        return result

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
