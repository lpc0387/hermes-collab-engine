from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

from .models import ComplexityScore, Plan, WBSNode
from .registry import MCPEntry, SkillEntry, ToolEntry, get_unified_registry


class Planner:
    def __init__(self, cwd: Path, model: str | None = None, timeout: int = 120, store=None):
        self.cwd = cwd
        self.model = model
        self.timeout = timeout
        self.store = store

    def assess(self, request: str) -> ComplexityScore:
        local = self._local_assess(request)
        if local.routing == "direct":
            return local
        try:
            score = self._claude_assess(request)
            if score is not None:
                return self._prefer_direct_for_simple(request, score)
        except Exception:
            pass
        return local

    def _local_assess(self, request: str) -> ComplexityScore:
        text = request.lower().strip()
        words = re.findall(r"\w+", text)
        steps = min(10, max(1, len(re.findall(r"[,;；，。\n]| and | then |同时|然后|并且", request)) + 1))
        simple_verbs = ["explain", "summarize", "read", "show", "list", "ping", "检查", "查看", "解释", "总结", "列出"]
        write_verbs = ["implement", "add", "fix", "update", "refactor", "delete", "实现", "增加", "新增", "修复", "更新", "重构", "删除"]
        broad_words = ["architecture", "framework", "engine", "协同", "架构", "框架"]
        coupling_words = ["集成", "dashboard", "sqlite", "worker", "memory", "面板", "planner", "scheduler"]
        domain = 7 if any(k in text for k in broad_words) else 3
        ambiguity = 7 if any(k in text for k in ["重新梳理", "整个", "自主", "self", "evolution", "复杂"]) else 2
        coupling = 7 if any(k in text for k in coupling_words) else 2
        risk = 6 if any(k in text for k in ["数据库", "sqlite", "持久化", "监控", "并行"]) else 3
        if any(k in text for k in write_verbs):
            risk = max(risk, 5)
        if len(words) <= 18 and steps <= 2 and not any(k in text for k in write_verbs + broad_words + coupling_words):
            domain = min(domain, 2)
            risk = min(risk, 2)
        complex_combo = (any(k in text for k in broad_words) or len(words) >= 4) and any(k in text for k in coupling_words) and risk >= 6
        if complex_combo:
            domain = max(domain, 8)
            ambiguity = max(ambiguity, 7)
            coupling = max(coupling, 8)
        overall = max(1, min(10, round((domain + steps + ambiguity + coupling + risk) / 5)))
        if complex_combo:
            overall = max(overall, 7)
        if overall <= 3:
            routing = "direct"
        elif overall <= 6:
            routing = "single"
        else:
            routing = "wbs"
        return ComplexityScore(domain, steps, ambiguity, coupling, risk, overall, routing)

    def _prefer_direct_for_simple(self, request: str, score: ComplexityScore) -> ComplexityScore:
        local = self._local_assess(request)
        if local.routing == "direct":
            return local
        return score

    def _heuristic_assess(self, request: str) -> ComplexityScore:
        return self._local_assess(request)

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

    def _node_fingerprint(self, node: WBSNode) -> str:
        words = re.findall(r"[\w/.-]+", " ".join([
            node.title,
            node.description,
            node.capability,
        ]).lower())
        stop_words = {
            "the", "and", "for", "with", "from", "that", "this", "task", "node", "phase",
            "implementation", "analysis", "planning", "verification", "实现", "分析", "规划", "验证",
        }
        normalized = " ".join(word for word in words if len(word) > 2 and word not in stop_words)
        if not normalized:
            normalized = f"{node.capability}:{node.title.lower()}"
        return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]

    def _deduplicate_nodes(self, nodes: list[WBSNode]) -> list[WBSNode]:
        unique: list[WBSNode] = []
        by_fingerprint: dict[str, WBSNode] = {}
        duplicate_to_keeper: dict[str, str] = {}
        for node in nodes:
            node.fingerprint = node.fingerprint or self._node_fingerprint(node)
            keeper = by_fingerprint.get(node.fingerprint)
            if keeper is None:
                by_fingerprint[node.fingerprint] = node
                unique.append(node)
                continue
            duplicate_to_keeper[node.id] = keeper.id
            for dependency in node.dependencies:
                if dependency != keeper.id and dependency not in keeper.dependencies:
                    keeper.dependencies.append(dependency)
            for target in node.write_targets:
                if target not in keeper.write_targets:
                    keeper.write_targets.append(target)
            if node.brief and node.brief not in keeper.brief:
                keeper.brief = f"{keeper.brief}\n\nDuplicate merged from {node.id}: {node.brief}" if keeper.brief else node.brief
        if not duplicate_to_keeper:
            return unique
        kept_ids = {node.id for node in unique}
        for node in unique:
            rewritten = [duplicate_to_keeper.get(dep, dep) for dep in node.dependencies]
            node.dependencies = [dep for dep in dict.fromkeys(rewritten) if dep != node.id and dep in kept_ids]
        return unique

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

    def decompose(self, request: str, max_nodes: int = 8, capabilities: list[str] | None = None) -> Plan:
        lessons_block = self._load_recent_lessons()
        score = self._local_assess(request)
        if score.routing == "single":
            return self.fallback_wbs(request, score=score)
        if capabilities:
            caps_list = ", ".join(capabilities)
            caps_block = (
                f"Worker agent native capabilities: [{caps_list}]\n"
                "Mapping rule: The agent already provides native capabilities. "
                "From the available skills/tools lists below, select the MOST RELEVANT ones for each node. "
                "Prefer custom/user-registered skills over built-in ones when both cover similar ground. "
                "Always assign at least one skill per implementation node. "
                "Leave skills_json/tools_json as \"\" only for trivial read-only nodes.\n\n"
            )
        else:
            caps_block = ""
        # Build available skills/tools list blocks from registry
        registry = get_unified_registry()
        available_skills_block = ""
        available_tools_block = ""
        skill_entries = registry.list_by_type(SkillEntry)
        tool_entries = registry.list_by_type(ToolEntry)
        mcp_entries = registry.list_by_type(MCPEntry)
        if skill_entries:
            lines = ["Available skills:"]
            for s in skill_entries:
                caps = ", ".join(s.capabilities) if s.capabilities else "general"
                lines.append(f"- {s.name}: {s.description} [capabilities: {caps}]")
            available_skills_block = "\n".join(lines) + "\n\n"
        if tool_entries or mcp_entries:
            lines = ["Available tools/MCP:"]
            for t in tool_entries:
                tools = ", ".join(t.allowed_tools) if t.allowed_tools else "none"
                lines.append(f"- {t.name}: {t.description} [allowed: {tools}]")
            for m in mcp_entries:
                tools = ", ".join(m.allowed_tools) if m.allowed_tools else "none"
                lines.append(f"- {m.name}: {m.description} [allowed: {tools}]")
            available_tools_block = "\n".join(lines) + "\n\n"
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
      "estimated_duration": 300,
      "write_targets": ["relative/file/or/directory"],
      "skills_json": "",
      "tools_json": ""
    }}
  ]
}}

{caps_block}{available_skills_block}{available_tools_block}skills_json and tools_json are optional. From the lists above, select the skills and tools that fit each node and set them as a JSON array string (e.g. "[\\"test-verify\\",\\"file-edit\\"]"). Leave "" to let the engine auto-select.
Create 4-{max_nodes} WBS nodes. complexity must be 1-10. estimated_duration is seconds and should be a positive integer.
Set write_targets to repository-relative files or directories each implementation node may write; use [] for read-only nodes.
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
                    write_targets=[str(target).strip().strip("/") for target in item.get("write_targets", []) if str(target).strip()],
                    fingerprint=str(item.get("fingerprint") or ""),
                    skills_json=str(item.get("skills_json") or ""),
                    tools_json=str(item.get("tools_json") or ""),
                ))
            if nodes:
                shared_brief = str(data.get("shared_brief") or "") if isinstance(data, dict) else ""
                nodes = self._deduplicate_nodes(nodes)
                return Plan(nodes=self._assign_checkpoints(nodes, self.store), shared_brief=shared_brief)
        except Exception:
            pass
        return self.fallback_wbs(request)

    def fallback_wbs(self, request: str, score: ComplexityScore | None = None) -> Plan:
        score = score or ComplexityScore(5, 5, 5, 5, 5, 7, "wbs")
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
                write_targets=[],
            ),
        ]
        if score.routing == "direct":
            nodes = [WBSNode(
                "wbs-1",
                "Direct execution",
                request,
                "general",
                score.overall,
                [],
                True,
                "Direct answer",
                estimated_duration=300,
                write_targets=[],
            )]
            return Plan(nodes=nodes, shared_brief="Simple request routed directly without decomposition.")
        if short or score.routing == "single":
            nodes.append(WBSNode(
                "wbs-2",
                "Implement solution",
                _desc("Plan and implement the solution end-to-end"),
                "implementation",
                max(4, score.overall),
                ["wbs-1"],
                False,
                "Implemented solution",
                brief="Use the analysis output to make the smallest safe code or documentation change that satisfies the request.",
                estimated_duration=900,
                write_targets=["."],
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
                write_targets=[],
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
                write_targets=["."],
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
            write_targets=[],
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
        for node in nodes:
            node.fingerprint = node.fingerprint or self._node_fingerprint(node)
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
