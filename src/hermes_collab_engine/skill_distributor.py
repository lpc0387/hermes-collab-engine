"""
Skill Distributor — centralized skill/tool/MCP dispatch for engine workers.

Replaces the old approach of injecting ALL 6 skills + ALL 6 tool profiles
into every worker's prompt. Instead, SkillDistributor resolves what a
specific node needs based on its capability + Leader assignment + Agent
backend support, and returns only the relevant skill content + tool
profiles + MCP server names.

The SKILL_TOOL_MAP and SKILL_MCP_MAP are kept in this standalone file so
they can be reused outside this project (e.g. by other engines).
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .agents import AgentBackend

logger = logging.getLogger(__name__)


# ── Skill → built-in tool mapping ───────────────────────────────────────
# Each skill name maps to the ToolProfile names it needs.
# Add new skills or tools here; the engine reads this at runtime.

SKILL_TOOL_MAP: dict[str, list[str]] = {
    "search-verify":            ["file-edit", "git-local", "mcp-readonly"],
    "implementation-focus":     ["file-edit", "git-local", "python-tests"],
    "test-verify":              ["file-edit", "python-tests"],
    "debug-root-cause":         ["file-edit", "git-local", "python-tests"],
    "risk-checkpoint":          [],
    "browser-automation":       ["browser-automation"],
    "frontend-optimization":   ["file-edit", "git-local"],
    "ui-design-v2":            ["file-edit", "mcp-readonly"],
    # Frontend design skills (installed from GitHub, Jul 2026)
    "design-taste-frontend":   ["file-edit", "git-local", "browser-automation"],
    "impeccable":              ["file-edit", "git-local", "browser-automation"],
    "hallmark":                ["file-edit", "git-local"],
    "lenis":                   ["file-edit", "git-local"],
    "gsap":                    ["file-edit", "git-local"],
}


# ── Skill → MCP server mapping ─────────────────────────────────────────
# Each skill may need certain MCP server types.
# The actual MCP server list is fetched from the live UnifiedRegistry;
# this map only declares *types* of MCP servers needed.
# agent_compat restricts which agents can use these MCP servers.

SKILL_MCP_MAP: dict[str, dict] = {
    "search-verify": {
        "mcp_servers": ["ferris-search", "baidu-search", "open-websearch", "filesystem", "search"],
        "agent_compat": ["claude-code", "hermes"],
        "readonly": True,
    },
    "implementation-focus": {
        "mcp_servers": ["filesystem"],
        "agent_compat": ["claude-code", "hermes"],
        "readonly": False,
    },
    "test-verify": {
        "mcp_servers": [],
        "agent_compat": [],
        "readonly": True,
    },
    "debug-root-cause": {
        "mcp_servers": ["filesystem"],
        "agent_compat": ["claude-code", "hermes"],
        "readonly": False,
    },
    "risk-checkpoint": {
        "mcp_servers": [],
        "agent_compat": [],
        "readonly": True,
    },
    "browser-automation": {
        "mcp_servers": ["puppeteer"],
        "agent_compat": ["claude-code", "hermes", "opencode"],
        "readonly": False,
    },
    "frontend-optimization": {
        "mcp_servers": ["daisyui"],
        "agent_compat": ["claude-code", "hermes"],
        "readonly": True,
    },
    "ui-design-v2": {
        "mcp_servers": ["shadcn-ui"],
        "agent_compat": ["claude-code", "hermes"],
        "readonly": True,
    },
    # Frontend design skills (installed from GitHub, Jul 2026)
    "design-taste-frontend": {
        "mcp_servers": ["shadcn-ui"],
        "agent_compat": ["claude-code", "hermes", "opencode"],
        "readonly": True,
    },
    "impeccable": {
        "mcp_servers": [],
        "agent_compat": ["claude-code", "hermes", "opencode"],
        "readonly": True,
    },
    "hallmark": {
        "mcp_servers": ["daisyui"],
        "agent_compat": ["claude-code", "hermes", "opencode"],
        "readonly": True,
    },
    "lenis": {
        "mcp_servers": [],
        "agent_compat": ["claude-code", "hermes", "opencode"],
        "readonly": True,
    },
    "gsap": {
        "mcp_servers": [],
        "agent_compat": ["claude-code", "hermes", "opencode"],
        "readonly": True,
    },
}


# ── Default node capability → skill mapping ────────────────────────────
# When the Leader hasn't explicitly assigned skills, the distributor
# falls back to these defaults based on node capability.

CAPABILITY_DEFAULT_SKILL: dict[str, str] = {
    "scope":            "search-verify",
    "evidence":         "search-verify",
    "analysis":         "search-verify",
    "implementation":   "implementation-focus",
    "coding":           "implementation-focus",
    "verification":     "test-verify",
    "debugging":        "debug-root-cause",
    "planning":         "risk-checkpoint",
    "docs":             "implementation-focus",
    # Fallback for unrecognised capabilities — use the general coding skill
    "general":          "implementation-focus",
    "design":           "frontend-optimization",
    "frontend":         "frontend-optimization",
    "ui":               "frontend-optimization",
    "design-v2":        "ui-design-v2",
    # Frontend design skills (installed from GitHub, Jul 2026)
    "creative-design":  "design-taste-frontend",
    "design-audit":     "impeccable",
    "anti-slop":        "hallmark",
    "animation":        "gsap",
    "smooth-scroll":    "lenis",
}


# ── Map consistency validation ─────────────────────────────────────────


def validate_maps(tool_registry: Any = None, skill_registry: Any = None) -> list[str]:
    """Check consistency between SKILL_TOOL_MAP, SKILL_MCP_MAP, and
    CAPABILITY_DEFAULT_SKILL.

    If *tool_registry* (a ``ToolRegistry`` instance) is provided, also
    validates that every tool profile name referenced in ``SKILL_TOOL_MAP``
    values is actually registered in the tool registry.

    If *skill_registry* (a ``SkillRegistry`` instance) is provided, also
    validates that every skill name referenced in the maps actually exists
    as a registered entry in the skill registry.

    Returns a list of warning messages (empty if all checks pass).
    Call at import time or during test setup to catch drift early.
    """
    warnings: list[str] = []
    all_skill_names = set(SKILL_TOOL_MAP) | set(SKILL_MCP_MAP)

    for name in all_skill_names:
        if name not in SKILL_TOOL_MAP:
            warnings.append(f"SKILL_MCP_MAP has key {name!r} missing from SKILL_TOOL_MAP")
        if name not in SKILL_MCP_MAP:
            warnings.append(f"SKILL_TOOL_MAP has key {name!r} missing from SKILL_MCP_MAP")

    for cap, skill in CAPABILITY_DEFAULT_SKILL.items():
        if skill not in SKILL_TOOL_MAP:
            warnings.append(
                f"CAPABILITY_DEFAULT_SKILL[{cap!r}] = {skill!r} not found in SKILL_TOOL_MAP"
            )
        if skill not in SKILL_MCP_MAP:
            warnings.append(
                f"CAPABILITY_DEFAULT_SKILL[{cap!r}] = {skill!r} not found in SKILL_MCP_MAP"
            )

    # Validate that tool names referenced in SKILL_TOOL_MAP values actually
    # exist in the tool registry (catches typos or renamed profiles).
    if tool_registry is not None:
        known_profiles = set()
        try:
            for p in tool_registry.list_all():
                known_profiles.add(p.name)
        except Exception:
            known_profiles = set()
        for skill_name, tool_names in SKILL_TOOL_MAP.items():
            for t in tool_names:
                if t not in known_profiles:
                    warnings.append(
                        f"SKILL_TOOL_MAP[{skill_name!r}] references unknown tool "
                        f"profile {t!r} — not found in ToolRegistry"
                    )

    # Validate that every skill name referenced in CAPABILITY_DEFAULT_SKILL
    # actually has a registered entry in the SkillRegistry.
    if skill_registry is not None:
        for cap, skill in CAPABILITY_DEFAULT_SKILL.items():
            if skill_registry.get(skill) is None:
                warnings.append(
                    f"CAPABILITY_DEFAULT_SKILL[{cap!r}] = {skill!r} not found in SkillRegistry"
                )
        for name in all_skill_names:
            if skill_registry.get(name) is None:
                warnings.append(
                    f"Skill {name!r} exists in SKILL_TOOL_MAP/SKILL_MCP_MAP "
                    f"but has no registered SkillRegistry entry"
                )

    return warnings


# validate_maps() is available for testing — call it with your live registries
# to verify SKILL_TOOL_MAP references exist.

class SkillDistributor:
    """Resolve skills, tools, and MCP servers for a specific worker node."""

    def __init__(
        self,
        unified_registry: Any = None,
    ):
        self.unified_registry = unified_registry

    @property
    def skill_registry(self):
        return self.unified_registry

    @property
    def tool_registry(self):
        return self.unified_registry

    def resolve_for_node(
        self,
        node_capability: str,
        leader_skills: list[str] | None,
        agent_backend: AgentBackend | None = None,
    ) -> tuple[list[str], list[str]]:
        """Return (skill_names, tool_profile_names) for a worker node.

        Resolution priority:
        1. Leader explicitly assigned skills → filter by Agent support
        2. No leader skills → fall back to capability default
        3. No match → empty (worker runs with no skill/tool injection)

        Agent compatibility check: if agent_backend has a non-empty
        ``supported_skills`` list, only skills in that list are kept.
        Tool profiles are resolved from two sources:
        - The static ``SKILL_TOOL_MAP`` (primary)
        - Each skill's ``required_tools`` field from the SkillRegistry (supplement)
        """
        # Step 1: determine skill names
        if leader_skills is not None:
            skills = leader_skills
        else:
            default = CAPABILITY_DEFAULT_SKILL.get(node_capability)
            if default is None:
                default = CAPABILITY_DEFAULT_SKILL.get("general", "")
                logger.warning(
                    "Unknown capability %r; falling back to 'general' -> %r",
                    node_capability, default,
                )
            skills = [default] if default else []

        # Step 2: filter by Agent backend support
        if agent_backend and agent_backend.supported_skills:
            skills = [s for s in skills if s in agent_backend.supported_skills]

        # Step 3: resolve tool profiles for these skills
        tool_names: list[str] = []
        seen_tools: set[str] = set()
        for s in skills:
            for t in SKILL_TOOL_MAP.get(s, []):
                if t not in seen_tools:
                    seen_tools.add(t)
                    tool_names.append(t)
            if self.skill_registry is not None:
                entry: SkillEntry | None = self.skill_registry.get(s)  # type: ignore[assignment]
                if entry is not None and entry.required_tools:
                    for t in entry.required_tools:
                        if t not in seen_tools:
                            seen_tools.add(t)
                            tool_names.append(t)

        # Step 4: filter tool names by Agent backend tool support
        if agent_backend and agent_backend.supported_tools:
            tool_names = [t for t in tool_names if t in agent_backend.supported_tools]

        return skills, tool_names

    def resolve_mcp(
        self,
        skill_names: list[str],
        agent_name: str,
    ) -> list[dict]:
        """Return MCP server descriptors for the given skills.

        Returns a list of dicts with keys: name, readonly.
        The list is filtered by agent compatibility and current availability.
        """
        needed_map: dict[str, dict] = {}

        for s in skill_names:
            entry = SKILL_MCP_MAP.get(s, {})
            compat = entry.get("agent_compat", [])
            if agent_name not in compat and compat:
                continue  # this MCP type doesn't support this agent

            for mcp_type in entry.get("mcp_servers", []):
                if mcp_type not in needed_map:
                    needed_map[mcp_type] = {
                        "name": mcp_type,
                        "readonly": entry.get("readonly", True),
                    }
                else:
                    # Merge readonly flags: if any skill needs write access,
                    # the server should be read-write (not readonly).
                    if not entry.get("readonly", True):
                        needed_map[mcp_type]["readonly"] = False

        needed = list(needed_map.values())

        # If UnifiedRegistry is available, cross-check against live MCP servers
        if self.unified_registry is not None:
            try:
                from .registry import MCPEntry
                # Match against server_name (e.g. "filesystem"), not the
                # qualified name (e.g. "mcp__filesystem__read_file").
                live_servers = {mcp.server_name for mcp in self.unified_registry.list_by_type(MCPEntry)}
                needed = [m for m in needed if m["name"] in live_servers]
            except Exception:
                pass  # registry unavailable; return as-configured

        return needed

    @staticmethod
    def combine_mcp_configs(*mcp_lists: list[dict]) -> list[dict]:
        """Merge multiple MCP server config lists into one deduplicated list.

        When the same server name appears in multiple lists with different
        ``readonly`` values, the combined entry uses ``readonly=False``
        (read-write), because any skill needing write access must get it.

        This is useful when combining MCP results from two independently
        resolved skill groups (e.g. plan-level + node-level overrides).
        """
        merged: dict[str, dict] = {}
        for mcp_list in mcp_lists:
            for m in mcp_list:
                name = m["name"]
                if name not in merged:
                    merged[name] = dict(m)
                elif not m.get("readonly", True):
                    merged[name]["readonly"] = False
        return list(merged.values())

    def render_for_prompt(
        self,
        skill_names: list[str],
        tool_names: list[str],
        mcp_servers: list[dict],
    ) -> tuple[str, str, str]:
        """Render skills, tools, and MCP blocks for the worker prompt.

        Returns (skills_block, tools_block, mcp_block).
        Each block contains the FULL content (not just names) for
        the resolved skills/tools, so the worker has actionable guidance.
        """
        skills_block = ""
        if skill_names:
            if self.unified_registry is not None:
                skills_list = []
                for name in skill_names:
                    entry = self.unified_registry.get(name)
                    if entry is not None:
                        skills_list.append(entry)
                    else:
                        logger.warning("render_for_prompt: skill %r not found in registry, skipping", name)
                skills_block = self.unified_registry.render_skills_for_prompt(skills_list)
            else:
                logger.warning("skill_registry not set; skills block empty for %s", skill_names)

        tools_block = ""
        if tool_names:
            if self.unified_registry is not None:
                profiles = []
                for name in tool_names:
                    entry = self.unified_registry.get(name)
                    if entry is not None:
                        profiles.append(entry)
                if profiles:
                    tools_block = self.unified_registry.render_tools_for_prompt(profiles)
            else:
                logger.warning("tool_registry not set; tools block empty for %s", tool_names)

        mcp_block = ""
        if mcp_servers:
            parts = ["MCP servers assigned to this worker:"]
            for m in mcp_servers:
                label = "read-only" if m.get("readonly") else "read-write"
                parts.append(f"  - {m['name']} ({label})")
            mcp_block = "\n".join(parts) + "\n\n"

        return skills_block, tools_block, mcp_block


__all__ = [
    "SKILL_TOOL_MAP",
    "SKILL_MCP_MAP",
    "CAPABILITY_DEFAULT_SKILL",
    "SkillDistributor",
    "validate_maps",
]
