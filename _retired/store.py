"""Configuration Center — SQLite storage layer.

Provides CRUD operations for namespaces, configuration items, and
version history with snapshot-based rollback support.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any

from .schema import SCHEMA_SQL, ConfigItem, ConfigVersion, Namespace


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _now_iso() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


class ConfigStore:
    """SQLite-backed persistent storage for the configuration center.

    Thread-safe via reentrant lock.  Every mutating operation that changes
    a config item's value writes a version snapshot so the audit trail is
    never lost.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        with self.lock:
            self.conn.executescript(SCHEMA_SQL)
            self.conn.commit()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self.lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def _query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def _one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        with self.lock:
            r = self.conn.execute(sql, params).fetchone()
            return dict(r) if r else None

    # ------------------------------------------------------------------
    # Namespace CRUD
    # ------------------------------------------------------------------

    def create_namespace(self, name: str, description: str = "", environment: str = "dev") -> Namespace:
        """Create a new namespace and return it."""
        ns_id = _new_id()
        now = _now_iso()
        self._execute(
            """INSERT INTO config_namespaces (id, name, description, environment, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (ns_id, name, description, environment, now, now),
        )
        return self.get_namespace(ns_id)

    def get_namespace(self, ns_id: str) -> Namespace:
        """Get a single namespace by id."""
        row = self._one("SELECT * FROM config_namespaces WHERE id=?", (ns_id,))
        if not row:
            raise KeyError(f"Namespace not found: {ns_id}")
        return Namespace.from_dict(row)

    def list_namespaces(self, environment: str | None = None) -> list[Namespace]:
        """List all namespaces, optionally filtered by environment."""
        if environment:
            rows = self._query(
                "SELECT * FROM config_namespaces WHERE environment=? ORDER BY created_at DESC",
                (environment,),
            )
        else:
            rows = self._query("SELECT * FROM config_namespaces ORDER BY created_at DESC")
        return [Namespace.from_dict(r) for r in rows]
    def update_namespace(self, ns_id: str, *,
                         name: str | None = None,
                         description: str | None = None,
                         environment: str | None = None) -> Namespace:
        """Update mutable fields of a namespace."""
        existing = self.get_namespace(ns_id)
        _ALLOWED_NS_COLS = {"name", "description", "environment"}
        updates = {}
        if name is not None:
            updates["name"] = name
        if description is not None:
            updates["description"] = description
        if environment is not None:
            updates["environment"] = environment
        if not updates:
            return existing

        for col in updates:
            if col not in _ALLOWED_NS_COLS:
                raise ValueError(f"invalid column: {col}")
        now = _now_iso()
        set_clause = ", ".join(f"{k}=?" for k in updates)
        values = list(updates.values()) + [now, ns_id]
        self._execute(
            f"UPDATE config_namespaces SET {set_clause}, updated_at=? WHERE id=?",
            tuple(values),
        )
        return self.get_namespace(ns_id)

    def delete_namespace(self, ns_id: str) -> None:
        """Delete a namespace and all its config items (cascade)."""
        # Verify exists
        self.get_namespace(ns_id)
        self._execute("DELETE FROM config_namespaces WHERE id=?", (ns_id,))

    # ------------------------------------------------------------------
    # Config Item CRUD
    # ------------------------------------------------------------------

    def create_config(self, namespace_id: str, key: str, value: str,
                      value_type: str = "string", description: str = "",
                      tags: str | list[str] = "[]",
                      changed_by: str = "system",
                      change_comment: str = "") -> ConfigItem:
        """Create a new config item under a namespace.

        Writes the initial version snapshot automatically.
        """
        # Verify namespace exists
        self.get_namespace(namespace_id)

        cfg_id = _new_id()
        now = _now_iso()
        if isinstance(tags, list):
            tags_str = json.dumps(tags, ensure_ascii=False)
        else:
            tags_str = tags

        self._execute(
            """INSERT INTO config_items (id, namespace_id, key, value, value_type,
               description, version, tags, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)""",
            (cfg_id, namespace_id, key, value, value_type, description, tags_str, now, now),
        )
        # Write initial version snapshot
        self._write_version_snapshot(cfg_id, key, value, value_type, description, tags_str, 1, changed_by, change_comment)
        return self.get_config(cfg_id)

    def get_config(self, cfg_id: str) -> ConfigItem:
        """Get a single config item by id."""
        row = self._one("SELECT * FROM config_items WHERE id=?", (cfg_id,))
        if not row:
            raise KeyError(f"Config item not found: {cfg_id}")
        return ConfigItem.from_dict(row)

    def get_config_by_key(self, namespace_id: str, key: str) -> ConfigItem | None:
        """Look up a config item by namespace + key."""
        row = self._one(
            "SELECT * FROM config_items WHERE namespace_id=? AND key=?",
            (namespace_id, key),
        )
        return ConfigItem.from_dict(row) if row else None

    def list_configs(self, namespace_id: str) -> list[ConfigItem]:
        """List all config items in a namespace."""
        rows = self._query(
            "SELECT * FROM config_items WHERE namespace_id=? ORDER BY key ASC",
            (namespace_id,),
        )
        return [ConfigItem.from_dict(r) for r in rows]

    def list_all_configs(self, namespace_id: str | None = None,
                         environment: str | None = None) -> list[ConfigItem]:
        """List configs across namespaces with optional filters.

        When *namespace_id* is provided, only configs in that namespace
        are returned.  When *environment* is provided (without namespace),
        configs in all namespaces matching that environment are returned.
        """
        if namespace_id:
            return self.list_configs(namespace_id)
        if environment:
            rows = self._query(
                """SELECT c.* FROM config_items c
                   JOIN config_namespaces n ON n.id = c.namespace_id
                   WHERE n.environment=? ORDER BY c.key ASC""",
                (environment,),
            )
            return [ConfigItem.from_dict(r) for r in rows]
        rows = self._query(
            "SELECT * FROM config_items ORDER BY namespace_id, key ASC",
        )
        return [ConfigItem.from_dict(r) for r in rows]

    def update_config(self, cfg_id: str, *,
                      value: str | None = None,
                      value_type: str | None = None,
                      description: str | None = None,
                      tags: str | list[str] | None = None,
                      changed_by: str = "system",
                      change_comment: str = "") -> ConfigItem:
        """Update mutable fields of a config item.

        Bumps the version and writes an audit snapshot whenever *value*
        changes (or the caller explicitly provides a change_comment).
        """
        existing = self.get_config(cfg_id)
        updates: dict[str, str] = {}
        if value is not None:
            updates["value"] = value
        if value_type is not None:
            updates["value_type"] = value_type
        if description is not None:
            updates["description"] = description
        if tags is not None:
            if isinstance(tags, list):
                updates["tags"] = json.dumps(tags, ensure_ascii=False)
            else:
                updates["tags"] = tags

        if not updates:
            return existing

        _ALLOWED_CFG_COLS = {"value", "value_type", "description", "tags"}
        for col in updates:
            if col not in _ALLOWED_CFG_COLS:
                raise ValueError(f"invalid column: {col}")

        new_version = existing.version + 1
        now = _now_iso()
        set_clause = ", ".join(f"{k}=?" for k in updates)
        values = list(updates.values()) + [new_version, now, cfg_id]
        self._execute(
            f"UPDATE config_items SET {set_clause}, version=?, updated_at=? WHERE id=?",
            tuple(values),
        )

        # Write version snapshot
        final_value = updates.get("value", existing.value)
        final_type = updates.get("value_type", existing.value_type)
        final_desc = updates.get("description", existing.description)
        final_tags = updates.get("tags", existing.tags)
        self._write_version_snapshot(
            cfg_id,
            existing.key,
            final_value,
            final_type,
            final_desc,
            final_tags,
            new_version,
            changed_by,
            change_comment,
        )
        return self.get_config(cfg_id)

    def delete_config(self, cfg_id: str) -> None:
        """Delete a config item and its version history."""
        self.get_config(cfg_id)  # verify exists
        self._execute("DELETE FROM config_items WHERE id=?", (cfg_id,))

    # ------------------------------------------------------------------
    # Version history & rollback
    # ------------------------------------------------------------------

    def _write_version_snapshot(self, config_id: str, key: str, value: str,
                                value_type: str, description: str, tags: str,
                                version: int, changed_by: str,
                                change_comment: str) -> None:
        vid = _new_id()
        now = _now_iso()
        self._execute(
            """INSERT INTO config_versions
               (id, config_id, version, key, value, value_type,
                description, tags, changed_by, change_comment, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (vid, config_id, version, key, value, value_type,
             description, tags, changed_by, change_comment, now),
        )

    def get_config_history(self, cfg_id: str) -> list[ConfigVersion]:
        """Return all version snapshots for a config item, newest first."""
        rows = self._query(
            "SELECT * FROM config_versions WHERE config_id=? ORDER BY version DESC",
            (cfg_id,),
        )
        return [ConfigVersion.from_dict(r) for r in rows]

    def rollback_config(self, cfg_id: str, target_version: int,
                        changed_by: str = "system",
                        change_comment: str = "") -> ConfigItem:
        """Roll back a config item to a previous version.

        This creates a NEW version whose value matches the target version's
        snapshot — the old snapshots are preserved.
        """
        # Verify config exists
        self.get_config(cfg_id)

        # Find the target version snapshot
        snapshot = self._one(
            "SELECT * FROM config_versions WHERE config_id=? AND version=?",
            (cfg_id, target_version),
        )
        if not snapshot:
            raise KeyError(f"Version {target_version} not found for config {cfg_id}")

        comment = change_comment or f"Rollback to version {target_version}"
        return self.update_config(
            cfg_id,
            value=snapshot["value"],
            value_type=snapshot["value_type"],
            description=snapshot["description"],
            tags=snapshot["tags"],
            changed_by=changed_by,
            change_comment=comment,
        )

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search_configs(self, query: str,
                       namespace_id: str | None = None) -> list[ConfigItem]:
        """Search config items by key or description (substring match)."""
        _safe = query.replace("%", r"\%").replace("_", r"\_")
        like = f"%{_safe}%"
        if namespace_id:
            rows = self._query(
                """SELECT * FROM config_items
                   WHERE namespace_id=? AND (key LIKE ? OR description LIKE ?)
                   ORDER BY key ASC""",
                (namespace_id, like, like),
            )
        else:
            rows = self._query(
                "SELECT * FROM config_items WHERE key LIKE ? OR description LIKE ? ORDER BY key ASC",
                (like, like),
            )
        return [ConfigItem.from_dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Summary / overview
    # ------------------------------------------------------------------

    def overview(self) -> dict[str, Any]:
        """Return summary statistics for the config center."""
        ns_count = self._one("SELECT COUNT(*) AS cnt FROM config_namespaces")["cnt"]
        cfg_count = self._one("SELECT COUNT(*) AS cnt FROM config_items")["cnt"]
        ver_count = self._one("SELECT COUNT(*) AS cnt FROM config_versions")["cnt"]
        envs = self._query("SELECT DISTINCT environment FROM config_namespaces")
        return {
            "namespaces": ns_count,
            "configs": cfg_count,
            "versions": ver_count,
            "environments": [r["environment"] for r in envs],
        }

    def close(self) -> None:
        self.conn.close()


__all__ = ["ConfigStore"]
