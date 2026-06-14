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

    @classmethod
    def from_legacy(cls, legacy) -> "SkillEntry":
        """Convert a skills.SkillEntry into a unified SkillEntry."""
        return cls(
            name=legacy.name,
            display_name=legacy.display_name,
            category=legacy.category,
            description=legacy.description,
            capabilities=list(legacy.applicable_node_types),
            source=legacy.source,
            priority=legacy.priority,
            content=legacy.content,
            file_path=getattr(legacy, "file_path", ""),
        )


@dataclass
class ToolEntry(RegistryEntry):
    """A tool profile describing allowed tools for workers."""

    allowed_tools: list[str] = field(default_factory=list)

    @classmethod
    def from_legacy(cls, legacy) -> "ToolEntry":
        """Convert a tools.ToolProfile into a unified ToolEntry."""
        return cls(
            name=legacy.name,
            display_name=legacy.display_name,
            category=legacy.category,
            description=legacy.description,
            capabilities=list(legacy.applicable_node_types),
            source=legacy.source,
            priority=legacy.priority,
            allowed_tools=list(legacy.allowed_tools),
        )


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

_PERSIST_KEY = "unified_registry_entries"

# Registry entry class dispatch: type string -> dataclass
_ENTRY_TYPES: dict[str, type[RegistryEntry]] = {
    "skill": SkillEntry,
    "tool": ToolEntry,
    "mcp": MCPEntry,
}


def _entry_type_key(entry: RegistryEntry) -> str:
    """Return the type string for an entry (skill / tool / mcp)."""
    if isinstance(entry, MCPEntry):
        return "mcp"
    if isinstance(entry, ToolEntry):
        return "tool"
    return "skill"


def _serialize_entry(entry: RegistryEntry) -> dict[str, Any]:
    """Serialize an entry to a JSON-safe dict with a ``_type`` discriminator."""
    data = entry.to_dict()
    data["_type"] = _entry_type_key(entry)
    return data


def _deserialize_entry(data: dict[str, Any]) -> RegistryEntry | None:
    """Reconstruct a RegistryEntry from a serialized dict."""
    type_key = data.pop("_type", "")
    cls = _ENTRY_TYPES.get(type_key)
    if cls is None:
        return None
    import dataclasses
    field_names = {f.name for f in dataclasses.fields(cls)}
    filtered = {k: v for k, v in data.items() if k in field_names}
    try:
        return cls(**filtered)
    except TypeError:
        return None


class UnifiedRegistry:
    """Capability-indexed registry for skills, tools, and MCP entries.

    Entries are indexed by their ``capabilities`` tags so the planner can
    select a bundle by node capability without scanning all entries.
    """

    def __init__(self, store: Any = None) -> None:
        self._entries: dict[str, RegistryEntry] = {}
        self._capability_index: dict[str, list[str]] = {}  # cap -> [entry names]
        self._store = store
        if store is not None:
            self._restore_persisted()

    # -- persistence --------------------------------------------------------

    def _restore_persisted(self) -> None:
        """Load web-persisted entries from the settings table."""
        if self._store is None:
            return
        try:
            raw = self._store.get_setting(_PERSIST_KEY)
        except Exception:
            return
        if not isinstance(raw, list):
            return
        for item in raw:
            entry = _deserialize_entry(dict(item))
            if entry is not None:
                self._index_entry(entry)

    def _persist_entries(self) -> None:
        """Save all non-hermes entries to the settings table."""
        if self._store is None:
            return
        web_entries = [
            _serialize_entry(e)
            for e in self._entries.values()
            if e.source != "hermes"
        ]
        try:
            self._store.set_setting(_PERSIST_KEY, web_entries)
        except Exception:
            pass

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
        # Persist web-added entries
        if entry.source != "hermes":
            self._persist_entries()

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
        # Persist after deletion
        self._persist_entries()
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

    # -- import from legacy registries --------------------------------------

    @classmethod
    def from_legacy(cls, skill_registry=None, tool_registry=None, store=None) -> "UnifiedRegistry":
        """Build a UnifiedRegistry from existing SkillRegistry and ToolRegistry."""
        reg = cls(store=store)
        if skill_registry is not None:
            for skill in skill_registry.list_all():
                reg.register(SkillEntry.from_legacy(skill))
        if tool_registry is not None:
            for profile in tool_registry.list_all():
                reg.register(ToolEntry.from_legacy(profile))
        return reg


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


def get_unified_registry(store: Any = None) -> UnifiedRegistry:
    """Return the default unified registry, building it lazily.

    If *store* is provided (first call), it is used for persistence.
    Subsequent calls return the cached instance regardless of *store*.
    """
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        from .skills import get_default_registry
        from .tools import get_default_tool_registry
        _DEFAULT_REGISTRY = UnifiedRegistry.from_legacy(
            skill_registry=get_default_registry(),
            tool_registry=get_default_tool_registry(),
            store=store,
        )
        # Merge MCP entries from config
        for mcp_entry in discover_mcp_entries():
            _DEFAULT_REGISTRY.register(mcp_entry)
    return _DEFAULT_REGISTRY
