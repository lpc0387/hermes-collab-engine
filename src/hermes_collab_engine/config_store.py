"""Config Store — atomic file operations, backup rotation, migration, and diagnostics.

Provides utilities for safely reading/writing JSON config files with
backup rotation, forward-compatible schema migration, token masking,
and health diagnostics. Designed for Hermes Collab Engine's provider
and runtime config management, but usable standalone.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tempfile
import time
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_LEGACY_CONFIG_FILENAME = ".runtime-config.json"
"""Old runtime config filename that ``load_with_migration`` can import from."""

_TOKEN_PATTERNS: list[tuple[str, str, re.Pattern]] = [
    ("sk-ant-", "Anthropic secret key", re.compile(r"^sk-ant-")),
    ("sk-", "Generic secret key", re.compile(r"^sk-")),
    ("pk-", "Public key", re.compile(r"^pk-")),
    ("fk-", "Fine-tune key", re.compile(r"^fk-")),
]
"""Ordered list of (prefix, label, compiled regex) for ``mask_token``."""



# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------


def atomic_write_json(path: str | Path, data: Any) -> None:
    """Atomically write *data* as JSON to *path*.

    Writes to a temporary file alongside the target, then performs an
    ``os.replace`` (atomic on POSIX). The resulting file has ``chmod 600``
    (owner-read/write only) to protect sensitive API keys.

    Args:
        path: Destination file path.
        data: JSON-serializable Python object.

    Raises:
        OSError: If the write or replace fails.
        TypeError: If *data* is not JSON-serializable.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        suffix=".json",
        prefix=".tmp_",
        dir=path.parent,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)  # chmod 600
        os.replace(tmp_path, path)
    except BaseException:
        _silent_unlink(tmp_path)
        raise


# ---------------------------------------------------------------------------
# Backup rotation
# ---------------------------------------------------------------------------


def backup_config(path: str | Path, *, max_keep: int = 5) -> Path | None:
    """Create a timestamped backup of the file at *path*.

    Old backups beyond *max_keep* are removed (oldest first).

    Args:
        path: Source file to back up.
        max_keep: Maximum number of backup files to retain (default 5).

    Returns:
        The backup file path, or ``None`` if the source did not exist.
    """
    path = Path(path)
    if not path.is_file():
        return None

    backup_dir = path.parent / ".backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    ts = time.strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"{path.name}.{ts}.bak"
    shutil.copy2(path, backup_path)
    # Mirror the source's mode so secrets stay protected (was 644 default
    # because shutil.copy2 preserves mtime/atime but not necessarily mode
    # in every Python version; lock it to 600 to match the engine convention).
    try:
        os.chmod(backup_path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass

    # Rotate: keep only the newest max_keep files
    _rotate_backups(backup_dir, path.name, max_keep)
    return backup_path


def _rotate_backups(backup_dir: Path, stem: str, max_keep: int) -> None:
    """Remove oldest backup files beyond *max_keep*."""
    backups = sorted(
        backup_dir.glob(f"{stem}.*.bak"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old in backups[max_keep:]:
        _silent_unlink(old)


# ---------------------------------------------------------------------------
# Load with migration
# ---------------------------------------------------------------------------


def load_with_migration(path: str | Path) -> dict[str, Any]:
    """Load JSON config from *path*, with forward-compatible migration.

    If *path* does not exist, the method looks for a legacy config file
    (``.runtime-config.json`` in the same directory) and migrates it to
    the new path. This ensures a smooth transition for existing users.

    Migration steps:
    1. If *path* exists — load and return directly.
    2. If *path* does not exist but the legacy file does — load the legacy
       file, write it to the new path via :func:`atomic_write_json`, and
       return the data.
    3. Neither exists — return an empty dict (no error).

    The loaded dict is normalised so that missing keys get sensible
    defaults (``providers`` defaults to ``[]``, ``default_provider``
    defaults to ``""``).

    Args:
        path: Config file path (new format).

    Returns:
        A dict with at least ``"providers"`` and ``"default_provider"`` keys.
    """
    path_obj = Path(path)
    config: dict[str, Any] = {}

    if path_obj.is_file():
        with open(path_obj, "r", encoding="utf-8") as f:
            config = json.load(f)
    else:
        # Try legacy filename in the same directory
        legacy = path_obj.parent / _LEGACY_CONFIG_FILENAME
        if legacy.is_file():
            with open(legacy, "r", encoding="utf-8") as f:
                config = json.load(f)
            # Migrate: write to the new path atomically
            atomic_write_json(path_obj, config)

    # Normalise top-level keys
    if not isinstance(config, dict):
        config = {}
    config.setdefault("providers", [])
    config.setdefault("default_provider", "")
    return config


# ---------------------------------------------------------------------------
# Save with backup
# ---------------------------------------------------------------------------


def save_with_backup(path: str | Path, data: Any, *, max_keep: int = 5) -> Path | None:
    """Serialise *data* to JSON, create a timestamped backup, then atomic write.

    This is a convenience that chains :func:`backup_config` and
    :func:`atomic_write_json` so the old version is always preserved
    before overwriting.

    Args:
        path: Destination file path.
        data: JSON-serialisable config dict.
        max_keep: Maximum backup files to retain (default 5).

    Returns:
        The backup path if one was created, ``None`` if the file didn't
        previously exist (no backup needed).
    """
    backup_path = backup_config(path, max_keep=max_keep)
    atomic_write_json(path, data)
    return backup_path


# ---------------------------------------------------------------------------
# Token masking
# ---------------------------------------------------------------------------


def mask_token(key: str) -> str:
    """Intelligently mask an API key/token for safe logging or display.

    Recognised prefixes (case-insensitive first 5 characters):
    - ``sk-ant-`` → ``sk-ant-...XXXX``  (Anthropic)
    - ``sk-...``  → ``sk-...XXXX``      (generic secret key)
    - ``pk-...``  → ``pk-...XXXX``      (public key)
    - ``fk-...``  → ``fk-...XXXX``      (fine-tune key)

    If none of the patterns match, returns the last 4 characters
    prefixed with ``...`` — this ensures secrets are never fully
    exposed even for unknown formats.

    For keys shorter than 8 characters, returns ``"****"`` (fully masked).

    Args:
        key: The raw token string.

    Returns:
        Masked token safe for display.
    """
    if not key or len(key) < 8:
        return "****"

    for _prefix, _label, pattern in _TOKEN_PATTERNS:
        if pattern.match(key):
            visible = key[-4:]
            prefix_len = len(_prefix)
            return f"{key[:prefix_len]}...{visible}"

    # Fallback: show last 4 chars
    return f"...{key[-4:]}"


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


def diagnose(path: str | Path) -> dict[str, Any]:
    """Inspect a config file and return a health/status dict.

    The returned dict contains:
    - ``exists`` (bool): Whether the file exists on disk.
    - ``path`` (str): Absolute path of the config file.
    - ``size_bytes`` (int): File size, or 0 if not found.
    - ``valid_json`` (bool): Whether the file parses as valid JSON.
    - ``readable`` (bool): Whether the file can be read.
    - ``permissions`` (str): Octal permission string (e.g. ``"600"``),
      or ``""`` if the file doesn't exist.
    - ``provider_count`` (int): Number of providers in the config.
    - ``has_default_provider`` (bool): Whether ``default_provider`` is set.
    - ``errors`` (list[str]): Human-readable issues found.

    Args:
        path: Path to the config file.

    Returns:
        A diagnostic dict (never raises).
    """
    path_obj = Path(path)
    result: dict[str, Any] = {
        "exists": False,
        "path": str(path_obj.resolve()),
        "size_bytes": 0,
        "valid_json": False,
        "readable": False,
        "permissions": "",
        "provider_count": 0,
        "has_default_provider": False,
        "errors": [],
    }

    if not path_obj.is_file():
        result["errors"].append("File not found")
        return result

    result["exists"] = True
    try:
        st = path_obj.stat()
        result["size_bytes"] = st.st_size
        mode = stat.S_IMODE(st.st_mode)
        result["permissions"] = oct(mode)[-3:]

        # Check readability
        if os.access(path_obj, os.R_OK):
            result["readable"] = True
        else:
            result["errors"].append("File is not readable")
    except OSError as exc:
        result["errors"].append(f"Cannot stat file: {exc}")
        return result

    # Try parsing JSON
    try:
        with open(path_obj, "r", encoding="utf-8") as f:
            data = json.load(f)
        result["valid_json"] = True
    except json.JSONDecodeError as exc:
        result["errors"].append(f"Invalid JSON: {exc}")
        return result
    except OSError as exc:
        result["errors"].append(f"Cannot read file: {exc}")
        return result

    # Inspect content
    if isinstance(data, dict):
        providers = data.get("providers", [])
        result["provider_count"] = len(providers) if isinstance(providers, list) else 0
        result["has_default_provider"] = bool(data.get("default_provider"))
    else:
        result["errors"].append("Config content is not a dict")

    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _silent_unlink(path: str | Path) -> None:
    """Remove a file, ignoring errors."""
    try:
        os.unlink(path)
    except OSError:
        pass


__all__ = [
    "atomic_write_json",
    "backup_config",
    "load_with_migration",
    "save_with_backup",
    "mask_token",
    "diagnose",
]
