"""Unified registry for skills, tools, and MCP integrations.

Provides a shared base class for all registry entries and a UnifiedRegistry
that indexes entries by capability tags.  The planner selects bundles by
capability, and WBS nodes pre-bind bundles so workers don't do runtime
tool-discovery.

MCP discovery reads from a JSON config file or ``HERMES_MCP_CONFIG``
environment variable.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Base entry
# ---------------------------------------------------------------------------

@dataclass
class RegistryEntry:
    """Common fields shared by skill, tool, and MCP entries."""

    name: str
    display_name: str
    category: str
    description: str
    capabilities: list[str]  # capability tags, e.g. ["implementation", "coding"]
    source: str              # "hermes" | "mcp" | "user"
    priority: int = 1        # lower = higher priority

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Specialized entries
# ---------------------------------------------------------------------------

@dataclass
class SkillEntry(RegistryEntry):
    """A markdown instruction block attached to workers."""

    content: str = ""
    file_path: str = ""  # disk path of the skill file
    required_tools: list[str] = field(default_factory=list)  # tool profiles needed (used by SkillDistributor)

@dataclass
class ToolEntry(RegistryEntry):
    """A tool profile describing allowed tools for workers."""

    allowed_tools: list[str] = field(default_factory=list)

@dataclass
class MCPEntry(RegistryEntry):
    """An MCP (Model Context Protocol) tool integration."""

    server_name: str = ""
    tool_name: str = ""
    endpoint: str = ""
    allowed_tools: list[str] = field(default_factory=list)
    config_path: str = ""  # disk path of the MCP config file

    @property
    def qualified_name(self) -> str:
        """Fully-qualified MCP tool name, e.g. ``mcp__filesystem__read_file``."""
        if self.server_name and self.tool_name:
            return f"mcp__{self.server_name}__{self.tool_name}"
        return self.name

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        data["qualified_name"] = self.qualified_name
        return data


# ---------------------------------------------------------------------------
# Unified registry
# ---------------------------------------------------------------------------


class UnifiedRegistry:
    """Capability-indexed registry for skills, tools, and MCP entries.

    Entries are indexed by their ``capabilities`` tags so the planner can
    select a bundle by node capability without scanning all entries.
    """

    def __init__(self) -> None:
        self._entries: dict[str, RegistryEntry] = {}
        self._capability_index: dict[str, list[str]] = {}  # cap -> [entry names]

    def _index_entry(self, entry: RegistryEntry) -> None:
        """Add an entry to the in-memory index (no persistence)."""
        self._entries[entry.name] = entry
        for cap in entry.capabilities:
            normalized = cap.strip().lower()
            bucket = self._capability_index.setdefault(normalized, [])
            if entry.name not in bucket:
                bucket.append(entry.name)

    # -- registration -------------------------------------------------------

    def register(self, entry: RegistryEntry) -> None:
        if not entry.name:
            raise ValueError("entry name is required")
        self._entries[entry.name] = entry
        for cap in entry.capabilities:
            normalized = cap.strip().lower()
            bucket = self._capability_index.setdefault(normalized, [])
            if entry.name not in bucket:
                bucket.append(entry.name)

    def get(self, name: str) -> RegistryEntry | None:
        return self._entries.get(name)

    def delete(self, name: str) -> bool:
        """Remove an entry by name. Returns True if deleted."""
        entry = self._entries.pop(name, None)
        if entry is None:
            return False
        for cap in entry.capabilities:
            bucket = self._capability_index.get(cap.strip().lower())
            if bucket and name in bucket:
                bucket.remove(name)
        return True

    def list_all(self) -> list[RegistryEntry]:
        return sorted(self._entries.values(), key=lambda e: (e.priority, e.name))

    def list_by_type(self, entry_type: type) -> list[RegistryEntry]:
        return sorted(
            [e for e in self._entries.values() if isinstance(e, entry_type)],
            key=lambda e: (e.priority, e.name),
        )

    # -- capability lookup --------------------------------------------------

    def select_for_capability(
        self,
        capability: str,
        *,
        entry_type: type | None = None,
        max_entries: int = 8,
    ) -> list[RegistryEntry]:
        """Select entries whose capability tags include *capability*.

        If *entry_type* is given, only return entries of that type.
        """
        normalized = (capability or "").strip().lower()
        if not normalized:
            return []
        # Wildcard-capable entries always match
        names = set(self._capability_index.get(normalized, []))
        names.update(self._capability_index.get("*", []))
        results = [self._entries[n] for n in names if n in self._entries]
        if entry_type is not None:
            results = [e for e in results if isinstance(e, entry_type)]
        results.sort(key=lambda e: (e.priority, e.name))
        return results[:max_entries]

    def select_skills(self, capability: str, task_text: str = "", *, max_skills: int = 3) -> list[SkillEntry]:
        """Select skills for a capability, with keyword scoring."""
        candidates = self.select_for_capability(capability, entry_type=SkillEntry, max_entries=max_skills * 2)
        if not task_text:
            return candidates[:max_skills]
        scored = [(self._score_entry(e, task_text), e) for e in candidates]
        scored.sort(key=lambda t: (-t[0], t[1].priority, t[1].name))
        return [e for score, e in scored if score > 0][:max_skills]

    def select_tools(self, capability: str, task_text: str = "", *, max_tools: int = 4) -> list[ToolEntry]:
        """Select tool entries for a capability, with keyword scoring."""
        candidates = self.select_for_capability(capability, entry_type=ToolEntry, max_entries=max_tools * 2)
        if not task_text:
            return candidates[:max_tools]
        scored = [(self._score_entry(e, task_text), e) for e in candidates]
        scored.sort(key=lambda t: (-t[0], t[1].priority, t[1].name))
        return [e for score, e in scored if score > 0][:max_tools]

    def select_mcp(self, capability: str, *, max_entries: int = 4) -> list[MCPEntry]:
        """Select MCP entries for a capability."""
        return self.select_for_capability(capability, entry_type=MCPEntry, max_entries=max_entries)

    # -- MCP server management -----------------------------------------------

    DEFAULT_MCP_CAPABILITIES = ["*"]

    def register_mcp_server(
        self,
        server_name: str,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        tools: list[str] | None = None,
        description: str = "",
        display_name: str | None = None,
        capabilities: list[str] | None = None,
        source: str = "web-ui",
    ) -> list[MCPEntry]:
        """Register an MCP server and all its tools as individual MCPEntries.

        Creates one MCPEntry per tool. If *tools* is empty/None, creates a
        single entry representing the server itself so it appears in listings.

        Returns the list of created entries.
        """
        if not server_name:
            raise ValueError("server_name is required")
        tools_list = tools or []
        caps = capabilities or list(self.DEFAULT_MCP_CAPABILITIES)
        display = display_name or f"MCP Server: {server_name}"
        endpoint_parts = [command]
        if args:
            endpoint_parts.extend(args)
        endpoint = " ".join(str(p) for p in endpoint_parts).strip()

        created: list[MCPEntry] = []
        # If no tools specified, create a single server-level entry
        if not tools_list:
            entry = MCPEntry(
                name=f"mcp__{server_name}__server",
                display_name=display,
                category="mcp",
                description=description or f"MCP server {server_name}",
                capabilities=caps,
                source=source,
                priority=2,
                server_name=server_name,
                tool_name="server",
                endpoint=endpoint,
                allowed_tools=[f"mcp__{server_name}__server"],
                config_path="",
            )
            self.register(entry)
            created.append(entry)
        else:
            for tool_name in tools_list:
                entry = MCPEntry(
                    name=f"mcp__{server_name}__{tool_name}",
                    display_name=f"{display}/{tool_name}",
                    category="mcp",
                    description=description or f"MCP tool {tool_name} from server {server_name}",
                    capabilities=caps,
                    source=source,
                    priority=2,
                    server_name=server_name,
                    tool_name=tool_name,
                    endpoint=endpoint,
                    allowed_tools=[f"mcp__{server_name}__{tool_name}"],
                    config_path="",
                )
                self.register(entry)
                created.append(entry)
        return created

    def list_mcp_servers(self) -> list[dict]:
        """List all MCP servers grouped by server name.

        Returns a list of dicts, each representing an MCP server with its
        tools listed under it.
        """
        mcp_entries = self.list_by_type(MCPEntry)
        servers: dict[str, dict] = {}
        for entry in mcp_entries:
            srv_name = entry.server_name or "unknown"
            if srv_name not in servers:
                # Derive display_name from the first entry's display_name
                first_display = entry.display_name
                if entry.tool_name and f"/{entry.tool_name}" in first_display:
                    first_display = first_display.rsplit("/", 1)[0].strip()
                servers[srv_name] = {
                    "server_name": srv_name,
                    "display_name": first_display,
                    "endpoint": entry.endpoint,
                    "tools": [],
                    "entry_count": 0,
                    "source": entry.source,
                }
            servers[srv_name]["tools"].append({
                "tool_name": entry.tool_name,
                "qualified_name": entry.qualified_name,
                "description": entry.description,
                "capabilities": entry.capabilities,
            })
            servers[srv_name]["entry_count"] += 1
            # Update endpoint if we find a non-empty one
            if entry.endpoint:
                servers[srv_name]["endpoint"] = entry.endpoint
        return sorted(servers.values(), key=lambda s: s["server_name"])

    def remove_mcp_server(self, server_name: str) -> int:
        """Remove all MCP entries for a given server name.

        Returns the number of entries removed.
        """
        to_remove = [
            e.name for e in self._entries.values()
            if isinstance(e, MCPEntry) and e.server_name == server_name
        ]
        if not to_remove:
            return 0
        for name in to_remove:
            self.delete(name)
        return len(to_remove)

    def allowed_tools_for_capability(self, capability: str) -> list[str]:
        """Merge allowed_tools from all tool and MCP entries for a capability."""
        seen: set[str] = set()
        result: list[str] = []
        for entry in self.select_for_capability(capability):
            tools = getattr(entry, "allowed_tools", None) or []
            for t in tools:
                if t not in seen:
                    seen.add(t)
                    result.append(t)
        return result

    # -- rendering ----------------------------------------------------------

    def render_skills_for_prompt(self, skills: list[SkillEntry]) -> str:
        if not skills:
            return ""
        parts = ["Relevant skills injected by Hermes:"]
        for skill in skills:
            path_hint = f"\nFile: {skill.file_path}" if skill.file_path else ""
            parts.append(f"\n### {skill.display_name} ({skill.name}){path_hint}\n{skill.content.strip()}")
        return "\n".join(parts) + "\n\n"

    def render_tools_for_prompt(self, tools: list[ToolEntry | MCPEntry]) -> str:
        if not tools:
            return ""
        parts = ["Tool profiles selected by Hermes:"]
        for entry in tools:
            tool_list = ", ".join(entry.allowed_tools) if hasattr(entry, "allowed_tools") else ""
            config_hint = f"\nConfig: {entry.config_path}" if getattr(entry, "config_path", "") else ""
            parts.append(
                f"\n### {entry.display_name} ({entry.name})\n"
                f"{entry.description}\n"
                f"Allowed tools: {tool_list}{config_hint}"
            )
        return "\n".join(parts) + "\n\n"

    # -- scoring helper -----------------------------------------------------

    @staticmethod
    def _score_entry(entry: RegistryEntry, task_text: str) -> int:
        score = 4 - max(1, min(3, entry.priority))
        text = task_text.lower()
        haystack = f"{entry.name} {entry.display_name} {entry.category} {entry.description}".lower()
        for token in haystack.split():
            if len(token) > 5 and token in text:
                score += 1
        if entry.category in text:
            score += 1
        return score

    # -- scoring helper -----------------------------------------------------

# ---------------------------------------------------------------------------
# MCP discovery
# ---------------------------------------------------------------------------

def discover_mcp_entries(config_path: str | None = None) -> list[MCPEntry]:
    """Load MCP tool entries from a JSON config file.

    Config format::

        {
          "mcpServers": {
            "filesystem": {
              "command": "npx",
              "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
              "tools": ["read_file", "list_directory", "write_file"]
            }
          }
        }

    Also checks ``HERMES_MCP_CONFIG`` env var if *config_path* is not given.
    """
    path = config_path or os.environ.get("HERMES_MCP_CONFIG")
    if not path:
        return []
    try:
        with open(path) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, PermissionError):
        return []

    entries: list[MCPEntry] = []
    servers = data.get("mcpServers", {})
    for server_name, server_cfg in servers.items():
        tools = server_cfg.get("tools", [])
        endpoint = ""
        cmd = server_cfg.get("command", "")
        args = server_cfg.get("args", [])
        if cmd:
            endpoint = f"{cmd} {' '.join(args)}".strip()
        for tool_name in tools:
            entry = MCPEntry(
                name=f"mcp__{server_name}__{tool_name}",
                display_name=f"MCP: {server_name}/{tool_name}",
                category="mcp",
                description=f"MCP tool {tool_name} from server {server_name}",
                capabilities=["*"],
                source="mcp",
                priority=2,
                server_name=server_name,
                tool_name=tool_name,
                endpoint=endpoint,
                allowed_tools=[f"mcp__{server_name}__{tool_name}"],
                config_path=path,
            )
            entries.append(entry)
    return entries


# ---------------------------------------------------------------------------
# Default singleton
# ---------------------------------------------------------------------------

_DEFAULT_REGISTRY: UnifiedRegistry | None = None


def get_unified_registry() -> UnifiedRegistry:
    """Return the default unified registry, building it lazily."""
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = UnifiedRegistry()
        # Register built-in skills
        _DEFAULT_REGISTRY.register(SkillEntry(
            name="ui-design-v2", display_name="UI Design v2 — shadcn/ui", category="design",
            description="Advanced UI design skill using shadcn/ui v4 components with Linear/Stripe/Vercel aesthetic.",
            content="(content truncated for display)",
            capabilities=["implementation", "design", "design-v2", "frontend", "ui", "coding"],
            priority=1, source="hermes", required_tools=["file-edit", "mcp-readonly"],
        ))
        _DEFAULT_REGISTRY.register(SkillEntry(
            name="implementation-focus", display_name="Focused Implementation", category="coding",
            description="Keep implementation shards concrete, minimal, and file-level.",
            content=(
                "- Make the smallest useful code change that satisfies this node.\n"
                "- Match surrounding naming, comments, and style.\n"
                "- Report exact files modified and avoid claiming unrun verification."
            ),
            capabilities=["implementation", "coding", "docs", "general"],
            priority=1, source="hermes",
        ))
        _DEFAULT_REGISTRY.register(SkillEntry(
            name="test-verify", display_name="Test & Verification", category="verification",
            description="Run targeted checks and report failures honestly.",
            content=(
                "- Prefer the narrowest regression test that proves this node.\n"
                "- If a command fails, include the failure reason in verification.\n"
                "- Do not mark partial work as complete when tests are failing."
            ),
            capabilities=["implementation", "verification", "debugging"],
            priority=1, source="hermes",
        ))
        _DEFAULT_REGISTRY.register(SkillEntry(
            name="search-verify", display_name="Multi-Source Search & Verification", category="research",
            description="Multi-source search verification.",
            content=("- Search multiple sources in parallel.\n- Cross-validate findings.\n- Produce a verification report with confidence levels."),
            capabilities=["analysis", "research", "planning", "scope", "evidence"],
            priority=1, source="hermes",
        ))
        _DEFAULT_REGISTRY.register(SkillEntry(
            name="debug-root-cause", display_name="Debug Root Cause", category="debugging",
            description="Trace failures to a concrete cause before fixing.",
            content=("- Reproduce or inspect the failing path before changing code.\n- Fix the cause rather than adding broad fallback behavior.\n- Add or update a regression check when practical."),
            capabilities=["debugging", "implementation"],
            priority=2, source="hermes",
        ))
        _DEFAULT_REGISTRY.register(SkillEntry(
            name="risk-checkpoint", display_name="Risk Checkpoint", category="planning",
            description="Call out high-risk or irreversible actions before proceeding.",
            content=("- Avoid destructive or credential-affecting actions unless explicitly authorized.\n- Surface blockers and risky assumptions."),
            capabilities=["implementation", "planning", "verification"],
            priority=3, source="hermes",
        ))
        _DEFAULT_REGISTRY.register(SkillEntry(
            name="browser-automation", display_name="Browser Automation", category="automation",
            description="Control a headless Chrome browser via GuidedRunner.",
            content="You have a headless Chrome browser available. Use the GuidedRunner for browser automation.",
            capabilities=["implementation", "verification", "debugging"],
            priority=1, source="hermes",
        ))
        _DEFAULT_REGISTRY.register(SkillEntry(
            name="frontend-optimization", display_name="Frontend Optimization & UI Design", category="design",
            description="Build accessible, performant frontends with Tailwind CSS and modern UX patterns.",
            content="(content truncated for display)",
            capabilities=["implementation", "verification", "design", "frontend", "ui"],
            priority=2, source="hermes",
        ))

        # ── Frontend design skills (installed from GitHub, Jul 2026) ──────────
        _DEFAULT_REGISTRY.register(SkillEntry(
            name="design-taste-frontend", display_name="Taste-Skill — Anti-Slop Frontend", category="design",
            description="Anti-slop frontend skill for landing pages, portfolios, and redesigns.",
            content=(
                "## Taste-Skill: Anti-Slop Frontend\n"
                "- Read the brief first, infer design direction before coding.\n"
                "- Set three dials: DESIGN_VARIANCE, MOTION_INTENSITY, VISUAL_DENSITY.\n"
                "- Never default to AI-purple gradients, centered hero, three feature cards, Inter+slate-900.\n"
                "- Use GSAP for animations with official API patterns (gsap.to(), timeline()).\n"
                "- Use Lenis for smooth scrolling integration with GSAP ScrollTrigger."
            ),
            capabilities=["implementation", "design", "creative-design", "frontend", "ui"],
            priority=2, source="hermes",
        ))
        _DEFAULT_REGISTRY.register(SkillEntry(
            name="gsap", display_name="GSAP Animation Library", category="design",
            description="GreenSock Animation Platform — high-performance JavaScript animation library.",
            content=(
                "## GSAP Animation\n"
                "- Use gsap.to(targets, vars) for tween animations from current state.\n"
                "- Use gsap.timeline() for sequenced multi-step animations.\n"
                "- Common eases: power2.out, back.out(1.7), elastic.out(1,0.3).\n"
                "- Always use camelCase CSS properties (backgroundColor, marginTop).\n"
                "- Register plugins: gsap.registerPlugin(ScrollTrigger).\n"
                "- Integrate with Lenis: lenis.on('scroll', ScrollTrigger.update)."
            ),
            capabilities=["implementation", "design", "animation", "frontend"],
            priority=2, source="hermes",
        ))
        _DEFAULT_REGISTRY.register(SkillEntry(
            name="lenis", display_name="Lenis Smooth Scroll", category="design",
            description="Lightweight, performant smooth scroll library with framework adapters.",
            content=(
                "## Lenis Smooth Scroll\n"
                "- Create: new Lenis({ duration: 1.2, easing: (t) => Math.min(1, 1.001 - Math.pow(2, -10 * t)) }).\n"
                "- RAF loop: function raf(time) { lenis.raf(time); requestAnimationFrame(raf); }.\n"
                "- GSAP integration: lenis.on('scroll', ScrollTrigger.update); gsap.ticker.add((t)=>lenis.raf(t*1000)).\n"
                "- Properties: lenis.animatedScroll, lenis.direction, lenis.progress, lenis.velocity.\n"
                "- Methods: lenis.scrollTo(target, {offset, duration, easing})."
            ),
            capabilities=["implementation", "design", "smooth-scroll", "frontend"],
            priority=2, source="hermes",
        ))
        _DEFAULT_REGISTRY.register(SkillEntry(
            name="hallmark", display_name="Hallmark — Anti-AI-Slop Design", category="design",
            description="Anti-AI-slop design skill for greenfield pages, audits, redesigns.",
            content=(
                "## Hallmark: Design That Refuses to Look AI-Generated\n"
                "- Four verbs: default (build), audit (score), redesign (replace structure), study (extract DNA).\n"
                "- Pre-emit self-critique on 6 axes before handing back output.\n"
                "- No fabricated metrics, testimonials, or logos.\n"
                "- 57 slop-test gates including contrast, typography, responsive checks.\n"
                "- Mobile responsiveness verified at 320/375/414/768px."
            ),
            capabilities=["implementation", "design", "anti-slop", "frontend", "ui"],
            priority=2, source="hermes",
        ))
        _DEFAULT_REGISTRY.register(SkillEntry(
            name="impeccable", display_name="Impeccable Design Language", category="design",
            description="Design guidance for AI coding agents — 23 commands, 60 detector rules.",
            content=(
                "## Impeccable: Design Guidance for AI Agents\n"
                "- 23 commands: craft, shape, critique, audit, polish, bolder, quieter, animate, etc.\n"
                "- Three modes: Persuade (landing/marketing), Operate (app UI/dashboards), Read (docs).\n"
                "- Never use Inter for everything, purple-blue gradients, cards-in-cards.\n"
                "- Before editing, load reference/new-work.md or reference/craft-floor.md."
            ),
            capabilities=["implementation", "design", "design-audit", "frontend", "ui"],
            priority=2, source="hermes",
        ))

        # Register built-in tool profiles
        _DEFAULT_REGISTRY.register(ToolEntry(
            name="file-edit", display_name="File Read/Edit", category="filesystem",
            description="Read and edit repository files for implementation work.",
            allowed_tools=["Read", "Edit", "Write", "MultiEdit"],
            capabilities=["implementation", "coding", "debugging", "verification", "analysis", "research", "planning", "docs"],
            priority=1, source="hermes",
        ))
        _DEFAULT_REGISTRY.register(ToolEntry(
            name="git-local", display_name="Local Git Inspection", category="git",
            description="Inspect local repository state without network effects.",
            allowed_tools=["Bash(git diff*)", "Bash(git status*)", "Bash(git ls-files*)"],
            capabilities=["*"],
            priority=1, source="hermes",
        ))
        _DEFAULT_REGISTRY.register(ToolEntry(
            name="python-tests", display_name="Python Test Runner", category="verification",
            description="Run local Python unit tests and syntax checks.",
            allowed_tools=["Bash(python3 -m unittest*)", "Bash(python3 -m py_compile*)", "Bash(bash -n*)"],
            capabilities=["implementation", "verification", "debugging"],
            priority=1, source="hermes",
        ))
        _DEFAULT_REGISTRY.register(ToolEntry(
            name="git-write", display_name="Git Write Operations", category="git",
            description="Clone, stage, commit, or push only when explicitly requested.",
            allowed_tools=["Bash(git clone*)", "Bash(git add*)", "Bash(git commit*)", "Bash(git push*)"],
            capabilities=["implementation"],
            priority=3, source="hermes",
        ))
        _DEFAULT_REGISTRY.register(ToolEntry(
            name="mcp-readonly", display_name="Read-only MCP Tools", category="mcp",
            description="Allow read-only MCP filesystem/search tools.",
            allowed_tools=["mcp__filesystem__read_file", "mcp__filesystem__list_directory", "mcp__search__query"],
            capabilities=["analysis", "research", "planning", "verification"],
            priority=2, source="hermes",
        ))
        _DEFAULT_REGISTRY.register(ToolEntry(
            name="browser-automation", display_name="Browser Automation", category="automation",
            description="Control a headless Chrome browser via intent DSL.",
            allowed_tools=["Bash(python3 -c *GuidedRunner*)"],
            capabilities=["implementation", "verification", "debugging"],
            priority=2, source="hermes",
        ))

        # Merge MCP entries from config
        for mcp_entry in discover_mcp_entries():
            _DEFAULT_REGISTRY.register(mcp_entry)
    return _DEFAULT_REGISTRY
