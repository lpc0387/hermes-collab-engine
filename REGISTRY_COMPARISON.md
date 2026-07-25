# UnifiedRegistry (registry.py) vs SkillRegistry (skills.py) + ToolRegistry (tools.py)

**Analysis date:** 2025-07-11
**Files analyzed:**
- `/root/hermes-collab-engine/src/hermes_collab_engine/registry.py` (569 lines)
- `/root/hermes-collab-engine/src/hermes_collab_engine/skills.py` (335 lines)
- `/root/hermes-collab-engine/src/hermes_collab_engine/tools.py` (191 lines)

---

## 1. Method Equivalence Mapping

### 1.1 Skill-like methods

| skills.py (SkillRegistry) | registry.py (UnifiedRegistry) | Status |
|---|---|---|
| `SkillRegistry()` | `UnifiedRegistry(store=None)` | **Incompatible** — registry adds `store` param |
| `register(skill)` | `register(entry)` | **Generic** — registry accepts any `RegistryEntry` subclass |
| `get(name) -> SkillEntry\|None` | `get(name) -> RegistryEntry\|None` | Same signature, broader return type |
| `list_all() -> list[SkillEntry]` | `list_all() -> list[RegistryEntry]` | Same signature, broader return type |
| `select_for_node(node_type, task_text, max_skills) -> list[SkillEntry]` | `select_skills(capability, task_text, max_skills) -> list[SkillEntry]` | **~Equivalent** param rename `node_type→capability`, identical `_score` logic but without `_TASK_KEYWORDS` dict |
| `render_for_prompt(skills) -> str` | `render_skills_for_prompt(skills) -> str` | **~Equivalent** — registry adds `File: {file_path}` hint line if `file_path` present |
| `refresh()` | — | **No equivalent** — registry reloads nothing |
| `_load_builtin_skills()` | — | **No equivalent** — registry has no builtins |
| `_score(skill, node_type, text) -> int` | `_score_entry(entry, task_text) -> int` | **Different** — see scoring section below |

### 1.2 Tool-like methods

| tools.py (ToolRegistry) | registry.py (UnifiedRegistry) | Status |
|---|---|---|
| `ToolRegistry()` | `UnifiedRegistry(store=None)` | **Incompatible** |
| `register(profile)` | `register(entry)` | Generic |
| `get(name) -> ToolProfile\|None` | `get(name) -> RegistryEntry\|None` | Same signature, broader return type |
| `list_all() -> list[ToolProfile]` | `list_all() -> list[RegistryEntry]` | Same signature, broader return type |
| `select_for_node(node_type, task_text, max_profiles) -> list[ToolProfile]` | `select_tools(capability, task_text, max_tools) -> list[ToolEntry]` | **~Equivalent** param rename `node_type→capability`, same filtering/max |
| `allowed_tools_for_profiles(profiles) -> list[str]` | `allowed_tools_for_capability(capability) -> list[str]` | **Different approach** — old: pre-filtered list → merge; new: capability query → merge *(see note)* |
| `render_for_prompt(profiles) -> str` | `render_tools_for_prompt(tools) -> str` | **~Equivalent** — registry renders ToolEntry\|MCPEntry, adds `Config: {config_path}` if present |
| `_load_builtin_profiles()` | — | **No equivalent** — registry has no builtins |
| `_score(profile, node_type, text) -> int` | `_score_entry(entry, task_text) -> int` | **Different** — see scoring section below |

> **Note on `allowed_tools_for_capability`:** The unified version internally calls `select_for_capability(capability)` (which scans the capability index), then merges `allowed_tools` from all matches. The old `allowed_tools_for_profiles` takes an already-selected list of `ToolProfile` objects. These are different call conventions but produce the same end effect.

---

## 2. Scoring Algorithm Differences

### SkillRegistry._score (skills.py)
```python
score = 4 - max(1, min(3, skill.priority))
# EXTRA: _TASK_KEYWORDS dict lookup for bonus +2
for word in _TASK_KEYWORDS.get(skill.name, ()):
    if word in text:
        score += 2
if skill.category in text or any(token in text for token in haystack.split() if len(token) > 5):
    score += 1
if node_type in [item.lower() for item in skill.applicable_node_types]:
    score += 1
# No short-circuit — always returns score (may be 0 or negative?)
```

### UnifiedRegistry._score_entry (registry.py)
```python
score = 4 - max(1, min(3, entry.priority))
# NO _TASK_KEYWORDS equivalent
for token in haystack.split():
    if len(token) > 5 and token in text:
        score += 1
if entry.category in text:
    score += 1
# No node_type match bonus (not applicable to generic scoring)
```

### ToolRegistry._score (tools.py)
```python
score = 4 - max(1, min(3, profile.priority))
if node_type in [item.lower() for item in profile.applicable_node_types]:
    score += 1
# EXTRA: keywords list bonus +2 per match
if profile.category in text:
    score += 1
for token in profile.keywords:
    if token.lower() in text:
        score += 2
# EXTRA: short-circuit on high-priority + no match
if profile.priority >= 3 and not matched:
    return 0
```

**Key scoring deltas:**
- `skills.py _TASK_KEYWORDS` → **dropped** in UnifiedRegistry (skill-name-based keyword bonus lost)
- `tools.py profile.keywords` → **dropped** in UnifiedRegistry (tool keywords bonus lost)
- `tools.py priority≥3 + no-match short-circuit` → **dropped** in UnifiedRegistry
- Node type exact match bonus → **dropped** in UnifiedRegistry (no `node_type` parameter)

---

## 3. Unique Features (registry.py only)

### `UnifiedRegistry` methods with no counterpart in skills.py or tools.py

| Method | Description |
|---|---|
| `__init__(store)` | Accepts optional `store` for web persistence |
| `delete(name)` | Remove single entry by name (with cap-index cleanup + persist) |
| `list_by_type(entry_type)` | Filter entries by dataclass type (SkillEntry/ToolEntry/MCPEntry) |
| `select_for_capability(capability, entry_type, max_entries)` | Generic capability-based entry selection (parent of select_skills/select_tools/select_mcp) |
| `select_mcp(capability, max_entries)` | MCP-specific selection |
| `register_mcp_server(server_name, command, args, env, tools, ...)` | Create multiple MCPEntries from a server definition |
| `list_mcp_servers()` | Group MCP entries by `server_name`, return structured dict with tool listing |
| `remove_mcp_server(server_name)` | Bulk-delete all entries for one MCP server |
| `allowed_tools_for_capability(capability)` | Capability-based tool allowlist merging |
| `render_tools_for_prompt(tools)` | Handles **both** ToolEntry and MCPEntry subtypes |
| `from_legacy(skill_registry, tool_registry, store)` | Migrate old registries into unified form |
| `_restore_persisted()` | Load persisted entries from DB store |
| `_persist_entries()` | Save non-hermes-sourced entries to DB store |
| `_index_entry(entry)` | Index entry in name + capability-index |
| `_score_entry(entry, task_text)` | Shared scoring function (static) |

### Free functions with no counterpart

| Function | Description |
|---|---|
| `discover_mcp_entries(config_path)` | Load MCP entries from JSON config file or `HERMES_MCP_CONFIG` env var |
| `get_unified_registry(store)` | Lazy singleton with legacy migration + MCP discovery |
| `_serialize_entry` / `_deserialize_entry` | JSON-safe serialization with type discriminator |
| `RegistryEntry` (base dataclass) | Common base with `to_dict()`, `capabilities`, `source`, `priority` |
| `MCPEntry` (dataclass) | MCP tool entry with `server_name`, `tool_name`, `endpoint`, `config_path`, `qualified_name` |
| `_ENTRY_TYPES` dict | Type string → dataclass mapping for deserialization |
| `_entry_type_key` | Derive type string from instance |

---

## 4. Unique Features (skills.py / tools.py only)

### SkillRegistry-only

| Feature | Description |
|---|---|
| `refresh()` | Reload builtin skills (exists as no-op stub) |
| `_load_builtin_skills()` | Hard-coded `_BUILTIN_SKILLS` — 8 skill definitions inline |
| `_TASK_KEYWORDS` dict | Skill-name-keyed keyword tuples for bonus scoring |
| `_DEFAULT_REGISTRY` singleton | Pre-initialized with builtins |

### ToolRegistry-only

| Feature | Description |
|---|---|
| `_load_builtin_profiles()` | Hard-coded `_BUILTIN_PROFILES` — 6 profiles inline |
| `ToolProfile.keywords` field | List of keywords for scoring (not in ToolEntry) |
| `ToolProfile.mcp_tools` property | Filters `allowed_tools` by `startswith("mcp__")` |
| `_DEFAULT_REGISTRY` singleton | Pre-initialized with builtins |

---

## 5. Data Model Differences

### 5.1 SkillEntry (skills.py) vs SkillEntry (registry.py)

| Field | skills.py | registry.py | Delta |
|---|---|---|---|
| `name` | ✅ | ✅ (inherited) | Same |
| `display_name` | ✅ | ✅ (inherited) | Same |
| `category` | ✅ | ✅ (inherited) | Same |
| `description` | ✅ | ✅ (inherited) | Same |
| `content` | ✅ | ✅ | Same |
| `applicable_node_types` | `list[str]` | — | **Removed** — renamed to `capabilities` in base |
| `capabilities` | — | `list[str]` (inherited) | **New** — replaces `applicable_node_types` |
| `priority` | `int` | `int` (inherited) | Same |
| `source` | `str` | `str` (inherited) | Same |
| `required_tools` | `list[str]` | `list[str]` | Same |
| `file_path` | — | `str = ""` | **New** — disk path of skill file |
| `to_dict()` | ✅ | ✅ (inherited) | Same |

### 5.2 ToolProfile (tools.py) vs ToolEntry (registry.py)

| Field | tools.py | registry.py | Delta |
|---|---|---|---|
| `name` | ✅ | ✅ (inherited) | Same |
| `display_name` | ✅ | ✅ (inherited) | Same |
| `category` | ✅ | ✅ (inherited) | Same |
| `description` | ✅ | ✅ (inherited) | Same |
| `allowed_tools` | `list[str]` | `list[str]` | Same |
| `applicable_node_types` | `list[str]` | — | **Removed** — renamed to `capabilities` in base |
| `capabilities` | — | `list[str]` (inherited) | **New** — replaces `applicable_node_types` |
| `priority` | `int` | `int` (inherited) | Same |
| `source` | `str = "hermes"` | `str` (inherited) | Same |
| `keywords` | `list[str]` | — | **Removed** — used by ToolRegistry._score, no UnifiedRegistry equivalent |
| `mcp_tools` (property) | ✅ | — | **Removed** — filter `allowed_tools` for mcp__ prefix; not ported |

### 5.3 MCPEntry (registry.py) — entirely new

Not present in old skills.py or tools.py.

| Field | Type | Purpose |
|---|---|---|
| `server_name` | `str` | Logical server name |
| `tool_name` | `str` | Tool name within server |
| `endpoint` | `str` | `command args...` string |
| `allowed_tools` | `list[str]` | Self-referential qualified names |
| `config_path` | `str` | Disk path of MCP config file |
| `qualified_name` | property | `mcp__{server}__{tool}` format |

---

## 6. Caller Analysis

### engine.py
```
from .skills import SkillRegistry, get_default_registry        # line 23
from .tools import ToolRegistry, get_default_tool_registry      # line 25
from .registry import get_unified_registry, SkillEntry as USkillEntry,
                     ToolEntry as UToolEntry, MCPEntry as UMCPEntry  # line 26

Constructor: self.skill_registry = skill_registry or get_default_registry()           # line 81
             self.tool_registry = tool_registry or get_default_tool_registry()        # line 82
             get_unified_registry(store=self.store)  # init only                    # line 90
             self._skill_distributor = SkillDistributor(
                 skill_registry=self.skill_registry, tool_registry=self.tool_registry)  # line 84-85

Refresh:    self.skill_registry.refresh()                          # line 1645
Get skill:  self.skill_registry.get(name)                          # line 1682
Get tool:   self.tool_registry.get(name)                           # line 1693
List all:   unified.list_by_type(USkillEntry)                     # line 1686
            unified.list_by_type(UToolEntry) + unified.list_by_type(UMCPEntry)  # line 1697

NOT used:   UnifiedRegistry.select_for_capability / select_skills / select_tools
```

**Pattern:** engine.py initializes UnifiedRegistry for listing/debugging endpoints, but the main workflow (planning, selection, rendering) still flows through old SkillRegistry + ToolRegistry singletons via SkillDistributor.

---

### server.py
```
GET /api/skills               → skills.get_default_registry().select_for_node()   # lines 41-43
GET /api/tools                → tools.get_default_tool_registry().select_for_node()  # lines 47-49
GET /api/mcp-servers          → get_unified_registry().list_mcp_servers()          # lines 120-122
GET /api/registry             → get_unified_registry().select_for_capability()     # lines 124-128
POST /api/mcp-servers         → get_unified_registry().register_mcp_server()       # lines 302-328
POST /api/registry            → get_unified_registry().register()                  # lines 350-381
DELETE /api/registry/<name>   → get_unified_registry().delete()                    # lines 409-419
```

**Pattern:** server.py uses **both** — old APIs for the legacy skill/tool endpoints, UnifiedRegistry for MCP endpoints and the new `/api/registry` endpoint.

---

### cli.py
```
hermes skills select          → skills.get_default_registry().select_for_node()       # lines 1095-1098
hermes skills list            → skills.get_default_registry().list_all()              # line 1100
hermes tools select           → tools.get_default_tool_registry().select_for_node()   # lines 1110-1113
hermes tools list             → tools.get_default_tool_registry().list_all()          # line 1115
hermes mcp list               → get_unified_registry().list_mcp_servers()             # lines 1129-1132
hermes mcp add                → get_unified_registry().register_mcp_server()          # lines 1166-1170
hermes mcp remove             → get_unified_registry().remove_mcp_server()            # line 1189
```

**Pattern:** Same as server.py — old APIs for skills/tools, UnifiedRegistry for MCP commands.

---

### planner.py
```
from .registry import MCPEntry, SkillEntry, ToolEntry, get_unified_registry  # line 12

build_context():
    registry = get_unified_registry()       # line 273
    skill_entries = registry.list_by_type(SkillEntry)      # line 276
    tool_entries = registry.list_by_type(ToolEntry)        # line 277
    mcp_entries = registry.list_by_type(MCPEntry)          # line 278
```

**Pattern:** planner.py uses **only** UnifiedRegistry — it reads all entries by type to build prompt blocks. It does NOT use select_for_capability or selection methods.

---

### skill_distributor.py
```
from .skills import SkillEntry, SkillRegistry       # line 20
from .tools import ToolRegistry                      # line 21

validate_maps(skill_registry, tool_registry)          # line 118 — validation only
render_for_prompt(skill_names, tool_names, ...):      # line 195
    self.skill_registry.render_for_prompt(...)         # line 349
    self.tool_registry.render_for_prompt(...)          # line 362
    (optionally) unified_registry.list_by_type(MCPEntry) for cross-check  # line 293-299
```

**Pattern:** skill_distributor.py still uses **old registries** exclusively for rendering. UnifiedRegistry is only optionally used for MCP server cross-check.

---

## 7. Migration Status Summary

```diff
# ——————— Already migrated to UnifiedRegistry ———————
✅ planner.py build_context() → registry.list_by_type()
✅ server.py  /api/registry endpoint → registry.select_for_capability()
✅ server.py  MCP endpoints → registry.register_mcp_server/list_mcp_servers()
✅ cli.py     MCP commands → registry.register_mcp_server/remove_mcp_server/list_mcp_servers()

# ——————— Still on old SkillRegistry/ToolRegistry ———————
❌ engine.py           main workflow (planning, execution) still uses skill_registry+tool_registry
❌ server.py           /api/skills + /api/tools endpoints → old select_for_node()
❌ cli.py              `hermes skills select` + `hermes tools select` → old select_for_node()
❌ skill_distributor   render_for_prompt() → old render_for_prompt() on SkillRegistry+ToolRegistry
❌ _TASK_KEYWORDS      scoring bonus in skills.py not ported to registry._score_entry
❌ ToolProfile.keywords scoring bonus in tools.py not ported to registry._score_entry

# ——————— UnifiedRegistry unique (no legacy counterpart) ———————
#  - Persistence via store (get_setting/set_setting)
#  - MCP server registration/mgmt (register_mcp_server, list_mcp_servers, remove_mcp_server)
#  - MCP discovery from JSON config (discover_mcp_entries)
#  - Generic capability-index with type filtering (select_for_capability)
#  - MCPEntry with server_name/tool_name/endpoint/qualified_name
#  - delete() + list_by_type() — no equivalent in old registries

# ——————— Old registries unique (no UnifiedRegistry counterpart) ———————
#  - Builtin skill/profile definitions hardcoded in _BUILTIN_SKILLS/_BUILTIN_PROFILES
#  - refresh() — reload builtins
#  - SkillRegistry._TASK_KEYWORDS dict for skill-name-specific keyword scoring
#  - ToolProfile.keywords field for keyword-based scoring
#  - ToolProfile.mcp_tools property
#  - Short-circuit: ToolRegistry._score returns 0 for high-priority+no-match
```
