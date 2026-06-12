from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from .models import ComplexityScore, WBSNode


class Planner:
    def __init__(self, cwd: Path, model: str | None = None, timeout: int = 120):
        self.cwd = cwd
        self.model = model
        self.timeout = timeout

    def assess(self, request: str) -> ComplexityScore:
        try:
            score = self._claude_assess(request)
            if score is not None:
                return score
        except Exception:
            pass
        return self._heuristic_assess(request)

    def _heuristic_assess(self, request: str) -> ComplexityScore:
        text = request.lower()
        steps = min(10, max(1, len(re.findall(r"[,;；，。\n]| and | then |同时|然后|并且", request)) + 1))
        domain = 7 if any(k in text for k in ["architecture", "framework", "engine", "协同", "架构", "框架"]) else 4
        ambiguity = 7 if any(k in text for k in ["重新梳理", "整个", "自主", "self", "evolution", "复杂"]) else 3
        coupling = 7 if any(k in text for k in ["集成", "dashboard", "sqlite", "worker", "memory", "面板"]) else 3
        risk = 6 if any(k in text for k in ["实现", "数据库", "持久化", "监控", "并行"]) else 3
        overall = round((domain + steps + ambiguity + coupling + risk) / 5)
        if overall <= 3:
            routing = "direct"
        elif overall <= 6:
            routing = "single"
        else:
            routing = "wbs"
        return ComplexityScore(domain, steps, ambiguity, coupling, risk, overall, routing)

    def _claude_assess(self, request: str) -> ComplexityScore | None:
        prompt = f"""You are scoring the complexity of a software engineering request.

User request:
{request}

Return ONLY a JSON array containing a single object with these integer fields (each 1-10):
domain, steps, ambiguity, coupling, risk, overall
and a string field routing which must be one of: "direct", "single", "wbs".
No prose, no code fences outside the JSON, just the array.
"""
        data = self._claude_json(prompt)
        if not isinstance(data, list) or not data:
            return None
        item = data[0]
        if not isinstance(item, dict):
            return None

        def _clamp(value) -> int:
            try:
                n = int(round(float(value)))
            except (TypeError, ValueError):
                return 5
            if n < 1:
                return 1
            if n > 10:
                return 10
            return n

        domain = _clamp(item.get("domain"))
        steps = _clamp(item.get("steps"))
        ambiguity = _clamp(item.get("ambiguity"))
        coupling = _clamp(item.get("coupling"))
        risk = _clamp(item.get("risk"))
        if "overall" in item:
            overall = _clamp(item.get("overall"))
        else:
            overall = round((domain + steps + ambiguity + coupling + risk) / 5)
            overall = max(1, min(10, overall))
        routing_raw = item.get("routing")
        routing = routing_raw if routing_raw in ("direct", "single", "wbs") else None
        if routing is None:
            if overall <= 3:
                routing = "direct"
            elif overall <= 6:
                routing = "single"
            else:
                routing = "wbs"
        return ComplexityScore(domain, steps, ambiguity, coupling, risk, overall, routing)

    def decompose(self, request: str, max_nodes: int = 8) -> list[WBSNode]:
        prompt = f"""You are designing a WBS for a software collaboration engine implementation.

Repository: {self.cwd}
User request:
{request}

Return ONLY a JSON array of 4-{max_nodes} WBS nodes. Each node must have:
id, title, description, capability, complexity (1-10), dependencies (array of ids), parallelizable (boolean), deliverable.
Design nodes so independent work can run in parallel while write-heavy implementation is sequenced safely.
"""
        try:
            data = self._claude_json(prompt)
            nodes = []
            for i, item in enumerate(data[:max_nodes], 1):
                nodes.append(WBSNode(
                    id=str(item.get("id") or f"wbs-{i}"),
                    title=str(item.get("title") or f"WBS {i}"),
                    description=str(item.get("description") or item.get("title") or ""),
                    capability=str(item.get("capability") or item.get("capabilityRequired") or "implementation"),
                    complexity=int(item.get("complexity") or 5),
                    dependencies=list(item.get("dependencies") or []),
                    parallelizable=bool(item.get("parallelizable", True)),
                    deliverable=str(item.get("deliverable") or "Completed work"),
                ))
            if nodes:
                return nodes
        except Exception:
            pass
        return self.fallback_wbs(request)

    def fallback_wbs(self, request: str) -> list[WBSNode]:
        snippet = request.strip()
        if len(snippet) > 1500:
            snippet = snippet[:1500] + "…"
        short = len(snippet) < 200

        def _desc(action: str) -> str:
            return f"{action} for the following request:\n\n{snippet}"

        nodes: list[WBSNode] = [
            WBSNode(
                "wbs-1",
                "Analyze request",
                _desc("Analyze the requirements, constraints, and scope"),
                "analysis",
                5,
                [],
                True,
                "Requirements analysis and scope summary",
            ),
        ]
        if short:
            nodes.append(WBSNode(
                "wbs-2",
                "Implement solution",
                _desc("Plan and implement the solution end-to-end"),
                "implementation",
                6,
                ["wbs-1"],
                False,
                "Implemented solution",
            ))
            last_execute_id = "wbs-2"
        else:
            nodes.append(WBSNode(
                "wbs-2",
                "Plan solution",
                _desc("Design the approach and plan implementation steps"),
                "planning",
                5,
                ["wbs-1"],
                True,
                "Implementation plan",
            ))
            nodes.append(WBSNode(
                "wbs-3",
                "Implement solution",
                _desc("Implement the planned solution"),
                "implementation",
                7,
                ["wbs-2"],
                False,
                "Implemented solution",
            ))
            last_execute_id = "wbs-3"
        nodes.append(WBSNode(
            "wbs-verify",
            "Verify and document",
            _desc("Verify correctness and document the result"),
            "verification",
            5,
            [last_execute_id],
            False,
            "Verification report and documentation",
        ))
        return nodes

    def _claude_json(self, prompt: str):
        cmd = ["claude", "-p", prompt, "--output-format", "json"]
        if self.model:
            cmd.extend(["--model", self.model])
        proc = subprocess.run(cmd, cwd=self.cwd, text=True, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=self.timeout)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr)
        outer = json.loads(proc.stdout)
        text = outer.get("result", "")
        match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
        if match:
            text = match.group(1)
        match = re.search(r"\[[\s\S]*\]", text)
        if match:
            text = match.group(0)
        return json.loads(text)
