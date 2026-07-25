"""Data models for the Configuration Center module.

Defines the core entities: Namespace, ConfigItem, and ConfigVersion
as frozen dataclasses with validation and serialization support.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Namespace
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Namespace:
    """A logical grouping of configuration items.

    Examples: "payment-service", "auth-service", "shared".
    """

    id: str
    name: str
    description: str = ""
    environment: str = "dev"  # dev, staging, prod
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict[str, Any]) -> Namespace:
        return Namespace(
            id=str(data.get("id", "")),
            name=str(data.get("name", "")),
            description=str(data.get("description", "")),
            environment=str(data.get("environment", "dev")),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
        )


# ---------------------------------------------------------------------------
# ConfigItem
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConfigItem:
    """A single configuration key-value pair within a namespace."""

    id: str
    namespace_id: str
    key: str
    value: str  # JSON-encoded value
    value_type: str = "string"  # string, number, boolean, json
    description: str = ""
    version: int = 1
    tags: str = "[]"  # JSON array of tag strings
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        # Decode tags for API responses
        try:
            result["tags"] = json.loads(self.tags) if isinstance(self.tags, str) else self.tags
        except (json.JSONDecodeError, TypeError):
            result["tags"] = []
        # Decode value for display
        if self.value_type == "json":
            try:
                result["value_parsed"] = json.loads(self.value)
            except (json.JSONDecodeError, TypeError):
                result["value_parsed"] = self.value
        else:
            result["value_parsed"] = self.value
        return result

    @staticmethod
    def from_dict(data: dict[str, Any]) -> ConfigItem:
        tags = data.get("tags", "[]")
        if isinstance(tags, list):
            tags = json.dumps(tags, ensure_ascii=False)
        return ConfigItem(
            id=str(data.get("id", "")),
            namespace_id=str(data.get("namespace_id", "")),
            key=str(data.get("key", "")),
            value=str(data.get("value", "")),
            value_type=str(data.get("value_type", "string")),
            description=str(data.get("description", "")),
            version=int(data.get("version", 1)),
            tags=tags,
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
        )


# ---------------------------------------------------------------------------
# ConfigVersion (audit trail)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConfigVersion:
    """An immutable snapshot of a config item at a specific version."""

    id: str
    config_id: str
    version: int
    key: str
    value: str
    value_type: str
    description: str
    tags: str
    changed_by: str = "system"
    change_comment: str = ""
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        try:
            result["tags"] = json.loads(self.tags) if isinstance(self.tags, str) else self.tags
        except (json.JSONDecodeError, TypeError):
            result["tags"] = []
        return result

    @staticmethod
    def from_dict(data: dict[str, Any]) -> ConfigVersion:
        tags = data.get("tags", "[]")
        if isinstance(tags, list):
            tags = json.dumps(tags, ensure_ascii=False)
        return ConfigVersion(
            id=str(data.get("id", "")),
            config_id=str(data.get("config_id", "")),
            version=int(data.get("version", 1)),
            key=str(data.get("key", "")),
            value=str(data.get("value", "")),
            value_type=str(data.get("value_type", "string")),
            description=str(data.get("description", "")),
            tags=tags,
            changed_by=str(data.get("changed_by", "system")),
            change_comment=str(data.get("change_comment", "")),
            created_at=str(data.get("created_at", "")),
        )


# ---------------------------------------------------------------------------
# Schema definition (SQL DDL)
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS config_namespaces (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    environment TEXT NOT NULL DEFAULT 'dev',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS config_items (
    id TEXT PRIMARY KEY,
    namespace_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL DEFAULT '',
    value_type TEXT NOT NULL DEFAULT 'string',
    description TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    tags TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (namespace_id) REFERENCES config_namespaces(id) ON DELETE CASCADE,
    UNIQUE(namespace_id, key)
);

CREATE TABLE IF NOT EXISTS config_versions (
    id TEXT PRIMARY KEY,
    config_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL DEFAULT '',
    value_type TEXT NOT NULL DEFAULT 'string',
    description TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '[]',
    changed_by TEXT NOT NULL DEFAULT 'system',
    change_comment TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (config_id) REFERENCES config_items(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_config_items_namespace ON config_items(namespace_id);
CREATE INDEX IF NOT EXISTS idx_config_versions_config ON config_versions(config_id);
"""

__all__ = [
    "Namespace",
    "ConfigItem",
    "ConfigVersion",
    "SCHEMA_SQL",
    "_now_iso",
]
