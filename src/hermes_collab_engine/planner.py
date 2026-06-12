from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from .models import ComplexityScore, Plan, WBSNode


class Planner:
    def __init__(self, cwd: Path, model: str | None = None, timeout: int = 120, store=None):
        self.cwd = cwd
        self.model = model
        self.timeout = timeout
        self.store = store

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

    def _load_recent_lessons(self, limit: int = 6) -> str:
        if self.store is None:
            return ""
        try:
            lessons = self.store.lessons(limit=limit)
        except Exception:
            return ""
        if not lessons:
            return ""
        lines = []
        for lesson in lessons:
            scope = lesson.get("scope") or "global"
            if scope not in {"global", "project"}:
                continue
            category = lesson.get("category") or "general"
            text = str(lesson.get("lesson") or "").strip()
            if text:
                lines.append(f"- [{scope}/{category}] {text}")
        if not lines:
            return ""
        return "Recent planning lessons to apply:\n" + "\n".join(lines) + "\n\n"

    def _checkpoint_lesson_texts(self, store=None, limit: int = 20) -> list[str]:
        if store is None:
            return []
        try:
            lessons = store.lessons(limit=limit, scope="engine")
        except Exception:
            return []
        texts = []
        for lesson in lessons:
            text = str(lesson.get("lesson") or "").strip().lower()
            if text:
                texts.append(text)
        return texts

    def _assign_checkpoints(self, nodes: list[WBSNode], store=None) -> list[WBSNode]:
        children_map: dict[str, list[WBSNode]] = {node.id: [] for node in nodes}
        for node in nodes:
            for dependency in node.dependencies:
                children_map.setdefault(dependency, []).append(node)

        lesson_texts = self._checkpoint_lesson_texts(store)
        for node in nodes:
            if node.complexity >= 7:
                node.checkpoint = True
            children = children_map.get(node.id, [])
            if node.capability == "implementation" and any(child.capability == "implementation" for child in children):
                node.checkpoint = True
            title_words = [word for word in re.findall(r"\w+", node.title.lower()) if len(word) > 3]
            if title_words and any(any(word in text for word in title_words) for text in lesson_texts):
                node.checkpoint = True
        return nodes

    def decompose(self, request: str, max_nodes: int = 8) -> Plan:
        lessons_block = self._load_recent_lessons()
        prompt = f"""You are designing a WBS for a software collaboration engine implementation.

Repository: {self.cwd}
{lessons_block}User request:
{request}

Return ONLY one JSON object with this schema:
{{
  "shared_brief": "Short context all workers should share before their node-specific task.",
  "nodes": [
    {{
      "id": "wbs-1",
      "title": "Node title",
      "description": "Detailed node task",
      "capability": "analysis|planning|implementation|verification|general",
      "complexity": 1,
      "dependencies": ["wbs-1"],
      "parallelizable": true,
      "deliverable": "Expected worker output",
      "brief": "Node-specific brief with scope, boundaries, and useful context.",
      "estimated_duration": 300
    }}
  ]
}}

Create 4-{max_nodes} WBS nodes. complexity must be 1-10. estimated_duration is seconds and should be a positive integer.
Design nodes so independent work can run in parallel while write-heavy implementation is sequenced safely.
No prose, no code fences outside the JSON, just the object.
"""
        try:
            data = self._claude_json(prompt)
            if isinstance(data, list):
                data = {"nodes": data}
            raw_nodes = data.get("nodes", []) if isinstance(data, dict) else []
            nodes = []
            for i, item in enumerate(raw_nodes[:max_nodes], 1):
                if not isinstance(item, dict):
                    continue
                nodes.append(WBSNode(
                    id=str(item.get("id") or f"wbs-{i}"),
                    title=str(item.get("title") or f"WBS {i}"),
                    description=str(item.get("description") or item.get("title") or ""),
                    capability=str(item.get("capability") or item.get("capabilityRequired") or "implementation"),
                    complexity=int(item.get("complexity") or 5),
                    dependencies=list(item.get("dependencies") or []),
                    parallelizable=bool(item.get("parallelizable", True)),
                    deliverable=str(item.get("deliverable") or "Completed work"),
                    brief=str(item.get("brief") or ""),
                    estimated_duration=int(item["estimated_duration"]) if item.get("estimated_duration") is not None else None,
                ))
            if nodes:
                shared_brief = str(data.get("shared_brief") or "") if isinstance(data, dict) else ""
                return Plan(nodes=self._assign_checkpoints(nodes, self.store), shared_brief=shared_brief)
        except Exception:
            pass
        return self.fallback_wbs(request)

    def fallback_wbs(self, request: str) -> Plan:
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
                brief="Clarify requirements, constraints, affected areas, and verification needs before implementation begins.",
                estimated_duration=300,
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
                brief="Use the analysis output to make the smallest safe code or documentation change that satisfies the request.",
                estimated_duration=900,
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
                brief="Turn the analysis into a concrete implementation strategy with sequencing, risks, and file-level focus.",
                estimated_duration=600,
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
                brief="Apply the planned changes while preserving existing behavior and coordinating write-heavy edits safely.",
                estimated_duration=1200,
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
            brief="Verify the completed work with targeted checks and document outcomes, including any skipped validation.",
            estimated_duration=600,
        ))
        children_map: dict[str, list[WBSNode]] = {node.id: [] for node in nodes}
        for node in nodes:
            for dependency in node.dependencies:
                children_map.setdefault(dependency, []).append(node)
        for node in nodes:
            if node.complexity >= 5:
                node.checkpoint = True
            children = children_map.get(node.id, [])
            if node.capability == "implementation" and any(child.capability == "implementation" for child in children):
                node.checkpoint = True

        shared_brief = (
            "Fallback plan generated without leader model output. Keep scope tight, pass useful findings through dependencies, "
            "and report verification honestly."
        )
        return Plan(nodes=nodes, shared_brief=shared_brief)

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
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        object_match = re.search(r"\{[\s\S]*\}", text)
        array_match = re.search(r"\[[\s\S]*\]", text)
        matches = [m for m in (object_match, array_match) if m]
        if matches:
            match = min(matches, key=lambda m: m.start())
            return json.loads(match.group(0))
        return json.loads(text)
