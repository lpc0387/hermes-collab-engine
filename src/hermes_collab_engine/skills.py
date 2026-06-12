"""Skill registry and worker prompt selection.

Skills are short markdown instruction blocks the leader can attach to workers
based on WBS node capability and task wording.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass
class SkillEntry:
    name: str
    display_name: str
    category: str
    description: str
    content: str
    applicable_node_types: list[str]
    priority: int
    source: str

    def to_dict(self) -> dict:
        return asdict(self)


class SkillRegistry:
    """In-memory registry for built-in and custom worker skills."""

    def __init__(self):
        self._skills: dict[str, SkillEntry] = {}
        self._load_builtin_skills()

    def register(self, skill: SkillEntry) -> None:
        if not skill.name:
            raise ValueError("skill name is required")
        self._skills[skill.name] = skill

    def get(self, name: str) -> SkillEntry | None:
        return self._skills.get(name)

    def list_all(self) -> list[SkillEntry]:
        return sorted(self._skills.values(), key=lambda s: (s.priority, s.name))

    def select_for_node(
        self,
        node_type: str,
        task_text: str = "",
        *,
        max_skills: int = 3,
    ) -> list[SkillEntry]:
        """Select relevant skills for a node capability and task text."""
        normalized_type = (node_type or "").strip().lower()
        text = (task_text or "").lower()
        candidates = [
            skill for skill in self._skills.values()
            if normalized_type in [item.lower() for item in skill.applicable_node_types]
            or "*" in skill.applicable_node_types
        ]
        scored = [(self._score(skill, normalized_type, text), skill) for skill in candidates]
        selected = [skill for score, skill in scored if score > 0]
        selected.sort(key=lambda s: (s.priority, s.name))
        return selected[:max(0, max_skills)]

    def render_for_prompt(self, skills: list[SkillEntry]) -> str:
        if not skills:
            return ""
        parts = ["Relevant skills injected by Hermes:"]
        for skill in skills:
            parts.append(f"\n### {skill.display_name} ({skill.name})\n{skill.content.strip()}")
        return "\n".join(parts) + "\n\n"

    def _score(self, skill: SkillEntry, node_type: str, text: str) -> int:
        score = 4 - max(1, min(3, skill.priority))
        haystack = f"{skill.name} {skill.display_name} {skill.category} {skill.description}".lower()
        for word in _TASK_KEYWORDS.get(skill.name, ()):  # skill-specific task hints
            if word in text:
                score += 2
        if skill.category in text or any(token in text for token in haystack.split() if len(token) > 5):
            score += 1
        if node_type in [item.lower() for item in skill.applicable_node_types]:
            score += 1
        return score

    def _load_builtin_skills(self) -> None:
        for skill in _BUILTIN_SKILLS:
            self.register(skill)


_TASK_KEYWORDS: dict[str, tuple[str, ...]] = {
    "implementation-focus": ("implement", "modify", "write", "code", "working implementation"),
    "test-verify": ("test", "verify", "unittest", "pytest", "regression"),
    "search-verify": ("find", "search", "scope", "evidence", "read-only", "locate"),
    "debug-root-cause": ("bug", "debug", "failure", "traceback", "error", "fix"),
    "risk-checkpoint": ("risk", "checkpoint", "security", "permission", "destructive"),
}


_BUILTIN_SKILLS = [
    SkillEntry(
        name="implementation-focus",
        display_name="Focused Implementation",
        category="coding",
        description="Keep implementation shards concrete, minimal, and file-level.",
        content=(
            "- Make the smallest useful code change that satisfies this node.\n"
            "- Match surrounding naming, comments, and style.\n"
            "- Report exact files modified and avoid claiming unrun verification."
        ),
        applicable_node_types=["implementation", "coding"],
        priority=1,
        source="hermes",
    ),
    SkillEntry(
        name="test-verify",
        display_name="Test & Verification",
        category="verification",
        description="Run targeted checks and report failures honestly.",
        content=(
            "- Prefer the narrowest regression test that proves this node.\n"
            "- If a command fails, include the failure reason in verification.\n"
            "- Do not mark partial work as complete when tests are failing."
        ),
        applicable_node_types=["implementation", "verification", "debugging"],
        priority=1,
        source="hermes",
    ),
    SkillEntry(
        name="search-verify",
        display_name="Search & Evidence",
        category="research",
        description="Locate existing patterns before changing behavior.",
        content=(
            "- Identify the smallest relevant entrypoints before editing.\n"
            "- Reuse existing abstractions instead of creating parallel systems.\n"
            "- Preserve useful file and symbol evidence for downstream workers."
        ),
        applicable_node_types=["analysis", "research", "planning"],
        priority=1,
        source="hermes",
    ),
    SkillEntry(
        name="debug-root-cause",
        display_name="Debug Root Cause",
        category="debugging",
        description="Trace failures to a concrete cause before fixing.",
        content=(
            "- Reproduce or inspect the failing path before changing code.\n"
            "- Fix the cause rather than adding broad fallback behavior.\n"
            "- Add or update a regression check when practical."
        ),
        applicable_node_types=["debugging", "implementation"],
        priority=2,
        source="hermes",
    ),
    SkillEntry(
        name="risk-checkpoint",
        display_name="Risk Checkpoint",
        category="planning",
        description="Call out high-risk or irreversible actions before proceeding.",
        content=(
            "- Avoid destructive, outward-facing, or credential-affecting actions unless explicitly authorized.\n"
            "- Surface blockers and risky assumptions in the result JSON notes.\n"
            "- Keep verification local unless the task asks for external effects."
        ),
        applicable_node_types=["implementation", "planning", "verification"],
        priority=3,
        source="hermes",
    ),
]


_DEFAULT_REGISTRY = SkillRegistry()


def get_default_registry() -> SkillRegistry:
    return _DEFAULT_REGISTRY
