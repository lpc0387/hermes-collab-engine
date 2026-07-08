"""
8765 Hermes Collab Engine v7.0 — Agent Capability Profiles

Defines capability scores for each agent backend and provides
the routing function select_best_agent() for capability-based dispatch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class CapabilityProfile:
    """Capability scores (1-10) for a single agent."""
    display_name: str = ""
    capabilities: dict[str, int] = field(default_factory=dict)
    max_concurrency: int = 3
    cost_weight: float = 1.0
    is_aggregator: bool = False
    default_fallback: bool = False


# ── V4 Capability Profiles ──────────────────────────────────────
# Each agent declares what it's good at (1-10 scale).
# These are used by select_best_agent() for capability routing.

CAPABILITY_PROFILES: dict[str, CapabilityProfile] = {
    "opencode": CapabilityProfile(
        display_name="OpenCode",
        capabilities={
            "general-coding": 8,
            "file-edit": 7,
            "implementation": 7,
            "general": 7,
            "git-ops": 8,
            "test-run": 7,
            "execution": 9,
            "debug": 6,
        },
        max_concurrency=3,
        cost_weight=1.0,
        default_fallback=True,
    ),
    "claude-code": CapabilityProfile(
        display_name="Claude Code",
        capabilities={
            "file-edit": 9,
            "implementation": 9,
            "general": 8,
            "mcp-host": 9,
            "search": 8,
            "reasoning": 8,
            "design": 7,
            "architecture": 6,
            "refactor": 8,
        },
        max_concurrency=2,
        cost_weight=1.5,
    ),
    "codex": CapabilityProfile(
        display_name="Codex CLI",
        capabilities={
            "file-edit": 7,
            "implementation": 7,
            "general": 7,
            "git-ops": 7,
            "execution": 8,
            "scaffold": 8,
            "prototype": 7,
        },
        max_concurrency=2,
        cost_weight=1.2,
    ),
    "hermes": CapabilityProfile(
        display_name="Hermes Agent",
        capabilities={
            "planning": 9,
            "analysis": 9,
            "orchestration": 9,
            "delegation": 9,
            "research": 7,
            "review": 8,
            "evaluation": 9,
            "verification": 8,
        },
        max_concurrency=1,
        cost_weight=0.8,
        is_aggregator=True,
    ),
    "mimocode": CapabilityProfile(
        display_name="Mimo Code",
        capabilities={
            "general-coding": 6,
            "file-edit": 6,
            "execution": 6,
        },
        max_concurrency=2,
        cost_weight=1.0,
    ),
}


# ── Capability inference rules ──────────────────────────────────
# Maps planner node keywords to capability types.

CAPABILITY_KEYWORDS: dict[str, str] = {
    # Analysis / planning
    "analyze": "analysis",
    "scope": "analysis",
    "research": "analysis",
    "investigate": "research",
    "plan": "planning",
    "coordinate": "orchestration",
    "orchestrate": "orchestration",

    # Implementation
    "implement": "file-edit",
    "add": "file-edit",
    "fix": "file-edit",
    "update": "file-edit",
    "refactor": "refactor",
    "restructure": "refactor",

    # Scaffold / project init
    "scaffold": "scaffold",
    "init": "scaffold",
    "setup": "scaffold",
    "template": "scaffold",

    # Testing
    "test": "test-run",
    "verify": "test-run",
    "validate": "test-run",
    "check": "test-run",

    # Design
    "design": "design",
    "architecture": "architecture",

    # Search / investigation
    "search": "search",
    "find": "search",

    # Review / evaluation
    "review": "review",
    "audit": "review",
    "evaluate": "evaluation",
    "assess": "evaluation",
}


def infer_capability(node_title: str, node_description: str = "") -> str:
    """Infer capability type from a WBS node's title and description.

    Uses keyword matching on the first word/verb of the node title.
    Falls back to 'general-coding' if no match.
    """
    text = (node_title + " " + node_description).lower()
    for keyword, cap in CAPABILITY_KEYWORDS.items():
        if keyword in text:
            return cap
    return "general-coding"


def select_best_agent(
    capability: str,
    available_agents: list[str],
    exclude: set[str] | None = None,
    load_counts: dict[str, int] | None = None,
) -> str | None:
    """Select the best agent for a given capability from available agents.

    Args:
        capability: Required capability type (e.g. 'file-edit', 'test-run')
        available_agents: List of agent names to choose from
        exclude: Agent names to exclude (e.g. already failed agents)
        load_counts: Current load per agent for load-aware routing

    Returns:
        Best agent name, or first available agent if no match, or None if none available.
    """
    if not available_agents:
        return None

    exclude_set = set(exclude or [])
    loads = load_counts or {}

    best_agent: str | None = None
    best_score = -1

    for name in available_agents:
        if name in exclude_set:
            continue

        profile = CAPABILITY_PROFILES.get(name)
        if profile is None:
            continue

        # Base score from capability match
        score = profile.capabilities.get(capability, 0)

        # Load penalty: each concurrently running task reduces score
        load_penalty = loads.get(name, 0) * 1.0
        effective_score = score - load_penalty

        # Prefer agents with higher max_concurrency when loads are equal
        if effective_score > best_score:
            best_score = effective_score
            best_agent = name
        elif effective_score == best_score and best_agent is not None:
            # Tie-break: prefer higher max_concurrency
            curr_profile = CAPABILITY_PROFILES.get(best_agent)
            if profile and curr_profile:
                if profile.max_concurrency > curr_profile.max_concurrency:
                    best_agent = name

    # Fallback: first available agent not excluded
    if best_agent is None:
        for name in available_agents:
            if name not in exclude_set:
                best_agent = name
                break

    return best_agent


def get_aggregator(available_agents: list[str] | None = None) -> str | None:
    """Find the default aggregator agent (is_aggregator=True)."""
    for name, profile in CAPABILITY_PROFILES.items():
        if profile.is_aggregator:
            if available_agents is None or name in available_agents:
                return name
    return None
