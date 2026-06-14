"""Worker tool and MCP profile management.

Tool profiles describe which local or MCP tools may be exposed to a worker for
a node. The engine selects profiles from node capability and task wording, then
passes the merged allow-list to agent backends that support tool restrictions.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class ToolProfile:
    name: str
    display_name: str
    category: str
    description: str
    allowed_tools: list[str]
    applicable_node_types: list[str]
    priority: int
    source: str = "hermes"
    keywords: list[str] = field(default_factory=list)

    @property
    def mcp_tools(self) -> list[str]:
        return [tool for tool in self.allowed_tools if tool.startswith("mcp__")]

    def to_dict(self) -> dict:
        data = asdict(self)
        data["mcp_tools"] = self.mcp_tools
        return data


class ToolRegistry:
    """In-memory registry for worker tool profiles."""

    def __init__(self):
        self._profiles: dict[str, ToolProfile] = {}
        self._load_builtin_profiles()

    def register(self, profile: ToolProfile) -> None:
        if not profile.name:
            raise ValueError("tool profile name is required")
        self._profiles[profile.name] = profile

    def get(self, name: str) -> ToolProfile | None:
        return self._profiles.get(name)

    def list_all(self) -> list[ToolProfile]:
        return sorted(self._profiles.values(), key=lambda p: (p.priority, p.name))

    def select_for_node(
        self,
        node_type: str,
        task_text: str = "",
        *,
        max_profiles: int = 4,
    ) -> list[ToolProfile]:
        normalized_type = (node_type or "").strip().lower()
        text = (task_text or "").lower()
        candidates = [
            profile for profile in self._profiles.values()
            if normalized_type in [item.lower() for item in profile.applicable_node_types]
            or "*" in profile.applicable_node_types
        ]
        scored = [(self._score(profile, normalized_type, text), profile) for profile in candidates]
        selected = [profile for score, profile in scored if score > 0]
        selected.sort(key=lambda p: (p.priority, p.name))
        return selected[:max(0, max_profiles)]

    def allowed_tools_for_profiles(self, profiles: list[ToolProfile]) -> list[str]:
        seen: set[str] = set()
        allowed: list[str] = []
        for profile in profiles:
            for tool in profile.allowed_tools:
                if tool not in seen:
                    seen.add(tool)
                    allowed.append(tool)
        return allowed

    def render_for_prompt(self, profiles: list[ToolProfile]) -> str:
        if not profiles:
            return ""
        parts = ["Tool profiles selected by Hermes:"]
        for profile in profiles:
            tools = ", ".join(profile.allowed_tools)
            parts.append(
                f"\n### {profile.display_name} ({profile.name})\n"
                f"{profile.description}\n"
                f"Allowed tools: {tools}"
            )
        return "\n".join(parts) + "\n\n"

    def _score(self, profile: ToolProfile, node_type: str, text: str) -> int:
        score = 4 - max(1, min(3, profile.priority))
        if node_type in [item.lower() for item in profile.applicable_node_types]:
            score += 1
        haystack = f"{profile.name} {profile.display_name} {profile.category} {profile.description}".lower()
        matched = False
        if profile.category in text:
            score += 1
            matched = True
        for token in profile.keywords:
            if token.lower() in text:
                score += 2
                matched = True
        if any(token in text for token in haystack.split() if len(token) > 5):
            score += 1
            matched = True
        if profile.priority >= 3 and not matched:
            return 0
        return score

    def _load_builtin_profiles(self) -> None:
        for profile in _BUILTIN_PROFILES:
            self.register(profile)


_BUILTIN_PROFILES = [
    ToolProfile(
        name="file-edit",
        display_name="File Read/Edit",
        category="filesystem",
        description="Read and edit repository files for implementation work.",
        allowed_tools=["Read", "Edit", "Write", "MultiEdit"],
        applicable_node_types=["implementation", "coding", "debugging", "verification", "analysis", "research", "planning", "docs"],
        priority=1,
        keywords=["file", "read", "edit", "write", "modify", "implementation", "docs"],
    ),
    ToolProfile(
        name="git-local",
        display_name="Local Git Inspection",
        category="git",
        description="Inspect local repository state without network effects.",
        allowed_tools=["Bash(git diff*)", "Bash(git status*)", "Bash(git ls-files*)"],
        applicable_node_types=["*"],
        priority=1,
        keywords=["git", "diff", "status", "files modified"],
    ),
    ToolProfile(
        name="python-tests",
        display_name="Python Test Runner",
        category="verification",
        description="Run local Python unit tests and syntax checks.",
        allowed_tools=["Bash(python3 -m unittest*)", "Bash(python3 -m py_compile*)", "Bash(bash -n*)"],
        applicable_node_types=["implementation", "verification", "debugging"],
        priority=1,
        keywords=["test", "verify", "unittest", "pytest", "regression", "py_compile"],
    ),
    ToolProfile(
        name="git-write",
        display_name="Git Write Operations",
        category="git",
        description="Clone, stage, commit, or push only when explicitly requested by the task.",
        allowed_tools=["Bash(git clone*)", "Bash(git add*)", "Bash(git commit*)", "Bash(git push*)"],
        applicable_node_types=["implementation"],
        priority=3,
        keywords=["clone", "commit", "push", "stage", "git add"],
    ),
    ToolProfile(
        name="mcp-readonly",
        display_name="Read-only MCP Tools",
        category="mcp",
        description="Allow read-only MCP filesystem/search tools when an MCP-backed task asks for them.",
        allowed_tools=[
            "mcp__filesystem__read_file",
            "mcp__filesystem__list_directory",
            "mcp__search__query",
        ],
        applicable_node_types=["analysis", "research", "planning", "verification"],
        priority=2,
        keywords=["mcp", "tool", "external context", "read-only"],
    ),
]


_DEFAULT_REGISTRY = ToolRegistry()


def get_default_tool_registry() -> ToolRegistry:
    return _DEFAULT_REGISTRY
