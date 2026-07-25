"""Path constants for the distill module.

Centralised so the cron / lib / tests all agree on locations and the\nnext dev can override DBs cleanly.
"""
from __future__ import annotations

import os
from pathlib import Path

# --- Engine side (this repo) --------------------------------------------
_THIS_DIR = Path(__file__).resolve().parent
ENGINE_ROOT = _THIS_DIR.parent.parent.parent
ENGINE_DB = ENGINE_ROOT / "data" / "collab.sqlite3"

# --- Shared Hermes user-land targets ------------------------------------
HERMES_ROOT = Path(os.environ.get("HERMES_ROOT", str(Path.home() / ".hermes")))
MEMORY_DIR = HERMES_ROOT / "memories"
MEMORY_FILE = MEMORY_DIR / "MEMORY.md"
USER_MEMORY_FILE = MEMORY_DIR / "USER.md"
SKILLS_ROOT = HERMES_ROOT / "skills"

# --- Distill module-local -------------------------------------------------
DISTILL_LOG_DIR = Path(os.environ.get("DISTILL_LOG_DIR", "/var/log"))
DISTILL_LOG_FILE = DISTILL_LOG_DIR / "hermes-distill.log"

# --- Localtime ----------------------------------------------------------
# SQLite stores timestamps in UTC by default; the engine uses
# CURRENT_TIMESTAMP which is UTC.  We convert with 'localtime' modifier
# in SQL so the "today" boundary matches the operator's wall clock.
